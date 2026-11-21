"""Phase A host tools: image pricing, aspect ratios, fetch_asset's SSRF guard."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app import meter
from app.fetch import FetchRefused, check_url, fetch
from app.tools import aspect_ratio


def test_image_tokens_priced_at_image_rate():
    usage = SimpleNamespace(prompt_token_count=100, cached_content_token_count=0,
                            candidates_token_count=1120, thoughts_token_count=0)
    assert meter.cost_usd("gemini-nano-banana-2.1", usage) == pytest.approx(0.0336 + 100 * 1.5e-6)


def test_aspect_ratio_picks_nearest_supported():
    assert aspect_ratio("1200x627") == "16:9"
    assert aspect_ratio("1080x1350") == "4:5"
    assert aspect_ratio("portrait") == "9:16"
    assert aspect_ratio("garbage") == "1:1"


async def _public(host, port):
    return {"evil.example": ["10.0.0.5"]}.get(host, ["93.184.216.34"])


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "http://localhost/x", "http://169.254.169.254/latest", "http://metadata.google.internal/",
    "http://user:pw@example.com/a.png", "http://example.com:8080/a.png", "http://evil.example/a.png",
    "http://[::ffff:127.0.0.1]/a.png",
])
def test_check_url_refuses_unsafe(url):
    with pytest.raises(FetchRefused):
        asyncio.run(check_url(url, _public))


def test_fetch_rechecks_redirects_and_types():
    def handler(req: httpx.Request):
        if req.url.path == "/hop":
            return httpx.Response(302, headers={"location": "http://evil.example/x.png"})
        if req.url.path == "/page":
            return httpx.Response(200, headers={"content-type": "text/html"}, text="<html>")
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"png")

    async def go(path):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await fetch(f"https://cdn.example{path}", client=c, resolver=_public)

    assert asyncio.run(go("/ok.png")) == (b"png", "image/png")
    for bad in ("/hop", "/page"):
        with pytest.raises(FetchRefused):
            asyncio.run(go(bad))



def test_clamp_shrinks_relabels_and_drops():
    import io

    from google.adk.models import LlmRequest
    from google.genai import types
    from PIL import Image

    from app.guards import MAX_EDGE, clamp_images

    def png(w, h):
        b = io.BytesIO()
        Image.new("RGB", (w, h), "white").save(b, "PNG")
        return b.getvalue()

    big, small = png(3000, 1000), png(100, 100)
    req = LlmRequest(contents=[types.Content(role="user", parts=[
        types.Part(text="hi"),
        types.Part.from_bytes(data=big, mime_type="image/png"),
        types.Part.from_bytes(data=small, mime_type="application/octet-stream"),
        types.Part.from_bytes(data=b"PK\x03\x04", mime_type="application/zip"),
        types.Part.from_bytes(data=b"%PDF-1.4", mime_type="application/pdf"),
    ])])
    asyncio.run(clamp_images(None, req))
    parts = req.contents[0].parts
    assert parts[0].text == "hi"
    assert max(Image.open(io.BytesIO(parts[1].inline_data.data)).size) == MAX_EDGE
    assert parts[2].inline_data.mime_type == "image/png"
    assert "not shown" in parts[3].text
    assert parts[4].inline_data.mime_type == "application/pdf"


def test_turn_notes_date_and_budget(monkeypatch):
    import datetime as dt

    from app.guards import turn_notes

    monkeypatch.setenv("STUDIO_TIMEZONE", "Asia/Kolkata")
    monkeypatch.setenv("STUDIO_JOB_BUDGET_USD", "1.00")
    state = {"usage": {"cost_usd": 0.5}}
    turn_notes(state, dt.datetime(2026, 11, 17, 20, 0, tzinfo=dt.UTC))
    assert "Wednesday 2026-11-18" in state["today_note"] and state["budget_note"] == ""
    state["usage"]["cost_usd"] = 1.2
    turn_notes(state)
    assert "$1.20" in state["budget_note"]


def test_tool_errors_reach_the_model():
    from app.guards import tool_error_boundary

    r = asyncio.run(tool_error_boundary(SimpleNamespace(name="bash"), {}, None, RuntimeError("secret url")))
    assert r["status"] == "error" and "RuntimeError" in r["error"] and "secret" not in r["error"]
