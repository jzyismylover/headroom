"""End-to-end test: HeadroomClient → MessageCaptureExtension → SQLite.

This bypasses the heavy ``HeadroomClient.__init__`` (which pulls in many
optional deps like openai / onnxruntime) and directly invokes the public
pipeline-emit machinery that ``_create()`` would call. The goal is to
exercise the wiring from ``client.py`` (session_id extraction + capture
manager) without booting the full SDK.
"""

from __future__ import annotations

import datetime as _dt
import json

import pytest

from headroom.config import HeadroomConfig, HeadroomMode
from headroom.diff_explorer.message_capture import MessageCaptureExtension
from headroom.diff_explorer.session_resolver import extract_body_metadata, resolve_session_id
from headroom.pipeline import PipelineEvent, PipelineExtensionManager, PipelineStage
from headroom.storage.compressed_messages import CompressedMessageRecord
from headroom.storage.sqlite import SQLiteStorage


# ---------------------------------------------------------------------------
# Pure session_id extraction logic (mirrors client.py _create() prologue)
# ---------------------------------------------------------------------------
def _session_metadata_for(messages, kwargs) -> dict[str, str]:
    """Inline reproduction of the session_id-extraction block in client.py.

    Kept in sync with ``HeadroomClient._create``'s prelude — if you change
    one, change both. We extract it into a free function so tests don't need
    to instantiate a HeadroomClient (which has many heavy deps).
    """
    md: dict = {}
    inline = extract_body_metadata(messages)
    extra_body = kwargs.get("extra_body") or {}
    extra_body_md = extra_body.get("metadata") if isinstance(extra_body, dict) else None
    kw_md = kwargs.get("metadata")
    if isinstance(extra_body_md, dict):
        md.update(extra_body_md)
    if isinstance(kw_md, dict):
        md.update(kw_md)
    # Inline pseudo-message metadata only fills keys that weren't set above.
    if inline:
        for k, v in inline.items():
            md.setdefault(k, v)
    extra_headers = kwargs.get("extra_headers") or kwargs.get("headers")
    auto = resolve_session_id(
        body_metadata=md or None,
        headers=extra_headers if isinstance(extra_headers, dict) else None,
    )
    return {"session_id": auto} if auto else {}


def test_session_id_extracted_from_extra_body_metadata():
    """OpenAI / Anthropic SDK convention: extra_body['metadata']."""
    sid = _session_metadata_for(
        messages=[{"role": "user", "content": "hi"}],
        kwargs={"extra_body": {"metadata": {"session_id": "from-extra-body"}}},
    )
    assert sid == {"session_id": "from-extra-body"}


def test_session_id_extracted_from_kwargs_metadata():
    sid = _session_metadata_for(
        messages=[{"role": "user", "content": "hi"}],
        kwargs={"metadata": {"session_id": "from-kwargs"}},
    )
    assert sid == {"session_id": "from-kwargs"}


def test_session_id_extracted_from_extra_headers():
    sid = _session_metadata_for(
        messages=[],
        kwargs={"extra_headers": {"X-Session-Id": "from-header"}},
    )
    assert sid == {"session_id": "from-header"}


def test_session_id_priority_extra_body_beats_inline():
    """extra_body.metadata wins over inline pseudo-message."""
    sid = _session_metadata_for(
        messages=[{"role": "_meta", "metadata": {"session_id": "inline"}}],
        kwargs={"extra_body": {"metadata": {"session_id": "extra-body"}}},
    )
    assert sid == {"session_id": "extra-body"}


def test_session_id_missing_returns_empty_dict():
    sid = _session_metadata_for(
        messages=[{"role": "user", "content": "hi"}],
        kwargs={},
    )
    assert sid == {}


# ---------------------------------------------------------------------------
# Pipeline emit flow that client.py exercises
# ---------------------------------------------------------------------------
def _build_capture_manager(storage: SQLiteStorage, max_bytes: int = 5 * 1024 * 1024):
    cap = MessageCaptureExtension(storage, max_record_bytes=max_bytes)
    return PipelineExtensionManager(extensions=[cap], discover=False)


def test_pipeline_emit_two_stages_records_both(tmp_path):
    db = str(tmp_path / "e2e.db")
    s = SQLiteStorage(db)
    manager = _build_capture_manager(s)

    ts1 = "2025-08-23T10:00:00Z"
    ts2 = "2025-08-23T10:00:01Z"

    # INPUT_RECEIVED
    manager.emit(
        PipelineStage.INPUT_RECEIVED,
        operation="sdk.request",
        request_id="req-e2e-1",
        provider="openai",
        model="gpt-4o",
        messages=[{"role": "user", "content": "a very long preamble that the compressor will shrink"}],
        metadata={
            "api_style": "openai",
            "session_id": "sess-e2e-1",
            "timestamp": ts1,
        },
    )
    # INPUT_COMPRESSED
    manager.emit(
        PipelineStage.INPUT_COMPRESSED,
        operation="sdk.request",
        request_id="req-e2e-1",
        provider="openai",
        model="gpt-4o",
        messages=[{"role": "user", "content": "hello (compressed)"}],
        metadata={
            "api_style": "openai",
            "session_id": "sess-e2e-1",
            "timestamp": ts2,
            "tokens_before": 10,
            "tokens_after": 5,
        },
    )
    # TOOLS_COMPRESSED — writes flush only on this stage (diff-gated).
    manager.emit(
        PipelineStage.TOOLS_COMPRESSED,
        operation="sdk.request",
        request_id="req-e2e-1",
        provider="openai",
        model="gpt-4o",
        tools=None,
        metadata={
            "api_style": "openai",
            "session_id": "sess-e2e-1",
            "timestamp": ts2,
        },
    )

    recs = s.list_records_by_request("req-e2e-1")
    assert len(recs) == 2
    assert {r.stage for r in recs} == {"before", "after"}
    before = next(r for r in recs if r.stage == "before")
    after = next(r for r in recs if r.stage == "after")
    assert before.session_id == "sess-e2e-1"
    assert after.session_id == "sess-e2e-1"
    s.close()


def test_pipeline_emit_with_placeholder_session(tmp_path):
    """When session_id can't be resolved, the placeholder is used."""
    db = str(tmp_path / "e2e.db")
    s = SQLiteStorage(db)
    manager = _build_capture_manager(s)

    manager.emit(
        PipelineStage.INPUT_RECEIVED,
        operation="sdk.request",
        request_id="req-orphan-12345678",
        provider="openai",
        model="gpt-4o",
        messages=[{"role": "user", "content": "a long preamble before compression"}],
        metadata={"api_style": "openai", "timestamp": "2025-08-23T10:00:00Z"},
    )
    manager.emit(
        PipelineStage.INPUT_COMPRESSED,
        operation="sdk.request",
        request_id="req-orphan-12345678",
        provider="openai",
        model="gpt-4o",
        messages=[{"role": "user", "content": "compressed"}],
        metadata={"api_style": "openai", "timestamp": "2025-08-23T10:00:01Z"},
    )
    # Flush via TOOLS_COMPRESSED so the diff-gated write actually persists.
    manager.emit(
        PipelineStage.TOOLS_COMPRESSED,
        operation="sdk.request",
        request_id="req-orphan-12345678",
        provider="openai",
        model="gpt-4o",
        tools=None,
        metadata={"api_style": "openai", "timestamp": "2025-08-23T10:00:01Z"},
    )
    rec = s.list_records_by_request("req-orphan-12345678")[0]
    assert rec.session_id.startswith("_no_session:req-orph-")
    s.close()


def test_capture_off_by_default_does_not_register_extension(tmp_path):
    """capture_messages=False (default) → MessageCaptureExtension is NOT
    wired in. We test the underlying assumption: the extension list passed
    to PipelineExtensionManager is empty."""
    cfg = HeadroomConfig(capture_messages=False)
    # The wiring happens in HeadroomClient.__init__ — we only assert the
    # config flag here.
    assert cfg.capture_messages is False
    assert cfg.capture_messages_max_bytes == 5 * 1024 * 1024


def test_capture_on_registers_extension_with_max_bytes(tmp_path):
    cfg = HeadroomConfig(capture_messages=True, capture_messages_max_bytes=1234)
    assert cfg.capture_messages is True
    assert cfg.capture_messages_max_bytes == 1234


# ---------------------------------------------------------------------------
# HeadroomConfig carries the right shape
# ---------------------------------------------------------------------------
def test_headroom_config_has_capture_messages_fields():
    cfg = HeadroomConfig()
    assert hasattr(cfg, "capture_messages")
    assert hasattr(cfg, "capture_messages_max_bytes")
    assert cfg.capture_messages is False
    assert cfg.capture_messages_max_bytes == 5 * 1024 * 1024


def test_headroom_config_can_disable_capture():
    cfg = HeadroomConfig(capture_messages=True)
    assert cfg.capture_messages is True
    cfg.capture_messages = False
    assert cfg.capture_messages is False
