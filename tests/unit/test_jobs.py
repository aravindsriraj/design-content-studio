"""Background jobs: queueing rules, worker success/retry/resume/failure, and the email body."""

import asyncio
import datetime as dt

import pytest
from google.adk.events import Event, EventActions
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app import jobs, meter, notify, worker


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch, tmp_path):
    monkeypatch.setenv("STUDIO_DB_URL", f"sqlite+aiosqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("STUDIO_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("STUDIO_BUCKET", raising=False)
    monkeypatch.setattr(meter, "_engine", None)
    monkeypatch.setattr(jobs, "_table_ready", False)


MSG = {"role": "user", "parts": [{"text": "A LinkedIn card, 1200x627"}]}


class FakeRunner:
    """Stands in for ADK's Runner: records how it was called and replays scripted events."""

    app_name = "app"

    def __init__(self, fail=False):
        self.session_service = InMemorySessionService()
        self.calls = []
        self.fail = fail

    async def run_async(self, *, user_id, session_id, invocation_id=None, new_message=None):
        self.calls.append({"invocation_id": invocation_id, "new_message": new_message})
        s = await self.session_service.get_session(app_name="app", user_id=user_id, session_id=session_id)
        inv = invocation_id or "inv-1"
        e1 = Event(invocation_id=inv, author="studio",
                   content=types.Content(role="model", parts=[types.Part.from_text(text="Working")]),
                   actions=EventActions(artifact_delta={"card-1200x627.png": 0}))
        await self.session_service.append_event(s, e1)
        yield e1
        if self.fail:
            raise RuntimeError("model went away")
        e2 = Event(invocation_id=inv, author="studio",
                   content=types.Content(role="model", parts=[types.Part.from_text(text="Here is your card.")]))
        await self.session_service.append_event(s, e2)
        yield e2


@pytest.fixture
def sent(monkeypatch):
    out = []

    async def fake_send(to, subject, body):
        out.append((to, subject, body))
        return True

    monkeypatch.setattr(notify, "send", fake_send)
    return out


async def _new_job(runner, ws="alice"):
    s = await runner.session_service.create_session(app_name="app", user_id=ws, state={"title": "LinkedIn card"})
    return await jobs.create(ws, s.id, MSG, notify_email=f"{ws}@example.com")


def test_one_active_job_per_session():
    async def go():
        runner = FakeRunner()
        job = await _new_job(runner)
        with pytest.raises(jobs.Busy):
            await jobs.create("alice", job["session_id"], MSG, None)
        assert jobs.load_payload(job["id"]) == MSG
        await jobs.finish(job["id"], "succeeded")
        await jobs.create("alice", job["session_id"], MSG, None)  # free again once finished
        assert [j["session_id"] for j in await jobs.latest_per_session("alice")] == [job["session_id"]]
        assert await jobs.latest_per_session("bob") == []

    asyncio.run(go())


def test_stale_running_job_is_released():
    async def go():
        job = await _new_job(FakeRunner())
        old = dt.datetime.now(dt.UTC) - dt.timedelta(hours=4)
        async with (await jobs._engine()).begin() as conn:
            await conn.execute(jobs.jobs_table.update().values(status="running", updated_at=old))
        assert await jobs.active_for_session("alice", job["session_id"]) is None
        assert (await jobs.get(job["id"]))["status"] == "failed"

    asyncio.run(go())


def test_worker_success_records_invocation_and_emails(sent):
    async def go():
        runner = FakeRunner()
        job = await _new_job(runner)
        assert await worker.run_job(job["id"], final_attempt=False, runner=runner) is True
        done = await jobs.get(job["id"])
        assert done["status"] == "succeeded" and done["invocation_id"] == "inv-1" and done["attempts"] == 1
        assert runner.calls[0]["new_message"].parts[0].text == "A LinkedIn card, 1200x627"
        (to, subject, body), = sent
        assert to == "alice@example.com" and subject.startswith("Ready:")
        assert "card-1200x627.png" in body and "Here is your card." in body
        # Running the same job again is a no-op (idempotent).
        assert await worker.run_job(job["id"], final_attempt=True, runner=runner) is True
        assert len(runner.calls) == 1

    asyncio.run(go())


def test_worker_retry_then_resume_by_invocation(sent):
    async def go():
        runner = FakeRunner(fail=True)
        job = await _new_job(runner)
        with pytest.raises(RuntimeError):
            await worker.run_job(job["id"], final_attempt=False, runner=runner)
        mid = await jobs.get(job["id"])
        assert mid["status"] == "queued" and mid["invocation_id"] == "inv-1" and sent == []
        runner.fail = False  # the retry (a new Cloud Run attempt) resumes the same invocation
        assert await worker.run_job(job["id"], final_attempt=True, runner=runner) is True
        assert runner.calls[1] == {"invocation_id": "inv-1", "new_message": None}
        assert (await jobs.get(job["id"]))["attempts"] == 2 and len(sent) == 1

    asyncio.run(go())


def test_worker_final_failure_marks_failed_and_emails(sent):
    async def go():
        runner = FakeRunner(fail=True)
        job = await _new_job(runner)
        assert await worker.run_job(job["id"], final_attempt=True, runner=runner) is False
        failed = await jobs.get(job["id"])
        assert failed["status"] == "failed" and "model went away" in failed["error"]
        assert sent[0][1].startswith("Stopped:")

    asyncio.run(go())


def test_email_escapes_model_text():
    subject, body = notify.compose(ok=True, title="<b>x</b>", reply="<script>alert(1)</script>",
                                   files=["a.png"], link="https://example.com/?session=1")
    assert "<script>" not in body and "&lt;script&gt;" in body and subject == "Ready: <b>x</b>"


def test_email_renders_markdown_safely():
    reply = ("I built the **Atlan** pack from https://atlan.com.\n\n### What was captured\n"
             "- **Colours:** Atlan Blue (`#2026d2`) and `<b>x</b>`\n- [docs](javascript:alert(1)) and *voice*\n\n"
             "1. Upload fonts\n2. Confirm theme\n\n<img src=x onerror=alert(1)>")
    _, body = notify.compose(ok=True, title="Brand", reply=reply, files=["a.png"], link="https://e.com/?s=1&t=2")
    assert "<strong>Atlan</strong>" in body and "**" not in body and "###" not in body
    assert '<a href="https://atlan.com"' in body and "background:#2026d2" in body
    assert "<ul" in body and "<ol" in body and "<em>voice</em>" in body
    assert "<img" not in body and "&lt;img" in body and "&lt;b&gt;x&lt;/b&gt;" in body
    assert 'href="javascript' not in body and "https://e.com/?s=1&amp;t=2" in body
