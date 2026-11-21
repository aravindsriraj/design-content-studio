"""Plans, credits and Dodo Payments.

Credits come from the usage ledger, never a second counter: what a workspace used this period is
the ledger's model cost since `period_start`, repriced at each model's latest (standard) rate and
divided by CREDIT_USD, so one credit is about one finished post. Balance = the plan's monthly
allowance + bought top-ups - used. Top-ups carry over; the allowance does not (at a period's end,
whatever the period used beyond its allowance comes off the top-ups).

Enforcement: `/api/jobs` refuses a new request at zero; inside a run `credit_gate` (before every
model call, lead and helpers) ends the turn once the balance is OVERRUN below zero; `plan_gate`
(before every tool call) keeps Pro tools from Free workspaces.

Dodo Payments is the merchant of record. Checkout carries the workspace id in metadata; only a
verified webhook changes a plan or adds credits (deduplicated on webhook-id, in one transaction).
The API and webhook keys come from env or Secret Manager (`studio-dodo-api-key`,
`studio-dodo-webhook-key`); without them billing reports "not set up" and the app keeps working.
"""

from __future__ import annotations

import base64
import datetime as dt
import logging
import os
import time

import sqlalchemy as sa
from google.adk.models import LlmResponse
from google.genai import types

from app import meter

log = logging.getLogger(__name__)

CREDIT_USD = 0.90  # standard-rate model cost of one credit (a typical post, measured in production)
ALLOWANCE = {"free": 3.0, "pro": 20.0}
TOPUP_CREDITS = 10.0
FREE_PERIOD = dt.timedelta(days=30)
PRO_PERIOD_GRACE = dt.timedelta(days=32)  # a missed renewal webhook still rolls the period
OVERRUN = 1.0  # a running request may finish up to this far below zero
ACTIVE = ("active", "past_due")  # Dodo statuses that keep Pro
PRICING = {  # shown on the landing page and in the plan panel; Dodo's products hold the real prices
    "currency": "USD", "credit_note": "1 credit is about one finished post",
    "free": {"price": 0, "credits": ALLOWANCE["free"]},
    "pro": {"monthly": 39, "annual": 390, "credits": ALLOWANCE["pro"]},
    "topup": {"price": 20, "credits": TOPUP_CREDITS},
    "typical": {"post": 1, "brand": 1.5, "short_motion": 1.5, "film_1min": 3, "looks": 5},
}
# Tools that need Pro (motion, sound, parallel work, scheduling); Free keeps stills, charts, PDFs,
# diagrams, GIF edits and brand setup.
PRO_TOOLS = {
    "start_render_job", "watch_progress", "generate_voiceover", "generate_music", "generate_sound_effect",
    "transcribe_audio", "start_looks", "pick_look", "start_task", "task", "run_pipeline", "answer_pipeline",
    "schedule_wakeup",
}
ITEMS = {"pro_monthly": "DODO_PRODUCT_PRO_MONTHLY", "pro_annual": "DODO_PRODUCT_PRO_ANNUAL",
         "topup": "DODO_PRODUCT_TOPUP"}

_meta = sa.MetaData()
accounts = sa.Table(
    "billing_accounts", _meta,
    sa.Column("workspace_id", sa.String(128), primary_key=True),
    sa.Column("email", sa.String(320)),
    sa.Column("customer_id", sa.String(64), index=True),
    sa.Column("subscription_id", sa.String(64), index=True),
    sa.Column("product_id", sa.String(64)),
    sa.Column("status", sa.String(24)),  # Dodo subscription status; None = never subscribed
    sa.Column("access_until", sa.DateTime(timezone=True)),  # a cancelled plan's paid-up end
    sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
    sa.Column("topup_credits", sa.Float, nullable=False, default=0.0),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)
events = sa.Table(
    "billing_events", _meta,
    sa.Column("webhook_id", sa.String(128), primary_key=True),
    sa.Column("type", sa.String(64), nullable=False),
    sa.Column("at", sa.DateTime(timezone=True), nullable=False),
)
_ready = False


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _aware(t: dt.datetime | None) -> dt.datetime | None:
    return t.replace(tzinfo=dt.UTC) if t is not None and t.tzinfo is None else t  # sqlite drops tz


async def _engine():
    global _ready
    eng = await meter.engine()
    if not _ready:
        async with eng.begin() as conn:
            await conn.run_sync(_meta.create_all)
        _ready = True
    return eng


def owner_emails() -> set[str]:
    return {e.strip().lower() for e in os.environ.get("STUDIO_OWNER_EMAILS", "").split(",") if e.strip()}


# ---------------------------------------------------------------- credits


def standard_factor(model: str, on: dt.date) -> float:
    """Latest rate / rate in force on `on`: introductory prices count at their standard rate."""
    tiers = meter.PRICES.get(model)
    if not tiers:
        return 1.0
    current = [r for start, r in tiers if start <= on] or [tiers[0][1]]
    return tiers[-1][1][0] / current[-1][0] if current[-1][0] else 1.0


async def used_credits(ws: str, since: dt.datetime) -> float:
    t = meter.usage_table
    q = (sa.select(t.c.model, sa.func.date(t.c.at), sa.func.coalesce(sa.func.sum(t.c.cost_usd), 0.0))
         .where(t.c.workspace_id == ws, t.c.at >= since).group_by(t.c.model, sa.func.date(t.c.at)))
    async with (await _engine()).connect() as conn:
        rows = (await conn.execute(q)).all()
    usd = sum(float(cost) * standard_factor(model, day if isinstance(day, dt.date) else dt.date.fromisoformat(str(day)))
              for model, day, cost in rows)
    return usd / CREDIT_USD


async def _row(ws: str) -> dict | None:
    async with (await _engine()).connect() as conn:
        r = (await conn.execute(sa.select(accounts).where(accounts.c.workspace_id == ws))).mappings().first()
    return dict(r) if r else None


async def ensure(ws: str, email: str | None = None) -> dict:
    """The workspace's account row, created on first sight (Free, period starting now)."""
    row = await _row(ws)
    async with (await _engine()).begin() as conn:
        if row is None:
            row = {"workspace_id": ws, "email": email, "period_start": _now(), "topup_credits": 0.0, "updated_at": _now()}
            try:
                await conn.execute(accounts.insert().values(**row))
            except sa.exc.IntegrityError:  # a concurrent request created it
                pass
        elif email and row.get("email") != email:
            await conn.execute(accounts.update().where(accounts.c.workspace_id == ws).values(email=email, updated_at=_now()))
            row["email"] = email
    return await _row(ws) or row


def plan_of(row: dict, now: dt.datetime | None = None) -> str:
    now = now or _now()
    if (row.get("email") or "").lower() in owner_emails():
        return "pro"
    if row.get("status") in ACTIVE:
        return "pro"
    until = _aware(row.get("access_until"))
    if row.get("status") == "cancelled" and until and now < until:
        return "pro"
    return "free"


async def _roll(conn, ws: str, row: dict, used: float, plan: str, now: dt.datetime) -> None:
    """Start a new period: the allowance resets, overuse comes off the top-ups."""
    over = max(0.0, used - ALLOWANCE[plan])
    topup = max(0.0, float(row.get("topup_credits") or 0) - over)
    await conn.execute(accounts.update().where(accounts.c.workspace_id == ws)
                       .values(period_start=now, topup_credits=topup, updated_at=now))


async def status(ws: str, email: str | None = None) -> dict:
    """Plan, credits and subscription for one workspace (rolls an elapsed period first)."""
    row = await ensure(ws, email)
    now = _now()
    plan = plan_of(row, now)
    owner = (row.get("email") or "").lower() in owner_emails()
    start = _aware(row["period_start"])
    if not owner and now - start >= (FREE_PERIOD if plan == "free" else PRO_PERIOD_GRACE):
        async with (await _engine()).begin() as conn:
            await _roll(conn, ws, row, await used_credits(ws, start), plan, now)
        row = await _row(ws) or row
        start = _aware(row["period_start"])
    used = await used_credits(ws, start)
    topup = float(row.get("topup_credits") or 0)
    allowance = ALLOWANCE[plan]
    balance = None if owner else round(allowance + topup - used, 2)
    return {
        "plan": plan, "owner": owner, "allowance": allowance, "topup": round(topup, 2), "used": round(used, 2),
        "balance": balance, "period_start": start.isoformat(),
        "period_end": (start + FREE_PERIOD).isoformat() if plan == "free" and not owner else None,
        "subscription": {"status": row.get("status"), "access_until": (_aware(row.get("access_until")).isoformat()
                                                                      if row.get("access_until") else None)},
        "can_manage": bool(row.get("customer_id")),
        "billing_ready": bool(_api_key()),
        # For "about N credits" next to a request's ledger cost (the lead model is ~95% of spend).
        "credits_per_usd": round(standard_factor("gemini-3.8-flash", now.date()) / CREDIT_USD, 4),
    }


async def can_start(ws: str, email: str | None = None) -> tuple[bool, str]:
    s = await status(ws, email)
    if s["balance"] is None or s["balance"] > 0:
        return True, ""
    if s["plan"] == "free":
        return False, "You've used this month's free credits. Upgrade to Pro for 20 credits a month."
    return False, "You're out of credits for this period. Buy a top-up or wait for your plan to renew."


# ---------------------------------------------------------------- agent callbacks


async def plan_note(callback_context) -> None:
    """before_agent: what this workspace's plan allows, so the agent says so instead of failing."""
    try:
        s = await status(callback_context.user_id)
    except Exception:
        log.exception("plan status failed")
        return
    if s["owner"]:
        note = ""
    elif s["plan"] == "free":
        note = (f"\n[Plan] Free plan: {max(0.0, s['balance']):.1f} of {s['allowance']:.0f} credits left this month "
                "(1 credit is about one finished post). Free makes stills, charts, PDFs, diagrams and GIF edits, and "
                "sets up a brand. Motion and video, voice, music, parallel looks, helpers and check-ins need Pro: if "
                "asked for them, say so in one line and offer what Free can make instead; don't attempt them.")
    else:
        note = (f"\n[Plan] Pro: {max(0.0, s['balance']):.1f} credits left this period (about 1 per post, 1.5 for a "
                "short motion piece, 3 for a 1-minute film, 5 for three looks). If a request clearly needs more than "
                "what's left, say so and ask before starting.")
    callback_context.state["plan_note"] = note


async def credit_gate(callback_context, llm_request) -> LlmResponse | None:
    """before_model (lead and helpers): end the turn once a run is OVERRUN past zero credits."""
    try:
        s = await status(callback_context.user_id)
    except Exception:
        log.exception("credit check failed; letting the call through")
        return None
    if s["balance"] is None or s["balance"] > -OVERRUN:
        return None
    text = ("I've stopped here: this workspace is out of credits. Upgrade or buy a top-up from Plan & credits, "
            "then send \"continue\" and I'll pick up where I left off.")
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))


async def plan_gate(tool, args, tool_context) -> dict | None:
    """before_tool: Pro tools (and HyperFrames renders) are refused on Free, with a reason the agent relays."""
    pro_only = tool.name in PRO_TOOLS or (tool.name == "bash" and "hyperframes render" in str(args.get("command", "")))
    if not pro_only:
        return None
    try:
        s = await status(tool_context.user_id)
    except Exception:
        log.exception("plan check failed; allowing %s", tool.name)
        return None
    if s["plan"] == "pro":
        return None
    return {"status": "error", "error": f"{tool.name} needs the Pro plan. Tell the person in one line that motion, "
            "voice, music, looks and background work are part of Pro, and offer what the Free plan can make."}


# ---------------------------------------------------------------- Dodo Payments


_found: dict[str, str] = {}
_missing: dict[str, float] = {}  # name -> when it was last found missing; retried after 5 minutes


def _secret(env: str, name: str) -> str | None:
    """From env, else Secret Manager. Only a found key is cached, so keys added later are picked up."""
    if v := os.environ.get(env):
        return v
    if name in _found:
        return _found[name]
    if time.monotonic() - _missing.get(name, -1e9) < 300:
        return None
    try:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        creds, project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        project = os.environ.get("GOOGLE_CLOUD_PROJECT", project)
        r = AuthorizedSession(creds).get(
            f"https://secretmanager.googleapis.com/v1/projects/{project}/secrets/{name}/versions/latest:access", timeout=10)
        if r.status_code != 200:
            _missing[name] = time.monotonic()
            return None
        _found[name] = base64.b64decode(r.json()["payload"]["data"]).decode().strip()
        return _found[name]
    except Exception:
        log.info("no %s in Secret Manager; billing is not set up", name)
        _missing[name] = time.monotonic()
        return None


def _api_key() -> str | None:
    return _secret("DODO_PAYMENTS_API_KEY", "studio-dodo-api-key")


_clients: dict[tuple, object] = {}


def _client():
    from dodopayments import AsyncDodoPayments

    env = "live_mode" if os.environ.get("DODO_PAYMENTS_ENVIRONMENT") == "live_mode" else "test_mode"
    key = (_api_key(), _secret("DODO_PAYMENTS_WEBHOOK_KEY", "studio-dodo-webhook-key"), env)
    if key not in _clients:
        _clients[key] = AsyncDodoPayments(bearer_token=key[0], environment=env, webhook_key=key[1])
    return _clients[key]


class NotSetUp(Exception):
    pass


async def checkout(ws: str, email: str | None, item: str, app_url: str) -> str:
    """A Dodo checkout URL for one item; the plan changes only when its webhook arrives."""
    product = os.environ.get(ITEMS.get(item, ""), "")
    if not _api_key() or not product:
        raise NotSetUp(item)
    row = await ensure(ws, email)
    if item == "topup" and plan_of(row) != "pro":
        raise ValueError("Top-ups are for Pro; upgrade first.")
    if item != "topup" and row.get("status") in ACTIVE:
        raise ValueError("You're already on Pro. Manage your plan from Manage subscription.")
    customer = {"customer_id": row["customer_id"]} if row.get("customer_id") else ({"email": email} if email else None)
    session = await _client().checkout_sessions.create(
        product_cart=[{"product_id": product, "quantity": 1}],
        **({"customer": customer} if customer else {}),
        metadata={"workspace_id": ws},
        feature_flags={"allow_discount_code": True},
        return_url=f"{app_url.rstrip('/')}/?billing=done",
    )
    return session.checkout_url


async def portal(ws: str, app_url: str) -> str:
    row = await _row(ws)
    if not _api_key():
        raise NotSetUp("portal")
    if not row or not row.get("customer_id"):
        raise ValueError("No subscription to manage yet.")
    session = await _client().customers.customer_portal.create(row["customer_id"], return_url=app_url)
    return session.link


def _when(v) -> dt.datetime | None:
    if v is None or isinstance(v, dt.datetime):
        return _aware(v)
    return _aware(dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")))


async def _workspace_for(conn, data: dict) -> str | None:
    if ws := (data.get("metadata") or {}).get("workspace_id"):
        return ws
    customer = data.get("customer") or {}
    for col, key in ((accounts.c.subscription_id, data.get("subscription_id")),
                     (accounts.c.customer_id, customer.get("customer_id")),
                     (accounts.c.email, customer.get("email"))):
        if key and (r := (await conn.execute(sa.select(accounts.c.workspace_id).where(col == key))).first()):
            return r[0]
    return None


async def apply_event(webhook_id: str, kind: str, data: dict) -> str:
    """Apply one verified webhook exactly once. Returns what happened (for logs and tests)."""
    now = _now()
    async with (await _engine()).begin() as conn:
        try:
            await conn.execute(events.insert().values(webhook_id=webhook_id, type=kind, at=now))
        except sa.exc.IntegrityError:
            return "duplicate"
        ws = await _workspace_for(conn, data)
        if ws is None:
            log.warning("billing webhook %s (%s): no workspace", webhook_id, kind)
            return "unmatched"
        row = (await conn.execute(sa.select(accounts).where(accounts.c.workspace_id == ws))).mappings().first()
        if row is None:
            await conn.execute(accounts.insert().values(workspace_id=ws, period_start=now, topup_credits=0.0, updated_at=now))
            row = (await conn.execute(sa.select(accounts).where(accounts.c.workspace_id == ws))).mappings().first()
        row = dict(row)
        customer_id = (data.get("customer") or {}).get("customer_id") or row.get("customer_id")

        if kind.startswith("subscription."):
            st = data.get("status")
            until = _when(data.get("next_billing_date")) if (st == "cancelled" or data.get("cancel_at_next_billing_date")) else None
            values = {"customer_id": customer_id, "subscription_id": data.get("subscription_id"),
                      "product_id": data.get("product_id"), "status": st, "access_until": until, "updated_at": now}
            await conn.execute(accounts.update().where(accounts.c.workspace_id == ws).values(**values))
            if kind in ("subscription.active", "subscription.renewed") and st in ACTIVE:
                await _roll(conn, ws, row, await used_credits(ws, _aware(row["period_start"])),
                            plan_of(row, now), now)
            return f"{ws}: subscription {st}"

        if kind == "payment.succeeded" and not data.get("subscription_id"):
            topup_product = os.environ.get("DODO_PRODUCT_TOPUP", "")
            n = sum(int(i.get("quantity") or 1) for i in data.get("product_cart") or []
                    if topup_product and i.get("product_id") == topup_product)
            if n:
                await conn.execute(accounts.update().where(accounts.c.workspace_id == ws).values(
                    topup_credits=float(row.get("topup_credits") or 0) + TOPUP_CREDITS * n,
                    customer_id=customer_id, updated_at=now))
                return f"{ws}: +{TOPUP_CREDITS * n:g} credits"
        if customer_id and customer_id != row.get("customer_id"):
            await conn.execute(accounts.update().where(accounts.c.workspace_id == ws).values(customer_id=customer_id, updated_at=now))
        return f"{ws}: {kind} noted"


async def webhook(raw: bytes, headers: dict[str, str]) -> str:
    """Verify (raw body, Standard Webhooks) and apply. Raises ValueError on a bad signature."""
    if not _secret("DODO_PAYMENTS_WEBHOOK_KEY", "studio-dodo-webhook-key"):
        raise NotSetUp("webhook")
    try:
        event = _client().webhooks.unwrap(raw.decode("utf-8"), headers=headers)
    except Exception as e:
        raise ValueError("invalid signature") from e
    data = event.data.model_dump(mode="json") if hasattr(event.data, "model_dump") else dict(event.data)
    if event.type.startswith("subscription.") and data.get("subscription_id"):
        # Webhooks arrive out of order: apply the subscription's current state, not the event's.
        current = await _client().subscriptions.retrieve(data["subscription_id"])
        data = {**data, **current.model_dump(mode="json")}
    return await apply_event(headers["webhook-id"], event.type, data)
