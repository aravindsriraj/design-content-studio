"""Local vision judge over the delivered files, read back from the dev sandbox (STUDIO_SANDBOX_URL).

An eval signal only: LangChain found model taste judges unreliable as gates. Scores 1-5.
"""

import base64
import json
import os
import threading
import urllib.parse
import urllib.request

from google import genai
from google.genai import types
from pydantic import BaseModel

_local = threading.local()
SANDBOX = os.environ.get("STUDIO_SANDBOX_URL", "http://localhost:8080")

RUBRIC = """You art-direct a small studio. Grade the delivered file(s) (stills, PDF pages, GIF frames) against the request and the brand. Score 1-5.
Brand: warm paper ground #f6f1e7 or ink #14161a, signal orange #ff5a36 used sparingly, sand #e8dcc4, greys;
type is Geist (sans) and Fragment Mono; calm, exact, lots of space; never pure black, saturated primaries or magenta.
The "Example Studio" logo lockup is an official asset with its own serif wordmark; do not mark it down for that.
5 = a designer would ship it as is; 4 = small nits; 3 = needs a revision round; 2 = off-brand or broken; 1 = unusable.
Check: on-brand colours and type, clear hierarchy, legible at the target size, nothing clipped or overlapping,
requested copy exact, each size composed for its shape, and the case expectation.
When the request edits a file the person supplied (a reframe, a retime, a text swap), that file's own colours
and type are correct: judge the edit against the source, not against the studio brand.
Return JSON {"score": 1-5, "explanation": "<two sentences: what works, what fails>"}."""


_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp",
         "pdf": "application/pdf", "gif": "image/gif"}


def _gif_sheet(path: str) -> bytes:
    """First, middle and last frames side by side, made with ffmpeg in the sandbox."""
    q = urllib.parse.quote
    out = "/workspace/.judge/sheet.png"
    cmd = (f"n=$(ffprobe -v error -count_frames -select_streams v:0 -show_entries stream=nb_read_frames -of csv=p=0 '{path}'); "
           f"ffmpeg -loglevel error -y -i '{path}' -vf \"select='eq(n,0)+eq(n,trunc($n/2))+eq(n,$n-1)',scale=480:-1,tile=3x1\" "
           f"-frames:v 1 {out}")
    cmd = "mkdir -p /workspace/.judge && " + cmd
    req = urllib.request.Request(f"{SANDBOX}/exec", data=json.dumps({"command": cmd, "timeout": 60}).encode(),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=90).read()
    with urllib.request.urlopen(f"{SANDBOX}/files?path={q(out)}", timeout=30) as r:
        return base64.b64decode(json.load(r)["content_b64"])


class _Verdict(BaseModel):
    score: int
    explanation: str


def _client() -> genai.Client:
    c = getattr(_local, "client", None)
    if c is None:
        c = _local.client = genai.Client()
    return c


def _text(content) -> str:
    return " ".join(p.get("text", "") for p in ((content or {}).get("parts") or []) if isinstance(p, dict))


def _delivered(instance) -> list[str]:
    calls, ok = {}, []
    for t in (instance.get("agent_data") or {}).get("turns", []):
        for e in t.get("events", []):
            for p in ((e.get("content") or {}).get("parts") or []):
                if (fc := p.get("function_call")) and fc.get("name") == "deliver":
                    calls[fc.get("id")] = (fc.get("args") or {}).get("paths") or []
                if (fr := p.get("function_response")) and fr.get("name") == "deliver":
                    if (fr.get("response") or {}).get("status") == "delivered":
                        ok += calls.get(fr.get("id"), [])
    return list(dict.fromkeys(ok))


def evaluate(instance):
    paths = [p for p in _delivered(instance) if p.lower().endswith(tuple(_MIME))][:6]
    if not paths:
        return {"score": 0, "explanation": "no image delivered"}
    parts = [types.Part.from_text(text=f"{RUBRIC}\n\nRequest: {_text(instance.get('prompt'))}\n"
                                       f"Case reference: {_text((instance.get('reference') or {}).get('response'))}")]
    for p in paths:
        try:
            with urllib.request.urlopen(f"{SANDBOX}/files?path={urllib.parse.quote(p)}", timeout=30) as r:
                data = base64.b64decode(json.load(r)["content_b64"])
        except Exception as e:
            return {"score": 0, "explanation": f"could not read {p} from the sandbox: {e}"}
        mime = _MIME[p.lower().rsplit(".", 1)[-1]]
        if mime == "image/gif":  # the model sees a GIF's first frame only: send first/middle/last as one sheet
            data, mime = _gif_sheet(p), "image/png"
            parts.append(types.Part.from_text(text=f"{p} is animated; below are its first, middle and last frames."))
        parts += [types.Part.from_text(text=f"Delivered: {p}"), types.Part.from_bytes(data=data, mime_type=mime)]
    r = _client().models.generate_content(
        model="gemini-3.8-flash", contents=[types.Content(role="user", parts=parts)],
        config=types.GenerateContentConfig(temperature=0, response_mime_type="application/json", response_schema=_Verdict),
    )
    v = r.parsed
    if v is None:
        return {"score": 0, "explanation": r.text or ""}
    return {"score": max(1, min(5, v.score)), "explanation": v.explanation}
