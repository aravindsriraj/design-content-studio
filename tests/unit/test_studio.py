"""Deterministic checks for brand loading, staging and delivery (no model, no real sandbox)."""

import asyncio
import json
import shutil
from pathlib import Path

import pytest
from google.adk.environment import ExecutionResult

from app import sandbox, staging, tools
from app.brand.schema import BrandPackError, load_brand_pack


class FakeEnv:
    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.uploads = 0
        self.commands: list[str] = []

    async def read_file(self, path):
        if str(path) not in self.files:
            raise FileNotFoundError(str(path))
        return self.files[str(path)]

    async def write_file(self, path, content):
        self.files[str(path)] = content.encode() if isinstance(content, str) else content

    async def execute(self, command, *, timeout=None):
        self.commands.append(command)
        return ExecutionResult(exit_code=0)

    async def upload_zip(self, path, data):
        self.uploads += 1


class FakeToolContext:
    user_id = "ws1"

    def __init__(self):
        self.state: dict = {}
        self.saved: list[str] = []

    async def save_artifact(self, filename, part):
        self.saved.append(filename)
        return len(self.saved) - 1


def test_sample_brand_loads_and_broken_pack_lists_problems(tmp_path: Path):
    assert load_brand_pack(staging.SAMPLE_BRAND, check_fonts=False).brand.name == "Example Studio"
    broken = tmp_path / "brand"
    shutil.copytree(staging.SAMPLE_BRAND, broken)
    tokens = json.loads((broken / "tokens.json").read_text())
    tokens["design"]["allowedColors"] = ["#fff"]  # schema needs at least two
    (broken / "tokens.json").write_text(json.dumps(tokens))
    (broken / "voice.md").unlink()
    with pytest.raises(BrandPackError) as err:
        load_brand_pack(broken, check_fonts=False)
    assert any("voice.md" in p for p in err.value.problems)
    assert any("allowedColors" in p for p in err.value.problems)


def test_bundle_is_deterministic_and_complete():
    brand = staging.brand_for("ws1")
    zip1, digest1, families = staging.build_bundle(brand)
    zip2, digest2, _ = staging.build_bundle(brand)
    assert (zip1, digest1) == (zip2, digest2)
    assert set(families) == {"Fragment Mono", "Geist"}
    import io
    import zipfile

    names = zipfile.ZipFile(io.BytesIO(zip1)).namelist()
    for want in ("tools/html2png.mjs", "tools/brand-lint.mjs", "skills/image-ops/scripts/img.mjs", "brand/tokens.css", "brand/brand.toml"):
        assert want in names


def test_stage_uploads_once_then_skips():
    env, brand = FakeEnv(), staging.brand_for("ws1")
    assert asyncio.run(staging.stage(env, brand)) is None
    assert env.uploads == 1 and staging.MARKER in env.files
    assert asyncio.run(staging.stage(env, brand)) is None
    assert env.uploads == 1  # digest unchanged -> no second upload


def test_deliver_sends_each_file_once_and_stays_in_workspace(monkeypatch):
    env, ctx = FakeEnv(), FakeToolContext()
    env.files["/workspace/projects/a/card.png"] = b"\x89PNG one"

    async def fake_for_workspace(_ws):
        return env

    monkeypatch.setattr(sandbox, "for_workspace", fake_for_workspace)
    first = asyncio.run(tools.deliver(["/workspace/projects/a/card.png"], ctx))
    assert first["status"] == "delivered" and ctx.saved == ["card.png"]
    again = asyncio.run(tools.deliver(["projects/a/card.png"], ctx))
    assert again["already_delivered"] == ["card.png"] and ctx.saved == ["card.png"]
    env.files["/workspace/projects/a/card.png"] = b"\x89PNG two"  # changed file goes out again
    assert asyncio.run(tools.deliver(["projects/a/card.png"], ctx))["files"] == ["card.png (v1)"]
    assert asyncio.run(tools.deliver(["/etc/passwd"], ctx))["status"] == "not delivered"
    assert asyncio.run(tools.deliver(["../x.png"], ctx))["status"] == "not delivered"
    assert asyncio.run(tools.deliver([], ctx))["status"] == "not delivered"


def test_cost_and_ledger(tmp_path, monkeypatch):
    import datetime as dt
    from types import SimpleNamespace

    from google.genai import types

    from app import meter

    u = types.GenerateContentResponseUsageMetadata(
        prompt_token_count=1_000_000, cached_content_token_count=800_000,
        candidates_token_count=40_000, thoughts_token_count=60_000)
    # Argon intro rates: 200k uncached * $2 + 800k cached * $0.10 + 100k out * $10 = 0.40 + 0.08 + 1.00
    assert round(meter.cost_usd("gemini-4-argon", u, dt.date(2026, 11, 17)), 4) == 1.48
    assert round(meter.cost_usd("gemini-4-argon", u, dt.date(2026, 12, 1)), 4) == 2.96
    assert meter.cost_usd("unknown-model", u) == 0.0

    monkeypatch.setenv("STUDIO_DB_URL", f"sqlite+aiosqlite:///{tmp_path}/t.db")
    monkeypatch.setattr(meter, "_engine", None)
    ctx = SimpleNamespace(state={}, user_id="ws1", session=SimpleNamespace(id="s1"))

    async def run():
        await meter.record(ctx, role="lead", model="gemini-3.8-flash", usage=u)
        await meter.record(ctx, role="look", model="gemini-3.8-flash", usage=u)
        return await meter.workspace_spend("ws1"), await meter.workspace_spend("other")

    mine, other = asyncio.run(run())
    assert ctx.state["usage"]["calls"] == 2 and mine["calls"] == 2
    assert mine["cost_usd"] == round(ctx.state["usage"]["cost_usd"], 4) > 0
    assert other["calls"] == 0


def test_a_workspace_sandbox_on_an_old_template_is_replaced(monkeypatch):
    from types import SimpleNamespace

    from app import sandbox

    NEW, OLD = "eng/sandboxEnvironmentTemplates/new", "eng/sandboxEnvironmentTemplates/old"
    calls = {"deleted": [], "created": []}
    conn = SimpleNamespace(load_balancer_hostname="h", routing_token="t")

    def fake_client(existing_template):
        box = SimpleNamespace(name="sb-1", display_name="ws-alice", state="STATE_RUNNING",
                              sandbox_environment_template=existing_template, connection_info=conn)
        api = SimpleNamespace(
            list=lambda name: [box],
            get=lambda name: box,
            delete=lambda name: calls["deleted"].append(name),
            create=lambda name, config: (calls["created"].append(config["sandbox_environment_template"]),
                                         SimpleNamespace(response=SimpleNamespace(connection_info=conn)))[1],
            generate_access_token=lambda sa: "jwt",
        )
        return SimpleNamespace(agent_engines=SimpleNamespace(sandboxes=api))

    monkeypatch.setenv("STUDIO_SANDBOX_TEMPLATE", NEW)
    monkeypatch.setenv("STUDIO_SANDBOX_CALLER_SA", "sa@x")
    monkeypatch.setattr(sandbox, "_vertex", lambda: fake_client(OLD))
    sandbox._vertex_env("alice")
    assert calls == {"deleted": ["sb-1"], "created": [NEW]}

    calls.update(deleted=[], created=[])
    monkeypatch.setattr(sandbox, "_vertex", lambda: fake_client(NEW))
    sandbox._vertex_env("alice")
    assert calls == {"deleted": [], "created": []}  # current template: reused
