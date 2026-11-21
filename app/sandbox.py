"""Each workspace's computer: a sandbox running the shim from sandbox/runtime (port 8080).

Two backends, picked by STUDIO_SANDBOX:
  docker  one local container at STUDIO_SANDBOX_URL (default http://localhost:8080), shared by
          every workspace. Dev only. Start it with `docker run -p 127.0.0.1:8080:8080 studio-sandbox:dev`.
  vertex  one Agent Runtime Sandbox per workspace, named ws-<workspace_id>, created from the
          pinned template STUDIO_SANDBOX_TEMPLATE. Calls go through the sandbox load balancer
          with a JWT minted for STUDIO_SANDBOX_CALLER_SA plus the routing token.

Verified quirks (M0 spike): never set `owner` on create (every call then 403s), and find
templates by pinned resource name (templates.get drops display_name).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import shlex
import time
import uuid
import warnings
from pathlib import Path, PurePosixPath

import httpx
from google.adk.environment import BaseEnvironment, ExecutionResult

log = logging.getLogger(__name__)
WORKSPACE = PurePosixPath("/workspace")
_REFRESH_S = 30 * 60  # JWT lives 1 h; the routing token goes stale inside an hour too.


class SandboxGone(RuntimeError):
    """The sandbox stopped answering; the next turn provisions a new one."""


class SandboxEnvironment(BaseEnvironment):
    """ADK environment over the shim's HTTP API."""

    def __init__(self, base_url: str, headers: dict[str, str] | None = None):
        self.base_url = base_url.rstrip("/")
        self.headers = headers or {}
        self.refreshed_at = time.monotonic()

    @property
    def working_dir(self) -> Path:
        return Path(WORKSPACE)

    async def _request(self, method: str, path: str, *, timeout: float = 120, **kw) -> httpx.Response:
        async with httpx.AsyncClient(base_url=self.base_url, headers=self.headers, timeout=timeout) as c:
            try:
                r = await c.request(method, path, **kw)
            except httpx.TransportError as e:
                raise SandboxGone(f"sandbox unreachable: {type(e).__name__}") from e
        if r.status_code in (502, 503, 504):
            raise SandboxGone(f"sandbox returned {r.status_code}")
        return r

    async def execute(self, command: str, *, timeout: float | None = None) -> ExecutionResult:
        t = timeout or 120
        # The shim runs /bin/sh (dash); the agent's tool is `bash` and design-agent's skills use bash
        # (brace expansion, pipefail, [[ ]]), so every command runs under bash.
        r = await self._request("POST", "/exec", json={"command": f"exec bash -c {shlex.quote(command)}", "timeout": t},
                                timeout=t + 30)
        r.raise_for_status()
        return ExecutionResult(**r.json())

    async def read_file(self, path: Path) -> bytes:
        r = await self._request("GET", "/files", params={"path": str(path)})
        if r.status_code == 404:
            raise FileNotFoundError(str(path))
        r.raise_for_status()
        return base64.b64decode(r.json()["content_b64"])

    async def write_file(self, path: Path, content: str | bytes) -> None:
        data = content.encode() if isinstance(content, str) else content
        r = await self._request("POST", "/files", params={"path": str(path)},
                                json={"content_b64": base64.b64encode(data).decode()})
        r.raise_for_status()

    async def download_zip(self, path: str) -> bytes:
        r = await self._request("GET", "/files/zip", params={"path": path}, timeout=300)
        r.raise_for_status()
        return r.content

    async def upload_zip(self, path: str, data: bytes) -> None:
        r = await self._request("POST", "/files/zip", params={"path": path}, content=data,
                                headers={"Content-Type": "application/zip"}, timeout=300)
        r.raise_for_status()


# ---------------------------------------------------------------- providers

_envs: dict[str, SandboxEnvironment] = {}
_locks: dict[str, asyncio.Lock] = {}
# ponytail: per-process cache + per-workspace lock; two Cloud Run instances can race to create the
# same workspace's sandbox (the loser's copy expires by TTL). Add a DB row lock if that shows up.


def _vertex():
    import vertexai  # deferred: the docker backend never needs it

    with warnings.catch_warnings():  # agentplatform.Client lacks agent_engines.sandboxes as of 2.9
        warnings.simplefilter("ignore", FutureWarning)
        return vertexai.Client(
            project=os.environ["GOOGLE_CLOUD_PROJECT"],
            location=os.environ.get("STUDIO_SANDBOX_LOCATION", "asia-south1"),
            http_options={"api_version": "v1beta1"},
        )


def _vertex_env(workspace_id: str) -> SandboxEnvironment:
    """Blocking: find or create ws-<id>, then mint fresh credentials for it."""
    client = _vertex()
    template = os.environ["STUDIO_SANDBOX_TEMPLATE"]
    engine = template.split("/sandboxEnvironmentTemplates/")[0]
    name = f"ws-{workspace_id}"[:63]
    sandbox = next(
        (s for s in client.agent_engines.sandboxes.list(name=engine)
         if s.display_name == name and str(s.state).endswith("RUNNING")),
        None,
    )
    if sandbox is not None:  # list() returns stubs without connection_info.routing_token
        sandbox = client.agent_engines.sandboxes.get(name=sandbox.name)
        if str(getattr(sandbox, "sandbox_environment_template", "") or "") != template:
            # Built from an older image: replace it so the workspace gets the current toolchain.
            # Its files go with it; deliverables and conversations live outside the sandbox.
            log.info("sandbox %s is on an old template; replacing it", name)
            try:
                client.agent_engines.sandboxes.delete(name=str(sandbox.name))
            except Exception:
                log.exception("deleting the old sandbox failed; its TTL reaps it")
            sandbox = None
    if sandbox is None:
        sandbox = client.agent_engines.sandboxes.create(
            name=engine,
            config={"sandbox_environment_template": template, "display_name": name,
                    "ttl": os.environ.get("STUDIO_SANDBOX_TTL", "86400s")},
        ).response
    jwt = client.agent_engines.sandboxes.generate_access_token(os.environ["STUDIO_SANDBOX_CALLER_SA"])
    conn = sandbox.connection_info
    return SandboxEnvironment(
        f"https://{conn.load_balancer_hostname}",
        {"Authorization": f"Bearer {jwt}", "X-Sandbox-Routing-Token": conn.routing_token, "X-Sandbox-Port": "8080"},
    )


async def for_workspace(workspace_id: str) -> SandboxEnvironment:
    """The workspace's sandbox, provisioned or re-credentialed as needed."""
    if os.environ.get("STUDIO_SANDBOX", "docker") == "docker":
        return _envs.setdefault("docker", SandboxEnvironment(os.environ.get("STUDIO_SANDBOX_URL", "http://localhost:8080")))
    async with _locks.setdefault(workspace_id, asyncio.Lock()):
        env = _envs.get(workspace_id)
        if env is None or time.monotonic() - env.refreshed_at > _REFRESH_S:
            env = _envs[workspace_id] = await asyncio.to_thread(_vertex_env, workspace_id)
        return env


@contextlib.asynccontextmanager
async def capture_sandbox():
    """A throwaway sandbox WITH internet access and no credentials, for reading public websites.

    Never the workspace sandbox: page scripts run here, so nothing of the person's is in it.
    docker: the container at STUDIO_CAPTURE_URL (default http://localhost:8081), started with
    network. vertex: one sandbox per capture from STUDIO_CAPTURE_TEMPLATE (internet on), deleted after.
    """
    if os.environ.get("STUDIO_SANDBOX", "docker") == "docker":
        yield SandboxEnvironment(os.environ.get("STUDIO_CAPTURE_URL", "http://localhost:8081"))
        return

    def create():
        client = _vertex()
        template = os.environ["STUDIO_CAPTURE_TEMPLATE"]
        sb = client.agent_engines.sandboxes.create(
            name=template.split("/sandboxEnvironmentTemplates/")[0],
            config={"sandbox_environment_template": template, "display_name": f"capture-{uuid.uuid4().hex[:12]}",
                    "ttl": "3600s"},
        ).response
        jwt = client.agent_engines.sandboxes.generate_access_token(os.environ["STUDIO_SANDBOX_CALLER_SA"])
        conn = sb.connection_info
        env = SandboxEnvironment(f"https://{conn.load_balancer_hostname}", {
            "Authorization": f"Bearer {jwt}", "X-Sandbox-Routing-Token": conn.routing_token, "X-Sandbox-Port": "8080"})
        return client, sb, env

    client, sb, env = await asyncio.to_thread(create)
    try:
        yield env
    finally:
        try:
            await asyncio.to_thread(client.agent_engines.sandboxes.delete, name=str(sb.name))
        except Exception:  # the 1 h TTL reaps it anyway
            pass


def forget(workspace_id: str) -> None:
    """Drop a dead sandbox so the next call provisions a new one."""
    _envs.pop(workspace_id, None)
