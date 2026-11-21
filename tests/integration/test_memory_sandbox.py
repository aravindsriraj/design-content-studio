"""Memory layers round-trip through the sandbox (skips without the dev sandbox)."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app import memory, sandbox

try:
    httpx.get("http://localhost:8080/healthz", timeout=2).raise_for_status()
except Exception:
    pytest.skip("local sandbox not running", allow_module_level=True)


def test_memory_is_staged_saved_and_kept_per_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("STUDIO_SANDBOX", "docker")
    monkeypatch.setenv("STUDIO_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("STUDIO_BUCKET", raising=False)

    async def go():
        ctx = SimpleNamespace(user_id="mem-alice", state={})
        await memory.load_memory(ctx)
        assert ctx.state["memory_note"] == ""
        env = await sandbox.for_workspace("mem-alice")
        await env.execute("printf '# Team\\n- Headlines in sentence case.\\n' > /workspace/memories/agent/AGENTS.md", timeout=30)
        await memory.save_memory(ctx)
        ctx2 = SimpleNamespace(user_id="mem-alice", state={})
        await memory.load_memory(ctx2)
        assert "sentence case" in ctx2.state["memory_note"]
        bob = SimpleNamespace(user_id="mem-bob", state={})
        await memory.load_memory(bob)
        assert bob.state["memory_note"] == ""

    asyncio.run(go())
