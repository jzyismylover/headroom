#!/usr/bin/env bash
# Start the Headroom proxy server, pointing both the Anthropic and OpenAI
# upstream URLs at the same gateway (api.minimaxi.com).
#
# Notes:
#   * Each line-ending backslash must be followed by a NEWLINE, not a space,
#     otherwise bash escapes the space and argparser receives a malformed
#     argument (e.g. " --openai-api-url" with a leading space).
#   * HEADROOM_STORE_URL is consumed by the diff_explorer, not the proxy
#     server itself, but it's exported here so that dashboards / explorers
#     pointed at the same SQLite file work out of the box.
#   * As of the proxy-side capture change, every request the proxy handles
#     (Claude Code, Codex CLI, raw curl, etc.) is also written to
#     $HEADROOM_STORE_URL via a MessageCaptureExtension registered on the
#     proxy's own pipeline_extensions bus — the SDK is no longer required
#     to populate the Diff Explorer. Opt out with HEADROOM_PROXY_CAPTURE_DIFF=0.

set -euo pipefail

export HEADROOM_STORE_URL="sqlite:////Users/mrj/.headroom/diff.db"
# Default ON; set to 0 to disable proxy-side diff capture.
export HEADROOM_PROXY_CAPTURE_DIFF="${HEADROOM_PROXY_CAPTURE_DIFF:-1}"

exec uv run python -m headroom.proxy.server \
  --host 127.0.0.1 \
  --anthropic-api-url https://api.minimaxi.com/anthropic \
  --openai-api-url https://api.minimaxi.com/v1
