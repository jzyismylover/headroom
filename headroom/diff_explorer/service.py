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


def _safe_parse_tools(tools_json: str) -> Any:
    """Return the parsed tools list, or ``None`` when the payload is empty
    or unparseable.

    The Diff Explorer contract treats ``None`` (no tools in the request)
    and an unparseable payload as the same surface shape so the frontend
    can render a single "no tools" placeholder without branching.
    """
    if not tools_json:
        return None
    try:
        return json.loads(tools_json)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Diff Explorer: failed to parse tools_json: %s", exc)
        return None


def _record_to_stage_payload(
    record: CompressedMessageRecord | None,
) -> dict[str, Any] | None:
    """Convert a single ``CompressedMessageRecord`` to the API stage payload.

    Returns ``None`` when the record is missing (caller renders an empty pane).
    The tools payload surfaces as ``None`` for legacy records (no tools
    columns) and for unparseable JSON; the frontend treats that as
    "tools section hidden".
    """
    if record is None:
        return None

    parsed, ok = _safe_parse_messages(record.messages_json)
    if not ok:
        return {
            "messages": [],
            "tools": None,
            "tokens": record.tokens,
            "byte_size": record.byte_size,
            "tools_byte_size": record.tools_byte_size,
            "total_byte_size": record.byte_size + record.tools_byte_size,
            "truncated": True,
            "parse_error": "stored JSON could not be parsed",
        }

    truncated = False
    if isinstance(parsed, dict) and parsed.get("_truncated") is True:
        truncated = True
        parsed = []  # surface as empty messages with the truncated flag set

    tools = _safe_parse_tools(record.tools_json)
    return {
        "messages": parsed,
        "tools": tools,
        "tokens": record.tokens,
        "byte_size": record.byte_size,
        "tools_byte_size": record.tools_byte_size,
        "total_byte_size": record.byte_size + record.tools_byte_size,
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


def _flatten_content(content: Any) -> str:
    """Flatten a single message ``content`` into plain text.

    Mirrors the frontend ``extractText`` helper so the timeline preview
    matches what the dashboard would otherwise show for that message.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                btype = block.get("type")
                if btype == "text" and isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif btype == "tool_result":
                    inner = block.get("content")
                    if isinstance(inner, str):
                        parts.append(inner)
                    else:
                        parts.append(json.dumps(inner if inner is not None else block, ensure_ascii=False))
                else:
                    parts.append(json.dumps(block, ensure_ascii=False))
            else:
                parts.append("")
        return "\n".join(parts)
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False)


def _extract_preview(messages: Any, max_chars: int = 500) -> str:
    """Flatten a messages list into a single truncated preview string.

    ``messages`` is the parsed JSON from a record's ``messages_json``. When
    the payload is the oversized ``{"_truncated": true, ...}`` placeholder we
    surface a human-readable marker instead of the placeholder JSON.
    """
    if isinstance(messages, dict) and messages.get("_truncated") is True:
        return "[内容过大已截断]"
    if not isinstance(messages, list):
        return ""
    parts: list[str] = []
    for msg in messages:
        if isinstance(msg, dict):
            parts.append(_flatten_content(msg.get("content")))
        elif isinstance(msg, str):
            parts.append(msg)
        else:
            parts.append(json.dumps(msg, ensure_ascii=False))
    text = "\n".join(p for p in parts if p)
    if len(text) > max_chars:
        return text[:max_chars] + "…"
    return text


def _parse_delta(payload: Any) -> list[dict[str, Any]] | None:
    """Return the ``entries`` list when ``payload`` is a delta snapshot.

    New-format records store ``{"_delta": true, "entries": [...]}`` in
    ``messages_json``; everything else (legacy full-message arrays, the
    ``_truncated`` placeholder) returns ``None`` so callers branch to the
    legacy path.
    """
    if isinstance(payload, dict) and payload.get("_delta") is True:
        entries = payload.get("entries")
        return entries if isinstance(entries, list) else []
    return None


def _message_bytes(msg: Any) -> int:
    """Byte length of a single message's compact JSON (0 for None)."""
    if msg is None:
        return 0
    try:
        return len(
            json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
    except Exception:  # noqa: BLE001 — fail-open
        return 0


def _same_message(b: Any, a: Any) -> bool:
    """Content equality for two legacy full messages (key-order-insensitive)."""
    if b is None or a is None:
        return b is a
    try:
        return json.dumps(b, sort_keys=True, separators=(",", ":")) == \
            json.dumps(a, sort_keys=True, separators=(",", ":"))
    except Exception:  # noqa: BLE001 — fail-open
        return False


def _blocks_from_delta(
    before_entries: list[dict[str, Any]],
    after_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge before/after delta entries into index-keyed diff blocks."""
    by_index: dict[int, dict[str, Any]] = {}
    for side, entries in (("before", before_entries), ("after", after_entries)):
        for e in entries:
            i = e.get("i")
            if not isinstance(i, int):
                continue
            msg = e.get("m")
            entry = by_index.get(i)
            if entry is None:
                entry = {
                    "i": i,
                    "role": "",
                    "compressor": e.get("c"),
                    "before": None,
                    "after": None,
                }
                by_index[i] = entry
            if side == "before":
                entry["before"] = msg
            else:
                entry["after"] = msg
            if not entry["role"] and isinstance(msg, dict):
                entry["role"] = msg.get("role", "")
            if not entry["compressor"] and e.get("c"):
                entry["compressor"] = e.get("c")
    blocks = [by_index[i] for i in sorted(by_index)]
    for block in blocks:
        block["before_bytes"] = _message_bytes(block["before"])
        block["after_bytes"] = _message_bytes(block["after"])
    return blocks


def _blocks_from_legacy(
    before_payload: Any,
    after_payload: Any,
) -> list[dict[str, Any]]:
    """Index-aligned diff over legacy full-message arrays.

    Old records stored the complete context on both sides; the diff is
    recomputed here so the dashboard still shows only the changed blocks.
    """
    before = before_payload if isinstance(before_payload, list) else []
    after = after_payload if isinstance(after_payload, list) else []
    blocks: list[dict[str, Any]] = []
    for i in range(max(len(before), len(after))):
        b = before[i] if i < len(before) else None
        a = after[i] if i < len(after) else None
        if _same_message(b, a):
            continue
        anchor = a if a is not None else b
        blocks.append(
            {
                "i": i,
                "role": anchor.get("role", "") if isinstance(anchor, dict) else "",
                "compressor": None,
                "before": b,
                "after": a,
                "before_bytes": _message_bytes(b),
                "after_bytes": _message_bytes(a),
            }
        )
    return blocks


def _merge_blocks(before_payload: Any, after_payload: Any) -> tuple[list[dict[str, Any]], bool]:
    """Return ``(blocks, legacy)`` for a request's before/after payloads.

    Delta-format payloads merge their entries by index; legacy full arrays are
    diffed index-aligned. ``legacy`` tells the frontend the blocks were
    recomputed from full snapshots (no compressor attribution available).
    """
    before_delta = _parse_delta(before_payload)
    after_delta = _parse_delta(after_payload)
    if before_delta is not None and after_delta is not None:
        return _blocks_from_delta(before_delta, after_delta), False
    return _blocks_from_legacy(before_payload, after_payload), True


def _parse_transforms(transforms_json: str) -> tuple[list[str], list[str]]:
    """Return ``(transforms_applied, compressors)`` from a record's transforms_json."""
    if not transforms_json:
        return [], []
    try:
        data = json.loads(transforms_json)
    except (json.JSONDecodeError, ValueError):
        return [], []
    if not isinstance(data, dict):
        return [], []
    return list(data.get("transforms_applied") or []), list(data.get("compressors") or [])


def _preview_from_changes(before_json: str, after_json: str) -> str:
    """Preview = the flattened text of the blocks compression actually rewrote.

    For delta records this is the ``before``-side entry messages; for legacy
    records the recomputed diff. When nothing changed (or no before side), fall
    back to the full ``before`` messages so the timeline still has a label.
    """
    before_payload, okb = _safe_parse_messages(before_json or "")
    after_payload, oka = _safe_parse_messages(after_json or "")
    try:
        blocks, _ = _merge_blocks(
            before_payload if okb else [],
            after_payload if oka else [],
        )
    except Exception:  # noqa: BLE001 — fail-open
        blocks = []
    changed = [block["before"] for block in blocks if block.get("before") is not None]
    if changed:
        return _extract_preview(changed)
    if okb:
        return _extract_preview(before_payload)
    if oka:
        return _extract_preview(after_payload)
    return ""


def list_session_requests_for_api(
    storage: Storage,
    session_id: str,
    *,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Return one entry per ``request_id`` (before/after merged), newest-first.

    Each entry carries a ``preview`` string — the flattened text of the
    ``before`` messages (truncated) — so the dashboard can render a two-line
    clamp in the timeline without fetching the full diff per row.
    """
    try:
        records = storage.list_records_by_session(
            session_id, limit=min(limit, MAX_LIMIT), offset=offset
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Diff Explorer list_records_by_session failed: %s", exc)
        return {"total": 0, "items": []}

    # Group by request_id, preserving newest-first order of first appearance.
    grouped: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for rec in records:
        entry = grouped.get(rec.request_id)
        if entry is None:
            entry = {
                "request_id": rec.request_id,
                "session_id": rec.session_id,
                "model": rec.model,
                "api_style": rec.api_style,
                "tokens_before": 0,
                "tokens_after": 0,
                "byte_size_before": 0,
                "byte_size_after": 0,
                "messages_hash": rec.messages_hash,
                "before_ts": None,
                "after_ts": None,
                "before_json": None,
                "after_json": None,
            }
            grouped[rec.request_id] = entry
            order.append(rec.request_id)

        # Honest full-context byte size for this stage; fall back to the
        # stored delta-payload size for records written before ``full_bytes``
        # existed (legacy rows).
        side_bytes = rec.full_bytes if rec.full_bytes > 0 else rec.byte_size
        ts = rec.created_at.isoformat() + "Z"
        if rec.stage == "before":
            entry["tokens_before"] = rec.tokens
            entry["byte_size_before"] = side_bytes
            entry["messages_hash"] = rec.messages_hash
            entry["before_ts"] = ts
            entry["before_json"] = rec.messages_json
        else:
            entry["tokens_after"] = rec.tokens
            entry["byte_size_after"] = side_bytes
            entry["after_ts"] = ts
            entry["after_json"] = rec.messages_json

    items: list[dict[str, Any]] = []
    for request_id in order:
        entry = grouped[request_id]
        before_json = entry.pop("before_json")
        after_json = entry.pop("after_json")
        before_ts = entry.pop("before_ts")
        after_ts = entry.pop("after_ts")

        entry["timestamp"] = before_ts or after_ts or ""
        entry["preview"] = _preview_from_changes(before_json or "", after_json or "")
        items.append(entry)

    return {"total": len(items), "items": items}


def get_request_diff_for_api(
    storage: Storage,
    request_id: str,
) -> dict[str, Any]:
    """Return the before / after payload for a single request.

    Always returns 200 with possibly-null stages; the dashboard treats null
    as "stage was not captured for this request".

    Response shape (new-style): top-level ``blocks`` (index-merged before/after
    diff blocks, each with ``role`` / ``compressor`` / ``before`` / ``after`` /
    ``before_bytes`` / ``after_bytes``), ``compressors`` (display names),
    ``legacy`` (True when blocks were recomputed from old full snapshots),
    and honest byte-based sizes. ``stages`` is kept for backward compatibility
    with callers that render the raw snapshots.
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
            "compressors": [],
            "blocks": [],
            "legacy": False,
            "tokens_before": 0,
            "tokens_after": 0,
            "byte_size_before": 0,
            "byte_size_after": 0,
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
            "compressors": [],
            "blocks": [],
            "legacy": False,
            "tokens_before": 0,
            "tokens_after": 0,
            "byte_size_before": 0,
            "byte_size_after": 0,
        }

    before_rec = next((r for r in records if r.stage == "before"), None)
    after_rec = next((r for r in records if r.stage == "after"), None)
    anchor = before_rec or after_rec

    before_payload, _ = _safe_parse_messages(before_rec.messages_json if before_rec else "")
    after_payload, _ = _safe_parse_messages(after_rec.messages_json if after_rec else "")
    blocks, legacy = _merge_blocks(before_payload, after_payload)

    transforms_applied, compressors = _parse_transforms(anchor.transforms_json if anchor else "")

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
        "transforms_applied": transforms_applied,
        "compressors": compressors,
        "blocks": blocks,
        "legacy": legacy,
        "tokens_before": before_rec.tokens if before_rec else 0,
        "tokens_after": after_rec.tokens if after_rec else 0,
        "byte_size_before": before_rec.byte_size if before_rec else 0,
        "byte_size_after": after_rec.byte_size if after_rec else 0,
    }


def health_for_api(storage: Storage) -> dict[str, Any]:
    """Return a tiny health probe. Touches ``list_sessions`` to verify the
    SQLite file is readable."""
    try:
        storage.list_sessions(limit=1, offset=0)
        return {"ok": True, "storage_url": getattr(storage, "db_path", getattr(storage, "file_path", "unknown"))}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc)}
