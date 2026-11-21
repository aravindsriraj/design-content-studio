"""start_render_job + the durable watcher against a real HyperFrames render (skips without the dev sandbox)."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from google.adk.sessions import InMemorySessionService

from app import jobs, meter, sandbox, tasks, worker
from app.renders import start_render_job

try:
    httpx.get("http://localhost:8080/healthz", timeout=2).raise_for_status()
except Exception:
    pytest.skip("local sandbox not running", allow_module_level=True)


@pytest.fixture(autouse=True)
def db(monkeypatch, tmp_path):
    monkeypatch.setenv("STUDIO_DB_URL", f"sqlite+aiosqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("STUDIO_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STUDIO_SANDBOX", "docker")
    monkeypatch.delenv("STUDIO_BUCKET", raising=False)
    monkeypatch.setattr(meter, "_engine", None)
    monkeypatch.setattr(jobs, "_table_ready", False)
    monkeypatch.setattr(tasks, "_ready", False)


def _ctx(ss, session_id):
    inv = SimpleNamespace(session_service=ss)
    return SimpleNamespace(user_id="render-it", session=SimpleNamespace(id=session_id), _invocation_context=inv)


@pytest.mark.parametrize("command, expect", [
    ("hyperframes render . --quality draft --fps 30 --output out.mp4", "succeeded"),
    ("echo 'Error: composition not found' && exit 3", "failed"),
])
def test_render_job_ends_with_its_exit_code(monkeypatch, command, expect):
    dispatched = []

    async def no_dispatch(job_id):
        dispatched.append(job_id)

    monkeypatch.setattr(jobs, "dispatch", no_dispatch)

    async def go():
        env = await sandbox.for_workspace("render-it")
        await env.execute("rm -rf /workspace/projects/rit && mkdir -p /workspace/projects/rit && "
                          "cp /opt/design-agent/probe/composition.html /workspace/projects/rit/index.html && "
                          "cp /opt/design-agent/render/node_modules/gsap/dist/gsap.min.js /workspace/projects/rit/", timeout=30)
        ss = InMemorySessionService()
        lead = await ss.create_session(app_name="app", user_id="render-it")
        r = await start_render_job(command, "probe film", _ctx(ss, lead.id), workdir="/workspace/projects/rit")
        assert r["status"] == "watching", r
        t = next(t for t in await tasks.for_parent("render-it", lead.id))
        ok = await worker.run_job(t["job_id"], final_attempt=True)
        t = await tasks.get(t["id"])
        assert t["status"] == expect and ok == (expect == "succeeded")
        assert ("exit 0" if expect == "succeeded" else "exit 3") in t["report"]
        if expect == "succeeded":
            probe = await env.execute("ffprobe -v error -show_entries format=duration -of csv=p=0 /workspace/projects/rit/out.mp4", timeout=30)
            assert abs(float(probe.stdout) - 2.0) < 0.1
        else:
            assert "composition not found" in t["report"]

    asyncio.run(go())
