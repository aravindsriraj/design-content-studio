"""Usage meter: price every model call, keep a running total in session state, append to the ledger.

The ledger is one table in STUDIO_DB_URL (sqlite locally, Cloud SQL Postgres in production).
Session state carries the per-conversation total (`usage`), which also lands in eval traces.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from pathlib import Path

import sqlalchemy as sa
from google.adk.agents.callback_context import CallbackContext
from google.adk.models import LlmResponse
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

log = logging.getLogger(__name__)

# USD per 1M tokens: (input, cached input, output incl. thinking), by the date a price takes effect.
# Source: ai.google.dev/gemini-api/docs/latest-model (Gemini 4 Argon), checked 2026-11-17.
# gemini-4-hydrogen has no published price; Argon's rates stand in until it does (UNVERIFIED).
# gemini-3.8-flash: ai.google.dev/gemini-api/docs/pricing#gemini-3.8-flash, checked 2026-11-17.
_ARGON = [(dt.date(2026, 1, 1), (2.00, 0.10, 10.00)), (dt.date(2026, 12, 1), (4.00, 0.20, 20.00))]
PRICES: dict[str, list[tuple[dt.date, tuple[float, float, float]]]] = {
    "gemini-4-argon": _ARGON,
    "gemini-4-hydrogen": _ARGON,
    "gemini-3.8-flash": [(dt.date(2026, 1, 1), (0.75, 0.075, 3.75)), (dt.date(2027, 1, 1), (1.50, 0.15, 7.50))],
    # ai.google.dev/gemini-api/docs/pricing#gemini-nano-banana-2.1, checked 2026-11-17. No caching.
    "gemini-nano-banana-2.1": [(dt.date(2026, 1, 1), (1.50, 1.50, 7.50))],
    # ai.google.dev/gemini-api/docs/pricing#gemini-3.8-flash-tts, checked 2026-11-17: $9/1M audio out
    # (25 tokens per second of audio), doubling on 2027-01-01.
    "gemini-3.8-flash-tts": [(dt.date(2026, 1, 1), (0.50, 0.50, 9.00)), (dt.date(2027, 1, 1), (1.00, 1.00, 18.00))],
    # ai.google.dev/gemini-api/docs/pricing#gemini-3.5-transcribe, checked 2026-11-18: $2/1M audio in
    # (25 tokens a second), $12/1M text out; about $0.005 a minute.
    "gemini-3.5-transcribe": [(dt.date(2026, 1, 1), (2.00, 2.00, 12.00))],
}
# Per call, not per token. Lyria's price is not on the pricing page yet: UNVERIFIED stand-ins.
FIXED = {"lyria-3-clip-preview": 0.06, "lyria-3.5": 0.12}
# Image models bill generated images (candidates) at their own rate; thinking stays at the text rate.
IMAGE_OUT = {"gemini-nano-banana-2.1": 30.00}  # $0.0336 per 1K image (1120 tokens)


def cost_usd(model: str, usage, on: dt.date | None = None) -> float:
    """Price one call's usage_metadata. Unknown models cost 0 and log once per call."""
    tiers = PRICES.get(model)
    if not tiers or usage is None:
        if usage is not None:
            log.warning("no price for model %s; recording cost 0", model)
        return 0.0
    on = on or dt.date.today()
    rate_in, rate_cached, rate_out = [r for start, r in tiers if start <= on][-1]
    prompt = usage.prompt_token_count or 0
    cached = usage.cached_content_token_count or 0
    candidates, thoughts = usage.candidates_token_count or 0, usage.thoughts_token_count or 0
    out = candidates * IMAGE_OUT.get(model, rate_out) + thoughts * rate_out
    return ((prompt - cached) * rate_in + cached * rate_cached + out) / 1_000_000


# ---------------------------------------------------------------- ledger

_meta = sa.MetaData()
usage_table = sa.Table(
    "usage", _meta,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("workspace_id", sa.String(128), index=True, nullable=False),
    sa.Column("session_id", sa.String(128), index=True),
    sa.Column("role", sa.String(16), nullable=False),  # lead | helper | look | image
    sa.Column("model", sa.String(64), nullable=False),
    sa.Column("in_tok", sa.Integer, nullable=False),
    sa.Column("cached_tok", sa.Integer, nullable=False),
    sa.Column("out_tok", sa.Integer, nullable=False),
    sa.Column("cost_usd", sa.Float, nullable=False),
    sa.Column("at", sa.DateTime(timezone=True), nullable=False, index=True),
)
_engine: AsyncEngine | None = None


async def engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        url = os.environ.get("STUDIO_DB_URL", "sqlite+aiosqlite:///.data/studio.db")
        if url.startswith("sqlite") and ":///" in url:
            Path(url.split(":///", 1)[1]).parent.mkdir(parents=True, exist_ok=True)
        _engine = create_async_engine(url, pool_pre_ping=True)
        async with _engine.begin() as conn:
            await conn.run_sync(_meta.create_all)
    return _engine


async def record(ctx, *, role: str, model: str, usage) -> None:
    """Add one call to the conversation total (ctx.state) and the ledger. Never raises."""
    if usage is None:
        return
    cost = cost_usd(model, usage)
    row = {
        "in_tok": usage.prompt_token_count or 0,
        "cached_tok": usage.cached_content_token_count or 0,
        "out_tok": (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0),
    }
    total = dict(ctx.state.get("usage") or {"calls": 0, "in_tok": 0, "cached_tok": 0, "out_tok": 0, "cost_usd": 0.0})
    total["calls"] += 1
    for k, v in row.items():
        total[k] += v
    total["cost_usd"] = round(total["cost_usd"] + cost, 6)
    ctx.state["usage"] = total
    try:
        async with (await engine()).begin() as conn:
            await conn.execute(usage_table.insert().values(
                workspace_id=ctx.user_id, session_id=ctx.session.id, role=role, model=model,
                cost_usd=cost, at=dt.datetime.now(dt.UTC), **row,
            ))
    except Exception:  # metering must never break a turn; the state total still holds
        log.exception("ledger write failed")


async def record_fixed(ctx, *, role: str, model: str) -> None:
    """A per-call priced model (Lyria) into the conversation total and the ledger. Never raises."""
    cost = FIXED.get(model, 0.0)
    total = dict(ctx.state.get("usage") or {"calls": 0, "in_tok": 0, "cached_tok": 0, "out_tok": 0, "cost_usd": 0.0})
    total["calls"] += 1
    total["cost_usd"] = round(total["cost_usd"] + cost, 6)
    ctx.state["usage"] = total
    try:
        async with (await engine()).begin() as conn:
            await conn.execute(usage_table.insert().values(
                workspace_id=ctx.user_id, session_id=ctx.session.id, role=role, model=model, in_tok=0, cached_tok=0,
                out_tok=0, cost_usd=cost, at=dt.datetime.now(dt.UTC)))
    except Exception:
        log.exception("ledger write failed")


async def meter_lead(callback_context: CallbackContext, llm_response: LlmResponse) -> None:
    """after_model_callback for the lead agent and its helpers (role = the agent's name)."""
    if llm_response.partial:  # streamed chunks repeat usage; count the final one only
        return
    role = "lead" if callback_context.agent_name == "studio" else "helper"
    await record(callback_context, role=role, model=os.environ.get("STUDIO_MODEL", "gemini-3.8-flash"),
                 usage=llm_response.usage_metadata)


async def session_costs(workspace_id: str, session_ids: list[str]) -> dict[str, float]:
    """Ledger cost per session (helpers run in their own sessions)."""
    if not session_ids:
        return {}
    t = usage_table
    q = (sa.select(t.c.session_id, sa.func.coalesce(sa.func.sum(t.c.cost_usd), 0.0))
         .where(t.c.workspace_id == workspace_id, t.c.session_id.in_(session_ids)).group_by(t.c.session_id))
    async with (await engine()).connect() as conn:
        return {sid: round(float(c), 4) for sid, c in (await conn.execute(q)).all()}


async def workspace_spend(workspace_id: str, since: dt.datetime | None = None) -> dict:
    """Totals for the usage page."""
    t = usage_table
    q = sa.select(sa.func.count(), sa.func.coalesce(sa.func.sum(t.c.cost_usd), 0.0),
                  sa.func.coalesce(sa.func.sum(t.c.in_tok + t.c.out_tok), 0)).where(t.c.workspace_id == workspace_id)
    if since:
        q = q.where(t.c.at >= since)
    async with (await engine()).connect() as conn:
        calls, cost, tokens = (await conn.execute(q)).one()
    return {"calls": calls, "cost_usd": round(float(cost), 4), "tokens": int(tokens)}
