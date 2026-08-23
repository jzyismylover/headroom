"""Service layer for the Diff Explorer API.

Thin wrappers around the new ``Storage`` methods (``save_message_record`` /
``list_sessions`` / ``list_records_by_session`` / ``list_records_by_request``)
that turn raw records into the JSON-serializable dicts the fastapi router
returns. Also handles record lookup with the two-stage grouping needed by
``/api/diff-explorer/requests/{request_id}``.

All errors are caught here — the router is expected to be fail-open (return
200 with empty data) so a transient storage hiccup never breaks the dashboard.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..storage.base import Storage
from ..storage.compressed_messages import CompressedMessageRecord

logger = logging.getLogger(__name__)

# Cap list responses to avoid accidentally OOM-ing the proxy on a stale
# dashboard tab. The router may tighten further; this is the hard ceiling.
MAX_LIMIT = 500
DEFAULT_LIMIT = 100


def _safe_parse_messages(messages_json: str) -> tuple[Any, bool]:
    """Return ``(parsed, ok)``.

    On JSON failure we return ``(None, False)`` so the caller can emit a
    placeholder rather than 500. We strip whitespace defensively.
    """
    if not messages_json:
        return [], True
    try:
        return json.loads(messages_json), True
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Diff Explorer: failed to parse messages_json: %s", exc)
        return None, False


def _record_to_stage_payload(
    record: CompressedMessageRecord | None,
) -> dict[str, Any] | None:
    """Convert a single ``CompressedMessageRecord`` to the API stage payload.

    Returns ``None`` when the record is missing (caller renders an empty pane).
    """
    if record is None:
        return None

    parsed, ok = _safe_parse_messages(record.messages_json)
    if not ok:
        return {
            "messages": [],
            "tokens": record.tokens,
            "byte_size": record.byte_size,
            "truncated": True,
            "parse_error": "stored JSON could not be parsed",
        }

    truncated = False
    if isinstance(parsed, dict) and parsed.get("_truncated") is True:
        truncated = True
        parsed = []  # surface as empty messages with the truncated flag set

    return {
        "messages": parsed,
        "tokens": record.tokens,
        "byte_size": record.byte_size,
        "truncated": truncated,
    }


def list_sessions_for_api(
    storage: Storage,
    *,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Return ``{"total": N, "items": [...]}``.

    The total is approximated by re-querying without limit/offset so the
    dashboard can show correct pagination metadata. SQLiteStorage.list_sessions
    is a single GROUP BY — cheap enough to do twice.
    """
    try:
        items = storage.list_sessions(limit=min(limit, MAX_LIMIT), offset=offset)
        # Re-query for total (without offset)
        total_items = storage.list_sessions(limit=MAX_LIMIT, offset=0)
        return {"total": len(total_items), "items": items}
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.warning("Diff Explorer list_sessions failed: %s", exc)
        return {"total": 0, "items": []}


def list_session_requests_for_api(
    storage: Storage,
    session_id: str,
    *,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Return all records for a session, newest-first.

    Aggregates one entry per ``request_id`` (combining before / after stages
    into a single row that mirrors the existing ``RequestMetrics`` shape).
    """
    try:
        records = storage.list_records_by_session(
            session_id, limit=min(limit, MAX_LIMIT), offset=offset
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Diff Explorer list_records_by_session failed: %s", exc)
        return {"total": 0, "items": []}

    items: list[dict[str, Any]] = []
    for rec in records:
        items.append(
            {
                "record_id": rec.record_id,
                "request_id": rec.request_id,
                "session_id": rec.session_id,
                "stage": rec.stage,
                "model": rec.model,
                "api_style": rec.api_style,
                "timestamp": rec.created_at.isoformat() + "Z",
                "tokens": rec.tokens,
                "byte_size": rec.byte_size,
                "messages_hash": rec.messages_hash,
            }
        )
    return {"total": len(items), "items": items}


def get_request_diff_for_api(
    storage: Storage,
    request_id: str,
) -> dict[str, Any]:
    """Return the before / after payload for a single request.

    Always returns 200 with possibly-null stages; the dashboard treats null
    as "stage was not captured for this request".
    """
    try:
        records = storage.list_records_by_request(request_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Diff Explorer list_records_by_request failed: %s", exc)
        return {
            "request_id": request_id,
            "session_id": "",
            "model": "",
            "provider": "",
            "api_style": "openai",
            "stages": {"before": None, "after": None},
            "transforms_applied": [],
            "error": str(exc),
        }

    if not records:
        return {
            "request_id": request_id,
            "session_id": "",
            "model": "",
            "provider": "",
            "api_style": "openai",
            "stages": {"before": None, "after": None},
            "transforms_applied": [],
        }

    before_rec = next((r for r in records if r.stage == "before"), None)
    after_rec = next((r for r in records if r.stage == "after"), None)
    anchor = before_rec or after_rec

    return {
        "request_id": request_id,
        "session_id": anchor.session_id if anchor else "",
        "model": anchor.model if anchor else "",
        "provider": anchor.provider if anchor else "",
        "api_style": anchor.api_style if anchor else "openai",
        "stages": {
            "before": _record_to_stage_payload(before_rec),
            "after": _record_to_stage_payload(after_rec),
        },
        "transforms_applied": [],
    }


def health_for_api(storage: Storage) -> dict[str, Any]:
    """Return a tiny health probe. Touches ``list_sessions`` to verify the
    SQLite file is readable."""
    try:
        storage.list_sessions(limit=1, offset=0)
        return {"ok": True, "storage_url": getattr(storage, "db_path", getattr(storage, "file_path", "unknown"))}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc)}
