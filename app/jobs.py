"""Background jobs: one agent turn that runs to completion without anyone watching.

Flow: the web app POSTs a message -> `create()` stores the job row (Cloud SQL / sqlite) and the
message payload (GCS / .data) -> `dispatch()` starts a worker:
  STUDIO_JOBS=cloudrun  one Cloud Run Job execution (`python -m app.worker <job_id>`), retried by
                        Cloud Run on failure; ADK resumability continues the same invocation.
  STUDIO_JOBS=inline    an asyncio task in this process (local dev and tests only).
Progress lives in the ADK session (the UI polls it); status lives in the jobs table.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import uuid
from pathlib import Path

import sqlalchemy as sa

from app import meter

log = logging.getLogger(__name__)

ACTIVE = ("queued", "running")
STALE_AFTER = dt.timedelta(hours=3)  # a "running" job with no heartbeat this long is treated as dead

jobs_table = sa.Table(
    "jobs", meter._meta,
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column("workspace_id", sa.String(128), index=True, nullable=False),
    sa.Column("session_id", sa.String(128), index=True, nullable=False),
    sa.Column("status", sa.String(16), nullable=False),  # scheduled | queued | running | succeeded | failed | cancelled
    sa.Column("invocation_id", sa.String(128)),
    sa.Column("attempts", sa.Integer, nullable=False, default=0),
    sa.Column("notify_email", sa.String(320)),
    sa.Column("error", sa.Text),
    sa.Column("execution", sa.String(512)),  # Cloud Run execution name, for operators
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
)


_table_ready = False


async def _engine():
    """The shared engine, with the jobs table created even if the meter connected first."""
    global _table_ready
    eng = await meter.engine()
    if not _table_ready:
        async with eng.begin() as conn:
            await conn.run_sync(lambda c: jobs_table.create(c, checkfirst=True))
        _table_ready = True
    return eng


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _row(r) -> dict:
    d = dict(r._mapping)
    for k in ("created_at", "updated_at", "finished_at"):
        if d.get(k) is not None:
            d[k] = d[k].isoformat()
    return d


# ---------------------------------------------------------------- payload storage

def _payload_blob(job_id: str):
    from google.cloud import storage

    return storage.Client().bucket(os.environ["STUDIO_BUCKET"]).blob(f"jobs/{job_id}.json")


def _local_payload(job_id: str) -> Path:
    return Path(os.environ.get("STUDIO_DATA_DIR", ".data")) / "jobs" / f"{job_id}.json"


def save_payload(job_id: str, message: dict) -> None:
    data = json.dumps(message)
    if os.environ.get("STUDIO_BUCKET"):
        _payload_blob(job_id).upload_from_string(data, content_type="application/json")
    else:
        p = _local_payload(job_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data)


def load_payload(job_id: str) -> dict:
    if os.environ.get("STUDIO_BUCKET"):
        return json.loads(_payload_blob(job_id).download_as_text())
    return json.loads(_local_payload(job_id).read_text())


# ---------------------------------------------------------------- the table

class Busy(Exception):
    """The session already has a job in flight."""


async def active_for_session(workspace_id: str, session_id: str) -> dict | None:
    t = jobs_table
    q = sa.select(t).where(t.c.workspace_id == workspace_id, t.c.session_id == session_id,
                           t.c.status.in_(ACTIVE)).order_by(t.c.created_at.desc()).limit(1)
    async with (await _engine()).connect() as conn:
        r = (await conn.execute(q)).first()
    if r is None:
        return None
    job = _row(r)
    if _now() - r.updated_at.replace(tzinfo=r.updated_at.tzinfo or dt.UTC) > STALE_AFTER:
        await finish(job["id"], "failed", error="stopped responding")
        return None
    return job


async def create(workspace_id: str, session_id: str, message: dict, notify_email: str | None,
                 status: str = "queued") -> dict:
    """Queue a job for this session. Raises Busy if one is already queued or running. A "scheduled"
    job (a check-in, app.wakeups) waits for its time and skips that check; it is checked when due."""
    if status != "scheduled" and await active_for_session(workspace_id, session_id):
        raise Busy(session_id)
    job_id = str(uuid.uuid4())
    await asyncio.to_thread(save_payload, job_id, message)
    now = _now()
    row = {"id": job_id, "workspace_id": workspace_id, "session_id": session_id, "status": status,
           "attempts": 0, "notify_email": notify_email, "created_at": now, "updated_at": now}
    async with (await _engine()).begin() as conn:
        await conn.execute(jobs_table.insert().values(**row))
    return await get(job_id)


async def get(job_id: str) -> dict | None:
    async with (await _engine()).connect() as conn:
        r = (await conn.execute(sa.select(jobs_table).where(jobs_table.c.id == job_id))).first()
    return _row(r) if r else None


async def scheduled_for_session(workspace_id: str, session_id: str) -> list[dict]:
    t = jobs_table
    q = sa.select(t).where(t.c.workspace_id == workspace_id, t.c.session_id == session_id, t.c.status == "scheduled")
    async with (await _engine()).connect() as conn:
        return [_row(r) for r in (await conn.execute(q)).all()]


async def latest_per_session(workspace_id: str, limit: int = 100) -> list[dict]:
    t = jobs_table
    q = (sa.select(t).where(t.c.workspace_id == workspace_id, t.c.status != "scheduled")
         .order_by(t.c.created_at.desc()).limit(limit))
    async with (await _engine()).connect() as conn:
        rows = [_row(r) for r in (await conn.execute(q)).all()]
    seen, out = set(), []
    for r in rows:
        if r["session_id"] not in seen:
            seen.add(r["session_id"])
            out.append(r)
    return out


async def latest_for_session(workspace_id: str, session_id: str) -> dict | None:
    t = jobs_table
    q = (sa.select(t).where(t.c.workspace_id == workspace_id, t.c.session_id == session_id, t.c.status != "scheduled")
         .order_by(t.c.created_at.desc()).limit(1))
    async with (await _engine()).connect() as conn:
        r = (await conn.execute(q)).first()
    return _row(r) if r else None


async def update(job_id: str, **values) -> None:
    async with (await _engine()).begin() as conn:
        await conn.execute(jobs_table.update().where(jobs_table.c.id == job_id).values(updated_at=_now(), **values))


async def finish(job_id: str, status: str, error: str | None = None) -> None:
    await update(job_id, status=status, error=error, finished_at=_now())


# ---------------------------------------------------------------- dispatch

_inline_tasks: dict[str, asyncio.Task] = {}


async def dispatch(job_id: str) -> None:
    """Start a worker for the job. Never waits for the job itself."""
    mode = os.environ.get("STUDIO_JOBS", "inline")
    if mode == "inline":
        from app import worker  # import here: worker imports the agent

        task = asyncio.create_task(worker.run_job(job_id, final_attempt=True))
        _inline_tasks[job_id] = task  # keep a reference so the task isn't garbage-collected
        task.add_done_callback(lambda _t: _inline_tasks.pop(job_id, None))
        return
    execution = await asyncio.to_thread(_run_cloud_job, job_id)
    await update(job_id, execution=execution)


async def cancel(job_id: str) -> None:
    """Stop a job: mark it cancelled, then stop its worker (best effort; a stopped worker that
    restarts sees the status and exits)."""
    job = await get(job_id)
    if job is None or job["status"] not in (*ACTIVE, "scheduled"):
        return
    await finish(job_id, "cancelled")
    if task := _inline_tasks.get(job_id):
        task.cancel()
    elif job.get("execution"):
        await asyncio.to_thread(_cancel_cloud_execution, job["execution"])


def _cancel_cloud_execution(execution: str) -> None:
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    try:
        AuthorizedSession(creds).post(f"https://run.googleapis.com/v2/{execution}:cancel", json={}, timeout=30)
    except Exception:
        log.exception("cancel %s failed", execution)


def _run_cloud_job(job_id: str) -> str:
    """Start one execution of the worker Cloud Run Job with this job id as its argument."""
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    creds, project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", project)
    region = os.environ.get("STUDIO_REGION", "asia-south1")
    name = os.environ.get("STUDIO_WORKER_JOB", "studio-worker")
    url = f"https://run.googleapis.com/v2/projects/{project}/locations/{region}/jobs/{name}:run"
    body = {"overrides": {"containerOverrides": [{"args": [job_id]}], "taskCount": 1}}
    r = AuthorizedSession(creds).post(url, json=body, timeout=30)
    r.raise_for_status()
    return r.json().get("metadata", {}).get("name", "")
