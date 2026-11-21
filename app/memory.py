"""Memory (DA memory.py + instructions.md "Memory"): a team layer and a personal layer, as files.

design-agent mounts `/memories/agent/` (house rules everyone shares) and `/memories/user/` (how one
person likes their work), each with a short `AGENTS.md` index kept in context, and the agent edits
them with its file tools. Studio does the same per workspace:
- before a lead turn, both layers are staged into the sandbox at `/workspace/memories/{agent,user}/`
  and each `AGENTS.md` joins the per-turn instruction (after the cached prefix);
- after the turn, a layer whose files changed is saved back (GCS `workspaces/<id>/memories/<layer>.zip`,
  or `.data` locally).
Helpers never run these callbacks, so only the lead writes memory, as in design-agent.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import zipfile
from pathlib import Path

from app import sandbox

log = logging.getLogger(__name__)
LAYERS = ("agent", "user")
ROOT = "/workspace/memories"
MAX_BYTES, MAX_FILES, HOT_CHARS = 2 * 1024 * 1024, 100, 4000
# ponytail: last writer wins if two requests in one workspace edit the same layer at once; add a
# generation precondition on the GCS write if that ever matters.


def _key(workspace_id: str, user_id: str, layer: str) -> str:
    return f"workspaces/{workspace_id}/memories/{layer}" + (f"-{user_id}" if layer == "user" else "") + ".zip"


def _load(key: str) -> bytes | None:
    if bucket := os.environ.get("STUDIO_BUCKET"):
        from google.cloud import storage

        blob = storage.Client().bucket(bucket).blob(key)
        return blob.download_as_bytes() if blob.exists() else None
    p = Path(os.environ.get("STUDIO_DATA_DIR", ".data")) / key
    return p.read_bytes() if p.is_file() else None


def _save(key: str, data: bytes) -> None:
    if bucket := os.environ.get("STUDIO_BUCKET"):
        from google.cloud import storage

        storage.Client().bucket(bucket).blob(key).upload_from_string(data, content_type="application/zip")
        return
    p = Path(os.environ.get("STUDIO_DATA_DIR", ".data")) / key
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def _files(data: bytes | None) -> dict[str, bytes]:
    if not data:
        return {}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {n: z.read(n) for n in sorted(z.namelist()) if not n.endswith("/")}


def _digest(files: dict[str, bytes]) -> str:
    h = hashlib.sha256()
    for name, body in sorted(files.items()):
        h.update(name.encode() + b"\0" + hashlib.sha256(body).digest())
    return h.hexdigest()


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, body in sorted(files.items()):
            z.writestr(zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0)), body)
    return buf.getvalue()


async def load_memory(callback_context) -> None:
    """Lead before_agent: stage both layers and put their AGENTS.md into the instruction."""
    ws, uid = callback_context.user_id, callback_context.user_id  # one person per workspace today
    env = await sandbox.for_workspace(ws)
    hot, digests = [], {}
    await env.execute(f"rm -rf {ROOT} && mkdir -p {ROOT}/agent {ROOT}/user", timeout=30)
    for layer in LAYERS:
        files = _files(await asyncio.to_thread(_load, _key(ws, uid, layer)))
        digests[layer] = _digest(files)
        if files:
            await env.upload_zip(f"{ROOT}/{layer}", _zip(files))
        index = files.get("AGENTS.md", b"").decode(errors="replace").strip()
        if index:
            hot.append(f"--- /workspace/memories/{layer}/AGENTS.md ---\n{index[:HOT_CHARS]}")
    callback_context.state["temp:memory_digests"] = digests
    callback_context.state["memory_note"] = ("\n<memory>\nWhat you have been asked to remember (team rules, then this "
                                             "person's preferences):\n\n" + "\n\n".join(hot) + "\n</memory>") if hot else ""


async def save_memory(callback_context) -> None:
    """Lead after_agent: save each layer the turn changed. Never raises."""
    ws = uid = callback_context.user_id
    digests = callback_context.state.get("temp:memory_digests") or {}
    try:
        env = await sandbox.for_workspace(ws)
        for layer in LAYERS:
            files = _files(await env.download_zip(f"{ROOT}/{layer}"))
            if _digest(files) == digests.get(layer):
                continue
            if len(files) > MAX_FILES or sum(map(len, files.values())) > MAX_BYTES:
                log.warning("memory layer %s too large; not saved", layer)
                continue
            await asyncio.to_thread(_save, _key(ws, uid, layer), _zip(files))
    except Exception:
        log.exception("saving memory failed")
