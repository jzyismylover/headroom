"""Data model for compressed-message capture (Diff Explorer backend).

Each record is one snapshot of messages at a single pipeline stage of a single
request. Records are written by ``MessageCaptureExtension`` and read by
``headroom.dashboard.diff_explorer_api``.

Stage semantics (only two stages are captured; PRE_SEND is intentionally
omitted because the agent layer holds that payload itself):
- ``"before"``  -- raw messages at PipelineStage.INPUT_RECEIVED.
- ``"after"``   -- post-TransformPipeline + post-CacheOptimizer messages
  at PipelineStage.INPUT_COMPRESSED.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class CompressedMessageRecord:
    """One snapshot of messages at a pipeline stage for one request."""

    record_id: str  # PK, uuid4()
    request_id: str  # matches RequestMetrics.request_id
    session_id: str  # resolved from request body metadata / headers / placeholder
    stage: str  # "before" | "after"
    model: str
    provider: str
    api_style: str  # "openai" | "anthropic"
    messages_json: str  # compact JSON serialization of messages
    tokens: int  # token count for the captured messages
    messages_hash: str  # mirrors RequestMetrics.messages_hash semantics
    created_at: datetime  # UTC, naive (consistent with RequestMetrics)
    byte_size: int  # byte length of messages_json (UTF-8)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict (for JSONLStorage / API responses)."""
        return {
            "record_id": self.record_id,
            "request_id": self.request_id,
            "session_id": self.session_id,
            "stage": self.stage,
            "model": self.model,
            "provider": self.provider,
            "api_style": self.api_style,
            "messages_json": self.messages_json,
            "tokens": self.tokens,
            "messages_hash": self.messages_hash,
            "created_at": self.created_at.isoformat() + "Z",
            "byte_size": self.byte_size,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CompressedMessageRecord":
        """Deserialize from a JSON-safe dict."""
        from ..utils import parse_timestamp

        created_at_raw = data["created_at"]
        if isinstance(created_at_raw, str):
            created_at = parse_timestamp(created_at_raw)
        else:
            created_at = created_at_raw
        return cls(
            record_id=data["record_id"],
            request_id=data["request_id"],
            session_id=data["session_id"],
            stage=data["stage"],
            model=data["model"],
            provider=data["provider"],
            api_style=data["api_style"],
            messages_json=data["messages_json"],
            tokens=int(data["tokens"]),
            messages_hash=data["messages_hash"],
            created_at=created_at,
            byte_size=int(data["byte_size"]),
        )


# Sentinel prefix used when session_id cannot be resolved from the request.
# Web UI uses this prefix to offer a "show unassociated records" filter.
NO_SESSION_PREFIX = "_no_session:"
