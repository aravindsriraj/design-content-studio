"""Background helpers: work the lead starts and does not wait for (DA middleware/background.py).

`start_task` creates a child ADK session (app "helpers") for one worker, a `tasks` row and an
ordinary job on that child session, then returns at once; the lead replies with an ETA and its turn
ends. The worker job is durable like any job (Cloud Run retries, ADK resumes the invocation). When
it ends, the report lands here and the lead is woken on its own session with a notice: a new lead
job, or, if the lead is mid-turn, the notice waits and joins its next turn (`pending_notices`).

Worker prompt and effort tiers are design-agent's (`subagents.py`); effort maps to Gemini
thinking levels.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid

import sqlalchemy as sa
from google.adk.tools import ToolContext

from app import jobs, meter

log = logging.getLogger(__name__)
HELPERS_APP = "helpers"
EFFORTS = ("low", "high", "xhigh", "max")  # plus "render" (app.renders) and "pipeline" (app.pipelines.host)
DONE = ("succeeded", "failed", "cancelled")
ACTIVE_JOB = ("queued", "running")

tasks_table = sa.Table(
    "tasks", meter._meta,
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column("workspace_id", sa.String(128), index=True, nullable=False),
    sa.Column("parent_session_id", sa.String(128), index=True, nullable=False),  # the lead's session
    sa.Column("session_id", sa.String(128), index=True, nullable=False),  # the helper's own session
    sa.Column("job_id", sa.String(36)),
    sa.Column("label", sa.String(120), nullable=False),
    sa.Column("effort", sa.String(16), nullable=False),
    sa.Column("status", sa.String(16), nullable=False),  # queued | running | succeeded | failed | cancelled
    sa.Column("progress", sa.Text),  # latest report_progress note
    sa.Column("report", sa.Text),  # the helper's final message
    sa.Column("notified", sa.Boolean, nullable=False, default=False),  # the lead has heard how it ended
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
)
_ready = False


async def _engine():
    global _ready
    eng = await jobs._engine()
    if not _ready:
        async with eng.begin() as conn:
            await conn.run_sync(lambda c: tasks_table.create(c, checkfirst=True))
        _ready = True
    return eng


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _row(r) -> dict:
    d = dict(r._mapping)
    for k in ("created_at", "updated_at", "finished_at"):
        if d.get(k) is not None:
            d[k] = d[k].isoformat()
    return d


async def _select(*where) -> list[dict]:
    q = sa.select(tasks_table).where(*where).order_by(tasks_table.c.created_at)
    async with (await _engine()).connect() as conn:
        return [_row(r) for r in (await conn.execute(q)).all()]


async def get(task_id: str) -> dict | None:
    rows = await _select(tasks_table.c.id == task_id)
    return rows[0] if rows else None


async def for_helper_session(session_id: str) -> dict | None:
    rows = await _select(tasks_table.c.session_id == session_id)
    return rows[0] if rows else None


async def for_parent(workspace_id: str, parent_session_id: str) -> list[dict]:
    t = tasks_table
    rows = await _select(t.c.workspace_id == workspace_id, t.c.parent_session_id == parent_session_id)
    if await _sweep(rows):
        rows = await _select(t.c.workspace_id == workspace_id, t.c.parent_session_id == parent_session_id)
    return rows


async def _sweep(rows: list[dict]) -> bool:
    """DA's housekeeping sweep, lazily: a task whose job stopped without reporting (Cloud Run killed
    it, or it went silent past jobs.STALE_AFTER) is marked interrupted, so the lead hears of it and
    can resume_task it. Returns whether anything changed."""
    changed = False
    for t in rows:
        if t["status"] in DONE or t["status"] == "waiting" or not t["job_id"]:
            continue
        job = await jobs.get(t["job_id"])
        if job is None:
            continue
        stale = job["status"] in ACTIVE_JOB and _now() - dt.datetime.fromisoformat(job["updated_at"]).replace(
            tzinfo=dt.UTC) > jobs.STALE_AFTER
        if job["status"] in ("failed", "cancelled") or stale:
            await finish(t["id"], "failed", "Interrupted: its job stopped without reporting. resume_task continues it.")
            changed = True
    if changed:  # the lead hears now if it is idle, or with its next turn
        await wake_parent(rows[0]["workspace_id"], rows[0]["parent_session_id"], None)
    return changed


async def running(workspace_id: str, parent_session_id: str) -> list[dict]:
    return [t for t in await for_parent(workspace_id, parent_session_id) if t["status"] not in DONE]


async def update(task_id: str, **values) -> None:
    async with (await _engine()).begin() as conn:
        await conn.execute(tasks_table.update().where(tasks_table.c.id == task_id).values(updated_at=_now(), **values))


async def start(workspace_id: str, parent_session_id: str, brief: str, effort: str, label: str,
                session_service, notify_email: str | None = None, *, dispatch: bool = True,
                notified: bool = False) -> dict:
    """Create the helper's session, its task row and its job, and dispatch it."""
    child = await session_service.create_session(
        app_name=HELPERS_APP, user_id=workspace_id,
        state={"effort": effort, "parent_session_id": parent_session_id, "title": label})
    task_id = str(uuid.uuid4())
    now = _now()
    async with (await _engine()).begin() as conn:
        await conn.execute(tasks_table.insert().values(
            id=task_id, workspace_id=workspace_id, parent_session_id=parent_session_id, session_id=child.id,
            label=label[:120], effort=effort, status="queued", notified=notified, created_at=now, updated_at=now))
    job = await jobs.create(workspace_id, child.id, {"role": "user", "parts": [{"text": brief}]}, notify_email)
    await update(task_id, job_id=job["id"])
    if dispatch:
        await jobs.dispatch(job["id"])
    return await get(task_id)


async def finish(task_id: str, status: str, report: str) -> None:
    await update(task_id, status=status, report=report[:20000], finished_at=_now())


def notice(t: dict) -> str:
    """How a finished helper reaches the lead (DA harness/notices.py, in one block)."""
    how = {"succeeded": "finished", "failed": "failed", "cancelled": "was cancelled"}[t["status"]]
    return (f"[Background notice, not from the person] Task {t['id'][:8]} \"{t['label']}\" ({t['effort']}) {how}.\n"
            f"Its report:\n{(t.get('report') or '(no report)').strip()}")


async def pending_notices(workspace_id: str, parent_session_id: str) -> list[dict]:
    return [t for t in await for_parent(workspace_id, parent_session_id) if t["status"] in DONE and not t["notified"]]


async def wake_parent(workspace_id: str, parent_session_id: str, notify_email: str | None) -> bool:
    """Start a lead turn on the parent session carrying every un-heard ending. False if the lead is
    busy (its next turn picks the notices up) or nothing is pending."""
    pending = await pending_notices(workspace_id, parent_session_id)
    if not pending or await jobs.active_for_session(workspace_id, parent_session_id):
        return False
    if notify_email is None and (last := await jobs.latest_for_session(workspace_id, parent_session_id)):
        notify_email = last.get("notify_email")  # helper jobs carry none; the person's request does
    text = "\n\n".join(notice(t) for t in pending)
    try:
        job = await jobs.create(workspace_id, parent_session_id, {"role": "user", "parts": [{"text": text}]}, notify_email)
    except jobs.Busy:
        return False
    for t in pending:
        await update(t["id"], notified=True)
    await jobs.dispatch(job["id"])
    return True


# ---------------------------------------------------------------- tools


async def report_progress(note: str, tool_context: ToolContext, urgent: bool = False) -> str:
    """Tell the lead how this task is going.

    Send one when a section is done, when something blocks you or moves your ETA, and before you
    stop early: one or two sentences on what happened and what comes next, with a time. It reaches
    the lead with its next turn and shows in the person's progress view. Set `urgent` only when the
    lead must act now: you are blocked on a decision, or the plan no longer works.

    Args:
      note: One or two sentences.
      urgent: True only when the lead must act now.
    """
    t = await for_helper_session(tool_context.session.id)
    if t is None:
        return "Noted (this is not a background task, so nobody else will see it)."
    await update(t["id"], progress=("[urgent] " if urgent else "") + note[:2000])
    if urgent and not await jobs.active_for_session(t["workspace_id"], t["parent_session_id"]):
        last = await jobs.latest_for_session(t["workspace_id"], t["parent_session_id"])
        try:  # the lead is idle: wake it now, as design-agent does for urgent notes
            job = await jobs.create(t["workspace_id"], t["parent_session_id"], {"role": "user", "parts": [{"text": (
                f"[Background notice, not from the person] Task {t['id'][:8]} \"{t['label']}\" needs you now: {note}")}]},
                last.get("notify_email") if last else None)
            await jobs.dispatch(job["id"])
        except jobs.Busy:
            pass
    return "Sent."


async def fold_notices(callback_context) -> None:
    """Lead before_agent: endings that arrived while the lead was busy join this turn (DA inbox)."""
    ws, sid = callback_context.user_id, callback_context.session.id
    pending = await pending_notices(ws, sid)
    still = await running(ws, sid)
    lines = [notice(t) for t in pending]
    if still:
        lines.append("Still running: " + "; ".join(f"{t['id'][:8]} \"{t['label']}\" ({t['status']})" for t in still))
    callback_context.state["tasks_note"] = ("\n<background>\n" + "\n\n".join(lines) + "\n</background>") if lines else ""
    for t in pending:
        await update(t["id"], notified=True)
