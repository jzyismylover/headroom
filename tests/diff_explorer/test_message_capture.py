"""Unit tests for MessageCaptureExtension — two-stage capture + edge cases."""

from __future__ import annotations

import datetime as _dt
import json

import pytest

from headroom.diff_explorer.message_capture import MessageCaptureExtension
from headroom.diff_explorer.session_resolver import NO_SESSION_PREFIX
from headroom.pipeline import PipelineEvent, PipelineStage
from headroom.storage.compressed_messages import CompressedMessageRecord
from headroom.storage.sqlite import SQLiteStorage


_DEFAULT_MSG = [
    {"role": "user", "content": "a much longer preamble that outweighs the compressed after message"}
]


def _ev(
    *,
    stage: PipelineStage,
    request_id: str = "req-1",
    messages: list | None = _DEFAULT_MSG,
    tools: list | None = None,
    metadata: dict | None = None,
    headers: dict | None = None,
    model: str = "gpt-4o",
    provider: str = "openai",
) -> PipelineEvent:
    """Build a PipelineEvent. ``messages`` defaults to a single user
    message; pass ``messages=None`` (or ``messages=[]``) to test the
    None/empty handling paths. ``tools`` defaults to None; pass a list of
    tool schemas to exercise the tools diff path."""
    return PipelineEvent(
        stage=stage,
        operation="sdk.request",
        request_id=request_id,
        provider=provider,
        model=model,
        messages=messages,
        tools=tools,
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
    # Diff-gated writes are flushed on TOOLS_COMPRESSED.
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.TOOLS_COMPRESSED,
        metadata={"session_id": "s1", "timestamp": "2025-08-23T10:00:01Z"},
        tools=None,
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
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.INPUT_COMPRESSED,
        messages=[{"role": "user", "content": "compressed"}],
        metadata={"session_id": "from-meta"},
    ))
    # Flush on TOOLS_COMPRESSED (no tools carried = single tools-equal emit).
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.TOOLS_COMPRESSED,
        metadata={"session_id": "from-meta"},
        tools=None,
    ))
    rec = ext._storage.list_records_by_request("req-1")[0]
    assert rec.session_id == "from-meta"


def test_session_id_from_headers_when_metadata_missing(ext):
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.INPUT_RECEIVED,
        headers={"X-Session-Id": "from-header"},
    ))
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.INPUT_COMPRESSED,
        messages=[{"role": "user", "content": "compressed"}],
        headers={"X-Session-Id": "from-header"},
    ))
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.TOOLS_COMPRESSED,
        headers={"X-Session-Id": "from-header"},
        tools=None,
    ))
    rec = ext._storage.list_records_by_request("req-1")[0]
    assert rec.session_id == "from-header"


def test_session_id_from_inline_pseudo_message(ext):
    msgs = [{"role": "user", "content": "hi"}, {"role": "_meta", "metadata": {"session_id": "inline"}}]
    ext.on_pipeline_event(_ev(stage=PipelineStage.INPUT_RECEIVED, messages=msgs))
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.INPUT_COMPRESSED,
        messages=[{"role": "user", "content": "compressed"}],
    ))
    ext.on_pipeline_event(_ev(stage=PipelineStage.TOOLS_COMPRESSED, messages=msgs, tools=None))
    rec = ext._storage.list_records_by_request("req-1")[0]
    assert rec.session_id == "inline"


def test_session_id_missing_uses_placeholder(ext):
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.INPUT_RECEIVED,
        request_id="req-orphan-12345678",
        metadata={"timestamp": "2025-08-23T10:00:00Z"},
    ))
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.INPUT_COMPRESSED,
        request_id="req-orphan-12345678",
        messages=[{"role": "user", "content": "compressed"}],
        metadata={"timestamp": "2025-08-23T10:00:00Z"},
    ))
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.TOOLS_COMPRESSED,
        request_id="req-orphan-12345678",
        metadata={"timestamp": "2025-08-23T10:00:00Z"},
        tools=None,
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
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.INPUT_COMPRESSED,
        messages=[{"role": "user", "content": "compressed"}],
        metadata={"session_id": "s1"},
    ))
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.TOOLS_COMPRESSED,
        messages=[{"role": "user", "content": "compressed"}],
        metadata={"session_id": "s1"},
        tools=None,
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
def test_pending_cleaned_after_tools_compressed(ext):
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.INPUT_RECEIVED,
        metadata={"session_id": "s1"},
    ))
    assert "req-1" in ext._pending
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.INPUT_COMPRESSED,
        metadata={"session_id": "s1"},
    ))
    # INPUT_COMPRESSED no longer flushes — only TOOLS_COMPRESSED does.
    assert "req-1" in ext._pending
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.TOOLS_COMPRESSED,
        metadata={"session_id": "s1"},
        tools=None,
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
        ext.on_pipeline_event(_ev(
            stage=PipelineStage.INPUT_COMPRESSED,
            request_id=f"req-{i}",
            messages=[{"role": "user", "content": f"compressed-{i}"}],
            metadata={"session_id": f"s-{i}"},
        ))
        ext.on_pipeline_event(_ev(
            stage=PipelineStage.TOOLS_COMPRESSED,
            request_id=f"req-{i}",
            metadata={"session_id": f"s-{i}"},
            tools=None,
        ))
    sessions = ext._storage.list_sessions()
    assert {s["session_id"] for s in sessions} == {"s-0", "s-1", "s-2"}


def test_each_request_has_two_records(ext):
    """Two stages × one request = 2 records (after the diff-gated flush)."""
    ext.on_pipeline_event(_ev(stage=PipelineStage.INPUT_RECEIVED,
                              metadata={"session_id": "s1"}))
    ext.on_pipeline_event(_ev(stage=PipelineStage.INPUT_COMPRESSED,
                              messages=[{"role": "user", "content": "after"}],
                              metadata={"session_id": "s1"}))
    ext.on_pipeline_event(_ev(stage=PipelineStage.TOOLS_COMPRESSED,
                              messages=[{"role": "user", "content": "after"}],
                              metadata={"session_id": "s1"},
                              tools=None))
    recs = ext._storage.list_records_by_request("req-1")
    assert len(recs) == 2
    assert {r.stage for r in recs} == {"before", "after"}

# ---------------------------------------------------------------------------
# Diff-gated capture (messages + tools buffered; only writes when something
# changed). Verifies the new TOOLS_COMPRESSED-triggered write contract.
# ---------------------------------------------------------------------------
def test_noop_messages_and_tools_writes_nothing():
    """When messages AND tools are both unchanged, no record is persisted."""
    from unittest.mock import MagicMock

    mock_storage = MagicMock()
    ext = MessageCaptureExtension(mock_storage, max_record_bytes=1024)

    same_messages = [{"role": "user", "content": "hi"}]
    same_tools = [{"type": "function", "function": {"name": "echo"}}]

    ext.on_pipeline_event(_ev(
        stage=PipelineStage.INPUT_RECEIVED,
        messages=same_messages,
        tools=same_tools,
        metadata={"session_id": "s-noop"},
    ))
    # INPUT_COMPRESSED carries the same payload (no-op).
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.INPUT_COMPRESSED,
        messages=same_messages,
        metadata={"session_id": "s-noop"},
    ))
    # TOOLS_COMPRESSED carries the same tools (no-op).
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.TOOLS_COMPRESSED,
        messages=same_messages,
        tools=same_tools,
        metadata={"session_id": "s-noop"},
    ))

    mock_storage.save_message_record.assert_not_called()


def test_byte_equal_skips_despite_token_reduction():
    """A request whose normalized byte size is identical (e.g. only a
    cache_control breakpoint moved) is treated as uncompressed even when the
    pipeline reports a token reduction — this is the phantom-savings case
    that used to show 100% saved while byte_size stayed identical."""
    from unittest.mock import MagicMock

    mock_storage = MagicMock()
    ext = MessageCaptureExtension(mock_storage, max_record_bytes=1024)

    before_msgs = [
        {"role": "user", "content": "same content", "cache_control": {"type": "ephemeral"}}
    ]
    after_msgs = [{"role": "user", "content": "same content"}]

    ext.on_pipeline_event(_ev(
        stage=PipelineStage.INPUT_RECEIVED,
        messages=before_msgs,
        metadata={"session_id": "s-no-save"},
    ))
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.INPUT_COMPRESSED,
        messages=after_msgs,
        metadata={
            "session_id": "s-no-save",
            "tokens_before": 100,
            "tokens_after": 3,  # pipeline claims a big reduction...
        },
    ))
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.TOOLS_COMPRESSED,
        messages=after_msgs,
        tools=None,
        metadata={"session_id": "s-no-save"},
    ))

    # ...but the normalized bytes are identical, so nothing is persisted.
    mock_storage.save_message_record.assert_not_called()


def test_token_metadata_growth_skips():
    """tokens_after > tokens_before (compression made the request bigger)
    is also not a compression."""
    from unittest.mock import MagicMock

    mock_storage = MagicMock()
    ext = MessageCaptureExtension(mock_storage, max_record_bytes=1024)

    before_msgs = [{"role": "user", "content": "short"}]
    after_msgs = [{"role": "user", "content": "short but a much longer tail added"}]

    ext.on_pipeline_event(_ev(
        stage=PipelineStage.INPUT_RECEIVED,
        messages=before_msgs,
        metadata={"session_id": "s-growth"},
    ))
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.INPUT_COMPRESSED,
        messages=after_msgs,
        metadata={
            "session_id": "s-growth",
            "tokens_before": 10,
            "tokens_after": 40,
        },
    ))
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.TOOLS_COMPRESSED,
        messages=after_msgs,
        tools=None,
        metadata={"session_id": "s-growth"},
    ))

    mock_storage.save_message_record.assert_not_called()


def test_token_metadata_reduction_writes_pair():
    """tokens_after < tokens_before is the authoritative "was compressed"
    signal — the pair is persisted."""
    from unittest.mock import MagicMock

    mock_storage = MagicMock()
    ext = MessageCaptureExtension(mock_storage, max_record_bytes=1024)

    before_msgs = [{"role": "user", "content": "a very long preamble that " * 20}]
    after_msgs = [{"role": "user", "content": "compressed"}]

    ext.on_pipeline_event(_ev(
        stage=PipelineStage.INPUT_RECEIVED,
        messages=before_msgs,
        metadata={"session_id": "s-save"},
    ))
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.INPUT_COMPRESSED,
        messages=after_msgs,
        metadata={
            "session_id": "s-save",
            "tokens_before": 200,
            "tokens_after": 40,
        },
    ))
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.TOOLS_COMPRESSED,
        messages=after_msgs,
        tools=None,
        metadata={"session_id": "s-save"},
    ))

    assert mock_storage.save_message_record.call_count == 2
    saved = [c.args[0] for c in mock_storage.save_message_record.call_args_list]
    assert {s.stage for s in saved} == {"before", "after"}


def test_pipeline_tokens_persisted_into_records():
    """The pipeline-reported tokens (metadata tokens_before/tokens_after) are
    stored on the records even when the extension has no tokenizer bound."""
    from unittest.mock import MagicMock

    mock_storage = MagicMock()
    ext = MessageCaptureExtension(mock_storage, max_record_bytes=1024)

    before_msgs = [{"role": "user", "content": "long preamble"}]
    after_msgs = [{"role": "user", "content": "compressed"}]

    ext.on_pipeline_event(_ev(
        stage=PipelineStage.INPUT_RECEIVED,
        messages=before_msgs,
        metadata={"session_id": "s-tok"},
    ))
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.INPUT_COMPRESSED,
        messages=after_msgs,
        metadata={
            "session_id": "s-tok",
            "tokens_before": 321,
            "tokens_after": 99,
        },
    ))
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.TOOLS_COMPRESSED,
        messages=after_msgs,
        tools=None,
        metadata={"session_id": "s-tok"},
    ))

    saved = [c.args[0] for c in mock_storage.save_message_record.call_args_list]
    by_stage = {s.stage: s for s in saved}
    assert by_stage["before"].tokens == 321
    assert by_stage["after"].tokens == 99


def test_messages_changed_writes_pair_with_consistent_tools():
    """messages changed -> 2 records, tools_json identical on both sides."""
    from unittest.mock import MagicMock

    mock_storage = MagicMock()
    ext = MessageCaptureExtension(mock_storage, max_record_bytes=1024)

    before_msgs = [{"role": "user", "content": "long preamble here"}]
    after_msgs = [{"role": "user", "content": "compressed"}]
    tools = [{"type": "function", "function": {"name": "echo"}}]

    ext.on_pipeline_event(_ev(
        stage=PipelineStage.INPUT_RECEIVED,
        messages=before_msgs,
        tools=tools,
        metadata={"session_id": "s-m"},
    ))
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.INPUT_COMPRESSED,
        messages=after_msgs,
        metadata={"session_id": "s-m"},
    ))
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.TOOLS_COMPRESSED,
        messages=after_msgs,
        tools=tools,
        metadata={"session_id": "s-m"},
    ))

    assert mock_storage.save_message_record.call_count == 2
    saved = [c.args[0] for c in mock_storage.save_message_record.call_args_list]
    stages = sorted(s.stage for s in saved)
    assert stages == ["after", "before"]
    # tools_json identical on both records (because tools didn't change).
    assert saved[0].tools_json == saved[1].tools_json
    # Tools payload parses back to the same list.
    assert json.loads(saved[0].tools_json) == tools
    # Messages payloads are delta snapshots carrying only the changed block,
    # each side in its own form.
    assert json.loads(saved[0].messages_json) == {
        "_delta": True,
        "entries": [{"i": 0, "c": None, "m": before_msgs[0]}],
    }
    assert json.loads(saved[1].messages_json) == {
        "_delta": True,
        "entries": [{"i": 0, "c": None, "m": after_msgs[0]}],
    }
    # transforms_json carries the (empty) compressor attribution.
    assert json.loads(saved[0].transforms_json) == {
        "transforms_applied": [],
        "compressors": [],
    }


def test_delta_blocks_carry_compressor_attribute():
    """A single compressor marker attributes every changed block to it, and
    the resolved display name rides along in transforms_json."""
    from unittest.mock import MagicMock

    mock_storage = MagicMock()
    ext = MessageCaptureExtension(mock_storage, max_record_bytes=1024)

    before_msgs = [
        {"role": "user", "content": "long preamble"},
        {"role": "assistant", "content": "x" * 200},
    ]
    after_msgs = [
        {"role": "user", "content": "long preamble"},
        {"role": "assistant", "content": "y"},
    ]

    ext.on_pipeline_event(_ev(
        stage=PipelineStage.INPUT_RECEIVED,
        messages=before_msgs,
        metadata={"session_id": "s-kompress"},
    ))
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.INPUT_COMPRESSED,
        messages=after_msgs,
        metadata={
            "session_id": "s-kompress",
            "tokens_before": 300,
            "tokens_after": 30,
            "transforms_applied": ["kompress:assistant:0.10"],
        },
    ))
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.TOOLS_COMPRESSED,
        messages=after_msgs,
        tools=None,
        metadata={"session_id": "s-kompress"},
    ))

    assert mock_storage.save_message_record.call_count == 2
    saved = [c.args[0] for c in mock_storage.save_message_record.call_args_list]
    # Only index 1 (the assistant message) changed.
    for rec in saved:
        payload = json.loads(rec.messages_json)
        assert payload["_delta"] is True
        assert len(payload["entries"]) == 1
        assert payload["entries"][0]["i"] == 1
        assert payload["entries"][0]["c"] == "KCompress"
    transforms = json.loads(saved[0].transforms_json)
    assert transforms["compressors"] == ["KCompress"]
    assert transforms["transforms_applied"] == ["kompress:assistant:0.10"]


def test_tools_changed_writes_pair_with_consistent_messages():
    """tools changed -> 2 records, messages_json identical on both sides."""
    from unittest.mock import MagicMock

    mock_storage = MagicMock()
    ext = MessageCaptureExtension(mock_storage, max_record_bytes=1024)

    messages = [{"role": "user", "content": "hi"}]
    before_tools = [
        {"type": "function", "function": {"name": "echo", "description": "x" * 500}},
        {"type": "function", "function": {"name": "noop"}},
    ]
    after_tools = [{"type": "function", "function": {"name": "echo", "description": "x"}}]

    ext.on_pipeline_event(_ev(
        stage=PipelineStage.INPUT_RECEIVED,
        messages=messages,
        tools=before_tools,
        metadata={"session_id": "s-t"},
    ))
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.INPUT_COMPRESSED,
        messages=messages,
        metadata={"session_id": "s-t"},
    ))
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.TOOLS_COMPRESSED,
        messages=messages,
        tools=after_tools,
        metadata={"session_id": "s-t"},
    ))

    assert mock_storage.save_message_record.call_count == 2
    saved = [c.args[0] for c in mock_storage.save_message_record.call_args_list]
    # messages didn't change, so both delta payloads carry empty entries.
    assert json.loads(saved[0].messages_json) == {"_delta": True, "entries": []}
    assert json.loads(saved[1].messages_json) == {"_delta": True, "entries": []}
    # Tools differ between before and after.
    assert json.loads(saved[0].tools_json) == before_tools
    assert json.loads(saved[1].tools_json) == after_tools
    # tools_byte_size matches the actual JSON length.
    for rec in saved:
        assert rec.tools_byte_size == len(rec.tools_json.encode("utf-8"))


def test_only_input_received_no_write_pending_drops_via_cap(tmp_path):
    """If TOOLS_COMPRESSED never arrives, pending entries are NOT written
    to disk — they stay buffered until the cap kicks them out (FIFO).
    """
    from headroom.storage.sqlite import SQLiteStorage

    s = SQLiteStorage(str(tmp_path / "test.db"))
    # Use a higher cap so we can observe FIFO eviction rather than the cap
    # shrinking the buffer to a single entry on each new insert.
    ext = MessageCaptureExtension(s, max_record_bytes=1024, max_pending=3)
    for i in range(5):
        ext.on_pipeline_event(_ev(
            stage=PipelineStage.INPUT_RECEIVED,
            request_id=f"req-{i}",
            messages=[{"role": "user", "content": f"m{i}"}],
            metadata={"session_id": f"s-{i}"},
        ))
    # No TOOLS_COMPRESSED emit. No records should have been written.
    for i in range(5):
        assert s.list_records_by_request(f"req-{i}") == []
    # FIFO: oldest two (req-0, req-1) were evicted to make room for req-2..4.
    assert "req-0" not in ext._pending
    assert "req-1" not in ext._pending
    assert "req-2" in ext._pending
    assert "req-4" in ext._pending
    s.close()


def test_full_bytes_measures_full_context_per_stage():
    """full_bytes holds the honest full-context size (messages + tools) for
    each stage, distinct from the compact delta payload's byte_size."""
    from unittest.mock import MagicMock

    from headroom.diff_explorer.message_capture import _side_bytes

    mock_storage = MagicMock()
    ext = MessageCaptureExtension(mock_storage, max_record_bytes=1024)

    before_msgs = [{"role": "user", "content": "a very long preamble that " * 20}]
    after_msgs = [{"role": "user", "content": "compressed"}]
    before_tools = [{"type": "function", "function": {"name": "echo", "description": "x" * 300}}]
    after_tools = [{"type": "function", "function": {"name": "echo"}}]

    ext.on_pipeline_event(_ev(
        stage=PipelineStage.INPUT_RECEIVED,
        messages=before_msgs,
        tools=before_tools,
        metadata={"session_id": "s-full"},
    ))
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.INPUT_COMPRESSED,
        messages=after_msgs,
        metadata={"session_id": "s-full", "tokens_before": 200, "tokens_after": 40},
    ))
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.TOOLS_COMPRESSED,
        messages=after_msgs,
        tools=after_tools,
        metadata={"session_id": "s-full"},
    ))

    assert mock_storage.save_message_record.call_count == 2
    saved = [c.args[0] for c in mock_storage.save_message_record.call_args_list]
    by_stage = {s.stage: s for s in saved}
    # full_bytes == the normalized full-context size of that side (incl. tools).
    assert by_stage["before"].full_bytes == _side_bytes(before_msgs, before_tools)
    assert by_stage["after"].full_bytes == _side_bytes(after_msgs, after_tools)
    # The full-context size is larger than the compact delta payload stored.
    assert by_stage["before"].full_bytes > by_stage["before"].byte_size
    assert by_stage["after"].full_bytes > by_stage["after"].byte_size


def test_no_tools_in_request_emits_clean_records():
    """Requests without any tools should still write cleanly (tools_json empty)."""
    from unittest.mock import MagicMock

    mock_storage = MagicMock()
    ext = MessageCaptureExtension(mock_storage, max_record_bytes=1024)
    before_msgs = [{"role": "user", "content": "long preamble here"}]
    after_msgs = [{"role": "user", "content": "compressed"}]

    ext.on_pipeline_event(_ev(
        stage=PipelineStage.INPUT_RECEIVED,
        messages=before_msgs,
        tools=None,
        metadata={"session_id": "s-no-tools"},
    ))
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.INPUT_COMPRESSED,
        messages=after_msgs,
        metadata={"session_id": "s-no-tools"},
    ))
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.TOOLS_COMPRESSED,
        messages=after_msgs,
        tools=None,
        metadata={"session_id": "s-no-tools"},
    ))

    assert mock_storage.save_message_record.call_count == 2
    saved = [c.args[0] for c in mock_storage.save_message_record.call_args_list]
    for rec in saved:
        assert rec.tools_json == ""
        assert rec.tools_byte_size == 0
        assert rec.tools_hash == ""


def test_tools_compressed_without_pending_does_not_crash():
    """TOOLS_COMPRESSED arriving with no INPUT_RECEIVED context is a no-op."""
    from unittest.mock import MagicMock

    mock_storage = MagicMock()
    ext = MessageCaptureExtension(mock_storage, max_record_bytes=1024)
    ext.on_pipeline_event(_ev(
        stage=PipelineStage.TOOLS_COMPRESSED,
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "x"}}],
    ))
    mock_storage.save_message_record.assert_not_called()

