"""The agent's hands: bash and write_file in its sandbox, look at renders, deliver finals."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import io
import mimetypes
import os
from pathlib import Path, PurePosixPath

from google import genai
from google.adk.tools import ToolContext
from google.genai import types
from PIL import Image

from app import meter, sandbox

HEAD, TAIL = 10_000, 4_000  # chars of stdout/stderr kept per call
LOOK_MODEL = os.environ.get("STUDIO_LOOK_MODEL", "gemini-3.8-flash")
LOOK_MAX_IMAGES, LOOK_MAX_EDGE = 8, 1280
LOOK_PROMPT = """You look at images for the lead of a design job and answer its question about them. The lead does not see the images; your answer is all it keeps.
Answer what was asked, concretely: refer to images by number, say what is where, quote the text you can read, name colours and sizes when they matter, and say plainly what is wrong or different. If the images cannot answer the question, say so. Plain text, about 200 words or fewer, no preamble."""


@functools.cache
def _genai() -> genai.Client:
    """One client per process. A throwaway `genai.Client().aio` can be garbage-collected mid-request,
    closing its HTTP session; retries cover Vertex's 429s on bursts of image calls."""
    return genai.Client(http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=4)))


def _clip(text: str) -> str:
    if len(text) <= HEAD + TAIL:
        return text
    cut = len(text) - HEAD - TAIL
    return f"{text[:HEAD]}\n[...{cut} chars cut; page through with head/sed -n or read fewer files at once...]\n{text[-TAIL:]}"


def _workspace_path(path: str) -> PurePosixPath:
    p = PurePosixPath(path if path.startswith("/") else f"/workspace/{path}")
    if ".." in p.parts or not p.is_relative_to("/workspace"):
        raise ValueError(f"{path} is outside /workspace")
    return p


async def bash(command: str, tool_context: ToolContext, timeout_seconds: int = 120) -> dict:
    """Run a shell command on your computer (Debian, Node 22, Chrome, sharp, fonts; no network).

    The working directory is /workspace. Gates live in /workspace/tools, skill scripts in
    /workspace/skills/<skill>/scripts, the brand pack in /workspace/brand.

    Args:
      command: The shell command.
      timeout_seconds: Kill the command after this many seconds (max 600).
    """
    env = await sandbox.for_workspace(tool_context.user_id)
    r = await env.execute(command, timeout=min(max(timeout_seconds, 1), 600))
    return {"exit_code": r.exit_code, "stdout": _clip(r.stdout), "stderr": _clip(r.stderr), "timed_out": r.timed_out}


async def write_file(path: str, content: str, tool_context: ToolContext) -> dict:
    """Write a text file (HTML, CSS, copy, JSON) under /workspace, creating folders as needed.

    Args:
      path: Absolute path under /workspace, or relative to it.
      content: The full file content.
    """
    p = _workspace_path(path)
    env = await sandbox.for_workspace(tool_context.user_id)
    await env.write_file(Path(p), content)
    return {"written": str(p), "bytes": len(content.encode())}


def _fit(raw: bytes) -> tuple[bytes, str, tuple[int, int], tuple[int, int]]:
    with Image.open(io.BytesIO(raw)) as im:
        size, fmt = im.size, (im.format or "")
        if fmt in ("PNG", "JPEG", "WEBP") and max(size) <= LOOK_MAX_EDGE:
            return raw, f"image/{fmt.lower()}", size, size
        frame = im.copy()
    frame.thumbnail((LOOK_MAX_EDGE, LOOK_MAX_EDGE), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    if frame.mode in ("RGBA", "LA") or (frame.mode == "P" and "transparency" in frame.info):
        frame.convert("RGBA").save(out, "PNG")
        return out.getvalue(), "image/png", size, frame.size
    frame.convert("RGB").save(out, "JPEG", quality=85)
    return out.getvalue(), "image/jpeg", size, frame.size


async def look(paths: list[str], question: str, tool_context: ToolContext) -> str:
    """Look at images in /workspace and get a short answer; the images stay out of your context.

    A fast model looks at up to 8 images (each fitted within 1280 px) and answers `question` in
    about 200 words. Ask for what you need to decide: what is where, text you need read, colours,
    what is wrong, how two versions differ. Use it on every render before you deliver.

    Args:
      paths: Image files under /workspace.
      question: What you want to know about them.
    """
    wanted = [p.strip() for p in paths if p and p.strip()]
    if not wanted or not question.strip():
        return "Pass image paths under /workspace and a question."
    env = await sandbox.for_workspace(tool_context.user_id)
    parts: list[types.Part] = [types.Part.from_text(text=f"Question: {question.strip()}")]
    notes: list[str] = []
    for n, path in enumerate(wanted[:LOOK_MAX_IMAGES], 1):
        try:
            raw = await env.read_file(Path(_workspace_path(path)))
            data, mime, size, sent = await asyncio.to_thread(_fit, raw)
        except (FileNotFoundError, ValueError, OSError) as e:
            notes.append(f"{path}: {e or 'not found'}")
            continue
        shown = f", shown at {sent[0]}x{sent[1]}" if sent != size else ""
        parts += [types.Part.from_text(text=f"Image {n}: {path} ({size[0]}x{size[1]}{shown})"),
                  types.Part.from_bytes(data=data, mime_type=mime)]
    if len(wanted) > LOOK_MAX_IMAGES:
        notes.append(f"Looked at the first {LOOK_MAX_IMAGES} of {len(wanted)}; ask again for the rest.")
    if len(parts) == 1:
        return "Nothing to look at: " + " ".join(notes)
    try:
        reply = await _genai().aio.models.generate_content(
            model=LOOK_MODEL, contents=[types.Content(role="user", parts=parts)],
            config=types.GenerateContentConfig(system_instruction=LOOK_PROMPT),
        )
    except Exception as e:  # a failed look is a tool result, not a crash
        return f"The look failed ({type(e).__name__}); try again."
    await meter.record(tool_context, role="look", model=LOOK_MODEL, usage=reply.usage_metadata)
    return (reply.text or "(no answer)").strip() + ("\n\n" + "\n".join(notes) if notes else "")


async def deliver(paths: list[str], tool_context: ToolContext) -> dict:
    """Send finished files to the person. Nothing reaches them unless you call this.

    1 to 10 files under /workspace. A file already delivered unchanged in this conversation is
    skipped, so retrying is safe. Deliver first, then reply; never say a file is attached unless
    this returned status "delivered".

    Args:
      paths: Files under /workspace to send.
    """
    if not 1 <= len(paths) <= 10 or len(set(paths)) != len(paths):
        return {"status": "not delivered", "error": "Pass 1 to 10 distinct paths."}
    env = await sandbox.for_workspace(tool_context.user_id)
    sent: dict[str, str] = dict(tool_context.state.get("delivered", {}))
    delivered, skipped = [], []
    try:
        for path in paths:
            p = _workspace_path(path)
            data = await env.read_file(Path(p))
            digest = hashlib.sha256(data).hexdigest()
            if sent.get(str(p)) == digest:
                skipped.append(p.name)
                continue
            mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            version = await tool_context.save_artifact(p.name, types.Part.from_bytes(data=data, mime_type=mime))
            sent[str(p)] = digest
            delivered.append(f"{p.name} (v{version})")
    except (FileNotFoundError, ValueError) as e:
        return {"status": "not delivered", "error": str(e), "delivered_before_error": delivered}
    finally:
        tool_context.state["delivered"] = sent  # record partial sends too, so a retry never repeats them
    return {"status": "delivered", "files": delivered, "already_delivered": skipped}


IMAGE_MODEL = os.environ.get("STUDIO_IMAGE_MODEL", "gemini-nano-banana-2.1")
ASPECT_RATIOS = {"1:1": 1.0, "2:3": 2 / 3, "3:2": 3 / 2, "3:4": 3 / 4, "4:3": 4 / 3, "4:5": 4 / 5,
                 "5:4": 5 / 4, "9:16": 9 / 16, "16:9": 16 / 9, "21:9": 21 / 9}
_IMAGE_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


def aspect_ratio(size: str) -> str:
    """The supported Gemini aspect ratio closest to `1792x1024`, `16:9` or a name like `portrait`."""
    text = size.strip().lower().replace("/", ":")
    named = {"square": "1:1", "landscape": "16:9", "wide": "16:9", "portrait": "9:16", "tall": "9:16"}
    if text in ASPECT_RATIOS or text in named:
        return named.get(text, text)
    for sep in ("x", ":"):
        w, _, h = text.partition(sep)
        try:
            ratio = float(w) / float(h)
        except (ValueError, ZeroDivisionError):
            continue
        if ratio > 0:
            return min(ASPECT_RATIOS, key=lambda k: abs(ASPECT_RATIOS[k] - ratio))
    return "1:1"


async def generate_image(prompt: str, path: str, tool_context: ToolContext, size: str = "1024x1024",
                         quality: str = "low", n: int = 1) -> str:
    """Generate original imagery (illustration, scene, texture, hero object) into /workspace.

    Read the image-generation skill first. Never ask for the logo or exact copy: generate the
    picture without words or marks, then lay real type and the real logo over it in HTML.
    Every call costs money: explore at quality="low" (1K), finish once at "high" (2K).
    There are no transparent backgrounds: generate on a plain ground and cut the subject out.

    Args:
      prompt: Subject, action, medium, light, composition, palette in words and hex, and
        "no text, no words, no letters, no logos".
      path: Where to save it under /workspace, e.g. projects/<slug>/art/hero.png. With n > 1
        files get _0, _1 suffixes.
      size: Target size like "1536x1024" or a ratio like "16:9"; the nearest supported ratio is used.
      quality: "low" (1K, about $0.03) or "high" (2K, about $0.05).
      n: How many variants, 1 to 4 (made in parallel).
    """
    try:
        target = _workspace_path(path)
    except ValueError as e:
        return f"not generated: {e}"
    if not prompt.strip():
        return "not generated: pass a prompt."
    image_config = types.ImageConfig(aspect_ratio=aspect_ratio(size), image_size="2K" if quality == "high" else "1K")
    config = types.GenerateContentConfig(response_modalities=["IMAGE"], image_config=image_config)
    client = _genai().aio
    calls = [client.models.generate_content(model=IMAGE_MODEL, contents=prompt, config=config)
             for _ in range(max(1, min(n, 4)))]
    results = await asyncio.gather(*calls, return_exceptions=True)
    images, problems = [], []
    for r in results:
        if isinstance(r, Exception):
            problems.append(f"{type(r).__name__}: {str(r)[:200]}")
            continue
        await meter.record(tool_context, role="image", model=IMAGE_MODEL, usage=r.usage_metadata)
        found = [p.inline_data for c in (r.candidates or []) for p in (c.content.parts if c.content else []) or []
                 if p.inline_data and p.inline_data.data]
        images += found
        if not found:
            fb = r.prompt_feedback.block_reason if r.prompt_feedback else None
            finish = r.candidates[0].finish_reason if r.candidates else None
            problems.append(f"refused ({fb or finish or 'no image returned'})")
    env = await sandbox.for_workspace(tool_context.user_id)
    written = []
    for i, blob in enumerate(images):
        ext = _IMAGE_EXT.get(blob.mime_type or "", target.suffix or ".png")
        out = target.with_name(f"{target.stem}_{i}{ext}" if len(images) > 1 else f"{target.stem}{ext}")
        await env.write_file(Path(out), blob.data)
        written.append(str(out))
    lines = [f"wrote {', '.join(written)} ({image_config.aspect_ratio}, {image_config.image_size})"] if written else ["no images written."]
    return "\n".join(lines + [f"note: {p}" for p in problems])
