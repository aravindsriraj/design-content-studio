"""Check-ins: the lead wakes itself later with a note to its future self (DA harness/wakeups.py +
middleware/background.py `schedule_wakeup`), made durable.

A check-in is a lead job created now in status "scheduled" with the note as its message. When it is
due it is dispatched like any job:
- cloudrun: a Cloud Tasks task scheduled for the due time calls the Cloud Run Jobs API itself
  (`jobs:run` with the job id), so nothing in Studio has to stay awake and a restart loses nothing;
- inline (local dev): an asyncio timer.
A due check-in that finds the lead mid-turn tries again a minute later. A reply of exactly
`[quiet]` sends no email and the UI hides it.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os

from google.adk.tools import ToolContext

from app import jobs

log = logging.getLogger(__name__)
QUIET = "[quiet]"
NOTE = "[Check-in, not from the person] "
RETRY_SECONDS = 60
_timers: set[asyncio.Task] = set()


async def schedule_job(job_id: str, seconds: float) -> None:
    """Dispatch `job_id` in `seconds`."""
    if os.environ.get("STUDIO_JOBS", "inline") == "inline":
        async def later():
            await asyncio.sleep(seconds)
            await fire(job_id)

        t = asyncio.create_task(later())
        _timers.add(t)
        t.add_done_callback(_timers.discard)
        return
    await asyncio.to_thread(_cloud_task, job_id, seconds)


def _cloud_task(job_id: str, seconds: float) -> None:
    from google.cloud import tasks_v2
    from google.protobuf import timestamp_pb2

    project, region = os.environ["GOOGLE_CLOUD_PROJECT"], os.environ.get("STUDIO_REGION", "asia-south1")
    queue = os.environ.get("STUDIO_WAKEUP_QUEUE", "studio-wakeups")
    url = (f"https://run.googleapis.com/v2/projects/{project}/locations/{region}/jobs/"
           f"{os.environ.get('STUDIO_WORKER_JOB', 'studio-worker')}:run")
    due = timestamp_pb2.Timestamp()
    due.FromDatetime(dt.datetime.now(dt.UTC) + dt.timedelta(seconds=seconds))
    client = tasks_v2.CloudTasksClient()
    client.create_task(parent=client.queue_path(project, region, queue), task=tasks_v2.Task(
        schedule_time=due,
        http_request=tasks_v2.HttpRequest(
            http_method=tasks_v2.HttpMethod.POST, url=url, headers={"Content-Type": "application/json"},
            body=json.dumps({"overrides": {"containerOverrides": [{"args": [job_id]}], "taskCount": 1}}).encode(),
            oauth_token=tasks_v2.OAuthToken(service_account_email=os.environ["STUDIO_SERVICE_ACCOUNT"],
                                            scope="https://www.googleapis.com/auth/cloud-platform"))))


async def fire(job_id: str) -> None:
    """Inline mode: the due check-in starts (the worker re-checks everything)."""
    from app import worker

    await worker.run_job(job_id, final_attempt=True)


async def schedule_wakeup(minutes: float, prompt: str, reason: str, tool_context: ToolContext) -> dict:
    """Wake yourself later on this request, with `prompt` as a note to your future self.

    Use it to keep the person informed during long work and to check on work nothing reports on.
    It fires once; to keep checking in, schedule the next one when it fires, and stop once the work
    is done. Pick `minutes` (1 to 1440): about 10-15 for long work someone is following, 30-60 or
    more for slow or overnight work. Tasks and watches wake you themselves when they end or need
    you. `prompt` holds what to check, what to tell the person, and whether to check in again;
    `reason` is one short line shown in status. If nothing has changed when it fires, reply exactly
    `[quiet]`: nothing is sent.

    Args:
      minutes: 1 to 1440.
      prompt: The note to your future self.
      reason: One short line for status.
    """
    if not 1 <= minutes <= 1440:
        return {"status": "not scheduled", "error": "minutes must be 1 to 1440"}
    if not prompt.strip():
        return {"status": "not scheduled", "error": "leave yourself a note in prompt"}
    ws, sid = tool_context.user_id, tool_context.session.id
    last = await jobs.latest_for_session(ws, sid)
    job = await jobs.create(ws, sid, {"role": "user", "parts": [{"text": f"{NOTE}({reason.strip()}) {prompt.strip()}"}]},
                            last.get("notify_email") if last else None, status="scheduled")
    await schedule_job(job["id"], minutes * 60)
    due = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=minutes)
    return {"status": "scheduled", "id": job["id"][:8], "due_utc": due.strftime("%Y-%m-%d %H:%M"), "reason": reason}
