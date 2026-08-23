"""PipelineExtension that snapshots messages at INPUT_RECEIVED and INPUT_COMPRESSED.

This is the heart of the Diff Explorer backend. It hooks into the canonical
pipeline events without modifying any existing logic in ``HeadroomClient._create()``.

Two stages are captured:
    * ``"before"`` at ``PipelineStage.INPUT_RECEIVED`` — raw messages.
    * ``"after"``  at ``PipelineStage.INPUT_COMPRESSED`` — post-TransformPipeline
      + post-CacheOptimizer messages.

``PRE_SEND`` is intentionally NOT captured: by the time the request reaches
PRE_SEND, the agent layer (codex / claude code) already has a verbatim copy
of the final messages in its own request/response history. We only need the
two extreme points to render a meaningful before/after comparison.

Captures are opt-in via ``HeadroomConfig.capture_messages = True``. When the
config flag is off, the extension is never instantiated.

Robustness:
    * Storage failures are logged and swallowed — never raised into the
      caller pipeline.
    * Oversized messages (> ``max_record_bytes``) are truncated to a
      ``{"_truncated": true, ...}`` placeholder, leaving only metadata
      (tokens, hash, byte_size) to be persisted.
    * In-memory ``_pending`` entries are released on every ``after`` event,
      preventing memory leaks under long-lived sessions.
"""

from __future__ import annotations

import copy
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from ..pipeline import PipelineEvent, PipelineStage
from ..storage.base import Storage
from ..storage.compressed_messages import CompressedMessageRecord as _Record
from ..tokenizer import Tokenizer
from ..utils import compute_messages_hash
from .session_resolver import (
    placeholder_session_id,
    resolve_session_id,
)

logger = logging.getLogger(__name__)


def _strip_transient_keys(obj: Any) -> Any:
    """Recursively drop per-call cache directives for content-only comparison.

    Clients (notably Claude Code) move the ``cache_control`` breakpoint to the
    newest message on every call, so the exact same message carries the marker
    on one request and not the next. Ignoring it lets the diff gate treat a
    "breakpoint-only" change as a no-op instead of persisting a pair whose
    visible content is identical.
    """
    if isinstance(obj, dict):
        return {k: _strip_transient_keys(v) for k, v in obj.items() if k != "cache_control"}
    if isinstance(obj, list):
        return [_strip_transient_keys(v) for v in obj]
    return obj


def _messages_equal(a: list[dict[str, Any]] | None, b: list[dict[str, Any]] | None) -> bool:
    """Return True when two message lists are content-identical.

    Compares canonicalized JSON (transient cache directives stripped,
    deterministic separators) so semantically equivalent but cosmetically
    different payloads — reordered keys, a moved cache_control breakpoint —
    still register as equal.
    """
    return json.dumps(_strip_transient_keys(a or []), ensure_ascii=False, separators=(",", ":")) == \
        json.dumps(_strip_transient_keys(b or []), ensure_ascii=False, separators=(",", ":"))


def _tools_equal(
    a: list[dict[str, Any]] | None, b: list[dict[str, Any]] | None
) -> bool:
    """Return True when two tool schemas are structurally identical.

    Both being ``None`` (the common case when the request had no tools)
    counts as equal. ``default=str`` makes the JSON encoder forgiving
    for any custom tool object types a SDK might smuggle in.
    """
    return json.dumps(a, ensure_ascii=False, separators=(",", ":"), default=str) == \
        json.dumps(b, ensure_ascii=False, separators=(",", ":"), default=str)


def _actually_compressed(ctx: dict[str, Any]) -> bool:
    """Return True when the request genuinely shrank under compression.

    The honest signal is a byte reduction measured on the normalized
    content (transient ``cache_control`` markers stripped): ``after_bytes <
    before_bytes``. Requests that merely moved a ``cache_control``
    breakpoint or passed through untouched leave byte size unchanged and are
    dropped here — this is what removes the phantom "100% saved" rows where
    the stored ``byte_size`` was identical but the ``tokens`` column
    happened to drop (the token column is unreliable; ``byte_size`` is
    measured from the actually-stored JSON).

    When byte measurement fails (unserializable content), fall back to the
    pipeline-reported token counts (``metadata_after`` carries
    ``tokens_before`` / ``tokens_after``).
    """
    before_bytes = _side_bytes(
        ctx.get("before_messages"), ctx.get("before_tools")
    )
    after_bytes = _side_bytes(
        ctx.get("after_messages"), ctx.get("after_tools")
    )
    # Stash the full-context sizes for _save_pair so each side is measured
    # exactly once (serialization is the expensive part). When a request
    # fails the byte gate we fall through to the token fallback below but the
    # stashed values are still the honest full-context byte counts.
    ctx["full_bytes_before"] = before_bytes
    ctx["full_bytes_after"] = after_bytes
    if before_bytes and after_bytes:
        return after_bytes < before_bytes

    md = ctx.get("metadata_after") or {}
    before_tokens = md.get("tokens_before")
    after_tokens = md.get("tokens_after")
    if isinstance(before_tokens, (int, float)) and isinstance(after_tokens, (int, float)):
        return after_tokens < before_tokens

    return False


def _side_bytes(
    messages: list[dict[str, Any]] | None,
    tools: list[dict[str, Any]] | None,
) -> int:
    """Total UTF-8 bytes of one snapshot side (messages + tools, normalized).

    Measures both payloads so a request that only had its tools crushed still
    passes the "was compressed" gate even when the message list is untouched.
    """
    try:
        return len(
            json.dumps(
                [_strip_transient_keys(messages or []), tools or []],
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        )
    except Exception:  # noqa: BLE001 — fail-open
        return 0


# Best-effort mapping of pipeline transform markers to human-readable
# compressor names. Keys are the ``:``-separated marker segments observed in
# the wild (``kompress:tool:0.43``, ``router:cross_turn_dedup:2``).
_COMPRESSOR_NAMES: dict[str, str] = {
    "kompress": "KCompress",
    "smart": "SmartCrusher",
    "smart_crusher": "SmartCrusher",
    "code_aware": "CodeAware",
    "search": "SearchCompressor",
    "log": "LogCompressor",
    "text": "TextCrusher",
    "diff": "DiffCompressor",
    "html": "HtmlExtractor",
    "tabular": "TabularCompressor",
    "config": "ConfigCompressor",
    "mixed": "MixedCompressor",
    "cross_turn_dedup": "CrossTurnDedup",
    "read_lifecycle": "ReadLifecycle",
    "read_kompress_exp": "KCompress",
}


def _compressor_for_marker(marker: str) -> str | None:
    """Return the display name for a transform marker, if recognized.

    Markers are heterogeneous and free-form. We scan the ``:``-separated
    segments and return the first known compressor name found; markers that
    describe routing / cache-alignment decisions rather than content
    compression (``router:...``, ``cache_optimizer:...``, ``netcost:...``)
    contribute no segment that matches, so they are dropped rather than
    surfaced as bogus compressor tags.
    """
    for seg in marker.split(":"):
        name = _COMPRESSOR_NAMES.get(seg)
        if name is not None:
            return name
    return None


def _build_compressors(transforms_applied: list[str] | None) -> list[str]:
    """Map pipeline transform markers to human-readable compressor names.

    Deduplicated, order-preserving. Unrecognized markers (routing decisions,
    cache-alignment, net-cost bookkeeping) are dropped — the list answers
    "which compressor handled this request", not "echo the pipeline".
    """
    names: list[str] = []
    seen: set[str] = set()
    for marker in transforms_applied or []:
        name = _compressor_for_marker(marker)
        if name is None or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def _role_compressor_map(transforms_applied: list[str] | None) -> dict[str, str]:
    """Best-effort ``role -> compressor`` attribution from ``kompress:<role>`` markers.

    Only the Kompress transform records the message role it operated on, so
    this is the sole per-block attribution we can recover; multi-compressor
    requests otherwise fall back to the single-name shortcut in
    ``_save_pair``.
    """
    result: dict[str, str] = {}
    for marker in transforms_applied or []:
        parts = marker.split(":")
        if parts[0] == "kompress" and len(parts) >= 2 and parts[1]:
            result[parts[1]] = "KCompress"
    return result


def _compute_delta(
    before: list[dict[str, Any]] | None,
    after: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Index-aligned message-level diff of a before/after snapshot pair.

    Returns one entry per index whose content differs (using the same
    transient-key-normalized comparison as ``_messages_equal``)::

        [{"i": 3, "role": "tool", "c": <compressor|None>,
          "before": <message|None>, "after": <message|None>}, ...]

    ``before`` / ``after`` are the per-side message dicts (``None`` for an
    addition / removal). Indices that are content-identical are omitted, so
    the stored delta only carries the blocks compression actually touched.
    """
    before = before or []
    after = after or []
    delta: list[dict[str, Any]] = []
    for i in range(max(len(before), len(after))):
        b = before[i] if i < len(before) else None
        a = after[i] if i < len(after) else None
        if b is None or a is None or not _messages_equal([b], [a]):
            delta.append(
                {
                    "i": i,
                    "role": (a or b).get("role", ""),
                    "c": None,
                    "before": b,
                    "after": a,
                }
            )
    return delta


class MessageCaptureExtension:
    """Snapshot messages at INPUT_RECEIVED / INPUT_COMPRESSED into storage."""

    def __init__(
        self,
        storage: Storage,
        *,
        max_record_bytes: int = 5 * 1024 * 1024,
        tokenizer: Tokenizer | None = None,
        max_pending: int = 10_000,
    ):
        self._storage = storage
        self._max_bytes = max_record_bytes
        self._tokenizer = tokenizer
        self._max_pending = max_pending
        # request_id -> {"session_id", "model", "provider", "api_style",
        #                "messages_hash"}
        # Populated on INPUT_RECEIVED, consumed+released on INPUT_COMPRESSED.
        self._pending: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # PipelineExtension entry point
    # ------------------------------------------------------------------
    def on_pipeline_event(self, event: PipelineEvent) -> PipelineEvent | None:
        try:
            return self._handle(event)
        except Exception as exc:  # noqa: BLE001 — fail-open per extension contract
            logger.warning(
                "MessageCaptureExtension failed during %s: %s",
                event.stage.value if event.stage else "?",
                exc,
            )
            return event  # never break the pipeline

    def _handle(self, event: PipelineEvent) -> PipelineEvent | None:
        if event.stage == PipelineStage.INPUT_RECEIVED:
            if not event.messages:
                # Defensive: a None/empty messages list yields nothing
                # useful to capture.
                return event
            sid = self._resolve_session_id(event)
            ts = self._resolve_timestamp(event)
            if sid is None:
                # PipelineEvent has no timestamp field — use the metadata
                # timestamp when available (HeadroomClient._create injects
                # the request timestamp there) or fall back to now().
                sid = placeholder_session_id(event.request_id, ts)
            # Pending size cap: if the cap is reached we drop the OLDEST entry
            # (FIFO). This is a defense against session runaways where the
            # agent never sends the post-compression emit (e.g. crash mid-
            # turn). The dropped entry simply means we won't capture its
            # ``after`` stage — nothing was written to disk yet.
            if len(self._pending) >= self._max_pending:
                try:
                    oldest_key = next(iter(self._pending))
                    self._pending.pop(oldest_key, None)
                except StopIteration:
                    pass
            self._pending[event.request_id] = {
                "session_id": sid,
                "model": event.model or "",
                "provider": event.provider or "",
                "api_style": (event.metadata or {}).get("api_style", "openai"),
                "before_messages": copy.deepcopy(event.messages or []),
                "before_tools": copy.deepcopy(event.tools),
                "before_ts": ts,
                "after_messages": None,
                "after_tools": None,
                "after_ts": None,
                "metadata_after": {},
            }
            # We deliberately DO NOT write the ``before`` record yet. The
            # capture decision (and any tools diff) is made once we have
            # BOTH the INPUT_COMPRESSED and TOOLS_COMPRESSED payloads in
            # hand, so a no-op request does not bloat the store.
            return event

        if event.stage == PipelineStage.INPUT_COMPRESSED:
            ctx = self._pending.get(event.request_id)
            if ctx is not None and event.messages is not None:
                ctx["after_messages"] = copy.deepcopy(event.messages)
                ctx["after_ts"] = self._resolve_timestamp(event)
                md = event.metadata or {}
                ctx["metadata_after"] = {
                    "tokens_before": md.get("tokens_before"),
                    "tokens_after": md.get("tokens_after"),
                    "transforms_applied": list(md.get("transforms_applied") or []),
                }
            return event

        if event.stage == PipelineStage.TOOLS_COMPRESSED:
            ctx = self._pending.pop(event.request_id, None)
            if ctx is None:
                return event
            ctx["after_tools"] = event.tools
            if ctx["after_ts"] is None:
                ctx["after_ts"] = self._resolve_timestamp(event)
            if ctx["after_messages"] is None:
                # INPUT_COMPRESSED never fired (e.g. SDK audit mode) — no
                # compression happened, so there is no before/after pair worth
                # persisting. The pending entry was already popped above.
                return event
            # Only persist a before/after pair when compression actually
            # shrank the request. Requests that passed through untouched, ran
            # in audit mode, or only moved a cache_control breakpoint are
            # dropped here so the dashboard only surfaces real compressions.
            if not _actually_compressed(ctx):
                logger.debug(
                    "MessageCaptureExtension skip non-compressed request_id=%s",
                    event.request_id,
                )
                return event
            self._save_pair(event.request_id, ctx)
            return event

        # PRE_SEND / INPUT_REMEMBERED / everything else: pass-through.
        return event

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _resolve_session_id(self, event: PipelineEvent) -> str | None:
        """Look up session_id from event metadata / headers, falling back
        to scanning the messages list for an inline ``_meta`` pseudo-entry."""
        # 1) explicit metadata injected by HeadroomClient._create
        md = event.metadata or {}
        sid = md.get("session_id")
        if isinstance(sid, str) and sid.strip():
            return sid.strip()

        # 2) HTTP headers (proxy scenario)
        if event.headers:
            sid = resolve_session_id(headers=dict(event.headers))
            if sid:
                return sid

        # 3) inline pseudo-message metadata (rare; future-proofing)
        from .session_resolver import extract_body_metadata as _extract

        inline = _extract(event.messages)
        if inline:
            sid = resolve_session_id(body_metadata=inline)
            if sid:
                return sid

        return None

    def _save_pair(self, request_id: str, ctx: dict[str, Any]) -> None:
        """Persist both ``before`` and ``after`` snapshots for one request.

        Instead of duplicating the full context on both records (which made
        every row carry the same prompt prefix and grew the store
        quadratically), each record stores ONLY the message blocks that
        changed under compression, in a compact delta payload::

            {"_delta": true, "entries": [{"i": 3, "c": "SmartCrusher",
                                          "m": { <message dict> }}]}

        ``i`` is the index in the full message list (before/after pair up by
        ``i``), ``c`` is the best-effort compressor attribution, and ``m`` is
        that side's form of the message. The full ``transforms_applied`` list
        and the resolved compressor display names ride along in
        ``transforms_json``.

        Tools are stored as-is (unchanged by the delta refactor). Storage
        failures are logged and swallowed so the caller pipeline is never
        affected.
        """
        before_messages = ctx.get("before_messages") or []
        after_messages = ctx.get("after_messages") or before_messages
        before_tools = ctx.get("before_tools")
        after_tools = ctx.get("after_tools") if ctx.get("after_tools") is not None else before_tools

        md = ctx.get("metadata_after") or {}
        markers = list(md.get("transforms_applied") or [])
        compressors = _build_compressors(markers)
        transforms_json = json.dumps(
            {"transforms_applied": markers, "compressors": compressors},
            ensure_ascii=False,
            separators=(",", ":"),
        )

        # Prefer the pipeline-reported token counts (``metadata_after``) so
        # records carry real numbers even without a bound tokenizer; fall
        # back to the extension's own tokenizer over the REAL per-side
        # messages (the delta payload's shape is not tokenizable).
        before_tokens = md.get("tokens_before")
        if not isinstance(before_tokens, (int, float)):
            before_tokens = self._count_tokens(before_messages, ctx["model"])
        after_tokens = md.get("tokens_after")
        if not isinstance(after_tokens, (int, float)):
            after_tokens = self._count_tokens(after_messages, ctx["model"])

        delta = _compute_delta(before_messages, after_messages)
        if len(compressors) == 1:
            single = compressors[0]
            for entry in delta:
                entry["c"] = single
        else:
            role_map = _role_compressor_map(markers)
            for entry in delta:
                entry["c"] = role_map.get(entry.get("role", "")) or None

        before_entries = [
            {"i": entry["i"], "c": entry["c"], "m": entry["before"]} for entry in delta
        ]
        after_entries = [
            {"i": entry["i"], "c": entry["c"], "m": entry["after"]} for entry in delta
        ]

        # Full-context byte size per side (measured once in _actually_compressed
        # and stashed on ctx; recompute as a fallback so _save_pair stays
        # correct even if a future caller skips the gate).
        full_bytes_before = ctx.get("full_bytes_before")
        if not full_bytes_before:
            full_bytes_before = _side_bytes(before_messages, before_tools)
        full_bytes_after = ctx.get("full_bytes_after")
        if not full_bytes_after:
            full_bytes_after = _side_bytes(after_messages, after_tools)

        for stage, payload, tools, ts, meta_tokens in (
            ("before", {"_delta": True, "entries": before_entries}, before_tools, ctx.get("before_ts"), before_tokens),
            ("after", {"_delta": True, "entries": after_entries}, after_tools, ctx.get("after_ts"), after_tokens),
        ):
            self._save(
                request_id=request_id,
                ctx=ctx,
                stage=stage,
                messages=payload,
                tools=tools,
                timestamp=ts,
                tokens_override=meta_tokens,
                transforms_json=transforms_json,
                full_bytes=full_bytes_before if stage == "before" else full_bytes_after,
            )

    def _save(
        self,
        *,
        request_id: str,
        ctx: dict[str, Any],
        stage: str,
        messages: list[dict[str, Any]] | None,
        tools: list[dict[str, Any]] | None,
        timestamp: Any,
        tokens_override: Any = None,
        transforms_json: str = "",
        full_bytes: int = 0,
    ) -> None:
        if messages is None:
            return

        messages_json = json.dumps(
            messages,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        try:
            encoded_size = len(messages_json.encode("utf-8"))
        except Exception:  # noqa: BLE001 — extremely defensive
            encoded_size = 0

        if encoded_size > self._max_bytes:
            messages_json = json.dumps(
                {
                    "_truncated": True,
                    "original_byte_size": encoded_size,
                    "stage": stage,
                    "request_id": request_id,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            encoded_size = len(messages_json.encode("utf-8"))
            tokens = 0
        else:
            # Pipeline-reported count wins (accurate even without a bound
            # tokenizer); fall back to the extension's own tokenizer.
            if tokens_override is not None:
                try:
                    tokens = int(tokens_override)
                except (TypeError, ValueError):
                    tokens = self._count_tokens(messages, ctx["model"])
            else:
                tokens = self._count_tokens(messages, ctx["model"])

        # Tools payload: serialize separately so storage backend can index
        # byte_size / hash per field. Skip when neither side carried any
        # tools (the common case for chat-style agents with no function
        # calling).
        if tools is None:
            tools_json = ""
            tools_byte_size = 0
            tools_hash = ""
        else:
            try:
                tools_json = json.dumps(
                    tools,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                )
            except Exception as exc:  # noqa: BLE001 — fail-open
                logger.debug(
                    "MessageCaptureExtension tools serialization failed: %s", exc
                )
                tools_json = ""
            tools_byte_size = len(tools_json.encode("utf-8"))
            tools_hash = compute_messages_hash(tools)

        record = _Record(
            record_id=str(uuid.uuid4()),
            request_id=request_id,
            session_id=ctx["session_id"],
            stage=stage,
            model=ctx["model"],
            provider=ctx["provider"],
            api_style=ctx["api_style"],
            messages_json=messages_json,
            tokens=tokens,
            messages_hash=compute_messages_hash(messages),
            created_at=self._to_naive_utc(timestamp),
            byte_size=encoded_size,
            full_bytes=full_bytes,
            tools_json=tools_json,
            tools_byte_size=tools_byte_size,
            tools_hash=tools_hash,
            transforms_json=transforms_json,
        )
        try:
            self._storage.save_message_record(record)
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.warning("MessageCaptureExtension save failed: %s", exc)

    def _count_tokens(self, messages: list[dict[str, Any]], model: str) -> int:
        """Best-effort token count. Returns 0 when no tokenizer is bound."""
        if self._tokenizer is None:
            return 0
        try:
            return self._tokenizer.count_messages(messages)
        except Exception:  # noqa: BLE001
            return 0

    @staticmethod
    def _to_naive_utc(ts: Any) -> datetime:
        if isinstance(ts, datetime):
            if ts.tzinfo is not None:
                ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
            return ts
        return datetime.now(timezone.utc).replace(tzinfo=None)

    @staticmethod
    def _resolve_timestamp(event: PipelineEvent) -> datetime:
        """Best-effort timestamp recovery.

        PipelineEvent itself carries no timestamp field, but
        ``HeadroomClient._create`` passes the request timestamp through
        ``metadata["timestamp"]`` (ISO8601 string) when it emits events.
        Fall back to ``datetime.utcnow()`` when the field is absent.
        """
        from ..utils import parse_timestamp

        md = event.metadata or {}
        ts = md.get("timestamp")
        if isinstance(ts, datetime):
            return ts
        if isinstance(ts, str) and ts:
            try:
                return parse_timestamp(ts)
            except Exception:  # noqa: BLE001
                pass
        return datetime.now(timezone.utc).replace(tzinfo=None)


# Convenience alias so callers can ``from headroom.observability.message_capture import CompressedMessageRecord``
# (although the canonical import path is ``headroom.storage.compressed_messages``).
CompressedMessageRecord = _Record
