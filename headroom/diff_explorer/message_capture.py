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
            if sid is None:
                # PipelineEvent has no timestamp field — use the metadata
                # timestamp when available (HeadroomClient._create injects
                # the request timestamp there) or fall back to now().
                ts = self._resolve_timestamp(event)
                sid = placeholder_session_id(event.request_id, ts)
            # Pending size cap: if the cap is reached we drop the OLDEST entry
            # (FIFO). This is a defense against session runaways where the
            # agent never sends the post-compression emit (e.g. crash mid-
            # turn). The dropped entry simply means we won't capture its
            # ``after`` stage — the ``before`` was already saved.
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
                "messages_hash": compute_messages_hash(event.messages or []),
            }
            self._save(event.request_id, "before", event.messages, self._resolve_timestamp(event))
        elif event.stage == PipelineStage.INPUT_COMPRESSED:
            if not event.messages:
                return event
            self._save(event.request_id, "after", event.messages, self._resolve_timestamp(event))
            self._pending.pop(event.request_id, None)
        # PRE_SEND is intentionally ignored.
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

    def _save(
        self,
        request_id: str,
        stage: str,
        messages: list[dict[str, Any]] | None,
        timestamp: Any,
    ) -> None:
        ctx = self._pending.get(request_id)
        if ctx is None or messages is None:
            # Nothing to capture (likely an audit-mode path or an out-of-order
            # emit). Just return — we deliberately do NOT create an "after"
            # record without a corresponding "before" context.
            return

        messages_json = json.dumps(
            messages,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        truncated = False
        try:
            encoded_size = len(messages_json.encode("utf-8"))
        except Exception:  # noqa: BLE001 — extremely defensive
            encoded_size = 0

        if encoded_size > self._max_bytes:
            truncated = True
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
            tokens = self._count_tokens(messages, ctx["model"])

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
            messages_hash=ctx["messages_hash"],
            created_at=self._to_naive_utc(timestamp),
            byte_size=encoded_size,
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
