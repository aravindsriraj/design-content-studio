"""M0 spike, cloud half: run spike_sandbox.py against a real Agent Runtime Sandbox (BYOC image).

Calls mirror google/adk-samples long-horizon-harness horizon/sandbox/{provider,lifecycle}.py.

  uv run --no-project --with "google-cloud-aiplatform[agent-engines]" python scripts/spike/vertex_sandbox.py \
      --project YOUR_PROJECT_ID --location asia-south1 \
      --image asia-south1-docker.pkg.dev/YOUR_PROJECT_ID/studio/sandbox:m0 \
      --caller-sa studio-sandbox-caller@YOUR_PROJECT_ID.iam.gserviceaccount.com

Creates (or reuses) an Agent Engine "studio-sandbox-host" and a hermetic template for the image,
creates one sandbox with a 1h TTL, runs the spike, then deletes the sandbox (--keep to skip).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import vertexai
from vertexai._genai.types import AgentEngineConfig

ENGINE_NAME = "studio-sandbox-host"
TEMPLATE_NAME = "studio-hermetic"


def engine(client) -> str:
    for e in client.agent_engines.list():
        r = getattr(e, "api_resource", None)
        if r is not None and getattr(r, "display_name", None) == ENGINE_NAME:
            return str(r.name)
    return str(client.agent_engines.create(config=AgentEngineConfig(display_name=ENGINE_NAME)).api_resource.name)


def template(client, eng: str, image: str) -> str:
    for t in client.agent_engines.sandboxes.templates.list(name=eng):
        t = client.agent_engines.sandboxes.templates.get(name=str(t.name))
        spec = getattr(getattr(t, "custom_container_environment", None), "custom_container_spec", None)
        if t.display_name == TEMPLATE_NAME and getattr(spec, "image_uri", None) == image:
            return str(t.name)
    op = client.agent_engines.sandboxes.templates.create(
        name=eng,
        display_name=TEMPLATE_NAME,
        config={
            "custom_container_environment": {
                "custom_container_spec": {"image_uri": image},
                "ports": [{"port": 8080, "protocol": "TCP"}],
            },
            "egress_control_config": {"internet_access": False},
        },
    )
    return str(op.response.name)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--location", required=True)
    ap.add_argument("--image", required=True)
    ap.add_argument("--caller-sa", required=True)
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()

    client = vertexai.Client(project=a.project, location=a.location, http_options={"api_version": "v1beta1"})
    t: dict[str, float] = {}

    t0 = time.monotonic()
    eng = engine(client)
    t["engine_s"] = time.monotonic() - t0
    print("engine:", eng)

    t0 = time.monotonic()
    tpl = template(client, eng, a.image)
    t["template_s"] = time.monotonic() - t0
    print("template:", tpl)

    t0 = time.monotonic()
    op = client.agent_engines.sandboxes.create(
        name=eng, config={"sandbox_environment_template": tpl, "display_name": "studio-spike", "ttl": "3600s"}
    )
    sb = op.response
    t["create_sandbox_s"] = time.monotonic() - t0
    print("sandbox:", sb.name)

    try:
        jwt = str(client.agent_engines.sandboxes.generate_access_token(a.caller_sa))
        conn = sb.connection_info
        spike = Path(__file__).with_name("spike_sandbox.py")
        t0 = time.monotonic()
        rc = subprocess.call([
            sys.executable, str(spike), f"https://{conn.load_balancer_hostname}",
            "--header", f"Authorization: Bearer {jwt}",
            "--header", f"X-Sandbox-Routing-Token: {conn.routing_token}",
            "--header", "X-Sandbox-Port: 8080",
            "--out", str(Path(__file__).resolve().parents[2] / ".spike-out" / "vertex"),
        ])
        t["spike_total_s"] = time.monotonic() - t0
    finally:
        if not a.keep:
            client.agent_engines.sandboxes.delete(name=str(sb.name))
            print("sandbox deleted")
    print(json.dumps({"rc": rc, **{k: round(v, 1) for k, v in t.items()}}))
    return rc


if __name__ == "__main__":
    sys.exit(main())
