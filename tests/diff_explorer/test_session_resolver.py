"""Unit tests for headroom.diff_explorer.session_resolver."""

from __future__ import annotations

import datetime as _dt

from headroom.diff_explorer.session_resolver import (
    NO_SESSION_PREFIX,
    SESSION_BODY_KEYS,
    SESSION_HEADER_KEYS,
    extract_body_metadata,
    placeholder_session_id,
    resolve_session_id,
)


# ---------------------------------------------------------------------------
# resolve_session_id
# ---------------------------------------------------------------------------
def test_body_metadata_session_id_wins():
    assert resolve_session_id(body_metadata={"session_id": "sess-abc"}) == "sess-abc"


def test_body_metadata_user_id_fallback():
    assert resolve_session_id(body_metadata={"user_id": "u-123"}) == "u-123"


def test_body_metadata_codex_session_id():
    assert resolve_session_id(body_metadata={"codex_session_id": "cx-1"}) == "cx-1"


def test_body_metadata_thread_id():
    assert resolve_session_id(body_metadata={"thread_id": "t-1"}) == "t-1"


def test_body_metadata_priority_order():
    """session_id beats codex_session_id beats user_id beats thread_id."""
    md = {"thread_id": "t", "user_id": "u", "codex_session_id": "cx", "session_id": "s"}
    assert resolve_session_id(body_metadata=md) == "s"


def test_auto_sentinel_skipped():
    """``"auto"`` is treated as "no session yet" by some SDKs."""
    assert resolve_session_id(body_metadata={"session_id": "auto"}) is None
    assert resolve_session_id(body_metadata={"session_id": "AUTO"}) is None


def test_whitespace_only_skipped():
    assert resolve_session_id(body_metadata={"session_id": "   "}) is None
    assert resolve_session_id(body_metadata={"session_id": ""}) is None


def test_whitespace_stripped():
    assert resolve_session_id(body_metadata={"session_id": "  trim-me  "}) == "trim-me"


def test_non_string_body_value_skipped():
    """Defensive: non-string metadata values should not crash."""
    assert resolve_session_id(body_metadata={"session_id": 123}) is None
    assert resolve_session_id(body_metadata={"user_id": None}) is None
    assert resolve_session_id(body_metadata={"session_id": ["array"]}) is None


# --- Headers ---
def test_header_x_session_id():
    assert resolve_session_id(headers={"X-Session-Id": "h-789"}) == "h-789"


def test_header_x_session_id_lowercase():
    assert resolve_session_id(headers={"x-session-id": "lc-456"}) == "lc-456"


def test_header_anthropic_request_id():
    assert resolve_session_id(headers={"Request-Id": "req-abc"}) == "req-abc"


def test_header_openai_x_request_id():
    assert resolve_session_id(headers={"x-request-id": "xreq-abc"}) == "xreq-abc"


def test_header_priority_order():
    headers = {"x-request-id": "last", "x-session-id": "first"}
    assert resolve_session_id(headers=headers) == "first"


def test_header_case_insensitive():
    """Headers are normalized to lowercase before lookup."""
    headers = {"X-SESSION-ID": "upper-mixed"}
    assert resolve_session_id(headers=headers) == "upper-mixed"


def test_body_beats_header():
    """When both body metadata and headers have candidates, body wins."""
    sid = resolve_session_id(
        body_metadata={"session_id": "body-wins"},
        headers={"X-Session-Id": "header-loses"},
    )
    assert sid == "body-wins"


def test_no_input_returns_none():
    assert resolve_session_id() is None


def test_none_inputs_return_none():
    assert resolve_session_id(headers=None, body_metadata=None) is None


def test_empty_dicts_return_none():
    assert resolve_session_id(headers={}, body_metadata={}) is None


# ---------------------------------------------------------------------------
# extract_body_metadata
# ---------------------------------------------------------------------------
def test_extract_returns_empty_for_none():
    assert extract_body_metadata(None) == {}


def test_extract_returns_empty_for_empty_list():
    assert extract_body_metadata([]) == {}


def test_extract_returns_empty_when_no_metadata():
    assert extract_body_metadata([{"role": "user", "content": "hi"}]) == {}


def test_extract_inline_pseudo_message():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "_meta", "metadata": {"session_id": "inline"}},
    ]
    assert extract_body_metadata(msgs) == {"session_id": "inline"}


def test_extract_top_level_metadata():
    msgs = [{"role": "user", "content": "hi", "metadata": {"k": "v"}}]
    assert extract_body_metadata(msgs) == {"k": "v"}


def test_extract_skips_malformed_entries():
    """Non-dict or non-dict-metadata entries must not crash."""
    msgs = [None, "string", {"metadata": "not-a-dict"}, {"no_metadata_field": True}]
    assert extract_body_metadata(msgs) == {}


def test_extract_picks_last_metadata():
    """When multiple entries have metadata, the last one wins."""
    msgs = [
        {"role": "user", "metadata": {"k": "first"}},
        {"role": "user", "metadata": {"k": "last"}},
    ]
    assert extract_body_metadata(msgs) == {"k": "last"}


# ---------------------------------------------------------------------------
# placeholder_session_id
# ---------------------------------------------------------------------------
def test_placeholder_format():
    rid = "a1b2c3d4-1234-5678-9012-abcdef123456"
    ts = _dt.datetime(2025, 8, 23, 10, 24, 55)
    assert placeholder_session_id(rid, ts) == "_no_session:a1b2c3d4-2025-08-23T10:24:55Z"


def test_placeholder_uses_request_id_first_8_chars():
    rid = "abcdefgh-extra"
    ts = _dt.datetime(2025, 1, 1, 0, 0, 0)
    assert placeholder_session_id(rid, ts) == "_no_session:abcdefgh-2025-01-01T00:00:00Z"


def test_placeholder_handles_short_request_id():
    assert placeholder_session_id("abc", _dt.datetime(2025, 1, 1)) == "_no_session:abc-2025-01-01T00:00:00Z"


def test_placeholder_handles_empty_request_id():
    assert placeholder_session_id("", _dt.datetime(2025, 1, 1)).startswith("_no_session:unknown-")


def test_placeholder_handles_none_timestamp():
    result = placeholder_session_id("abcdefgh", None)
    assert result.startswith("_no_session:abcdefgh-unknown")


def test_placeholder_prefix_constant():
    """The NO_SESSION_PREFIX must match what we document."""
    assert NO_SESSION_PREFIX == "_no_session:"
    assert all(p.startswith(NO_SESSION_PREFIX) or p != "_no_session:"
               for p in (placeholder_session_id("abcdefgh", _dt.datetime(2025, 1, 1)),))


# ---------------------------------------------------------------------------
# Constants sanity
# ---------------------------------------------------------------------------
def test_body_keys_priority_order():
    """session_id must come first in SESSION_BODY_KEYS."""
    assert SESSION_BODY_KEYS[0] == "session_id"
    assert "codex_session_id" in SESSION_BODY_KEYS
    assert "user_id" in SESSION_BODY_KEYS
    assert "thread_id" in SESSION_BODY_KEYS


def test_header_keys_priority_order():
    """x-session-id must come first in SESSION_HEADER_KEYS."""
    assert SESSION_HEADER_KEYS[0] == "x-session-id"
    assert "x-conversation-id" in SESSION_HEADER_KEYS
    assert "x-thread-id" in SESSION_HEADER_KEYS
    assert "x-codex-session-id" in SESSION_HEADER_KEYS
    assert "request-id" in SESSION_HEADER_KEYS
    assert "x-request-id" in SESSION_HEADER_KEYS
