"""Resolve session_id from request body metadata / HTTP headers.

The Diff Explorer feature groups requests by session_id. We deliberately
do NOT require the calling agent (codex / claude code / OpenAI / Anthropic SDKs)
to plumb any explicit session_id kwarg — instead we passively read whatever
session identifier the agent already sends. This mirrors the parsing logic
already used by ``headroom.proxy.output_savings_policy._stable_response_identifier``.

Resolution priority (first non-empty wins):
    1. ``body_metadata`` keys, in order:
        - ``session_id``
        - ``codex_session_id``
        - ``user_id``         (claude code uses this as the session anchor)
        - ``thread_id``
    2. ``headers`` keys, case-insensitive:
        - ``x-session-id``
        - ``x-conversation-id``
        - ``x-thread-id``
        - ``x-codex-session-id``
        - ``x-codex-turn-metadata``  (JSON; ``session_id`` / ``thread_id``)
        - ``session-id``            (codex desktop)
        - ``thread-id``             (codex desktop)
        - ``request-id``            (anthropic fallback)
        - ``x-request-id``          (openai fallback)

Returns ``None`` if no candidate is found — the caller decides the fallback
strategy (placeholder string vs. drop the record).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

# Body metadata keys, in priority order. Lookup is case-sensitive (the
# ``metadata`` payload is a JSON object and Python dict semantics apply).
SESSION_BODY_KEYS: tuple[str, ...] = (
    "session_id",
    "codex_session_id",
    "user_id",
    "thread_id",
)

# HTTP header names (lowercase canonical form) to inspect, in priority order.
# Lookup is case-insensitive — we normalize via ``headers.lower()``.
#
# ``x-headroom-session-id`` is the explicit session id proxy.compute_session_id
# reads at the top of its priority list (cache/prefix_tracker.py:1398); the
# Diff Explorer should agree with the proxy on what counts as the canonical
# id when one is set.
SESSION_HEADER_KEYS: tuple[str, ...] = (
    "x-headroom-session-id",
    "x-claude-code-session-id",
    "x-session-id",
    "x-codex-turn-metadata",
    "session-id",
    "x-conversation-id",
    "x-thread-id",
    "thread-id",
    "x-codex-session-id",
    "request-id",
    "x-request-id",
)

# Sentinel prefix used when no session_id can be resolved. The placeholder
# format ``_no_session:<8-char-uid>-<iso8601-ts>`` is documented in
# ``shares/sdd/headroom/方案.md`` section A9.
NO_SESSION_PREFIX = "_no_session:"

# Sentinel value used by some SDKs when the agent has no session yet.
_AUTO_SENTINEL = "auto"


def resolve_session_id(
    *,
    messages: list[dict[str, Any]] | None = None,
    headers: Mapping[str, str] | dict[str, str] | None = None,
    body_metadata: dict[str, Any] | None = None,
) -> str | None:
    """Return the first resolvable session_id, or ``None``.

    Args:
        messages: Optional messages list — currently unused but accepted
            for future extension (e.g., scanning for an inline ``_meta``
            pseudo-message).
        headers: HTTP header mapping (case-insensitive). Any Mapping works;
            string keys are lower-cased before lookup.
        body_metadata: Parsed ``body["metadata"]`` dict (typical OpenAI /
            Anthropic SDK layout). String values are stripped and tested
            against the auto sentinel.
    """
    # 1) body.metadata.* (most stable — codex/claude code always set this)
    md = body_metadata or {}
    for key in SESSION_BODY_KEYS:
        val = md.get(key)
        if isinstance(val, str):
            stripped = val.strip()
            if stripped and stripped.lower() != _AUTO_SENTINEL:
                return stripped
        # Non-string values are silently skipped (defensive — these fields
        # should always be strings in practice).

    # 2) HTTP header (some agents / proxies carry session info in headers)
    if headers:
        lower_headers = {
            (k.lower() if isinstance(k, str) else k): v for k, v in headers.items()
        }
        for hk in SESSION_HEADER_KEYS:
            v = lower_headers.get(hk)
            if not isinstance(v, str) or not v.strip():
                continue
            if hk == "x-codex-turn-metadata":
                sid = _session_id_from_codex_turn_metadata(v)
                if sid:
                    return sid
                continue
            return v.strip()

    return None


def _session_id_from_codex_turn_metadata(raw: str) -> str | None:
    """Extract ``session_id`` (falling back to ``thread_id``) from the JSON
    ``x-codex-turn-metadata`` header that Codex Desktop sends on every request.

    The header value is a JSON object; ``session_id`` is the stable anchor
    that stays identical across all tool_calls within a turn (and across
    turns of the same conversation), so grouping on it makes every request
    of one conversation land in the same dashboard session.
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    for key in ("session_id", "thread_id"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def extract_body_metadata(messages: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Recover a ``metadata`` dict from a messages list, if present.

    Two layouts are supported:
        1. Anthropic / OpenAI standard: ``{"role": "...", "metadata": {...}}``
           (rarely used — most providers don't accept top-level metadata on
           message entries).
        2. Pseudo-message convention: a trailing ``{"role": "_meta",
           "metadata": {...}}`` entry that some custom agents use to forward
           metadata into a request body.

    The pseudo-message form is only honored for dict-shaped entries with a
    ``role == "_meta"`` AND a dict-shaped ``metadata`` value. Any other
    shape is treated as "no metadata here".

    Returns an empty dict if no metadata is found.
    """
    if not messages:
        return {}
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        meta = msg.get("metadata")
        if isinstance(meta, dict):
            return meta
        # Some agents inject an explicit "_meta" pseudo-message.
        if msg.get("role") == "_meta" and isinstance(msg.get("metadata"), dict):
            return msg["metadata"]
    return {}


def placeholder_session_id(request_id: str, timestamp: Any) -> str:
    """Build the ``_no_session:<uid>-<iso8601>`` placeholder string.

    Used by MessageCaptureExtension when no session_id could be resolved
    from the request. Format is documented in
    ``shares/sdd/headroom/方案.md`` section A9.

    Args:
        request_id: The RequestMetrics.request_id. The first 8 characters
            are used as the unique suffix.
        timestamp: A ``datetime`` (naive UTC) or ``None``. ISO8601 with
            ``Z`` suffix when available; the literal ``"unknown"`` when not.
    """
    uid = (request_id or "")[:8] or "unknown"
    if timestamp is None:
        ts = "unknown"
    else:
        try:
            ts = timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
        except AttributeError:
            ts = str(timestamp)
    return f"{NO_SESSION_PREFIX}{uid}-{ts}"
