"""The studio server: ADK's API (sessions, /run_sse, artifacts) + /api routes + the web app.

Auth (STUDIO_AUTH):
  off       local dev, evals and the playground. Every request acts as X-Dev-User (default "dev").
  firebase  production. Requests need `Authorization: Bearer <Firebase ID token>`; the token's uid is
            the workspace id and must equal the ADK user_id in the path or body. Only the routes the
            web app uses are reachable; ADK's dev, eval and builder endpoints answer 404.
Cloud Run (K_SERVICE set) refuses to start with auth off.
"""

import asyncio
import datetime as dt
import json
import logging
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from google.adk.cli.fast_api import get_fast_api_app

from app import billing, jobs, meter
from app.app_utils import services
from app.brand import store as brand_store
from app.brand.schema import BrandPackError

load_dotenv()
AUTH = os.environ.get("STUDIO_AUTH", "off")
if AUTH not in ("off", "firebase"):
    raise RuntimeError(f"STUDIO_AUTH must be 'off' or 'firebase', not {AUTH!r}")
if os.environ.get("K_SERVICE") and AUTH != "firebase":
    raise RuntimeError("refusing to serve on Cloud Run without STUDIO_AUTH=firebase")

log = logging.getLogger(__name__)
APP_NAME = "app"
AGENT_DIR = str(Path(__file__).resolve().parent.parent)
WEB_DIST = Path(AGENT_DIR) / "web" / "dist"
MAX_BODY = 25 * 1024 * 1024  # attachments ride inline in /run_sse bodies
_USER_PATH = re.compile(rf"^/apps/{APP_NAME}/users/([^/]+)(?:/|$)")
_PUBLIC = re.compile(r"^/(?:$|assets/|favicon|index\.html$|api/config$|health$)")
_ALLOWED = re.compile(rf"^/(?:api/|run_sse$|run$|apps/{APP_NAME}/users/)")

app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=AUTH == "off",  # ADK dev UI at /dev-ui only when auth is off
    artifact_service_uri=services.ARTIFACT_SERVICE_URI,
    session_service_uri=services.SESSION_SERVICE_URI,
    allow_origins=os.getenv("ALLOW_ORIGINS", "").split(",") if os.getenv("ALLOW_ORIGINS") else None,
    otel_to_cloud=bool(os.environ.get("K_SERVICE")),
)
app.title = "studio"


def _verify_firebase(token: str) -> dict:
    import firebase_admin
    from firebase_admin import auth

    if not firebase_admin._apps:
        firebase_admin.initialize_app(options={"projectId": os.environ.get("FIREBASE_PROJECT_ID") or os.environ["GOOGLE_CLOUD_PROJECT"]})
    return auth.verify_id_token(token)  # claims: uid, email, email_verified, ...


def _deny(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


@app.middleware("http")
async def workspace_auth(request: Request, call_next):
    path = request.url.path
    if AUTH == "off":
        request.state.uid = request.headers.get("x-dev-user", "dev")
        request.state.email = os.environ.get("STUDIO_DEV_EMAIL")
        return await call_next(request)
    if _PUBLIC.match(path) and request.method == "GET":
        return await call_next(request)
    if path == "/api/billing/webhook" and request.method == "POST":
        return await call_next(request)  # Dodo signs it; the route verifies the signature
    if not _ALLOWED.match(path):
        return _deny(404, "Not Found")
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not token:
        return _deny(401, "sign in required")
    try:
        claims = await asyncio.to_thread(_verify_firebase, token)
    except Exception:
        return _deny(401, "invalid or expired sign-in")
    # Email/password accounts must verify their address first (Google accounts arrive verified):
    # every request spends model time, so throwaway addresses don't get in.
    if (claims.get("firebase") or {}).get("sign_in_provider") == "password" and not claims.get("email_verified"):
        return _deny(403, "verify your email address to continue")
    uid = claims["uid"]
    request.state.uid = uid
    request.state.email = claims.get("email") if claims.get("email_verified") else None
    if (m := _USER_PATH.match(path)) and m.group(1) != uid:
        return _deny(403, "not your workspace")
    if path in ("/run_sse", "/run"):
        body = await request.body()
        if len(body) > MAX_BODY:
            return _deny(413, "message too large")
        try:
            payload = json.loads(body)
        except ValueError:
            return _deny(400, "invalid JSON")
        if payload.get("user_id") != uid or payload.get("app_name") != APP_NAME:
            return _deny(403, "not your workspace")
        # Session state (delivery record, cost total) and serving tier are server-owned.
        if payload.get("state_delta") or payload.get("service_tier"):
            return _deny(400, "state_delta and service_tier are not accepted")
    elif request.method == "DELETE" and _USER_PATH.match(path):
        # ADK's own delete would drop the conversation and leave its files and jobs behind.
        return _deny(405, "delete a request with DELETE /api/requests/{session_id}")
    elif request.method == "POST" and _USER_PATH.match(path) and path.rstrip("/").endswith("/sessions"):
        body = await request.body()
        if body and (json.loads(body or b"{}") or {}).get("state"):
            return _deny(400, "initial session state is not accepted")
    return await call_next(request)


def uid(request: Request) -> str:
    return request.state.uid


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.get("/api/config")
async def config() -> dict:
    """What the web app needs before sign-in. Firebase web config values are public by design."""
    return {
        "auth": AUTH,
        "app_name": APP_NAME,
        "pricing": billing.PRICING,
        "firebase": {
            "apiKey": os.environ.get("FIREBASE_API_KEY", ""),
            "authDomain": os.environ.get("FIREBASE_AUTH_DOMAIN", ""),
            "projectId": os.environ.get("FIREBASE_PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT", ""),
            "appId": os.environ.get("FIREBASE_APP_ID", ""),
        } if AUTH == "firebase" else None,
    }


@app.get("/api/me")
async def me(request: Request, ws: str = Depends(uid)) -> dict:
    return {"workspace_id": ws, "email": request.state.email}


@app.get("/api/brand")
async def get_brand(ws: str = Depends(uid)) -> dict:
    try:
        return brand_store.summary(await asyncio.to_thread(brand_store.load, ws))
    except BrandPackError as e:
        return {"error": "stored brand pack is invalid", "problems": e.problems}


@app.get("/api/brand/draft")
async def get_brand_draft(ws: str = Depends(uid)) -> dict:
    """The draft pack brand onboarding built, waiting for review ({} when there is none)."""
    try:
        return await asyncio.to_thread(brand_store.draft_summary, ws) or {}
    except BrandPackError as e:
        return {"error": "draft brand pack is invalid", "problems": e.problems}


@app.post("/api/brand/draft/activate")
async def activate_brand_draft(ws: str = Depends(uid)) -> dict:
    try:
        pack = await asyncio.to_thread(brand_store.activate_draft, ws)
    except BrandPackError as e:
        raise HTTPException(422, {"error": "draft could not be activated; your current brand is unchanged", "problems": e.problems}) from None
    return brand_store.summary(pack)


@app.delete("/api/brand/draft")
async def discard_brand_draft(ws: str = Depends(uid)) -> dict:
    await asyncio.to_thread(brand_store.discard_draft, ws)
    return {}


@app.put("/api/brand")
async def put_brand(file: UploadFile, ws: str = Depends(uid)) -> dict:
    data = await file.read(brand_store.MAX_ZIP_BYTES + 1)
    try:
        pack = await asyncio.to_thread(brand_store.save, ws, data)
    except BrandPackError as e:
        raise HTTPException(422, {"error": "brand pack is invalid; your current brand is unchanged", "problems": e.problems}) from None
    return brand_store.summary(pack)


@app.post("/api/jobs", status_code=202)
async def start_job(request: Request, ws: str = Depends(uid)) -> dict:
    """Queue one agent turn to run in the background. Body: {"session_id"?: str, "message": Content}."""
    body = await request.body()
    if len(body) > MAX_BODY:
        raise HTTPException(413, "message too large")
    try:
        payload = json.loads(body)
        message = payload["message"]
        if message.get("role") != "user" or not message.get("parts"):
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(400, 'send {"message": {"role": "user", "parts": [...]}}') from None
    ok, why = await billing.can_start(ws, request.state.email)
    if not ok:
        raise HTTPException(402, why)
    sessions = services.get_session_service()
    session_id = payload.get("session_id")
    if session_id:
        if await sessions.get_session(app_name=APP_NAME, user_id=ws, session_id=session_id) is None:
            raise HTTPException(404, "no such request")
    else:
        session_id = (await sessions.create_session(app_name=APP_NAME, user_id=ws)).id
    try:
        job = await jobs.create(ws, session_id, message, notify_email=request.state.email)
    except jobs.Busy:
        raise HTTPException(409, "this request is still working; wait for it to finish") from None
    try:
        await jobs.dispatch(job["id"])
    except Exception as e:
        await jobs.finish(job["id"], "failed", error=f"could not start: {type(e).__name__}")
        raise HTTPException(503, "could not start the job; try again") from None
    return {"job": _public_job(await jobs.get(job["id"])), "session_id": session_id}


def _app_url(request: Request) -> str:
    return os.environ.get("APP_URL") or str(request.base_url)


@app.get("/api/billing")
async def get_billing(request: Request, ws: str = Depends(uid)) -> dict:
    """This workspace's plan, credits and subscription."""
    return await billing.status(ws, request.state.email)


@app.post("/api/billing/checkout")
async def billing_checkout(request: Request, ws: str = Depends(uid)) -> dict:
    """Body: {"item": "pro_monthly" | "pro_annual" | "topup"} -> {"url": Dodo checkout}."""
    item = (await request.json() or {}).get("item", "")
    if item not in billing.ITEMS:
        raise HTTPException(400, "unknown item")
    try:
        return {"url": await billing.checkout(ws, request.state.email, item, _app_url(request))}
    except billing.NotSetUp:
        raise HTTPException(503, "Payments aren't set up yet.") from None
    except ValueError as e:
        raise HTTPException(409, str(e)) from None


@app.post("/api/billing/portal")
async def billing_portal(request: Request, ws: str = Depends(uid)) -> dict:
    """-> {"url": Dodo's customer portal} for invoices, plan changes and cancelling."""
    try:
        return {"url": await billing.portal(ws, _app_url(request))}
    except billing.NotSetUp:
        raise HTTPException(503, "Payments aren't set up yet.") from None
    except ValueError as e:
        raise HTTPException(409, str(e)) from None


@app.post("/api/billing/webhook")
async def billing_webhook(request: Request) -> dict:
    """Dodo Payments webhooks: verified on the raw body, applied once per webhook-id."""
    headers = {k: request.headers.get(k, "") for k in ("webhook-id", "webhook-signature", "webhook-timestamp")}
    try:
        result = await billing.webhook(await request.body(), headers)
    except billing.NotSetUp:
        raise HTTPException(503, "billing not set up") from None
    except ValueError:
        raise HTTPException(401, "invalid signature") from None
    log.info("billing webhook %s: %s", headers["webhook-id"], result)
    return {"received": True}


@app.delete("/api/requests/{session_id}")
async def delete_request(session_id: str, ws: str = Depends(uid)) -> dict:
    """Stop a request's work and delete it with every file it produced (the usage ledger stays)."""
    from app import cleanup

    if await services.get_session_service().get_session(app_name=APP_NAME, user_id=ws, session_id=session_id) is None:
        raise HTTPException(404, "no such request")
    return await cleanup.delete_request(ws, session_id)


@app.get("/api/jobs")
async def list_jobs(ws: str = Depends(uid)) -> list[dict]:
    """The latest job of each of this workspace's requests (for the sidebar and polling)."""
    return [_public_job(j) for j in await jobs.latest_per_session(ws)]


@app.get("/api/tasks")
async def list_tasks(session_id: str, ws: str = Depends(uid)) -> list[dict]:
    """Background helpers the lead started for one request (scoped to this workspace)."""
    from app import meter, tasks

    rows = await tasks.for_parent(ws, session_id)
    costs = await meter.session_costs(ws, [t["session_id"] for t in rows])
    return [{**{k: t[k] for k in ("id", "label", "effort", "status", "progress", "created_at", "finished_at")},
             "cost_usd": costs.get(t["session_id"], 0.0)} for t in rows]


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str, ws: str = Depends(uid)) -> dict:
    job = await jobs.get(job_id)
    if job is None or job["workspace_id"] != ws:
        raise HTTPException(404, "no such job")
    return _public_job(job)


def _public_job(job: dict) -> dict:
    keys = ("id", "session_id", "status", "attempts", "error", "created_at", "updated_at", "finished_at")
    return {k: job.get(k) for k in keys}


@app.get("/api/usage")
async def usage(ws: str = Depends(uid)) -> dict:
    month_start = dt.datetime.now(dt.UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return {"this_month": await meter.workspace_spend(ws, since=month_start), "all_time": await meter.workspace_spend(ws)}


if WEB_DIST.is_dir():  # last, so API routes win
    app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
