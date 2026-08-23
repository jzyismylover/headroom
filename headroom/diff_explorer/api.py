"""FastAPI router for the Diff Explorer dashboard.

Mount point: ``/api/diff-explorer/*``. Endpoints are intentionally
read-only (GET only) — the dashboard never writes to storage directly;
captures happen via ``MessageCaptureExtension`` at request time.

Storage is read from the ``HEADROOM_STORE_URL`` environment variable (or
``tempfile.gettempdir()/headroom.db`` by default) so the dashboard can run
in a process separate from the SDK. Reusing the same SQLite file as the
proxy / SDK is the contract — see ``shares/sdd/headroom/方案.md`` §B6.
"""

from __future__ import annotations

import logging
import os
import tempfile
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from ..storage import create_storage
from ..storage.base import Storage
from .service import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    get_request_diff_for_api,
    health_for_api,
    list_session_requests_for_api,
    list_sessions_for_api,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/diff-explorer", tags=["diff-explorer"])


# ---------------------------------------------------------------------------
# Storage lifecycle — created lazily on first request, kept process-global.
# ---------------------------------------------------------------------------
_storage_singleton: Storage | None = None


def _get_storage() -> Storage:
    """Return a process-wide Storage instance, creating it on first call.

    The instance is keyed by ``HEADROOM_STORE_URL``. If the env var is unset
    we fall back to ``tempfile.gettempdir()/headroom.db`` (matching the
    default in ``HeadroomClient.__init__``).
    """
    global _storage_singleton
    if _storage_singleton is not None:
        return _storage_singleton

    store_url = os.environ.get("HEADROOM_STORE_URL")
    if not store_url:
        db_path = os.path.join(tempfile.gettempdir(), "headroom.db")
        store_url = f"sqlite:///{db_path}"
    logger.info("Diff Explorer: using storage %s", store_url)
    _storage_singleton = create_storage(store_url)
    return _storage_singleton


def reset_storage_singleton() -> None:
    """Drop the cached storage. Test hook — call between test cases so a
    new ``HEADROOM_STORE_URL`` (or temp dir) takes effect."""
    global _storage_singleton
    if _storage_singleton is not None:
        try:
            _storage_singleton.close()
        except Exception:  # noqa: BLE001
            pass
    _storage_singleton = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.get("/sessions")
def list_sessions(
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """List all known ``session_id``s with aggregate stats."""
    return list_sessions_for_api(_get_storage(), limit=limit, offset=offset)


@router.get("/sessions/{session_id}/requests")
def list_session_requests(
    session_id: str,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """List all records (one per stage) for a single session."""
    if not session_id or len(session_id) > 256:
        raise HTTPException(status_code=400, detail="session_id must be 1..256 chars")
    return list_session_requests_for_api(
        _get_storage(), session_id, limit=limit, offset=offset
    )


@router.get("/requests/{request_id}")
def get_request_diff(request_id: str) -> dict[str, Any]:
    """Return the before / after payload for a single request."""
    if not request_id or len(request_id) > 256:
        raise HTTPException(status_code=400, detail="request_id must be 1..256 chars")
    return get_request_diff_for_api(_get_storage(), request_id)


@router.get("/health")
def health() -> dict[str, Any]:
    """Liveness probe — touches storage to surface a readable SQLite file."""
    return health_for_api(_get_storage())
