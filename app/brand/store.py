"""Per-workspace brand packs: validated on upload, kept in GCS (or .data locally), cached by version.

A pack is a zip of the brand folder (brand.toml, tokens.json, fonts.toml, voice.md,
graphic-language.md, logos/, fonts/). Font `file` paths in fonts.toml are relative to the pack.
With STUDIO_BUCKET set the zip lives at gs://<bucket>/workspaces/<id>/brand.zip and each instance
caches it by object generation, so a new upload reaches every Cloud Run instance on its next turn.
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

from app.brand.schema import BrandPack, BrandPackError, load_brand_pack

SAMPLE = Path(__file__).resolve().parent / "sample"
CACHE = Path(os.environ.get("STUDIO_DATA_DIR", ".data")) / "brands"
MAX_ZIP_BYTES = 25 * 1024 * 1024
MAX_FILES = 200


def _blob(workspace_id: str, name: str = "brand.zip"):
    bucket = os.environ.get("STUDIO_BUCKET")
    if not bucket:
        return None
    from google.cloud import storage

    return storage.Client().bucket(bucket).blob(f"workspaces/{workspace_id}/{name}")


def _extract(data: bytes, dest: Path) -> None:
    """Unzip into dest, refusing traversal, links and oversized archives. Strips one wrapping folder."""
    if len(data) > MAX_ZIP_BYTES:
        raise BrandPackError(dest, [f"zip is over {MAX_ZIP_BYTES // 2**20} MB"])
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise BrandPackError(dest, ["not a zip file"]) from None
    members = [m for m in zf.infolist() if not m.is_dir() and "__MACOSX" not in m.filename]
    if len(members) > MAX_FILES:
        raise BrandPackError(dest, [f"more than {MAX_FILES} files"])
    if sum(m.file_size for m in members) > 4 * MAX_ZIP_BYTES:
        raise BrandPackError(dest, ["unpacked size too large"])
    names = [m.filename for m in members]
    top = {n.split("/", 1)[0] for n in names}
    strip = (top.pop() + "/") if len(top) == 1 and all("/" in n for n in names) else ""
    root = dest.resolve()
    for m in members:
        rel = m.filename[len(strip):]
        target = (dest / rel).resolve()
        if not rel or not target.is_relative_to(root) or (m.external_attr >> 16) & 0o170000 == 0o120000:
            raise BrandPackError(dest, [f"unsafe path in zip: {m.filename}"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(zf.read(m))


def validate(data: bytes) -> BrandPack:
    """Validate a pack zip without storing it (raises BrandPackError listing every problem)."""
    with tempfile.TemporaryDirectory() as tmp:
        _extract(data, Path(tmp))
        return load_brand_pack(Path(tmp), project_root=Path(tmp))


def save(workspace_id: str, data: bytes) -> BrandPack:
    """Validate, then store. The previous pack stays active if validation fails."""
    validate(data)
    if blob := _blob(workspace_id):
        blob.upload_from_string(data, content_type="application/zip")
        version = str(blob.generation)
    else:
        version = "local"
    return _cache(workspace_id, version, data)


def _cache(workspace_id: str, version: str, data: bytes) -> BrandPack:
    ws_dir = CACHE / workspace_id
    final = ws_dir / version
    tmp = ws_dir / f".tmp-{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    _extract(data, tmp)
    shutil.rmtree(final, ignore_errors=True)
    tmp.rename(final)
    for old in ws_dir.iterdir():
        if old != final and not old.name.startswith(".tmp"):
            shutil.rmtree(old, ignore_errors=True)
    return load_brand_pack(final, project_root=final)


def load(workspace_id: str) -> BrandPack:
    """The workspace's brand pack, or the bundled sample brand when none was uploaded."""
    if blob := _blob(workspace_id):
        if not blob.exists():
            return load_brand_pack(SAMPLE, check_fonts=False)
        blob.reload()
        cached = CACHE / workspace_id / str(blob.generation)
        if cached.is_dir():
            return load_brand_pack(cached, project_root=cached)
        return _cache(workspace_id, str(blob.generation), blob.download_as_bytes())
    local = CACHE / workspace_id / "local"
    if local.is_dir():
        return load_brand_pack(local, project_root=local)
    return load_brand_pack(SAMPLE, check_fonts=False)


def summary(pack: BrandPack) -> dict:
    d = pack.tokens.design
    return {
        "name": pack.brand.name,
        "description": pack.brand.description,
        "is_sample": pack.root.resolve() == SAMPLE.resolve(),
        "colors": list(d.allowed_colors),
        "banned_colors": list(d.banned_colors.colors) if d.banned_colors else [],
        "fonts": sorted({f.family for f in pack.fonts.fonts}),
        "formats": [f.model_dump() for f in pack.brand.formats],
        "logos": [lg.file for lg in pack.brand.logos],
    }


# ---------------------------------------------------------------- drafts (brand onboarding)
# A pack the agent built from a website waits here until the person activates it.


def _draft_path(workspace_id: str) -> Path:
    return CACHE.parent / "brand-drafts" / f"{workspace_id}.zip"  # outside CACHE/<ws>, which _cache() prunes


def save_draft(workspace_id: str, data: bytes) -> BrandPack:
    pack = validate(data)
    if blob := _blob(workspace_id, "brand-draft.zip"):
        blob.upload_from_string(data, content_type="application/zip")
    else:
        _draft_path(workspace_id).parent.mkdir(parents=True, exist_ok=True)
        _draft_path(workspace_id).write_bytes(data)
    return pack


def load_draft(workspace_id: str) -> bytes | None:
    if blob := _blob(workspace_id, "brand-draft.zip"):
        return blob.download_as_bytes() if blob.exists() else None
    p = _draft_path(workspace_id)
    return p.read_bytes() if p.is_file() else None


def discard_draft(workspace_id: str) -> None:
    if blob := _blob(workspace_id, "brand-draft.zip"):
        if blob.exists():
            blob.delete()
    else:
        _draft_path(workspace_id).unlink(missing_ok=True)


def activate_draft(workspace_id: str) -> BrandPack:
    data = load_draft(workspace_id)
    if data is None:
        raise BrandPackError(Path(workspace_id), ["there is no draft brand pack"])
    pack = save(workspace_id, data)
    discard_draft(workspace_id)
    return pack


def draft_summary(workspace_id: str) -> dict | None:
    """Summary of the draft, with its review notes (QUESTIONS.md) when the agent wrote them."""
    data = load_draft(workspace_id)
    if data is None:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        _extract(data, Path(tmp))
        pack = load_brand_pack(Path(tmp), project_root=Path(tmp))
        out = summary(pack)
        notes = Path(tmp) / "QUESTIONS.md"
        out["questions"] = notes.read_text()[:4000] if notes.is_file() else ""
        out["is_sample"] = False
    return out
