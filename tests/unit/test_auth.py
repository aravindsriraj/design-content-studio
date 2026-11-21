"""Workspace isolation at the server edge (STUDIO_AUTH=firebase), with token verification faked."""

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("STUDIO_AUTH", "firebase")
    monkeypatch.setenv("STUDIO_DB_URL", f"sqlite+aiosqlite:///{tmp_path}/t.db")
    monkeypatch.delenv("K_SERVICE", raising=False)
    import app.fast_api_app as server

    server = importlib.reload(server)
    tokens = {"tok-alice": "alice", "tok-bob": "bob", "tok-carol-unverified": "carol", "tok-dan-verified": "dan"}

    def fake_verify(token):
        uid = tokens[token]  # KeyError -> invalid token
        if uid in ("carol", "dan"):  # email/password accounts
            return {"uid": uid, "email": f"{uid}@example.com", "email_verified": uid == "dan",
                    "firebase": {"sign_in_provider": "password"}}
        return {"uid": uid, "email": f"{uid}@example.com", "email_verified": True,
                "firebase": {"sign_in_provider": "google.com"}}

    monkeypatch.setattr(server, "_verify_firebase", fake_verify)
    from app import meter

    monkeypatch.setattr(meter, "_engine", None)
    return TestClient(server.app)


def auth(tok):
    return {"Authorization": f"Bearer {tok}"}


def test_public_and_hidden_routes(client):
    assert client.get("/api/config").json()["auth"] == "firebase"
    assert client.get("/health").status_code == 200
    assert client.get("/list-apps", headers=auth("tok-alice")).status_code == 404  # ADK dev route hidden
    assert client.get("/dev-ui/").status_code == 404


def test_requires_valid_token(client):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/me", headers=auth("forged")).status_code == 401
    assert client.get("/api/me", headers=auth("tok-alice")).json() == {"workspace_id": "alice", "email": "alice@example.com"}


def test_cannot_touch_another_workspace(client):
    assert client.get("/apps/app/users/bob/sessions", headers=auth("tok-alice")).status_code == 403
    assert client.get("/apps/app/users/alice/sessions", headers=auth("tok-alice")).status_code == 200
    body = {"app_name": "app", "user_id": "bob", "session_id": "x", "new_message": {"role": "user", "parts": [{"text": "hi"}]}}
    assert client.post("/run_sse", json=body, headers=auth("tok-alice")).status_code == 403
    assert client.post("/run_sse", json={**body, "app_name": "other", "user_id": "alice"}, headers=auth("tok-alice")).status_code == 403
    mine = {**body, "user_id": "alice"}
    assert client.post("/run_sse", json={**mine, "state_delta": {"usage": {}}}, headers=auth("tok-alice")).status_code == 400
    assert client.post("/run_sse", json={**mine, "service_tier": "priority"}, headers=auth("tok-alice")).status_code == 400
    assert client.post("/apps/app/users/alice/sessions", json={"state": {"delivered": {}}}, headers=auth("tok-alice")).status_code == 400
    assert client.post("/apps/app/users/alice/sessions", json={}, headers=auth("tok-alice")).status_code == 200


def test_usage_is_per_workspace(client):
    assert client.get("/api/usage", headers=auth("tok-alice")).json()["all_time"]["calls"] == 0


def test_cloud_run_refuses_auth_off(monkeypatch):
    monkeypatch.setenv("STUDIO_AUTH", "off")
    monkeypatch.setenv("K_SERVICE", "studio")
    import app.fast_api_app as server

    with pytest.raises(RuntimeError, match="refusing"):
        importlib.reload(server)
    monkeypatch.delenv("K_SERVICE")
    importlib.reload(server)  # leave the module importable for other tests


def _sample_zip(with_fonts: bool) -> bytes:
    import io
    import zipfile
    from pathlib import Path

    sample, fonts = Path("app/brand/sample"), Path("sandbox/fonts")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for f in sample.rglob("*"):
            if f.is_file():
                z.write(f, f"acme-brand/{f.relative_to(sample)}")
        if with_fonts:
            for name in ("Geist-Variable.ttf", "Geist-Italic-Variable.ttf", "FragmentMono-Regular.ttf", "FragmentMono-Italic.ttf"):
                z.write(fonts / name, f"acme-brand/fonts/{name}")
    return buf.getvalue()


def test_brand_upload_validates_and_is_per_workspace(client, monkeypatch, tmp_path):
    from app.brand import store

    monkeypatch.setattr(store, "CACHE", tmp_path / "brands")
    monkeypatch.delenv("STUDIO_BUCKET", raising=False)
    put = lambda tok, data: client.put("/api/brand", headers=auth(tok), files={"file": ("b.zip", data, "application/zip")})  # noqa: E731

    bad = put("tok-alice", _sample_zip(with_fonts=False))
    assert bad.status_code == 422 and any("missing" in p for p in bad.json()["detail"]["problems"])
    assert client.get("/api/brand", headers=auth("tok-alice")).json()["is_sample"] is True  # unchanged

    good = put("tok-alice", _sample_zip(with_fonts=True))
    assert good.status_code == 200 and good.json()["is_sample"] is False
    assert client.get("/api/brand", headers=auth("tok-alice")).json()["is_sample"] is False
    assert client.get("/api/brand", headers=auth("tok-bob")).json()["is_sample"] is True  # bob unaffected
    assert put("tok-alice", b"not a zip").status_code == 422


def test_background_jobs_are_per_workspace(client, monkeypatch, tmp_path):
    from app import jobs

    monkeypatch.setenv("STUDIO_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("STUDIO_BUCKET", raising=False)
    monkeypatch.setattr(jobs, "_table_ready", False)
    started = []

    async def fake_dispatch(job_id):
        started.append(job_id)

    monkeypatch.setattr(jobs, "dispatch", fake_dispatch)
    msg = {"message": {"role": "user", "parts": [{"text": "A LinkedIn card"}]}}

    r = client.post("/api/jobs", json=msg, headers=auth("tok-alice"))
    assert r.status_code == 202, r.text
    sid, job = r.json()["session_id"], r.json()["job"]
    assert job["status"] == "queued" and started == [job["id"]]
    assert "notify_email" not in job and "workspace_id" not in job
    assert asyncio_run(jobs.get(job["id"]))["notify_email"] == "alice@example.com"

    assert client.post("/api/jobs", json={**msg, "session_id": sid}, headers=auth("tok-alice")).status_code == 409
    assert client.post("/api/jobs", json={**msg, "session_id": sid}, headers=auth("tok-bob")).status_code == 404
    assert client.get(f"/api/jobs/{job['id']}", headers=auth("tok-bob")).status_code == 404
    assert client.get(f"/api/jobs/{job['id']}", headers=auth("tok-alice")).json()["status"] == "queued"
    assert [j["session_id"] for j in client.get("/api/jobs", headers=auth("tok-alice")).json()] == [sid]
    assert client.get("/api/jobs", headers=auth("tok-bob")).json() == []
    assert client.post("/api/jobs", json={"message": {"role": "model", "parts": []}}, headers=auth("tok-alice")).status_code == 400


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


def test_brand_draft_review_then_activate(client, monkeypatch, tmp_path):
    """Onboarding saves a draft; the live brand changes only on activate; drafts are per workspace."""
    from app.brand import store

    monkeypatch.delenv("STUDIO_BUCKET", raising=False)
    monkeypatch.setattr(store, "CACHE", tmp_path / "brands")
    assert client.get("/api/brand/draft", headers=auth("tok-alice")).json() == {}
    store.save_draft("alice", _sample_zip(with_fonts=True))
    draft = client.get("/api/brand/draft", headers=auth("tok-alice")).json()
    assert draft["name"] == "Example Studio" and draft["is_sample"] is False
    assert client.get("/api/brand/draft", headers=auth("tok-bob")).json() == {}
    assert client.get("/api/brand", headers=auth("tok-alice")).json()["is_sample"] is True  # not live yet
    assert client.post("/api/brand/draft/activate", headers=auth("tok-alice")).status_code == 200
    assert client.get("/api/brand", headers=auth("tok-alice")).json()["is_sample"] is False
    assert client.get("/api/brand/draft", headers=auth("tok-alice")).json() == {}  # consumed
    assert client.post("/api/brand/draft/activate", headers=auth("tok-alice")).status_code == 422


def test_tasks_are_scoped_to_the_workspace(client, monkeypatch, tmp_path):
    import asyncio

    from app import jobs, meter, tasks

    monkeypatch.setenv("STUDIO_DB_URL", f"sqlite+aiosqlite:///{tmp_path}/t.db")
    monkeypatch.setattr(meter, "_engine", None)
    monkeypatch.setattr(jobs, "_table_ready", False)
    monkeypatch.setattr(tasks, "_ready", False)

    async def no_dispatch(job_id):
        return None

    monkeypatch.setattr(jobs, "dispatch", no_dispatch)
    from google.adk.sessions import InMemorySessionService

    asyncio.run(tasks.start("alice", "s-1", "brief", "low", "grabs", InMemorySessionService()))
    rows = client.get("/api/tasks", params={"session_id": "s-1"}, headers=auth("tok-alice")).json()
    assert [r["label"] for r in rows] == ["grabs"] and "report" not in rows[0]
    assert client.get("/api/tasks", params={"session_id": "s-1"}, headers=auth("tok-bob")).json() == []


def test_deleting_a_request_removes_its_files_helpers_and_jobs(client, monkeypatch, tmp_path):
    import asyncio

    from google.adk.artifacts import InMemoryArtifactService
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    from app import jobs, meter, tasks
    from app.app_utils import services

    monkeypatch.setenv("STUDIO_DB_URL", f"sqlite+aiosqlite:///{tmp_path}/d.db")
    monkeypatch.setenv("STUDIO_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("STUDIO_BUCKET", raising=False)
    monkeypatch.setattr(meter, "_engine", None)
    monkeypatch.setattr(jobs, "_table_ready", False)
    monkeypatch.setattr(tasks, "_ready", False)
    ss, arts = InMemorySessionService(), InMemoryArtifactService()
    monkeypatch.setattr(services, "get_session_service", lambda: ss)
    monkeypatch.setattr(services, "get_artifact_service", lambda: arts)

    async def no_dispatch(job_id):
        return None

    monkeypatch.setattr(jobs, "dispatch", no_dispatch)
    png = types.Part.from_bytes(data=b"\x89PNG", mime_type="image/png")

    async def setup():
        s = await ss.create_session(app_name="app", user_id="alice")
        keep = await ss.create_session(app_name="app", user_id="alice")
        for _version in range(2):
            await arts.save_artifact(app_name="app", user_id="alice", session_id=s.id, filename="card.png", artifact=png)
        await arts.save_artifact(app_name="app", user_id="alice", session_id=keep.id, filename="other.png", artifact=png)
        await arts.save_artifact(app_name="app", user_id="alice", session_id=s.id, filename="user:logo.png", artifact=png)
        job = await jobs.create("alice", s.id, {"role": "user", "parts": [{"text": "hi"}]}, None)
        t = await tasks.start("alice", s.id, "brief", "low", "look a: warm", ss)
        await arts.save_artifact(app_name="helpers", user_id="alice", session_id=t["session_id"], filename="look.png", artifact=png)
        return s.id, keep.id, job["id"], t

    sid, keep, job_id, t = asyncio.run(setup())
    auth = {"Authorization": "Bearer tok-bob"}
    assert client.delete(f"/api/requests/{sid}", headers=auth).status_code == 404  # not bob's
    assert client.delete(f"/apps/app/users/alice/sessions/{sid}", headers={"Authorization": "Bearer tok-alice"}).status_code == 405
    r = client.delete(f"/api/requests/{sid}", headers={"Authorization": "Bearer tok-alice"})
    assert r.status_code == 200 and r.json()["files"] == 2 and r.json()["helpers"] == 1

    async def check():
        assert await ss.get_session(app_name="app", user_id="alice", session_id=sid) is None
        assert await arts.list_artifact_keys(app_name="app", user_id="alice", session_id=sid) in ([], ["user:logo.png"], ["logo.png"])
        kept = await arts.list_artifact_keys(app_name="app", user_id="alice", session_id=keep)
        assert "other.png" in kept and "user:logo.png" in kept  # other requests and shared files stay
        assert await arts.list_artifact_keys(app_name="helpers", user_id="alice", session_id=t["session_id"]) == []
        assert await ss.get_session(app_name="helpers", user_id="alice", session_id=t["session_id"]) is None
        assert await jobs.get(job_id) is None and await tasks.get(t["id"]) is None
        assert not jobs._local_payload(job_id).exists()

    asyncio.run(check())



def test_email_password_accounts_must_verify_their_address(client):
    r = client.get("/api/me", headers=auth("tok-carol-unverified"))
    assert r.status_code == 403 and "verify" in r.json()["detail"]
    r = client.get("/api/me", headers=auth("tok-dan-verified"))
    assert r.status_code == 200 and r.json() == {"workspace_id": "dan", "email": "dan@example.com"}
