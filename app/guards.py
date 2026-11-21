"""Model- and tool-call guards, ported from design-agent's middleware onto ADK callbacks.

- `tool_error_boundary` (DA middleware/tool_error_boundary.py): a tool that raises comes back to
  the model as a tool error instead of ending the run with its progress.
- `clamp_images` (DA middleware/image_clamp.py): inline images are sniffed from their bytes and
  fitted within 1500 px on the way to the model; a type the model cannot take is dropped with a
  note. Only the outgoing request changes; stored events and sandbox files keep the originals.
- `turn_notes` (DA middleware/today.py + budget.py): today's date, and once spend passes
  STUDIO_JOB_BUDGET_USD, a note to ask the person before starting more work. Both ride the
  per-turn instruction, after the cached static prefix.
"""

from __future__ import annotations

import datetime as dt
import io
import logging
import os
from zoneinfo import ZoneInfo

from google.genai import types
from PIL import Image

log = logging.getLogger(__name__)
MAX_EDGE = 1500
_IMAGE_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png", "GIF": "image/gif", "WEBP": "image/webp"}
_MODEL_TAKES = ("application/pdf", "video/", "audio/", "text/")


async def tool_error_boundary(tool, args, tool_context, error: Exception) -> dict:
    """on_tool_error_callback: the model hears the failure (class name only; messages can hold request text)."""
    log.warning("tool %s raised %s", tool.name, type(error).__name__)
    return {"status": "error", "error": f"{tool.name} failed ({type(error).__name__}). "
            "Try again once; if it fails again, work around it or say plainly what could not be done."}


def _clamp(blob: types.Blob) -> types.Part | None:
    """The part to send instead, or None to send it unchanged. A text Part means 'dropped'."""
    mime = (blob.mime_type or "").lower()
    if not mime.startswith("image/") and mime != "application/octet-stream":
        return None if mime.startswith(_MODEL_TAKES) else types.Part.from_text(
            text=f"[An attached {mime or 'file'} is not shown to the model; it is in /workspace/attachments.]")
    try:
        with Image.open(io.BytesIO(blob.data)) as im:
            fmt, size = im.format or "", im.size
            if fmt not in _IMAGE_TYPES:
                raise ValueError(fmt)
            if max(size) <= MAX_EDGE and _IMAGE_TYPES[fmt] == mime:
                return None
            if max(size) <= MAX_EDGE:  # right bytes, wrong label
                return types.Part.from_bytes(data=blob.data, mime_type=_IMAGE_TYPES[fmt])
            frame = im.copy()  # first frame of an animation
    except Exception:
        return types.Part.from_text(text="[An attached image could not be read; it is in /workspace/attachments.]")
    frame.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    if frame.mode in ("RGBA", "LA", "P"):
        frame.convert("RGBA").save(out, "PNG")
        return types.Part.from_bytes(data=out.getvalue(), mime_type="image/png")
    frame.convert("RGB").save(out, "JPEG", quality=88)
    return types.Part.from_bytes(data=out.getvalue(), mime_type="image/jpeg")


async def clamp_images(callback_context, llm_request) -> None:
    """before_model_callback: rewrite oversized, mislabeled or unsupported inline parts in the request."""
    contents = []
    for content in llm_request.contents or []:
        parts, changed = [], False
        for part in content.parts or []:
            swap = _clamp(part.inline_data) if part.inline_data and part.inline_data.data else None
            parts.append(swap or part)
            changed = changed or swap is not None
        contents.append(content.model_copy(update={"parts": parts}) if changed else content)
    llm_request.contents = contents


def turn_notes(state, now: dt.datetime | None = None) -> None:
    """Called from before_agent once per turn: today's date, and the budget note when spend passed it."""
    zone = os.environ.get("STUDIO_TIMEZONE", "UTC")
    try:
        local = (now or dt.datetime.now(dt.UTC)).astimezone(ZoneInfo(zone))
    except Exception:
        zone, local = "UTC", (now or dt.datetime.now(dt.UTC))
    state["today_note"] = f"\n[Today is {local:%A %Y-%m-%d} ({zone}); this turn started at {local:%H:%M}.]"
    budget = float(os.environ.get("STUDIO_JOB_BUDGET_USD") or 0)
    spent = float((state.get("usage") or {}).get("cost_usd") or 0)
    state["budget_note"] = (
        f"\n[Budget] This conversation's model spend is about ${spent:.2f}, past its ${budget:.2f} budget. "
        "Before starting more work, ask the person whether to continue; finish only what is already running."
        if budget and spent >= budget else "")
