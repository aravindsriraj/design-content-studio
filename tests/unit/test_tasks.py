"""Background helpers: a helper's end wakes the lead, the email waits for the last turn, notices fold in."""

import asyncio
from types import SimpleNamespace

import pytest

from app import jobs, tasks, worker
from tests.unit.test_jobs import MSG, FakeRunner, fresh_db, sent  # noqa: F401  (fixtures)


@pytest.fixture(autouse=True)
def tasks_db(monkeypatch, fresh_db):  # noqa: F811
    monkeypatch.setattr(tasks, "_ready", False)


@pytest.fixture
def dispatched(monkeypatch):
    out = []

    async def fake_dispatch(job_id):
        out.append(job_id)

    monkeypatch.setattr(jobs, "dispatch", fake_dispatch)
    return out


class HelperRunner(FakeRunner):
    app_name = tasks.HELPERS_APP

    async def run_async(self, *, user_id, session_id, invocation_id=None, new_message=None):
        s = await self.session_service.get_session(app_name=self.app_name, user_id=user_id, session_id=session_id)
        from google.adk.events import Event
        from google.genai import types

        e = Event(invocation_id="h-1", author="helper",
                  content=types.Content(role="model", parts=[types.Part.from_text(text="Look a done: out/a.png")]))
        await self.session_service.append_event(s, e)
        yield e


def test_helper_end_wakes_lead_and_email_waits_for_last_turn(sent, dispatched):  # noqa: F811
    async def go():
        lead = FakeRunner()
        lead_s = await lead.session_service.create_session(app_name="app", user_id="alice", state={"title": "Ad"})
        j1 = await jobs.create("alice", lead_s.id, MSG, notify_email="alice@example.com")
        await jobs.update(j1["id"], status="running")

        t = await tasks.start("alice", lead_s.id, "Make look a", "low", "look a", lead.session_service)
        assert t["status"] == "queued" and len(dispatched) == 1
        helper = HelperRunner()
        helper.session_service = lead.session_service

        # The helper ends while the lead's turn still runs: its notice waits.
        assert await worker.run_job(t["job_id"], final_attempt=True, runner=helper)
        t = await tasks.get(t["id"])
        assert t["status"] == "succeeded" and "out/a.png" in t["report"] and not t["notified"]
        assert sent == []

        # The lead's turn ends: no "Ready" email; a wake turn carries the notice instead.
        await jobs.update(j1["id"], status="queued")
        assert await worker.run_job(j1["id"], final_attempt=True, runner=lead)
        assert sent == [] and len(dispatched) == 2
        wake = dispatched[-1]
        text = jobs.load_payload(wake)["parts"][0]["text"]
        assert "Background notice" in text and "out/a.png" in text
        assert (await tasks.get(t["id"]))["notified"]
        assert (await jobs.get(wake))["notify_email"] == "alice@example.com"

        # The wake turn ends with nothing running: now the person hears.
        assert await worker.run_job(wake, final_attempt=True, runner=lead)
        assert len(sent) == 1 and sent[0][1].startswith("Ready")

    asyncio.run(go())


def test_lead_turn_folds_waiting_notices_and_lists_running(dispatched):
    async def go():
        lead = FakeRunner()
        s = await lead.session_service.create_session(app_name="app", user_id="alice")
        done = await tasks.start("alice", s.id, "brief", "low", "grabs", lead.session_service)
        await tasks.start("alice", s.id, "brief", "xhigh", "build", lead.session_service)
        await tasks.finish(done["id"], "succeeded", "12 frames in out/")
        ctx = SimpleNamespace(user_id="alice", session=SimpleNamespace(id=s.id), state={})
        await tasks.fold_notices(ctx)
        note = ctx.state["tasks_note"]
        assert "12 frames" in note and "Still running" in note and "build" in note
        assert (await tasks.get(done["id"]))["notified"]
        await tasks.fold_notices(ctx)
        assert "12 frames" not in ctx.state["tasks_note"]  # heard once

    asyncio.run(go())


def test_cancelled_helper_never_runs(dispatched):
    async def go():
        lead = FakeRunner()
        s = await lead.session_service.create_session(app_name="app", user_id="alice")
        t = await tasks.start("alice", s.id, "brief", "low", "grabs", lead.session_service)
        await tasks.update(t["id"], status="cancelled")
        assert not await worker.run_job(t["job_id"], final_attempt=True, runner=HelperRunner())
        assert (await jobs.get(t["job_id"]))["status"] == "cancelled"

    asyncio.run(go())


def test_resume_failed_helper_continues_in_its_session(dispatched):
    from app.helpers import resume_task

    async def go():
        lead = FakeRunner()
        s = await lead.session_service.create_session(app_name="app", user_id="alice")
        t = await tasks.start("alice", s.id, "brief", "xhigh", "build", lead.session_service)
        await jobs.finish(t["job_id"], "failed")
        await tasks.finish(t["id"], "failed", "boom")
        ctx = SimpleNamespace(user_id="alice", session=SimpleNamespace(id=s.id))
        r = await resume_task(t["id"][:8], ctx)
        assert r["status"] == "resumed"
        t2 = await tasks.get(t["id"])
        assert t2["status"] == "queued" and t2["job_id"] != t["job_id"] and not t2["notified"]
        job = await jobs.get(t2["job_id"])
        assert job["session_id"] == t["session_id"]  # same helper conversation
        assert (await resume_task(t["id"][:8], ctx))["status"] == "not resumed"  # already running

    asyncio.run(go())


def test_observations_are_per_workspace_and_learn_helper_saves(monkeypatch):
    from app.harness.learning import ObservationStore
    from app.learning import LEARN_LABEL, WorkspaceStore, on_helper_end, save_proposals
    from app.pipelines import host

    monkeypatch.setattr(host, "_ready", False)

    async def go():
        await save_proposals("alice", "stills", [{"text": "The person asked for larger type twice", "evidence": ["r1", "r2"]}])
        assert "larger type" in await ObservationStore(WorkspaceStore("alice")).render("stills")
        assert await ObservationStore(WorkspaceStore("bob")).render("stills") in ("", None)  # isolated
        task = {"label": f"{LEARN_LABEL}stills", "workspace_id": "bob"}
        await on_helper_end(task, 'Proposals:\n```json\n[{"text": "Bob prefers dark grounds", "evidence": ["pick b"]}]\n```')
        assert "dark grounds" in await ObservationStore(WorkspaceStore("bob")).render("stills")

    asyncio.run(go())


class QuietRunner(FakeRunner):
    async def run_async(self, *, user_id, session_id, invocation_id=None, new_message=None):
        from google.adk.events import Event
        from google.genai import types

        s = await self.session_service.get_session(app_name="app", user_id=user_id, session_id=session_id)
        e = Event(invocation_id="q-1", author="studio", content=types.Content(role="model", parts=[types.Part.from_text(text="[quiet]")]))
        await self.session_service.append_event(s, e)
        yield e


def test_check_in_waits_for_a_busy_lead_and_quiet_sends_nothing(monkeypatch, sent):  # noqa: F811
    from app import wakeups
    from app.wakeups import schedule_wakeup

    later = []

    async def fake_schedule(job_id, seconds):
        later.append((job_id, seconds))

    monkeypatch.setattr(wakeups, "schedule_job", fake_schedule)

    async def go():
        lead = QuietRunner()
        s = await lead.session_service.create_session(app_name="app", user_id="alice", state={"title": "Film"})
        busy = await jobs.create("alice", s.id, MSG, "alice@example.com")
        await jobs.update(busy["id"], status="running")
        ctx = SimpleNamespace(user_id="alice", session=SimpleNamespace(id=s.id))
        r = await schedule_wakeup(15, "Check the render; tell the person the ETA.", "render ETA", ctx)
        assert r["status"] == "scheduled" and later[-1][1] == 900  # scheduled even while the lead is mid-turn
        job_id = later[-1][0]
        assert (await jobs.get(job_id))["status"] == "scheduled"
        assert await jobs.active_for_session("alice", s.id)  # the check-in does not count as active

        assert await worker.run_job(job_id, final_attempt=True, runner=lead)  # due, lead busy: later
        assert later[-1] == (job_id, wakeups.RETRY_SECONDS) and (await jobs.get(job_id))["status"] == "scheduled"

        await jobs.finish(busy["id"], "succeeded")
        assert await worker.run_job(job_id, final_attempt=True, runner=lead)
        assert (await jobs.get(job_id))["status"] == "succeeded" and sent == []  # [quiet]: no email
        assert (await jobs.get(job_id))["notify_email"] == "alice@example.com"

    asyncio.run(go())


def test_a_helper_whose_job_died_is_reported_interrupted(dispatched):
    import datetime as dt

    async def go():
        lead = FakeRunner()
        s = await lead.session_service.create_session(app_name="app", user_id="alice")
        t = await tasks.start("alice", s.id, "brief", "xhigh", "build", lead.session_service)
        await jobs.update(t["job_id"], status="running")
        old = dt.datetime.now(dt.UTC) - jobs.STALE_AFTER - dt.timedelta(minutes=1)
        async with (await jobs._engine()).begin() as conn:
            await conn.execute(jobs.jobs_table.update().where(jobs.jobs_table.c.id == t["job_id"]).values(updated_at=old))
        rows = await tasks.for_parent("alice", s.id)
        assert rows[0]["status"] == "failed" and "Interrupted" in rows[0]["report"]
        assert rows[0]["notified"] and "Interrupted" in jobs.load_payload(dispatched[-1])["parts"][0]["text"]  # lead woken

    asyncio.run(go())
