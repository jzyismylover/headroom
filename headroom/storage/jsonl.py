"""JSONL file storage implementation for Headroom SDK."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import RequestMetrics
from ..utils import format_timestamp, parse_timestamp
from .base import Storage
from .compressed_messages import CompressedMessageRecord


class JSONLStorage(Storage):
    """JSONL file-based metrics storage."""

    def __init__(self, file_path: str):
        """
        Initialize JSONL storage.

        Args:
            file_path: Path to JSONL file.
        """
        self.file_path = file_path
        self._ensure_file_exists()

    def _ensure_file_exists(self) -> None:
        """Create file and parent directories if they don't exist."""
        path = Path(self.file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.touch()

    def _metrics_to_dict(self, metrics: RequestMetrics) -> dict[str, Any]:
        """Convert RequestMetrics to serializable dict."""
        return {
            "id": metrics.request_id,
            "timestamp": format_timestamp(metrics.timestamp),
            "model": metrics.model,
            "stream": metrics.stream,
            "mode": metrics.mode,
            "tokens_input_before": metrics.tokens_input_before,
            "tokens_input_after": metrics.tokens_input_after,
            "tokens_output": metrics.tokens_output,
            "block_breakdown": metrics.block_breakdown,
            "waste_signals": metrics.waste_signals,
            "stable_prefix_hash": metrics.stable_prefix_hash,
            "cache_alignment_score": metrics.cache_alignment_score,
            "cached_tokens": metrics.cached_tokens,
            "transforms_applied": metrics.transforms_applied,
            "tool_units_dropped": metrics.tool_units_dropped,
            "turns_dropped": metrics.turns_dropped,
            "messages_hash": metrics.messages_hash,
            "error": metrics.error,
        }

    def _dict_to_metrics(self, data: dict[str, Any]) -> RequestMetrics:
        """Convert dict to RequestMetrics."""
        return RequestMetrics(
            request_id=data["id"],
            timestamp=parse_timestamp(data["timestamp"]),
            model=data["model"],
            stream=data["stream"],
            mode=data["mode"],
            tokens_input_before=data["tokens_input_before"],
            tokens_input_after=data["tokens_input_after"],
            tokens_output=data.get("tokens_output"),
            block_breakdown=data.get("block_breakdown", {}),
            waste_signals=data.get("waste_signals", {}),
            stable_prefix_hash=data.get("stable_prefix_hash", ""),
            cache_alignment_score=data.get("cache_alignment_score", 0.0),
            cached_tokens=data.get("cached_tokens"),
            transforms_applied=data.get("transforms_applied", []),
            tool_units_dropped=data.get("tool_units_dropped", 0),
            turns_dropped=data.get("turns_dropped", 0),
            messages_hash=data.get("messages_hash", ""),
            error=data.get("error"),
        )

    def save(self, metrics: RequestMetrics) -> None:
        """Save request metrics."""
        data = self._metrics_to_dict(metrics)
        with open(self.file_path, "a") as f:
            f.write(json.dumps(data) + "\n")

    def get(self, request_id: str) -> RequestMetrics | None:
        """Get metrics by request ID."""
        for metrics in self.iter_all():
            if metrics.request_id == request_id:
                return metrics
        return None

    def query(
        self,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        model: str | None = None,
        mode: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[RequestMetrics]:
        """Query metrics with filters."""
        results: list[RequestMetrics] = []
        skipped = 0

        for metrics in self.iter_all():
            # Apply filters
            if start_time is not None and metrics.timestamp < start_time:
                continue
            if end_time is not None and metrics.timestamp > end_time:
                continue
            if model is not None and metrics.model != model:
                continue
            if mode is not None and metrics.mode != mode:
                continue

            # Handle offset
            if skipped < offset:
                skipped += 1
                continue

            results.append(metrics)

            # Handle limit
            if len(results) >= limit:
                break

        # Sort by timestamp descending
        results.sort(key=lambda m: m.timestamp, reverse=True)
        return results

    def count(
        self,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        model: str | None = None,
        mode: str | None = None,
    ) -> int:
        """Count metrics matching filters."""
        count = 0

        for metrics in self.iter_all():
            if start_time is not None and metrics.timestamp < start_time:
                continue
            if end_time is not None and metrics.timestamp > end_time:
                continue
            if model is not None and metrics.model != model:
                continue
            if mode is not None and metrics.mode != mode:
                continue
            count += 1

        return count

    def iter_all(self) -> Iterator[RequestMetrics]:
        """Iterate over all stored metrics."""
        if not Path(self.file_path).exists():
            return

        with open(self.file_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    yield self._dict_to_metrics(data)
                except json.JSONDecodeError:
                    # Skip malformed lines
                    continue

    def get_summary_stats(
        self,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> dict[str, Any]:
        """Get summary statistics."""
        total_requests = 0
        total_tokens_before = 0
        total_tokens_after = 0
        total_cache_alignment = 0.0
        audit_count = 0
        optimize_count = 0

        for metrics in self.iter_all():
            if start_time is not None and metrics.timestamp < start_time:
                continue
            if end_time is not None and metrics.timestamp > end_time:
                continue

            total_requests += 1
            total_tokens_before += metrics.tokens_input_before
            total_tokens_after += metrics.tokens_input_after
            total_cache_alignment += metrics.cache_alignment_score

            if metrics.mode == "audit":
                audit_count += 1
            elif metrics.mode == "optimize":
                optimize_count += 1

        total_tokens_saved = total_tokens_before - total_tokens_after
        avg_tokens_saved = total_tokens_saved / total_requests if total_requests > 0 else 0
        avg_cache_alignment = total_cache_alignment / total_requests if total_requests > 0 else 0

        return {
            "total_requests": total_requests,
            "total_tokens_before": total_tokens_before,
            "total_tokens_after": total_tokens_after,
            "total_tokens_saved": total_tokens_saved,
            "avg_tokens_saved": avg_tokens_saved,
            "avg_cache_alignment": avg_cache_alignment,
            "audit_count": audit_count,
            "optimize_count": optimize_count,
        }

    def close(self) -> None:
        """No-op for file storage."""
        pass

    # ------------------------------------------------------------------
    # Diff Explorer backend — file scan implementation
    # ------------------------------------------------------------------
    def save_message_record(self, record: CompressedMessageRecord) -> None:
        """Append a record to the same JSONL file.

        Note: ``record_id`` is upserted by appending a new line; downstream
        readers should pick the most recent line per ``record_id``. This is
        acceptable because each extension emits a fresh ``uuid4()`` per stage.
        """
        path = Path(self.file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")

    def list_sessions(self, *, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        """Aggregate session stats by scanning the JSONL file.

        Performance: O(N) on file size; intended for development usage.

        The aggregated token totals come exclusively from message records
        themselves (``stage == "before"`` contributes to
        ``total_tokens_before`` and vice versa). We do NOT join against
        ``RequestMetrics`` rows because the JSONL file stores both kinds of
        records on separate lines and distinguishing them mid-scan is
        non-trivial — callers wanting joined data should use SQLiteStorage.

        Byte totals prefer ``full_bytes`` (honest full-context size) and fall
        back to ``byte_size`` (stored delta payload) for legacy lines written
        before ``full_bytes`` existed.
        """
        agg: dict[str, dict[str, Any]] = {}
        for rec in self._iter_message_records():
            entry = agg.setdefault(
                rec.session_id,
                {
                    "session_id": rec.session_id,
                    "request_ids": set(),
                    "first_seen": rec.created_at.isoformat() + "Z",
                    "last_seen": rec.created_at.isoformat() + "Z",
                    "total_tokens_before": 0,
                    "total_tokens_after": 0,
                    "total_bytes_before": 0,
                    "total_bytes_after": 0,
                },
            )
            entry["request_ids"].add(rec.request_id)
            iso_ts = rec.created_at.isoformat() + "Z"
            if iso_ts < entry["first_seen"]:
                entry["first_seen"] = iso_ts
            if iso_ts > entry["last_seen"]:
                entry["last_seen"] = iso_ts
            side_bytes = rec.full_bytes if rec.full_bytes > 0 else rec.byte_size
            if rec.stage == "before":
                entry["total_tokens_before"] += rec.tokens
                entry["total_bytes_before"] += side_bytes
            elif rec.stage == "after":
                entry["total_tokens_after"] += rec.tokens
                entry["total_bytes_after"] += side_bytes

        rows = []
        for entry in agg.values():
            bytes_before = entry["total_bytes_before"]
            bytes_after = entry["total_bytes_after"]
            saved_bytes = max(0, bytes_before - bytes_after)
            pct = (saved_bytes / bytes_before * 100.0) if bytes_before > 0 else 0.0
            rows.append(
                {
                    "session_id": entry["session_id"],
                    "request_count": len(entry["request_ids"]),
                    "first_seen": entry["first_seen"],
                    "last_seen": entry["last_seen"],
                    "total_tokens_before": entry["total_tokens_before"],
                    "total_tokens_after": entry["total_tokens_after"],
                    "total_bytes_before": bytes_before,
                    "total_bytes_after": bytes_after,
                    "savings_percent": round(pct, 2),
                }
            )
        rows.sort(key=lambda r: r["last_seen"], reverse=True)
        return rows[offset : offset + limit]

    def list_records_by_session(
        self,
        session_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[CompressedMessageRecord]:
        matching = [r for r in self._iter_message_records() if r.session_id == session_id]
        matching.sort(key=lambda r: (r.created_at, r.record_id), reverse=True)
        # Upsert semantics: keep only the last record per ``record_id``.
        seen: set[str] = set()
        deduped: list[CompressedMessageRecord] = []
        for rec in matching:
            if rec.record_id in seen:
                continue
            seen.add(rec.record_id)
            deduped.append(rec)
        return deduped[offset : offset + limit]

    def list_records_by_request(self, request_id: str) -> list[CompressedMessageRecord]:
        matching = [r for r in self._iter_message_records() if r.request_id == request_id]
        # Upsert: keep the last record per record_id.
        last_per_id: dict[str, CompressedMessageRecord] = {}
        for rec in matching:
            last_per_id[rec.record_id] = rec
        result = sorted(last_per_id.values(), key=lambda r: (r.created_at, r.stage))
        return result

    def _iter_message_records(self) -> Iterator[CompressedMessageRecord]:
        """Yield every ``CompressedMessageRecord`` line in the JSONL file.

        Upsert semantics: when the same ``record_id`` appears multiple
        times, only the LAST occurrence is yielded (matching the SQLite
        ``INSERT OR REPLACE`` contract on the abstract base class).
        """
        path = Path(self.file_path)
        if not path.exists():
            return
        last_per_id: dict[str, CompressedMessageRecord] = {}
        order: list[str] = []
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Distinguish message records from RequestMetrics lines by
                # the presence of a ``stage`` field.
                if "stage" not in data or "session_id" not in data:
                    continue
                try:
                    rec = CompressedMessageRecord.from_dict(data)
                except Exception:  # noqa: BLE001 — skip malformed rows
                    continue
                if rec.record_id not in last_per_id:
                    order.append(rec.record_id)
                last_per_id[rec.record_id] = rec
        for rid in order:
            yield last_per_id[rid]
