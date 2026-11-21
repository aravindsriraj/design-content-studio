"""Vendored design-agent looks against a real sandbox (skips without the local dev sandbox)."""

import asyncio

import httpx
import pytest

from app import sandbox
from app.looks import Looks, _Exec, next_ids

try:
    httpx.get("http://localhost:8080/healthz", timeout=2).raise_for_status()
except Exception:
    pytest.skip("local sandbox not running", allow_module_level=True)


def test_prepare_collect_pick_round_trip(monkeypatch):
    monkeypatch.setenv("STUDIO_SANDBOX", "docker")
    P = "/workspace/projects/looks-it"

    async def go():
        env = await sandbox.for_workspace("looks-it")
        await env.execute(f"rm -rf {P} && mkdir -p {P} && echo base > {P}/card.html && echo keep > {P}/notes.md", timeout=30)
        looks = Looks(_Exec(env))
        ids = next_ids([r.id for r in await looks.collect(P)], 2)
        assert ids == ["a", "b"]
        await looks._prepare(P, ids, {"a": "warm", "b": "bold"})
        await env.execute(f"sleep 1; echo BOLD > {P}/looks/b/card.html; "
                          f"echo '{{\"id\":\"b\",\"summary\":\"big type\",\"stills\":[\"out/1.png\"]}}' > {P}/looks/b/look.json", timeout=30)
        found = {r.id: r for r in await looks.collect(P)}
        assert found["b"].direction == "bold" and found["b"].summary == "big type"
        picked = await looks.pick(P, "b")
        assert picked.changed == ["card.html"]
        out = (await env.execute(f"cat {P}/card.html {P}/notes.md {picked.backup}/card.html", timeout=30)).stdout.split()
        assert out == ["BOLD", "keep", "base"]
        assert next_ids([r.id for r in await looks.collect(P)], 1) == ["c"]

    asyncio.run(go())
