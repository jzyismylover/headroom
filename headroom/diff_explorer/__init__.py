"""Diff Explorer backend — session resolution + message capture + API.

Public surface (re-exported for convenience):
    * ``SessionResolver`` helpers (see ``session_resolver``)
    * ``MessageCaptureExtension`` (see ``message_capture``)
    * ``service`` layer (storage-agnostic JSON shaping for the API)
    * ``api`` fastapi router (mount at /api/diff-explorer)
"""

from .api import reset_storage_singleton, router as diff_explorer_router
from .message_capture import CompressedMessageRecord, MessageCaptureExtension
from .service import (
    get_request_diff_for_api,
    health_for_api,
    list_session_requests_for_api,
    list_sessions_for_api,
)
from .session_resolver import (
    NO_SESSION_PREFIX,
    SESSION_BODY_KEYS,
    SESSION_HEADER_KEYS,
    extract_body_metadata,
    placeholder_session_id,
    resolve_session_id,
)

__all__ = [
    "CompressedMessageRecord",
    "MessageCaptureExtension",
    "NO_SESSION_PREFIX",
    "SESSION_BODY_KEYS",
    "SESSION_HEADER_KEYS",
    "diff_explorer_router",
    "extract_body_metadata",
    "get_request_diff_for_api",
    "health_for_api",
    "list_session_requests_for_api",
    "list_sessions_for_api",
    "placeholder_session_id",
    "reset_storage_singleton",
    "resolve_session_id",
]
