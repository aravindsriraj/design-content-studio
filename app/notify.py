"""Email the person when their background job finishes (Resend).

The API key comes from RESEND_API_KEY, else Secret Manager secret STUDIO_RESEND_SECRET
(default "studio-resend-api-key", latest version). No key: the email is skipped and logged,
never a job failure. Until a sending domain is verified in Resend, STUDIO_EMAIL_FROM must stay
"onboarding@resend.dev", which can only deliver to the Resend account owner's address.
"""

from __future__ import annotations

import base64
import functools
import html
import logging
import os
import re

import httpx

log = logging.getLogger(__name__)
RESEND_URL = "https://api.resend.com/emails"


@functools.cache
def _api_key() -> str | None:
    if key := os.environ.get("RESEND_API_KEY"):
        return key
    try:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        creds, project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        project = os.environ.get("GOOGLE_CLOUD_PROJECT", project)
        secret = os.environ.get("STUDIO_RESEND_SECRET", "studio-resend-api-key")
        r = AuthorizedSession(creds).get(
            f"https://secretmanager.googleapis.com/v1/projects/{project}/secrets/{secret}/versions/latest:access",
            timeout=10,
        )
        if r.status_code != 200:
            log.warning("no Resend key in Secret Manager (%s); emails are skipped", r.status_code)
            return None
        return base64.b64decode(r.json()["payload"]["data"]).decode().strip()
    except Exception:
        log.exception("could not read the Resend key; emails are skipped")
        return None


# The app's palette (web/src/styles.css), inlined: email clients drop <style> blocks and variables.
INK, INK2, INK3, RULE, DESK, SHEET, MAT, SIGNAL, DANGER = (
    "#1b2321", "#4c5753", "#7c8783", "#dde2de", "#f6f7f5", "#ffffff", "#2b4a3f", "#1488ad", "#a8321f")
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
_CODE = f"font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px;background:{DESK};border:1px solid {RULE};border-radius:4px;padding:1px 5px;white-space:nowrap"


def _inline(text: str) -> str:
    """Escaped text with the agent's inline markdown: `code` (hex colours get a swatch), **bold**, *italic*, links."""
    out, parts = [], re.split(r"(`[^`\n]+`)", text)
    for i, part in enumerate(parts):
        if i % 2:  # a code span: escape, never format inside it
            code = part[1:-1]
            swatch = (f'<span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:{code};'
                      f'border:1px solid {RULE};margin-right:4px;vertical-align:-1px"></span>'
                      if re.fullmatch(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?", code) else "")
            out.append(f'<span style="{_CODE}">{swatch}{html.escape(code)}</span>')
            continue
        s = html.escape(part, quote=False)
        s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s\"]+)\)", rf'<a href="\2" style="color:{SIGNAL}">\1</a>', s)
        s = re.sub(r"(?<![\"=>])(https?://[^\s<)\"]*[^\s<)\".,;:!?])", rf'<a href="\1" style="color:{SIGNAL}">\1</a>', s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?![*\w])", r"<em>\1</em>", s)
        out.append(s)
    return "".join(out)


def _markdown(text: str) -> str:
    """The agent's reply (markdown) as email-safe HTML: headings, lists, paragraphs. Everything is escaped."""
    blocks, para, items, kind = [], [], [], ""
    p = f"margin:0 0 14px;color:{INK2}"

    def flush():
        nonlocal para, items, kind
        if para:
            blocks.append(f'<p style="{p}">{"<br>".join(_inline(x) for x in para)}</p>')
        if items:
            lis = "".join(f'<li style="margin:0 0 6px">{_inline(x)}</li>' for x in items)
            blocks.append(f'<{kind} style="margin:0 0 14px;padding-left:20px;color:{INK2}">{lis}</{kind}>')
        para, items, kind = [], [], ""

    for line in text.strip().splitlines():
        s = line.strip()
        if not s:
            flush()
        elif m := re.match(r"#{1,6}\s+(.*)", s):
            flush()
            blocks.append(f'<p style="margin:20px 0 8px;font-size:15px;font-weight:600;color:{INK}">{_inline(m[1])}</p>')
        elif m := re.match(r"(?:[-*•]|(\d+)[.)])\s+(.*)", s):
            want = "ol" if m[1] else "ul"
            if para or (items and kind != want):
                flush()
            kind = want
            items.append(m[2])
        elif items and line[:1].isspace():  # a wrapped list item
            items[-1] += " " + s
        else:
            if items:
                flush()
            para.append(s)
    flush()
    return "".join(blocks)


def compose(*, ok: bool, title: str, reply: str, files: list[str], link: str, error: str | None = None) -> tuple[str, str]:
    """(subject, html body). Everything user- or model-written is escaped before any formatting."""
    short = title if len(title) <= 60 else title[:57] + "…"
    subject = f"Ready: {short}" if ok else f"Stopped: {short}"
    status, color, lead = (("Ready", MAT, "Your request is done.") if ok
                           else ("Stopped", DANGER, "Your request stopped before it finished."))
    if ok:
        content = _markdown(reply)
        if files:
            chips = "".join(
                f'<span style="display:inline-block;margin:0 6px 6px 0;padding:5px 10px;border:1px solid {RULE};'
                f'border-radius:999px;font-size:13px;color:{INK}">{html.escape(f)}</span>' for f in files)
            content += (f'<p style="margin:22px 0 8px;font-size:13px;color:{INK3}">'
                        f'{len(files)} {"file" if len(files) == 1 else "files"} delivered</p><div>{chips}</div>')
    else:
        content = (f'<p style="margin:0 0 14px;color:{INK2}">{html.escape(error or "Something went wrong.")}</p>'
                   f'<p style="margin:0;color:{INK2}">Open it and send a short note like “continue” to pick up where it left off.</p>')
    body = f"""<!doctype html><html><body style="margin:0;padding:0;background:{DESK}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{DESK}"><tr><td align="center" style="padding:32px 16px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;font-family:{FONT};font-size:15px;line-height:1.6;color:{INK}">
<tr><td style="padding:0 4px 16px;font-size:16px;font-weight:600;color:{INK}">Design Content Studio</td></tr>
<tr><td style="background:{SHEET};border:1px solid {RULE};border-radius:14px;padding:28px 28px 30px">
<p style="margin:0 0 10px"><span style="display:inline-block;padding:3px 10px;border-radius:999px;background:{color};color:#ffffff;font-size:12px;font-weight:600">{status}</span></p>
<p style="margin:0 0 4px;font-size:13px;color:{INK3}">{lead}</p>
<h1 style="margin:0 0 22px;font-size:21px;line-height:1.3;font-weight:600;color:{INK}">{html.escape(title)}</h1>
{content}
<table role="presentation" cellpadding="0" cellspacing="0" style="margin-top:26px"><tr><td style="border-radius:8px;background:{SIGNAL}">
<a href="{html.escape(link)}" style="display:inline-block;padding:11px 20px;color:#ffffff;font-weight:600;text-decoration:none">Open in Design Content Studio</a>
</td></tr></table>
</td></tr>
<tr><td style="padding:16px 4px 0;font-size:12px;color:{INK3}">You're getting this because you asked Design Content Studio to email you when a request finishes.</td></tr>
</table></td></tr></table></body></html>"""
    return subject, body


async def send(to: str | None, subject: str, body_html: str) -> bool:
    """Send one email. Returns True if Resend accepted it. Never raises."""
    if not to:
        log.info("no recipient; email skipped")
        return False
    key = _api_key()
    if not key:
        return False
    sender = os.environ.get("STUDIO_EMAIL_FROM", "Design Content Studio <onboarding@resend.dev>")
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(RESEND_URL, headers={"Authorization": f"Bearer {key}"},
                             json={"from": sender, "to": [to], "subject": subject, "html": body_html})
        if r.status_code >= 300:
            log.warning("Resend rejected the email: %s %s", r.status_code, r.text[:300])
            return False
        return True
    except httpx.HTTPError:
        log.exception("email send failed")
        return False
