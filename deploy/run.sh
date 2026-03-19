#!/usr/bin/env bash
# =============================================================================
# Manual launch script (alternative to systemd)
# Optimized for Contabo VPS: 64GB RAM, 12 CPU, 350GB NVMe
# =============================================================================
set -euo pipefail

APP_DIR="/opt/bet365"
cd "$APP_DIR"

# Load config
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# Defaults
PROXY_URLS="${PROXY_URLS:?ERROR: Set PROXY_URLS in .env}"
WS_PORT="${WS_PORT:-8765}"
WS_TOKEN="${WS_TOKEN:-}"
TABS_PER_BROWSER="${TABS_PER_BROWSER:-8}"
MAX_WORKERS="${MAX_WORKERS:-0}"
CONCURRENCY="${CONCURRENCY:-10}"
SNAPSHOT_INTERVAL="${SNAPSHOT_INTERVAL:-10}"
LTESOCKS_API_TOKEN="${LTESOCKS_API_TOKEN:-}"
LTESOCKS_PORT_IDS="${LTESOCKS_PORT_IDS:-}"

# Build proxy args
PROXY_ARGS=""
IFS="," read -ra URLS <<< "$PROXY_URLS"
for url in "${URLS[@]}"; do
    PROXY_ARGS+=" --proxy $url"
done

# Build LTESocks args
LTESOCKS_ARGS=""
if [ -n "$LTESOCKS_API_TOKEN" ] && [ "$LTESOCKS_API_TOKEN" != "your_api_token_here" ]; then
    LTESOCKS_ARGS="--ltesocks-token $LTESOCKS_API_TOKEN --ltesocks-ports $LTESOCKS_PORT_IDS"
fi

# Create log directory
mkdir -p logs

echo "Starting bet365 stream..."
echo "  Proxies:      ${#URLS[@]} configured"
echo "  WS port:      $WS_PORT"
echo "  Tabs/browser: $TABS_PER_BROWSER"
echo "  Concurrency:  $CONCURRENCY"
echo "  Max workers:  $MAX_WORKERS (0=unlimited)"
echo "  LTESocks:     ${LTESOCKS_API_TOKEN:+enabled}${LTESOCKS_API_TOKEN:-disabled}"

# Run with output split: JSONL to file, logs to terminal
exec venv/bin/python main.py \
    --all-markets \
    $PROXY_ARGS \
    $LTESOCKS_ARGS \
    --ws-port "$WS_PORT" \
    ${WS_TOKEN:+--ws-token "$WS_TOKEN"} \
    --tabs-per-browser "$TABS_PER_BROWSER" \
    --max-workers "$MAX_WORKERS" \
    --concurrency "$CONCURRENCY" \
    --snapshot-interval "$SNAPSHOT_INTERVAL" \
    > >(tee -a logs/stream.jsonl) \
    2>&1
