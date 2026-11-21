"""Studio's host for design-agent pipelines (DA middleware/pipeline_tools.py), on durable jobs.

`run_pipeline` starts a pipeline as a background task (effort "pipeline"). Its job runs DA's
`WorkflowRunner` unchanged (`app.harness.workflow`) with:
- `SqlStore`: DA's store interface (aget/aput/asearch/adelete) over Studio's database, so memoized
  steps and run records survive the job; a retried job resumes the run and finished steps replay;
- `InlineHelpers`: each step is a helper run inline in the pipeline's own job (DA `run_inline`), a
  task row the person sees, which never wakes the lead by itself;
- the bus: phases and step notes become the task's progress line.
When the run ends, its task ends and the lead is woken with the result. When it asks a question, the
task waits, the lead is woken with the question, and `answer_pipeline` continues it in a new job.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from google.adk.tools import ToolContext

from app import jobs, meter, tasks
from app.harness import events as ev
from app.harness.protocols import HelperResult
from app.harness.workflow import WorkflowError, WorkflowRunner
from app.pipelines.launch_film import launch_film

PIPELINES = {"launch_film": launch_film}

kv_table = sa.Table(
    "harness_kv", meter._meta,
    sa.Column("namespace", sa.String(255), primary_key=True),
    sa.Column("key", sa.String(255), primary_key=True),
    sa.Column("value", sa.Text, nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)
_ready = False


async def _engine():
    global _ready
    eng = await meter.engine()
    if not _ready:
        async with eng.begin() as conn:
            await conn.run_sync(lambda c: kv_table.create(c, checkfirst=True))
        _ready = True
    return eng


@dataclass
class Item:
    key: str
    value: Any
    namespace: tuple = ()


class SqlStore:
    """The subset of LangGraph's BaseStore that design-agent's harness uses."""

    @staticmethod
    def _ns(namespace) -> str:
        return "/".join(namespace)

    async def aput(self, namespace, key: str, value: dict) -> None:
        t, ns = kv_table, self._ns(namespace)
        row = {"namespace": ns, "key": key, "value": json.dumps(value), "updated_at": dt.datetime.now(dt.UTC)}
        async with (await _engine()).begin() as conn:
            await conn.execute(t.delete().where(t.c.namespace == ns, t.c.key == key))
            await conn.execute(t.insert().values(**row))

    async def aget(self, namespace, key: str) -> Item | None:
        t = kv_table
        async with (await _engine()).connect() as conn:
            r = (await conn.execute(sa.select(t.c.value).where(t.c.namespace == self._ns(namespace), t.c.key == key))).first()
        return Item(key, json.loads(r[0]), tuple(namespace)) if r else None

    async def asearch(self, namespace, *, filter: dict | None = None, limit: int = 10, **_: Any) -> list[Item]:
        t = kv_table
        async with (await _engine()).connect() as conn:
            rows = (await conn.execute(sa.select(t.c.key, t.c.value).where(t.c.namespace == self._ns(namespace)))).all()
        items = [Item(k, json.loads(v), tuple(namespace)) for k, v in rows]
        if filter:
            items = [i for i in items if all(i.value.get(k) == v for k, v in filter.items())]
        return items[:limit]

    async def adelete(self, namespace, key: str) -> None:
        t = kv_table
        async with (await _engine()).begin() as conn:
            await conn.execute(t.delete().where(t.c.namespace == self._ns(namespace), t.c.key == key))


class InlineHelpers:
    """DA's HelperRunner: every step is a helper task run to completion inside this job."""

    def __init__(self, workspace_id: str, parent_session_id: str, session_service) -> None:
        self.ws, self.parent, self.ss = workspace_id, parent_session_id, session_service

    async def run(self, brief: str, *, effort: str, label: str = "", wave: str | None = None) -> HelperResult:
        from app import worker

        t = await tasks.start(self.ws, self.parent, brief, effort if effort in tasks.EFFORTS else "high",
                              label or "step", self.ss, dispatch=False, notified=True)
        try:
            await worker.run_job(t["job_id"], final_attempt=True)
        except Exception as e:  # a crashed step is a failed step, never a dead run
            await tasks.finish(t["id"], "failed", f"{type(e).__name__}")
        t = await tasks.get(t["id"])
        status = {"succeeded": "done", "cancelled": "cancelled"}.get(t["status"], "failed")
        return HelperResult(task_id=t["id"][:8], status=status, report=t["report"] or "",
                            error="" if status == "done" else (t["report"] or t["status"]))

    async def start(self, brief: str, *, effort: str, label: str = "", wave: str | None = None) -> str:
        t = await tasks.start(self.ws, self.parent, brief, effort, label or "step", self.ss)
        return t["id"][:8]


async def run_pipeline_job(job: dict, task: dict, session_service) -> bool:
    """The pipeline task's job: start, resume or answer the run, then wait until it ends or asks."""
    spec = json.loads((await asyncio.to_thread(jobs.load_payload, job["id"]))["parts"][0]["text"])
    store, bus = SqlStore(), ev.EventBus()

    feed = {"open": True, "lock": asyncio.Lock()}  # bus events land asynchronously

    async def on_event(e: ev.Event) -> None:
        async with feed["lock"]:
            if feed["open"] and e.kind in (ev.JOB_PHASE, ev.PIPELINE_STEP) and e.text:
                await tasks.update(task["id"], progress=e.text[:600])

    bus.subscribe(on_event)
    runner = WorkflowRunner(store, InlineHelpers(task["workspace_id"], task["parent_session_id"], session_service), bus)
    for name, fn in PIPELINES.items():
        runner.register(name, fn)
    link = await store.aget(("studio", "pipeline_task"), task["id"])
    if link is None:
        run_id = await runner.start(spec["pipeline"], spec.get("args") or {}, task["parent_session_id"])
        await store.aput(("studio", "pipeline_task"), task["id"], {"run_id": run_id})
    else:
        run_id = link.value["run_id"]
        try:
            if spec.get("answer") is None:
                raise WorkflowError("no answer")
            await runner.answer(run_id, spec["answer"])
        except WorkflowError:  # a retried answer job, or a restart: carry on from the memo
            await runner.resume(run_id, reason="after a restart")
    record = await runner.wait(run_id)
    await asyncio.sleep(0)  # let events already published start
    async with feed["lock"]:
        feed["open"] = False  # from here on the task's line is the outcome, not a step note
    await jobs.finish(job["id"], "succeeded" if record.status != "failed" else "failed")
    if record.status == "waiting":
        q = record.waiting[0]
        await tasks.update(task["id"], status="waiting", progress=f"Waiting for an answer: {q['question']}")
        await _wake(task, f"Pipeline {task['id'][:8]} ({record.name}) asks: {q['question']} "
                          f"Options: {', '.join(q.get('options') or []) or 'free text'}. Ask the person, then "
                          f"answer_pipeline with their words.")
        return True
    if record.status == "done":
        await tasks.finish(task["id"], "succeeded", json.dumps(record.result, indent=1)[:20000])
    elif record.status == "cancelled":
        await tasks.finish(task["id"], "cancelled", "cancelled")
    else:
        await tasks.finish(task["id"], "failed", record.error or "failed")
    await tasks.wake_parent(task["workspace_id"], task["parent_session_id"], None)
    return record.status == "done"


async def _wake(task: dict, text: str) -> None:
    from app.renders import _wake_now

    await _wake_now(task, text)


# ---------------------------------------------------------------- the lead's tools


async def run_pipeline(name: str, tool_context: ToolContext, args: dict | None = None) -> dict:
    """Start a pipeline: a production whose order, reviews and retries are code. It runs in the
    background; each step is a helper. You are woken once when it finishes, fails, or asks
    something (answer with answer_pipeline). Pipelines: launch_film (args: project, the folder
    under /workspace/projects/ holding BRIEF.md, the approved storyboard and animatic and a frozen
    timeline.js; notes: optional, for the planner; builders_at_once: optional).

    Args:
      name: The pipeline's name.
      args: Its arguments.
    """
    if name not in PIPELINES:
        return {"status": "not started", "error": f"no pipeline {name!r}; pipelines: {', '.join(PIPELINES)}"}
    spec = {"pipeline": name, "args": args or {}}
    t = await tasks.start(tool_context.user_id, tool_context.session.id, json.dumps(spec), "pipeline",
                          f"pipeline: {name}", tool_context._invocation_context.session_service)
    return {"status": "started", "id": t["id"][:8], "pipeline": name,
            "next": "Tell the person what is running and roughly how long, then end your turn."}


async def answer_pipeline(task_id: str, answer: str, tool_context: ToolContext) -> dict:
    """Answer the question a pipeline is waiting on, in the person's words, and let it continue
    (its finished steps replay; only the rest runs).

    Args:
      task_id: The pipeline's id.
      answer: The person's answer.
    """
    rows = [t for t in await tasks.for_parent(tool_context.user_id, tool_context.session.id)
            if t["id"].startswith(task_id) and t["effort"] == "pipeline"]
    if not rows or rows[0]["status"] != "waiting":
        return {"status": "not answered", "error": "no pipeline with that id is waiting for an answer"}
    t = rows[0]
    spec = json.loads((await asyncio.to_thread(jobs.load_payload, t["job_id"]))["parts"][0]["text"])
    spec["answer"] = answer
    job = await jobs.create(tool_context.user_id, t["session_id"], {"role": "user", "parts": [{"text": json.dumps(spec)}]}, None)
    await tasks.update(t["id"], status="queued", job_id=job["id"], progress=f"Answered: {answer[:200]}")
    await jobs.dispatch(job["id"])
    return {"status": "answered", "id": t["id"][:8]}
