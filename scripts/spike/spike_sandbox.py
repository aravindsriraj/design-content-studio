"""M0 spike: prove the sandbox image renders a still at 3 sizes through the shim API.

Uses only the shim's public endpoints (the same ones the agent's Environment layer will call):
upload tools + a fixture page as zips, run html2png and artifact-contract via /exec, pull the PNGs back.

  uv run --no-project python scripts/spike/spike_sandbox.py http://localhost:8080
  uv run --no-project python scripts/spike/spike_sandbox.py https://<lb-host> \
      --header "Authorization: Bearer <jwt>" --header "X-Sandbox-Routing-Token: <tok>" --header "X-Sandbox-Port: 8080"

Exits non-zero on any failure; prints timings as one JSON line at the end.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SIZES = ["1200x627", "1600x900", "1080x1350"]


def zip_dir(src: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(src.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(src).as_posix())
    return buf.getvalue()


class Shim:
    def __init__(self, base: str, headers: dict[str, str]):
        self.base, self.headers = base.rstrip("/"), headers

    def call(self, method: str, path: str, body: bytes | None = None, ctype: str = "application/json", timeout: float = 300):
        req = urllib.request.Request(self.base + path, data=body, method=method, headers={**self.headers, "Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            print(f"{method} {path} -> {e.code}: {e.read()[:500]!r} server={e.headers.get('server')}", file=sys.stderr)
            raise

    def exec(self, command: str, timeout: float = 120) -> dict:
        out = json.loads(self.call("POST", "/exec", json.dumps({"command": command, "timeout": timeout}).encode(), timeout=timeout + 30))
        print(f"$ {command}\n  exit={out['exit_code']}\n  {out['stdout'].strip()[:800]}\n  {out['stderr'].strip()[:800]}")
        return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("base_url")
    ap.add_argument("--header", action="append", default=[], help="'Name: value', repeatable")
    ap.add_argument("--out", default=str(ROOT / ".spike-out"))
    args = ap.parse_args()
    shim = Shim(args.base_url, dict(h.split(": ", 1) for h in args.header))
    t: dict[str, float] = {}

    t0 = time.monotonic()
    for _ in range(90):  # cold start: poll healthz up to ~180 s
        try:
            shim.call("GET", "/healthz", timeout=10)
            break
        except Exception:
            time.sleep(2)
    else:
        print("sandbox never became healthy", file=sys.stderr)
        return 1
    t["healthy_s"] = time.monotonic() - t0

    t0 = time.monotonic()
    shim.call("POST", "/files/zip?path=tools", zip_dir(ROOT / "app" / "sandbox_tools"), "application/zip")
    shim.call("POST", "/files/zip?path=projects/spike", zip_dir(Path(__file__).parent), "application/zip")
    t["upload_s"] = time.monotonic() - t0

    probe = shim.exec("node --version && chrome-headless-shell --version; fc-match -f '%{family[0]}\\n' Inter; fc-match -f '%{family[0]}\\n' 'IBM Plex Mono'")
    if probe["exit_code"] != 0:
        return 1

    t0 = time.monotonic()
    sizes = " ".join(f"--size {s}" for s in SIZES)
    render = shim.exec(f"node tools/html2png.mjs projects/spike/card.html --out projects/spike/out --name card {sizes} --require-font Inter --require-font 'IBM Plex Mono'")
    t["render_3_sizes_s"] = time.monotonic() - t0
    if render["exit_code"] != 0:
        return 1

    expects = " ".join(f"--expect projects/spike/out/card-{s}.png:{s}" for s in SIZES)
    if shim.exec(f"node tools/artifact-contract.mjs {expects}")["exit_code"] != 0:
        return 1

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for s in SIZES:
        data = json.loads(shim.call("GET", f"/files?path=projects/spike/out/card-{s}.png"))["content_b64"]
        (out / f"card-{s}.png").write_bytes(base64.b64decode(data))
    print(json.dumps({"ok": True, "out": str(out), **{k: round(v, 2) for k, v in t.items()}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
