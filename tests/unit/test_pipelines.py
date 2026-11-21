"""Pipelines on durable jobs: DA's WorkflowRunner over SqlStore; ask parks, answer replays the memo."""

import asyncio
import json

import pytest
from google.adk.sessions import InMemorySessionService

from app import jobs, tasks
from app.harness.protocols import HelperResult
from app.pipelines import host
from tests.unit.test_jobs import fresh_db  # noqa: F401


@pytest.fixture(autouse=True)
def tables(monkeypatch, fresh_db):  # noqa: F811
    monkeypatch.setattr(tasks, "_ready", False)
    monkeypatch.setattr(host, "_ready", False)


async def tiny(ctx, *, project: str):
    ctx.phase("plan")
    plan = await ctx.agent(f"plan {project}", effort="max", label="plan",
                           schema={"type": "object", "required": ["acts"], "properties": {"acts": {"type": "array"}}})
    ctx.phase("approval")
    answer = await ctx.ask("Render the final?", ["approve", "changes"])
    return {"acts": plan.value["acts"], "answer": answer}


def test_pipeline_parks_on_a_question_then_replays_after_the_answer(monkeypatch):
    calls, woke, dispatched = [], [], []

    async def fake_run(self, brief, *, effort, label="", wave=None):
        calls.append(label)
        return HelperResult(task_id="t", status="done", report='Planned.\n{"acts": ["a1", "a2"]}')

    async def fake_wake(task, text):
        woke.append(text)

    async def no_dispatch(job_id):
        dispatched.append(job_id)

    monkeypatch.setattr(host.InlineHelpers, "run", fake_run)
    monkeypatch.setattr(host, "_wake", fake_wake)
    monkeypatch.setattr(host, "PIPELINES", {"tiny": tiny})
    monkeypatch.setattr(jobs, "dispatch", no_dispatch)

    async def go():
        ss = InMemorySessionService()
        lead = await ss.create_session(app_name="app", user_id="alice")
        spec = {"pipeline": "tiny", "args": {"project": "film"}}
        t = await tasks.start("alice", lead.id, json.dumps(spec), "pipeline", "pipeline: tiny", ss)
        await host.run_pipeline_job(await jobs.get(t["job_id"]), t, ss)
        t = await tasks.get(t["id"])
        assert t["status"] == "waiting" and "Render the final?" in t["progress"]
        assert calls == ["plan"] and "asks: Render the final?" in woke[0]

        # answer_pipeline: a new job on the task's session carrying the answer
        from types import SimpleNamespace
        ctx = SimpleNamespace(user_id="alice", session=SimpleNamespace(id=lead.id))
        assert (await host.answer_pipeline(t["id"][:8], "approve", ctx))["status"] == "answered"
        t = await tasks.get(t["id"])
        await host.run_pipeline_job(await jobs.get(t["job_id"]), t, ss)
        t = await tasks.get(t["id"])
        assert t["status"] == "succeeded" and calls == ["plan"]  # the plan replayed from the memo
        assert json.loads(t["report"]) == {"acts": ["a1", "a2"], "answer": "approve"}

        # a retried answer job finds nothing waiting and resumes harmlessly
        await host.run_pipeline_job(await jobs.get(t["job_id"]), t, ss)
        assert calls == ["plan"]

    asyncio.run(go())
