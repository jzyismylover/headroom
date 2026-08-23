"""End-to-end test: /v1/responses traffic is captured into the Diff Explorer store.

Regression test for the bug where the Responses API path (used by Codex /
CLI clients) never emitted INPUT_RECEIVED / INPUT_COMPRESSED, so
MessageCaptureExtension could never persist a before/after pair and the
dashboard read an empty store even though traffic flowed through the proxy.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from headroom.diff_explorer.message_capture import MessageCaptureExtension  # noqa: E402
from headroom.proxy.loopback_guard import require_loopback  # noqa: E402
from headroom.proxy.server import ProxyConfig, create_app  # noqa: E402
from headroom.storage.sqlite import SQLiteStorage  # noqa: E402
from headroom.proxy.handlers.openai import _responses_input_to_learner_messages  # noqa: E402


def _fake_upstream_response(url: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "resp_test",
            "object": "response",
            "output": [],
            "usage": {"input_tokens": 12, "output_tokens": 3},
        },
        request=httpx.Request("POST", url),
    )


def _make_app(tmp_path, monkeypatch):
    """Build the proxy app with a MessageCaptureExtension on the pipeline bus.

    The capture storage's connection is created lazily inside the TestClient
    worker thread, so assertions query a *separate* SQLiteStorage instance on
    the same file to avoid sqlite3's thread-affinity check.
    """
    monkeypatch.setenv("HEADROOM_PROXY_CAPTURE_DIFF", "0")  # isolate from env
    db_path = str(tmp_path / "diff.db")
    config = ProxyConfig(
        optimize=True,
        cache_enabled=False,
        rate_limit_enabled=False,
        cost_tracking_enabled=False,
        discover_pipeline_extensions=False,
        pipeline_extensions=[MessageCaptureExtension(SQLiteStorage(db_path))],
    )
    app = create_app(config)
    app.dependency_overrides[require_loopback] = lambda: None
    return app, db_path


def _patch_compression(app, *, mutate: bool):
    """Stub the responses-API compression executor.

    When ``mutate`` is True it shortens the user text (simulating a real
    compression), otherwise it returns the body untouched (no-op).
    """
    server = app.state.proxy

    async def fake_compress(payload, *, model, request_id, timeout=30.0, client=None, savings_tags=None):
        body = json.loads(json.dumps(payload))
        if mutate and isinstance(body.get("input"), str) and len(body["input"]) > 200:
            body["input"] = body["input"][:20]
            modified, tokens_saved, reason = True, 10, None
        else:
            modified, tokens_saved, reason = False, 0, "no_compression"
        before = len(json.dumps(payload).encode("utf-8"))
        after = len(json.dumps(body).encode("utf-8"))
        return (body, modified, tokens_saved, ["responses_compression"], reason,
                before, after, before, {})

    server._compress_openai_responses_payload_in_executor = fake_compress
    return server


def _patch_upstream(app):
    server = app.state.proxy

    async def fake_retry(method, url, headers, body, stream=False, **kwargs):
        return _fake_upstream_response(url)

    server._retry_request = fake_retry
    return server


def _post(app, **body):
    with TestClient(app) as client:
        return client.post(
            "/v1/responses",
            headers={
                "Authorization": "Bearer sk-test",
                "x-codex-session-id": "codex-session-123",
            },
            json={"model": "gpt-4o", "input": "x" * 1000, **body},
        )


def test_responses_capture_writes_before_after_pair(tmp_path, monkeypatch):
    app, db_path = _make_app(tmp_path, monkeypatch)
    _patch_compression(app, mutate=True)
    _patch_upstream(app)

    resp = _post(app)
    assert resp.status_code == 200

    storage = SQLiteStorage(db_path)
    sessions = storage.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "codex-session-123"

    records = storage.list_records_by_session("codex-session-123")
    stages = {r.stage for r in records}
    assert stages == {"before", "after"}
    before = next(r for r in records if r.stage == "before")
    after = next(r for r in records if r.stage == "after")
    assert before.messages_json != after.messages_json


def test_responses_capture_noop_writes_nothing(tmp_path, monkeypatch):
    app, db_path = _make_app(tmp_path, monkeypatch)
    _patch_compression(app, mutate=False)
    _patch_upstream(app)

    resp = _post(app)
    assert resp.status_code == 200
    assert SQLiteStorage(db_path).list_sessions() == []


def test_responses_capture_conversion_preserves_items():
    """The chat-message conversion keeps tool calls / outputs visible."""
    body = {
        "instructions": "you are a helper",
        "input": [
            {"type": "message", "role": "user", "content": "run the command"},
            {
                "type": "function_call",
                "call_id": "call-1",
                "name": "shell",
                "arguments": json.dumps({"cmd": "ls"}),
            },
            {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": "file1\nfile2",
            },
        ],
    }
    messages = _responses_input_to_learner_messages(
        body["instructions"], body["input"]
    )
    roles = [m["role"] for m in messages]
    assert roles == ["system", "user", "assistant", "user"]
    tool_use = messages[2]["content"][0]
    assert tool_use["type"] == "tool_use"
    assert tool_use["name"] == "shell"
    tool_result = messages[3]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["content"] == "file1\nfile2"
