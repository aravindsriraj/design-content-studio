"""Plans and credits: ledger-derived balances, period rollover, Dodo webhooks, and the agent gates."""

import asyncio
import datetime as dt
from types import SimpleNamespace

import pytest

from app import billing, meter


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch, tmp_path):
    monkeypatch.setenv("STUDIO_DB_URL", f"sqlite+aiosqlite:///{tmp_path}/t.db")
    monkeypatch.setattr(meter, "_engine", None)
    monkeypatch.setattr(billing, "_ready", False)
    monkeypatch.setenv("STUDIO_OWNER_EMAILS", "owner@example.com")
    monkeypatch.setenv("DODO_PRODUCT_TOPUP", "pdt_topup")


def spend(ws, usd, model="gemini-3.8-flash", at=None):
    async def go():
        async with (await billing._engine()).begin() as conn:
            await conn.execute(meter.usage_table.insert().values(
                workspace_id=ws, session_id="s", role="lead", model=model, in_tok=0, cached_tok=0, out_tok=0,
                cost_usd=usd, at=at or dt.datetime.now(dt.UTC)))
    asyncio.run(go())


def test_standard_factor_reprices_introductory_rates():
    assert billing.standard_factor("gemini-3.8-flash", dt.date(2026, 11, 18)) == 2.0
    assert billing.standard_factor("gemini-3.8-flash", dt.date(2027, 2, 1)) == 1.0
    assert billing.standard_factor("lyria-3.5", dt.date(2026, 11, 18)) == 1.0


def test_free_balance_comes_from_the_ledger_and_blocks_at_zero():
    s = asyncio.run(billing.status("w1", "a@example.com"))
    assert s["plan"] == "free" and s["balance"] == 3.0
    spend("w1", 0.45)  # a typical post today: 0.45 * 2 (standard rate) / 0.90 = 1 credit
    assert asyncio.run(billing.status("w1"))["balance"] == 2.0
    spend("w1", 0.90)
    ok, why = asyncio.run(billing.can_start("w1"))
    assert not ok and "free credits" in why


def test_owner_is_pro_without_limits():
    spend("w2", 50)
    s = asyncio.run(billing.status("w2", "Owner@Example.com"))
    assert s["plan"] == "pro" and s["owner"] and s["balance"] is None
    assert asyncio.run(billing.can_start("w2"))[0]


def test_period_rolls_and_overuse_comes_off_topups():
    async def go():
        await billing.ensure("w3", "b@example.com")
        old = dt.datetime.now(dt.UTC) - dt.timedelta(days=31)
        async with (await billing._engine()).begin() as conn:
            await conn.execute(billing.accounts.update().where(billing.accounts.c.workspace_id == "w3")
                               .values(period_start=old, topup_credits=10.0))
        return old
    old = asyncio.run(go())
    spend("w3", 2.25, at=old + dt.timedelta(days=1))  # 5 credits in the old period: 2 over the free 3
    s = asyncio.run(billing.status("w3"))
    assert s["topup"] == 8.0 and s["used"] == 0.0 and s["balance"] == 11.0


def test_webhooks_upgrade_cancel_topup_and_dedupe():
    future = (dt.datetime.now(dt.UTC) + dt.timedelta(days=20)).isoformat()
    sub = {"subscription_id": "sub_1", "product_id": "pdt_pro", "status": "active", "metadata": {"workspace_id": "w4"},
           "customer": {"customer_id": "cus_1", "email": "c@example.com"}, "next_billing_date": future}
    assert "active" in asyncio.run(billing.apply_event("wh1", "subscription.active", sub))
    assert asyncio.run(billing.apply_event("wh1", "subscription.active", sub)) == "duplicate"
    s = asyncio.run(billing.status("w4"))
    assert s["plan"] == "pro" and s["balance"] == 20.0 and s["can_manage"]

    pay = {"payment_id": "pay_1", "customer": {"customer_id": "cus_1"}, "product_cart": [{"product_id": "pdt_topup", "quantity": 1}]}
    assert "+10" in asyncio.run(billing.apply_event("wh2", "payment.succeeded", pay))
    assert asyncio.run(billing.status("w4"))["balance"] == 30.0
    renewal_payment = {**pay, "subscription_id": "sub_1"}
    asyncio.run(billing.apply_event("wh3", "payment.succeeded", renewal_payment))
    assert asyncio.run(billing.status("w4"))["balance"] == 30.0  # subscription payments add nothing

    # Cancelled at period end: Pro until the paid-up date (matched by customer, no metadata).
    cancelled = {**sub, "status": "cancelled", "metadata": {}}
    asyncio.run(billing.apply_event("wh4", "subscription.cancelled", cancelled))
    assert asyncio.run(billing.status("w4"))["plan"] == "pro"
    on_hold = {**sub, "status": "on_hold", "next_billing_date": None}
    asyncio.run(billing.apply_event("wh5", "subscription.on_hold", on_hold))
    assert asyncio.run(billing.status("w4"))["plan"] == "free"


def test_gates_keep_pro_tools_from_free_and_stop_overrun():
    ctx = SimpleNamespace(user_id="w5", state={})
    asyncio.run(billing.ensure("w5", "d@example.com"))
    render = SimpleNamespace(name="start_render_job")
    assert asyncio.run(billing.plan_gate(render, {}, ctx))["status"] == "error"
    bash = SimpleNamespace(name="bash")
    assert asyncio.run(billing.plan_gate(bash, {"command": "npx hyperframes render ."}, ctx))
    assert asyncio.run(billing.plan_gate(bash, {"command": "node html2png.mjs"}, ctx)) is None
    asyncio.run(billing.plan_note(ctx))
    assert "Free plan" in ctx.state["plan_note"]

    assert asyncio.run(billing.credit_gate(ctx, None)) is None
    spend("w5", 2.0)  # 4.4 credits used of 3: past the 1-credit overrun
    stopped = asyncio.run(billing.credit_gate(ctx, None))
    assert stopped and "out of credits" in stopped.content.parts[0].text


def test_webhook_verifies_the_raw_body(monkeypatch):
    import base64
    import json
    import time

    from standardwebhooks.webhooks import Webhook

    secret = "whsec_" + base64.b64encode(b"test-secret-for-studio-billing!!").decode()
    monkeypatch.setenv("DODO_PAYMENTS_WEBHOOK_KEY", secret)
    monkeypatch.setenv("DODO_PAYMENTS_API_KEY", "dodo_test_x")
    billing._found.clear()
    billing._missing.clear()
    billing._clients.clear()
    asyncio.run(billing.ensure("w6", "e@example.com"))
    body = json.dumps({"business_id": "bus_1", "type": "payment.succeeded", "timestamp": "2026-11-18T10:00:00Z", "data": {
        "payload_type": "Payment", "payment_id": "pay_9", "business_id": "bus_1", "brand_id": "b", "status": "succeeded",
        "total_amount": 2000, "currency": "USD", "created_at": "2026-11-18T10:00:00Z", "digital_products_delivered": False,
        "disputes": [], "refunds": [], "settlement_amount": 2000, "settlement_currency": "USD", "metadata": {"workspace_id": "w6"},
        "billing": {"country": "IN"}, "customer": {"customer_id": "cus_9", "email": "e@example.com", "name": "E"},
        "product_cart": [{"product_id": "pdt_topup", "quantity": 1}]}})
    msg_id, now = "msg_1", dt.datetime.fromtimestamp(time.time(), dt.UTC)
    sig = Webhook(secret).sign(msg_id, now, body)
    headers = {"webhook-id": msg_id, "webhook-timestamp": str(int(now.timestamp())), "webhook-signature": sig}
    try:
        assert "+10" in asyncio.run(billing.webhook(body.encode(), headers))
        with pytest.raises(ValueError):
            asyncio.run(billing.webhook(body.replace("2000", "1").encode(), {**headers, "webhook-id": "msg_2"}))
    finally:
        billing._found.clear()
        billing._missing.clear()
        billing._clients.clear()
