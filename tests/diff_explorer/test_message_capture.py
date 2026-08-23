"""Unit tests for MessageCaptureExtension — two-stage capture + edge cases."""

from __future__ import annotations

import datetime as _dt

import pytest

from headroom.diff_explorer.message_capture import MessageCaptureExtension
from headroom.diff_explorer.session_resolver import NO_SESSION_PREFIX
from headroom.pipeline import PipelineEvent, PipelineStage
from headroom.storage.compressed_messages import CompressedMessageRecord
from headroom.storage.sqlite import SQLiteStorage


_DEFAULT_MSG = [{"role": "user", "content": "hi"}]


def _ev(
    *,
    stage: PipelineStage,
    request_id: str = "req-1",
    messages: list | None = _DEFAULT_MSG,
    metadata: dict | None = None,
    headers: dict | None = None,
    model: str = "gpt-4o",
    provider: str = "openai",
) -> PipelineEvent:
    """Build a PipelineEvent. ``messages`` defaults to a single user
    message; pass ``messages=None`` (or ``messages=[]``) to test the
    None/empty handling paths."""
    return PipelineEvent(
        stage=stage,
        operation="sdk.request",
        request_id=request_id,
        provider=provider,
        model=model,
        messages=messages,
        headers=headers,
        metadata=metadata or {},
    )


@pytest.fixture
def ext(tmp_path):
    s = SQLiteStorage(str(tmp_path / "test.db"))
    yield MessageCaptureExtension(s, max_record_bytes=1024)
    s.close()


# ---------------------------------------------------------------------------
# Two-stage happy path
# ---------------------------------------------------------------------------
def test_captures_before_and_after(ext):
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.INPUT_RECEIVED,
        metadata={"session_id": "s1", "timestamp": "2025-08-23T10:00:00Z"},
    ))
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.INPUT_COMPRESSED,
        metadata={"session_id": "s1", "timestamp": "2025-08-23T10:00:01Z"},
        messages=[{"role": "user", "content": "compressed"}],
    ))
    recs = ext._storage.list_records_by_request("req-1")
    assert len(recs) == 2
    stages = {r.stage for r in recs}
    assert stages == {"before", "after"}


def test_session_id_from_metadata(ext):
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.INPUT_RECEIVED,
        metadata={"session_id": "from-meta"},
    ))
    rec = ext._storage.list_records_by_request("req-1")[0]
    assert rec.session_id == "from-meta"


def test_session_id_from_headers_when_metadata_missing(ext):
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.INPUT_RECEIVED,
        headers={"X-Session-Id": "from-header"},
    ))
    rec = ext._storage.list_records_by_request("req-1")[0]
    assert rec.session_id == "from-header"


def test_session_id_from_inline_pseudo_message(ext):
    msgs = [{"role": "user", "content": "hi"}, {"role": "_meta", "metadata": {"session_id": "inline"}}]
    ext.on_pipeline_event(_ev(stage=PipelineStage.INPUT_RECEIVED, messages=msgs))
    rec = ext._storage.list_records_by_request("req-1")[0]
    assert rec.session_id == "inline"


def test_session_id_missing_uses_placeholder(ext):
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.INPUT_RECEIVED,
        request_id="req-orphan-12345678",
        metadata={"timestamp": "2025-08-23T10:00:00Z"},
    ))
    rec = ext._storage.list_records_by_request("req-orphan-12345678")[0]
    assert rec.session_id.startswith(NO_SESSION_PREFIX)
    assert "req-orph" in rec.session_id
    assert "2025-08-23T10:00:00Z" in rec.session_id


# ---------------------------------------------------------------------------
# PRE_SEND is intentionally NOT captured
# ---------------------------------------------------------------------------
def test_pre_send_not_captured(ext):
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.PRE_SEND,
        metadata={"session_id": "should-not-capture"},
    ))
    assert ext._storage.list_records_by_request("req-1") == []


# ---------------------------------------------------------------------------
# Oversize truncation
# ---------------------------------------------------------------------------
def test_oversize_record_is_truncated(ext):
    big_msg = [{"role": "user", "content": "x" * 5000}]
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.INPUT_RECEIVED,
        messages=big_msg,
        metadata={"session_id": "s1"},
    ))
    rec = ext._storage.list_records_by_request("req-1")[0]
    assert '"_truncated":true' in rec.messages_json
    assert rec.tokens == 0  # token count skipped when truncated


# ---------------------------------------------------------------------------
# Failure paths must not break the pipeline
# ---------------------------------------------------------------------------
def test_storage_failure_does_not_raise(tmp_path):
    class FailingStorage:
        def save_message_record(self, rec):
            raise RuntimeError("boom!")
        def list_sessions(self, **kw): return []
        def list_records_by_session(self, sid, **kw): return []
        def list_records_by_request(self, rid): return []

    ext = MessageCaptureExtension(FailingStorage(), max_record_bytes=1024)
    result = ext.on_pipeline_event(_ev(
        stage=PipelineStage.INPUT_RECEIVED,
        metadata={"session_id": "s1"},
    ))
    # Returns the event unchanged (pass-through)
    assert result is not None
    assert result.stage == PipelineStage.INPUT_RECEIVED


def test_messages_none_does_not_crash(ext):
    ext.on_pipeline_event(_ev(stage=PipelineStage.INPUT_RECEIVED, messages=None))
    assert ext._storage.list_records_by_request("req-1") == []


def test_out_of_order_after_does_not_crash(ext):
    """INPUT_COMPRESSED without preceding INPUT_RECEIVED must not crash."""
    ext.on_pipeline_event(_ev(stage=PipelineStage.INPUT_COMPRESSED))
    assert ext._storage.list_records_by_request("req-1") == []


def test_extension_returns_event_unchanged(ext):
    ev = _ev(stage=PipelineStage.INPUT_RECEIVED, metadata={"session_id": "s1"})
    result = ext.on_pipeline_event(ev)
    assert result is ev


# ---------------------------------------------------------------------------
# _pending memory management
# ---------------------------------------------------------------------------
def test_pending_cleaned_after_compressed(ext):
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.INPUT_RECEIVED,
        metadata={"session_id": "s1"},
    ))
    assert "req-1" in ext._pending
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.INPUT_COMPRESSED,
        metadata={"session_id": "s1"},
    ))
    assert "req-1" not in ext._pending


def test_pending_size_cap_drops_oldest(tmp_path):
    """When max_pending is exceeded, oldest entries are dropped (FIFO)."""
    s = SQLiteStorage(str(tmp_path / "test.db"))
    ext = MessageCaptureExtension(s, max_record_bytes=1024, max_pending=3)
    for i in range(5):
        ext.on_pipeline_event(_ev(
            stage=PipelineStage.INPUT_RECEIVED,
            request_id=f"req-{i}",
            metadata={"session_id": f"s{i}"},
        ))
    assert len(ext._pending) == 3
    # Oldest two (req-0, req-1) should have been dropped
    assert "req-0" not in ext._pending
    assert "req-1" not in ext._pending
    assert "req-4" in ext._pending
    s.close()


# ---------------------------------------------------------------------------
# Multiple sessions / requests
# ---------------------------------------------------------------------------
def test_multiple_requests_isolated(ext):
    for i in range(3):
        ext.on_pipeline_event(_ev(
            stage=PipelineStage.INPUT_RECEIVED,
            request_id=f"req-{i}",
            metadata={"session_id": f"s-{i}"},
        ))
    sessions = ext._storage.list_sessions()
    assert {s["session_id"] for s in sessions} == {"s-0", "s-1", "s-2"}


def test_each_request_has_two_records(ext):
    """Two stages × one request = 2 records."""
    ext.on_pipeline_event(_ev(stage=PipelineStage.INPUT_RECEIVED,
                              metadata={"session_id": "s1"}))
    ext.on_pipeline_event(_ev(stage=PipelineStage.INPUT_COMPRESSED,
                              messages=[{"role": "user", "content": "after"}],
                              metadata={"session_id": "s1"}))
    recs = ext._storage.list_records_by_request("req-1")
    assert len(recs) == 2
    assert {r.stage for r in recs} == {"before", "after"}
