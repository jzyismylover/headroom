"""Opt-in pipeline extension that captures every proxied request into the
Diff Explorer store (``HEADROOM_STORE_URL``).

Why this exists
---------------
The Diff Explorer dashboard reads from ``HEADROOM_STORE_URL`` (default
``tempfile.gettempdir()/headroom.db``). The only place that *writes* to it
is :class:`headroom.diff_explorer.message_capture.MessageCaptureExtension`,
which is registered by :class:`headroom.HeadroomClient` when
``HeadroomConfig.capture_messages=True``. That means any traffic that does
not go through the Python SDK (Claude Code, Codex CLI, Aider, raw curl,
anything) leaves the dashboard empty.

The proxy already emits ``PipelineStage.INPUT_RECEIVED`` and
``PipelineStage.INPUT_COMPRESSED`` events on its own ``pipeline_extensions``
bus (see ``headroom.proxy.handlers.anthropic`` and ``.openai``). So we just
register a ``MessageCaptureExtension`` on that bus and every request the
proxy handles — regardless of upstream client — lands in the same store
the SDK uses. The two writers are independent; the dashboard groups by
``request_id``, so duplicate writes are surfaced as siblings rather than
overwriting each other.

Configuration
-------------
- Default: ON. Set ``HEADROOM_PROXY_CAPTURE_DIFF=0`` to opt out.
- Storage: whatever ``HEADROOM_STORE_URL`` points at (matching the
  ``diff_explorer`` API). Falling back to ``tempfile.gettempdir()/headroom.db``
  when unset, mirroring ``headroom.diff_explorer.api._get_storage``.

Fail-open
---------
``MessageCaptureExtension.on_pipeline_event`` already wraps its handler in a
try/except (``message_capture.py``); a broken capture path will never break
the proxy forwarding path.
"""

from __future__ import annotations

import logging
import os
import tempfile

logger = logging.getLogger(__name__)

CAPTURE_DIFF_ENV = "HEADROOM_PROXY_CAPTURE_DIFF"


def diff_capture_extension_from_env() -> object | None:
    """Build a ``MessageCaptureExtension`` when enabled, else ``None``.

    Mirrors :func:`headroom.proxy.probe_recorder.probe_recorder_from_env`:
    one env var, ``None`` when off so ``pipeline_extensions.append(...)``
    is a no-op. Returns ``None`` (rather than raising) if the extension or
    its storage dependency cannot be imported — the proxy must keep working
    even if the diff_explorer submodule is broken in a particular install.
    """
    if os.environ.get(CAPTURE_DIFF_ENV, "1") not in ("1", "true", "TRUE", "yes"):
        logger.info("Proxy diff capture: disabled via %s", CAPTURE_DIFF_ENV)
        return None

    try:
        from headroom.diff_explorer.message_capture import MessageCaptureExtension
        from headroom.storage import create_storage
    except Exception as exc:  # noqa: BLE001 — import failure must not crash proxy
        logger.warning("Proxy diff capture: import failed (%s); capture disabled", exc)
        return None

    # Same resolution priority as headroom.diff_explorer.api._get_storage:
    # HEADROOM_STORE_URL → tempfile.gettempdir()/headroom.db.
    store_url = os.environ.get("HEADROOM_STORE_URL")
    if not store_url:
        db_path = os.path.join(tempfile.gettempdir(), "headroom.db")
        store_url = f"sqlite:///{db_path}"

    try:
        storage = create_storage(store_url)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Proxy diff capture: cannot open store %s (%s); capture disabled",
            store_url,
            exc,
        )
        return None

    extension = MessageCaptureExtension(storage)
    logger.info(
        "Proxy diff capture: ENABLED → %s (opt out via %s=0)",
        store_url,
        CAPTURE_DIFF_ENV,
    )
    return extension
