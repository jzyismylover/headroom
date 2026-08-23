"""Base storage interface for Headroom SDK."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..config import RequestMetrics

if TYPE_CHECKING:
    from .compressed_messages import CompressedMessageRecord


class Storage(ABC):
    """Abstract base class for metrics storage."""

    @abstractmethod
    def save(self, metrics: RequestMetrics) -> None:
        """
        Save request metrics.

        Args:
            metrics: RequestMetrics to save.
        """
        pass

    @abstractmethod
    def get(self, request_id: str) -> RequestMetrics | None:
        """
        Get metrics by request ID.

        Args:
            request_id: The request ID.

        Returns:
            RequestMetrics or None if not found.
        """
        pass

    @abstractmethod
    def query(
        self,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        model: str | None = None,
        mode: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[RequestMetrics]:
        """
        Query metrics with filters.

        Args:
            start_time: Filter by timestamp >= start_time.
            end_time: Filter by timestamp <= end_time.
            model: Filter by model name.
            mode: Filter by mode (audit/optimize).
            limit: Maximum results to return.
            offset: Number of results to skip.

        Returns:
            List of matching RequestMetrics.
        """
        pass

    @abstractmethod
    def count(
        self,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        model: str | None = None,
        mode: str | None = None,
    ) -> int:
        """
        Count metrics matching filters.

        Args:
            start_time: Filter by timestamp >= start_time.
            end_time: Filter by timestamp <= end_time.
            model: Filter by model name.
            mode: Filter by mode.

        Returns:
            Count of matching records.
        """
        pass

    @abstractmethod
    def iter_all(self) -> Iterator[RequestMetrics]:
        """
        Iterate over all stored metrics.

        Yields:
            RequestMetrics objects.
        """
        pass

    @abstractmethod
    def get_summary_stats(
        self,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> dict[str, Any]:
        """
        Get summary statistics.

        Args:
            start_time: Filter by timestamp >= start_time.
            end_time: Filter by timestamp <= end_time.

        Returns:
            Dict with summary stats (total_requests, tokens_saved, etc.)
        """
        pass

    def close(self) -> None:  # noqa: B027
        """Close storage connection if applicable."""
        pass

    # ------------------------------------------------------------------
    # Diff Explorer backend — compressed_message storage
    # ------------------------------------------------------------------
    @abstractmethod
    def save_message_record(self, record: "CompressedMessageRecord") -> None:
        """Persist a single before/after messages snapshot.

        Implementations must be idempotent on ``record_id`` (upsert).
        """

    @abstractmethod
    def list_sessions(self, *, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        """Return one entry per known ``session_id`` with aggregate stats.

        Each entry MUST contain at least: ``session_id``, ``request_count``,
        ``first_seen`` (ISO8601+Z), ``last_seen`` (ISO8601+Z),
        ``total_tokens_before``, ``total_tokens_after``. ``savings_percent``
        should be included when both totals are available.

        Returns an empty list when no records exist (does NOT raise).
        """

    @abstractmethod
    def list_records_by_session(
        self,
        session_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list["CompressedMessageRecord"]:
        """Return all records for a session, newest-first by ``created_at``.

        Returns an empty list when the session_id is unknown (does NOT raise).
        """

    @abstractmethod
    def list_records_by_request(self, request_id: str) -> list["CompressedMessageRecord"]:
        """Return every stage snapshot for a single request.

        Returned list length is typically 0 or 2 (before / after), but the
        contract permits 1 when one stage was skipped or capture failed.

        Returns an empty list when the request_id is unknown (does NOT raise).
        """

    def __enter__(self) -> Storage:
        """Context manager entry."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit."""
        self.close()
