"""Deleting a request: stop its work, then remove everything it produced.

Removed: the conversation (ADK session), every file it delivered (all versions), its background
helpers' sessions and files, its jobs, tasks, job payloads, scheduled check-ins and pipeline memos.
Kept on purpose: the usage ledger (what was spent stays on the bill) and user-scoped files shared
across requests. Files inside the workspace sandbox are not tracked per request; the sandbox is
recreated within a day.
"""

from __future__ import annotations

import asyncio
import logging
import os

import sqlalchemy as sa

from app import jobs, tasks
from app.app_utils import services

log = logging.getLogger(__name__)
APP_NAME = "app"


async def _delete_files(arts, app_name: str, ws: str, sid: str) -> int:
    """Every session-scoped file of one session (user-scoped files are shared and stay)."""
    shared = set(await arts.list_artifact_keys(app_name=app_name, user_id=ws, session_id=None) or [])
    names = [n for n in await arts.list_artifact_keys(app_name=app_name, user_id=ws, session_id=sid) or []
             if n not in shared and not n.startswith("user:")]
    for name in names:
        await arts.delete_artifact(app_name=app_name, user_id=ws, session_id=sid, filename=name)
    return len(names)


def _delete_payload(job_id: str) -> None:
    try:
        if os.environ.get("STUDIO_BUCKET"):
            jobs._payload_blob(job_id).delete()
        else:
            jobs._local_payload(job_id).unlink(missing_ok=True)
    except Exception:  # already gone
        pass


async def delete_request(ws: str, sid: str) -> dict:
    """Stop and delete one request of workspace `ws`. Returns what was removed."""
    sessions, arts = services.get_session_service(), services.get_artifact_service()
    helper_rows = await tasks._select(tasks.tasks_table.c.workspace_id == ws,
                                      tasks.tasks_table.c.parent_session_id == sid)

    # 1. Stop work: the lead's job, scheduled check-ins, every helper / render / pipeline job.
    for job in [await jobs.active_for_session(ws, sid), *await jobs.scheduled_for_session(ws, sid)]:
        if job:
            await jobs.cancel(job["id"])
    for t in helper_rows:
        if t["status"] not in tasks.DONE:
            await tasks.update(t["id"], status="cancelled", notified=True)
        if t["job_id"]:
            await jobs.cancel(t["job_id"])

    # 2. Files and helper conversations.
    files = await _delete_files(arts, APP_NAME, ws, sid)
    for t in helper_rows:
        files += await _delete_files(arts, tasks.HELPERS_APP, ws, t["session_id"])
        try:
            await sessions.delete_session(app_name=tasks.HELPERS_APP, user_id=ws, session_id=t["session_id"])
        except Exception:
            log.warning("helper session %s already gone", t["session_id"])

    # 3. Records: job payloads and rows, task rows, pipeline memos.
    session_ids = [sid, *(t["session_id"] for t in helper_rows)]
    jt = jobs.jobs_table
    async with (await jobs._engine()).begin() as conn:
        job_ids = [r[0] for r in (await conn.execute(
            sa.select(jt.c.id).where(jt.c.workspace_id == ws, jt.c.session_id.in_(session_ids)))).all()]
        await conn.execute(jt.delete().where(jt.c.workspace_id == ws, jt.c.session_id.in_(session_ids)))
        await conn.execute(tasks.tasks_table.delete().where(tasks.tasks_table.c.workspace_id == ws,
                                                            tasks.tasks_table.c.parent_session_id == sid))
    await asyncio.gather(*(asyncio.to_thread(_delete_payload, j) for j in job_ids))
    pipelines = [t for t in helper_rows if t["effort"] == "pipeline"]
    if pipelines:
        from app.pipelines.host import SqlStore, kv_table

        store = SqlStore()
        for t in pipelines:
            link = await store.aget(("studio", "pipeline_task"), t["id"])
            if link:
                run = link.value["run_id"]
                async with (await jobs._engine()).begin() as conn:
                    await conn.execute(kv_table.delete().where(kv_table.c.namespace == f"harness/workflows/{run}"))
                await store.adelete(("harness", "workflow_runs"), run)
            await store.adelete(("studio", "pipeline_task"), t["id"])

    # 4. The conversation itself.
    await sessions.delete_session(app_name=APP_NAME, user_id=ws, session_id=sid)
    return {"deleted": sid, "files": files, "helpers": len(helper_rows), "jobs": len(job_ids)}
