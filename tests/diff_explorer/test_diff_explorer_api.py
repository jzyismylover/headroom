"""FastAPI TestClient tests for the diff-explorer router.

Verifies the 4 endpoints declared in ``headroom/diff_explorer/api.py``
match the schema documented in ``shares/sdd/headroom/方案.md`` §B2.
"""

from __future__ import annotations

import datetime as _dt

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from headroom.diff_explorer.api import reset_storage_singleton, router as diff_explorer_router
from headroom.storage.compressed_messages import CompressedMessageRecord
from headroom.storage.sqlite import SQLiteStorage


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Yield a TestClient backed by a fresh SQLite file per test.

    Includes the same CORS middleware the proxy mounts, so CORS tests
    see realistic headers.
    """
    db = str(tmp_path / "api.db")
    monkeypatch.setenv("HEADROOM_STORE_URL", f"sqlite:///{db}")
    reset_storage_singleton()

    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["GET"],
        allow_headers=["*"],
    )
    app.include_router(diff_explorer_router)
    yield TestClient(app)
    reset_storage_singleton()


def _seed(db: str, *records: CompressedMessageRecord):
    s = SQLiteStorage(db)
    for r in records:
        s.save_message_record(r)
    s.close()


def _rec(**kw) -> CompressedMessageRecord:
    defaults = dict(
        record_id="r",
        request_id="req-1",
        session_id="sess-1",
        stage="before",
        model="gpt-4o",
        provider="openai",
        api_style="openai",
        messages_json='[{"role":"user","content":"hi"}]',
        tokens=10,
        messages_hash="h1",
        created_at=_dt.datetime(2025, 8, 23, 10, 0, 0),
        byte_size=30,
    )
    defaults.update(kw)
    return CompressedMessageRecord(**defaults)


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------
def test_health_ok(client, tmp_path):
    r = client.get("/api/diff-explorer/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


# ---------------------------------------------------------------------------
# /sessions
# ---------------------------------------------------------------------------
def test_sessions_empty(client):
    r = client.get("/api/diff-explorer/sessions")
    assert r.status_code == 200
    body = r.json()
    assert body == {"total": 0, "items": []}


def test_sessions_returns_required_fields(client, tmp_path):
    db = str(tmp_path / "api.db")
    _seed(db, _rec(record_id="b", stage="before", tokens=100, session_id="S1"))
    _seed(db, _rec(record_id="a", stage="after", tokens=20, session_id="S1"))
    r = client.get("/api/diff-explorer/sessions")
    body = r.json()
    assert body["total"] == 1
    item = body["items"][0]
    required = {"session_id", "request_count", "first_seen", "last_seen",
                "total_tokens_before", "total_tokens_after", "savings_percent"}
    assert required <= set(item.keys())
    assert item["total_tokens_before"] == 100
    assert item["total_tokens_after"] == 20
    assert item["savings_percent"] == 80.0


def test_sessions_ordered_newest_first(client, tmp_path):
    db = str(tmp_path / "api.db")
    _seed(db, _rec(record_id="a", session_id="older",
                   created_at=_dt.datetime(2025, 1, 1)))
    _seed(db, _rec(record_id="b", session_id="newer",
                   created_at=_dt.datetime(2025, 6, 1)))
    r = client.get("/api/diff-explorer/sessions")
    items = r.json()["items"]
    assert items[0]["session_id"] == "newer"
    assert items[1]["session_id"] == "older"


def test_sessions_limit_cap(client):
    """Limit > MAX_LIMIT must be rejected by fastapi validation."""
    r = client.get("/api/diff-explorer/sessions?limit=99999")
    assert r.status_code == 422


def test_sessions_limit_zero_rejected(client):
    r = client.get("/api/diff-explorer/sessions?limit=0")
    assert r.status_code == 422


def test_sessions_negative_offset_rejected(client):
    r = client.get("/api/diff-explorer/sessions?offset=-1")
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# /sessions/{session_id}/requests
# ---------------------------------------------------------------------------
def test_session_requests_returns_all_stages(client, tmp_path):
    db = str(tmp_path / "api.db")
    _seed(db, _rec(record_id="b", request_id="r1", session_id="S", stage="before", tokens=100))
    _seed(db, _rec(record_id="a", request_id="r1", session_id="S", stage="after", tokens=20))
    _seed(db, _rec(record_id="b2", request_id="r2", session_id="S", stage="before", tokens=50))
    r = client.get("/api/diff-explorer/sessions/S/requests")
    body = r.json()
    assert body["total"] == 3
    for item in body["items"]:
        assert {"request_id", "stage", "model", "tokens", "timestamp"} <= set(item.keys())


def test_session_requests_unknown_returns_empty(client):
    r = client.get("/api/diff-explorer/sessions/nope/requests")
    assert r.status_code == 200
    assert r.json() == {"total": 0, "items": []}


def test_session_requests_huge_id_rejected(client):
    """Path parameter > 256 chars is rejected (DoS guard)."""
    r = client.get("/api/diff-explorer/sessions/" + "x" * 300 + "/requests")
    assert r.status_code == 400


def test_session_requests_empty_id_rejected(client):
    """Trailing slash with empty session id — fastapi 404 is OK, we just
    make sure it doesn't 500."""
    r = client.get("/api/diff-explorer/sessions//requests")
    assert r.status_code in (400, 404)


# ---------------------------------------------------------------------------
# /requests/{request_id}
# ---------------------------------------------------------------------------
def test_request_diff_returns_two_stages(client, tmp_path):
    db = str(tmp_path / "api.db")
    _seed(db, _rec(record_id="b", request_id="r1", stage="before", tokens=100,
                   messages_json='[{"role":"user","content":"BEFORE"}]'))
    _seed(db, _rec(record_id="a", request_id="r1", stage="after", tokens=20,
                   messages_json='[{"role":"user","content":"AFTER"}]'))
    r = client.get("/api/diff-explorer/requests/r1")
    body = r.json()
    assert body["request_id"] == "r1"
    assert body["stages"]["before"]["tokens"] == 100
    assert body["stages"]["after"]["tokens"] == 20
    assert body["stages"]["before"]["messages"] == [{"role": "user", "content": "BEFORE"}]
    assert body["stages"]["after"]["messages"] == [{"role": "user", "content": "AFTER"}]


def test_request_diff_does_not_include_presend(client, tmp_path):
    """The Diff Explorer contract: stages ONLY contain before / after."""
    db = str(tmp_path / "api.db")
    _seed(db, _rec(record_id="b", request_id="r1", stage="before"))
    _seed(db, _rec(record_id="a", request_id="r1", stage="after"))
    r = client.get("/api/diff-explorer/requests/r1")
    body = r.json()
    assert set(body["stages"].keys()) == {"before", "after"}


def test_request_diff_unknown_returns_null_stages(client):
    """Unknown request_id must return 200 (not 404) with null stages."""
    r = client.get("/api/diff-explorer/requests/unknown")
    assert r.status_code == 200
    body = r.json()
    assert body["stages"]["before"] is None
    assert body["stages"]["after"] is None


def test_request_diff_truncated_placeholder(client, tmp_path):
    db = str(tmp_path / "api.db")
    _seed(db, _rec(record_id="b", request_id="r1", stage="before",
                   messages_json='{"_truncated": true, "original_byte_size": 9999, "stage": "before"}',
                   tokens=0, byte_size=80))
    r = client.get("/api/diff-explorer/requests/r1")
    body = r.json()
    assert body["stages"]["before"]["truncated"] is True
    assert body["stages"]["before"]["messages"] == []


def test_request_diff_huge_id_rejected(client):
    r = client.get("/api/diff-explorer/requests/" + "x" * 300)
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
def test_cors_preflight_allowed_origin(client):
    """OPTIONS preflight from http://localhost:5173 must succeed."""
    r = client.options(
        "/api/diff-explorer/sessions",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_cors_get_has_allow_origin_header(client, tmp_path):
    """A regular GET with Origin header gets the CORS response header."""
    db = str(tmp_path / "api.db")
    _seed(db, _rec(record_id="b", session_id="S", stage="before"))
    r = client.get(
        "/api/diff-explorer/sessions",
        headers={"Origin": "http://localhost:5173"},
    )
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"


# ---------------------------------------------------------------------------
# XSS protection — payload stored verbatim, frontend must escape
# ---------------------------------------------------------------------------
def test_xss_payload_stored_verbatim(client, tmp_path):
    """The API surfaces content as raw JSON; XSS prevention is the
    frontend's job. This test pins that contract — change with care."""
    db = str(tmp_path / "api.db")
    payload = '[{"role":"user","content":"<script>alert(1)</script>"}]'
    _seed(db, _rec(record_id="b", request_id="r1", stage="before",
                   messages_json=payload, byte_size=len(payload)))
    r = client.get("/api/diff-explorer/requests/r1")
    content = r.json()["stages"]["before"]["messages"][0]["content"]
    assert content == "<script>alert(1)</script>"


# ---------------------------------------------------------------------------
# reset_storage_singleton (test hook)
# ---------------------------------------------------------------------------
def test_reset_storage_singleton_picks_up_new_env(tmp_path, monkeypatch):
    """A test that monkeypatches HEADROOM_STORE_URL between resets must
    observe the new file."""
    db_a = str(tmp_path / "a.db")
    db_b = str(tmp_path / "b.db")

    monkeypatch.setenv("HEADROOM_STORE_URL", f"sqlite:///{db_a}")
    reset_storage_singleton()
    app = FastAPI()
    app.include_router(diff_explorer_router)
    client_a = TestClient(app)
    _seed(db_a, _rec(record_id="b", session_id="S-A", stage="before"))
    r = client_a.get("/api/diff-explorer/sessions")
    assert r.json()["items"][0]["session_id"] == "S-A"

    reset_storage_singleton()
    monkeypatch.setenv("HEADROOM_STORE_URL", f"sqlite:///{db_b}")
    reset_storage_singleton()
    _seed(db_b, _rec(record_id="b", session_id="S-B", stage="before"))
    r = client_a.get("/api/diff-explorer/sessions")
    assert r.json()["items"][0]["session_id"] == "S-B"
