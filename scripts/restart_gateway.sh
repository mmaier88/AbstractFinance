#!/bin/bash
# IB Gateway scheduled restart script
# Run via cron: 0 22 * * 0 root /srv/abstractfinance/scripts/restart_gateway.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_FILE="/var/log/ibgateway-maintenance.log"

# Load environment for Telegram alerts
if [ -f "$PROJECT_DIR/.env" ]; then
    export $(grep -v '^#' "$PROJECT_DIR/.env" | xargs)
fi

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S'): $1" >> "$LOG_FILE"
    echo "$1"
}

send_alert() {
    local message="$1"
    if [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
        curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            -d "chat_id=${TELEGRAM_CHAT_ID}" \
            -d "text=$message" \
            -d "parse_mode=Markdown" > /dev/null
    fi
}

log "Starting scheduled IB Gateway restart"

cd "$PROJECT_DIR"

# Stop the gateway gracefully
log "Stopping IB Gateway..."
docker compose stop ibgateway

# Wait for clean shutdown
sleep 10

# Clear cached session data (prevents stale sessions)
log "Clearing gateway data volume..."
docker volume rm abstractfinance_ibgateway-data 2>/dev/null || true

# Start the gateway
log "Starting IB Gateway..."
docker compose up -d ibgateway

# Wait for gateway to initialize
log "Waiting for gateway initialization (120s)..."
sleep 120

# Verify connection
log "Verifying gateway connection..."
VERIFY_RESULT=$(docker compose exec -T trading-engine python -c "
from ib_insync import IB
import sys
try:
    ib = IB()
    ib.connect('ibgateway', 4004, clientId=99, timeout=30)
    if ib.isConnected():
        print('SUCCESS')
        ib.disconnect()
        sys.exit(0)
    else:
        print('FAILED: Not connected')
        sys.exit(1)
except Exception as e:
    print(f'FAILED: {e}')
    sys.exit(1)
" 2>&1)

if echo "$VERIFY_RESULT" | grep -q "SUCCESS"; then
    log "Gateway restart successful"
    send_alert "IB Gateway scheduled restart completed successfully"
else
    log "Gateway restart FAILED: $VERIFY_RESULT"
    send_alert "*ALERT*: IB Gateway restart FAILED!\n\n$VERIFY_RESULT"
    exit 1
fi

# Restart trading engine to re-establish connections
log "Restarting trading engine..."
docker compose restart trading-engine

log "Scheduled maintenance complete"
