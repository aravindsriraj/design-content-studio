"""Run one background job to completion: `python -m app.worker <job_id>` (a Cloud Run Job task).

Durability:
- The agent's progress is the ADK session (Cloud SQL): every event is saved as it happens.
- If a task dies, Cloud Run retries it. The retry finds the job's invocation_id and resumes that
  invocation (App has ResumabilityConfig), so completed tool calls are not redone; tools may run
  at least once, and `deliver` already skips files it has sent.
- Only the final attempt marks the job failed and emails; earlier failures put it back to queued.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time

from google.adk.runners import Runner
from google.genai import types

from app import jobs, notify, tasks
from app.app_utils import services

log = logging.getLogger("studio.worker")
HEARTBEAT_S = 30


def _attempt() -> int:
    return int(os.environ.get("CLOUD_RUN_TASK_ATTEMPT", "0"))


def _is_final_attempt() -> bool:
    return _attempt() >= int(os.environ.get("STUDIO_WORKER_MAX_RETRIES", "2"))


def make_runner(helper: bool = False) -> Runner:
    if helper:
        from app.helpers import helpers_app as adk_app
    else:
        from app.agent import app as adk_app

    return Runner(app=adk_app, session_service=services.get_session_service(),
                  artifact_service=services.get_artifact_service())


def _summary(session, invocation_id: str | None) -> tuple[str, list[str], str | None]:
    """(final reply text, delivered file names, error) for one invocation."""
    reply, files, error = "", [], None
    for ev in session.events if session else []:
        if invocation_id and ev.invocation_id != invocation_id:
            continue
        files += list((ev.actions.artifact_delta or {}).keys()) if ev.actions else []
        if ev.author != "user" and ev.content and ev.content.parts:
            text = "".join(p.text or "" for p in ev.content.parts if p.text and not p.thought)
            if text:
                reply = text
        if ev.error_message:
            error = ev.error_message
    return reply, list(dict.fromkeys(files)), error


async def run_job(job_id: str, *, final_attempt: bool | None = None, runner: Runner | None = None) -> bool:
    """Run (or resume) the job. Returns True on success. Raises on a retryable failure."""
    final = _is_final_attempt() if final_attempt is None else final_attempt
    job = await jobs.get(job_id)
    if job is None:
        log.error("job %s not found", job_id)
        return False
    if job["status"] in ("succeeded", "failed", "cancelled"):
        log.info("job %s already %s", job_id, job["status"])
        return job["status"] == "succeeded"

    ws, sid = job["workspace_id"], job["session_id"]
    if job["status"] == "scheduled":  # a check-in come due (app.wakeups)
        if await jobs.active_for_session(ws, sid):
            from app import wakeups

            await wakeups.schedule_job(job_id, wakeups.RETRY_SECONDS)  # the lead is mid-turn: a minute later
            return True
        await jobs.update(job_id, status="queued")
    helper = await tasks.for_helper_session(sid)  # a background helper's job, not a lead turn
    if helper and helper["status"] == "cancelled":
        await jobs.finish(job_id, "cancelled")
        return False
    await jobs.update(job_id, status="running", attempts=job["attempts"] + 1, error=None)
    if helper:
        await tasks.update(helper["id"], status="running")
    if helper and helper["effort"] == "render":
        return await _run_render_watch(job, helper)
    if helper and helper["effort"] == "pipeline":
        from app.pipelines.host import run_pipeline_job

        return await run_pipeline_job(job, helper, services.get_session_service())
    runner = runner or make_runner(helper=bool(helper))
    invocation_id = job.get("invocation_id")
    link = f"{os.environ.get('APP_URL', 'http://localhost:5173').rstrip('/')}/?session={sid}"
    try:
        if invocation_id:
            log.info("job %s: resuming invocation %s", job_id, invocation_id)
            events = runner.run_async(user_id=ws, session_id=sid, invocation_id=invocation_id)
        else:
            message = types.Content.model_validate(await asyncio.to_thread(jobs.load_payload, job_id))
            events = runner.run_async(user_id=ws, session_id=sid, new_message=message)
        last_beat = time.monotonic()
        async for ev in events:
            if not invocation_id and ev.invocation_id:
                invocation_id = ev.invocation_id
                await jobs.update(job_id, invocation_id=invocation_id)
            if time.monotonic() - last_beat > HEARTBEAT_S:
                await jobs.update(job_id)  # heartbeat: updated_at
                last_beat = time.monotonic()

        session = await runner.session_service.get_session(app_name=runner.app_name, user_id=ws, session_id=sid)
        reply, files, error = _summary(session, invocation_id)
        title = (session.state.get("title") if session else None) or "Your request"
        if error:
            raise RuntimeError(error)
        await jobs.finish(job_id, "succeeded")
        if helper:
            await tasks.finish(helper["id"], "succeeded", reply)
            from app.learning import on_helper_end

            await on_helper_end(helper, reply)
            await tasks.wake_parent(ws, helper["parent_session_id"], job.get("notify_email"))
            log.info("helper job %s succeeded", job_id)
            return True
        # Helpers that ended while this turn ran get a turn of their own; while any still run, the
        # job is not done, so "Ready" waits for the turn that ends with nothing in the background.
        woke = await tasks.wake_parent(ws, sid, job.get("notify_email"))
        still = await tasks.running(ws, sid)
        if woke or still:
            log.info("job %s succeeded; %d helper(s) running, email deferred", job_id, len(still))
            return True
        if reply.strip() == "[quiet]":  # a check-in with nothing to say (DA quiet.py)
            log.info("job %s succeeded quietly", job_id)
            return True
        subject, body = notify.compose(ok=True, title=title, reply=reply, files=files, link=link)
        sent = await notify.send(job.get("notify_email"), subject, body)
        log.info("job %s succeeded (%d files, email %s)", job_id, len(files), "sent" if sent else "skipped")
        return True
    except Exception as e:
        log.exception("job %s attempt %d failed", job_id, _attempt())
        if not final:
            await jobs.update(job_id, status="queued", error=f"retrying after: {type(e).__name__}")
            raise
        await jobs.finish(job_id, "failed", error=f"{type(e).__name__}: {e}"[:500])
        if helper:
            await tasks.finish(helper["id"], "failed", f"The helper stopped with an error ({type(e).__name__}).")
            await tasks.wake_parent(ws, helper["parent_session_id"], job.get("notify_email"))
            return False
        session = await runner.session_service.get_session(app_name=runner.app_name, user_id=ws, session_id=sid)
        title = (session.state.get("title") if session else None) or "Your request"
        subject, body = notify.compose(ok=False, title=title, reply="", files=[], link=link,
                                       error="The design run hit an error it could not recover from.")
        await notify.send(job.get("notify_email"), subject, body)
        return False


async def _run_render_watch(job: dict, helper: dict) -> bool:
    """A render watcher's job: poll the sandbox until the command (or watched job) ends."""
    from app import renders

    payload = await asyncio.to_thread(jobs.load_payload, job["id"])
    spec = json.loads(payload["parts"][0]["text"])
    last = time.monotonic()

    async def beat():
        nonlocal last
        if time.monotonic() - last > HEARTBEAT_S:
            await jobs.update(job["id"])
            last = time.monotonic()

    status, report = await renders.run_watch(helper, spec, on_beat=beat)
    await jobs.finish(job["id"], "succeeded" if status != "failed" else "failed")
    if status != "cancelled":
        await tasks.finish(helper["id"], status, report)
        await tasks.wake_parent(job["workspace_id"], helper["parent_session_id"], None)
    return status == "succeeded"


async def smoke(brief: str, email: str | None = None) -> bool:
    """Operator check: run one brief through the real job path in the internal smoke-test workspace."""
    runner = make_runner()
    session = await runner.session_service.create_session(app_name=runner.app_name, user_id="smoke-test")
    job = await jobs.create("smoke-test", session.id, {"role": "user", "parts": [{"text": brief}]}, email)
    log.info("smoke job %s in session %s", job["id"], session.id)
    return await run_job(job["id"], final_attempt=True, runner=runner)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    if len(sys.argv) in (3, 4) and sys.argv[1] == "--smoke":
        return 0 if asyncio.run(smoke(sys.argv[2], sys.argv[3] if len(sys.argv) == 4 else None)) else 1
    if sys.argv[1:] == ["--usage-report"]:
        from app.usage_report import report

        asyncio.run(report())
        return 0
    if len(sys.argv) != 2:
        print('usage: python -m app.worker <job_id> | --smoke "<brief>" [email]', file=sys.stderr)
        return 2
    try:
        return 0 if asyncio.run(run_job(sys.argv[1])) else 1
    except Exception:
        return 1  # non-zero: Cloud Run retries the task


if __name__ == "__main__":
    sys.exit(main())
