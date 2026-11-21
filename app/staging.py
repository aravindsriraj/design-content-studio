"""Put what the agent works with into the workspace's sandbox before each turn.

One zip: gates -> /workspace/tools, skill scripts and assets -> /workspace/skills/<name>/,
the brand pack -> /workspace/brand (plus generated tokens.css), brand font files the pack ships
-> /workspace/fonts. A digest marker skips the upload when nothing changed. Required brand
families are proven with fc-match inside the sandbox, which also covers fonts baked into the image.
Ported from design-agent's workspace/manifest.py + middleware/workspace.py.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import mimetypes
import shlex
import zipfile
from pathlib import Path

from google.adk.agents.callback_context import CallbackContext

from app import sandbox
from app.brand import store as brand_store
from app.brand.schema import (
    BrandPack,
    BrandPackError,
    render_tokens_css,
)
from app.guards import turn_notes

APP = Path(__file__).resolve().parent
TOOLS = APP / "sandbox_tools"
SKILLS = APP / "skills"
SAMPLE_BRAND = APP / "brand" / "sample"
MARKER = "/workspace/.studio-staged"
STAGED_ROOTS = ("tools", "skills", "brand")
_SKIP = {".DS_Store", "__pycache__", "node_modules", ".git"}
log = logging.getLogger(__name__)


def brand_for(workspace_id: str) -> BrandPack:
    return brand_store.load(workspace_id)


def _files(base: Path):
    for p in sorted(base.rglob("*")):
        rel = p.relative_to(base)
        if any(part in _SKIP for part in rel.parts) or p.name.startswith(".env"):
            continue
        if p.is_symlink():
            raise ValueError(f"refusing to stage a symlink: {rel}")
        if p.is_file():
            yield p, rel.as_posix()


def build_bundle(brand: BrandPack) -> tuple[bytes, str, tuple[str, ...]]:
    """(zip bytes, digest, required families). Deterministic: same inputs, same digest."""
    members: list[tuple[str, bytes]] = [(f"tools/{rel}", p.read_bytes()) for p, rel in _files(TOOLS)]
    for skill in sorted(d for d in SKILLS.iterdir() if d.is_dir()):
        # The whole skill, as design-agent mounts it: briefs and references are read by path.
        members += [(f"skills/{skill.name}/{rel}", p.read_bytes()) for p, rel in _files(skill)]
    members += [(f"brand/{rel}", p.read_bytes()) for p, rel in _files(brand.root)]
    if not (brand.root / "tokens.css").is_file():
        members.append(("brand/tokens.css", render_tokens_css(brand).encode()))
    for font in brand.fonts.fonts:
        src = (brand.root / font.file).resolve()
        if src.is_relative_to(brand.root.resolve()) and src.is_file():
            members.append((f"fonts/{src.name}", src.read_bytes()))
        # else: baked into the sandbox image (sample brand) or missing; fc-match decides.
    members.sort()
    families = brand.required_families()
    h = hashlib.sha256()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members:
            h.update(f"{name}\t{hashlib.sha256(data).hexdigest()}\n".encode())
            zf.writestr(zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0)), data)
    h.update(f"fonts\t{families}".encode())
    return buf.getvalue(), h.hexdigest(), families


async def stage(env: sandbox.SandboxEnvironment, brand: BrandPack) -> str | None:
    """Stage if stale. Returns None when ready, else a plain-language problem for the agent."""
    bundle, digest, families = build_bundle(brand)
    try:
        if (await env.read_file(Path(MARKER))).decode() == digest:
            return None
    except FileNotFoundError:
        pass
    roots = " ".join(f"/workspace/{r}" for r in STAGED_ROOTS)
    await env.execute(f"rm -rf {roots} && mkdir -p /workspace/fonts /workspace/projects /workspace/attachments")
    await env.upload_zip(".", bundle)
    checks = " && ".join(
        f"[ \"$(fc-match -f '%{{family[0]}}' {shlex.quote(f)})\" = {shlex.quote(f)} ] || {{ echo {shlex.quote(f)}; false; }}"
        for f in families
    ) or "true"
    r = await env.execute(f"fc-cache -f /workspace/fonts >/dev/null 2>&1; {checks}", timeout=60)
    if r.exit_code != 0:
        return f"Brand font did not install in the sandbox: {r.stdout.strip() or r.stderr.strip()}. Renders that need it will fail; tell the person."
    await env.write_file(Path(MARKER), digest)
    return None


def _attachment_name(index: int, part) -> str:
    blob = part.inline_data
    stem = Path(blob.display_name or f"attachment-{index}").name or f"attachment-{index}"
    stem = "".join(c if c.isalnum() or c in "._-" else "_" for c in stem)[:80]
    ext = mimetypes.guess_extension(blob.mime_type or "") or ""
    return stem if Path(stem).suffix else stem + ext


async def save_attachments(env: sandbox.SandboxEnvironment, user_content) -> list[str]:
    """Write files the person attached this turn to /workspace/attachments; return their paths."""
    paths = []
    for i, part in enumerate((user_content.parts or []) if user_content else [], 1):
        if part.inline_data and part.inline_data.data:
            path = f"/workspace/attachments/{_attachment_name(i, part)}"
            await env.write_file(Path(path), part.inline_data.data)
            paths.append(path)
    return paths


TOOLS_README = Path(__file__).parent / "sandbox_tools" / "README.md"


def brand_brief(brand: BrandPack) -> str:
    """The brand facts every job needs, so the agent starts composing instead of reading files.

    Rendered into the per-turn instruction (after the cached static prefix). graphic-language.md,
    logos and tokens.json stay in /workspace/brand for when a piece needs them.
    """
    css = (brand.root / "tokens.css").read_text() if (brand.root / "tokens.css").is_file() else render_tokens_css(brand)
    gl = brand.root / "graphic-language.md"
    parts = [
        ("brand.toml", (brand.root / "brand.toml").read_text(), 4000),
        ("tokens.css (link it from /workspace/brand/tokens.css; use these variables, never raw hex)", css, 4000),
        (brand.brand.files.voice, (brand.root / brand.brand.files.voice).read_text(), 4000),
        # Read in most runs anyway; here it costs no model round trip and rides the implicit cache.
        ("graphic-language.md", gl.read_text() if gl.is_file() else "", 12000),
        ("/workspace/tools/README.md (the gates and renderers)", TOOLS_README.read_text(), 12000),
    ]
    body = "\n\n".join(f"--- {name} ---\n{text.strip()[:cap]}" for name, text, cap in parts if text.strip())
    return f"\n<brand-brief>\nThe active brand pack, already read for you:\n\n{body}\n</brand-brief>"


def short_title(text: str, limit: int = 72) -> str:
    """The first ask as a sidebar title: whole words, an ellipsis when cut."""
    words = " ".join(text.split())
    if len(words) <= limit:
        return words or "Untitled request"
    return words[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:-") + "…"


async def prepare_workspace(callback_context: CallbackContext) -> None:
    """before_agent_callback: make sure the sandbox is up and staged; note problems in state."""
    ws = callback_context.user_id
    problem = None
    turn_notes(callback_context.state)
    callback_context.state["attachments_note"] = ""
    callback_context.state["brand_brief"] = ""
    if not callback_context.state.get("title"):  # thread name for the sidebar: the first ask
        content = callback_context.user_content
        text = " ".join(p.text for p in (content.parts or []) if p.text) if content else ""
        callback_context.state["title"] = short_title(text)
    try:
        brand = await asyncio.to_thread(brand_for, ws)  # may hit GCS
        callback_context.state["brand_brief"] = brand_brief(brand)
        env = await sandbox.for_workspace(ws)
        problem = await stage(env, brand)
        if attached := await save_attachments(env, callback_context.user_content):
            callback_context.state["attachments_note"] = (
                "\nFiles attached to this message are on your computer at: " + ", ".join(attached)
            )
    except BrandPackError as e:
        problem = "The brand pack is invalid:\n" + "\n".join(e.problems)
    except sandbox.SandboxGone:
        sandbox.forget(ws)
        problem = "Your computer is restarting; say so and ask the person to send the message again in a minute."
    except Exception as e:  # a staging failure must not kill the turn
        log.exception("staging failed for %s", ws)
        problem = f"Workspace setup failed ({type(e).__name__}); rendering may not work. Say so plainly."
    callback_context.state["workspace_status"] = f"<workspace-status>{problem}</workspace-status>" if problem else ""
