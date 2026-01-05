#!/bin/bash
# Gateway Watchdog - Auto-recovers stuck IB Gateway
# Run via cron: */15 * * * * root /srv/abstractfinance/scripts/gateway_watchdog.sh
#
# This script monitors the IB Gateway health and triggers a restart if:
# 1. The gateway container is unhealthy for too long
# 2. The API connection test fails
#
# Uses the existing restart_gateway.sh for full restart procedure.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_FILE="/var/log/gateway-watchdog.log"
STATE_FILE="/tmp/gateway_watchdog_state"

# How long (in minutes) the gateway can be unhealthy before restart
UNHEALTHY_THRESHOLD_MINUTES=30

# Load environment for Telegram alerts
if [ -f "$PROJECT_DIR/.env" ]; then
    export $(grep -v '^#' "$PROJECT_DIR/.env" | xargs)
fi

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S'): $1" >> "$LOG_FILE"
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

# Check gateway container health status
get_health_status() {
    docker inspect --format='{{.State.Health.Status}}' ibgateway 2>/dev/null || echo "unknown"
}

# Test actual API connection (more reliable than port check)
test_api_connection() {
    timeout 30 docker compose -f "$PROJECT_DIR/docker-compose.yml" exec -T trading-engine python -c "
from ib_insync import IB
import sys
try:
    ib = IB()
    ib.connect('ibgateway', 4000, clientId=99, timeout=20)
    if ib.isConnected():
        accounts = ib.managedAccounts()
        if accounts:
            print('CONNECTED')
            ib.disconnect()
            sys.exit(0)
    print('DISCONNECTED')
    sys.exit(1)
except Exception as e:
    print(f'ERROR: {e}')
    sys.exit(1)
" 2>/dev/null
}

# Read/write state for tracking how long gateway has been unhealthy
read_unhealthy_since() {
    if [ -f "$STATE_FILE" ]; then
        cat "$STATE_FILE"
    else
        echo "0"
    fi
}

write_unhealthy_since() {
    echo "$1" > "$STATE_FILE"
}

clear_unhealthy_state() {
    rm -f "$STATE_FILE"
}

# Main watchdog logic
main() {
    cd "$PROJECT_DIR"

    log "Watchdog check started"

    # First, check Docker health status
    health_status=$(get_health_status)
    log "Docker health status: $health_status"

    # Quick port connectivity check
    if ! nc -z localhost 4000 2>/dev/null; then
        log "Port 4000 not responding"
        health_status="unreachable"
    fi

    # If Docker says healthy or starting, verify with actual API test
    if [ "$health_status" = "healthy" ] || [ "$health_status" = "starting" ]; then
        api_result=$(test_api_connection 2>&1)
        if echo "$api_result" | grep -q "CONNECTED"; then
            log "API connection verified - gateway is healthy"
            clear_unhealthy_state
            exit 0
        else
            log "API connection failed despite healthy status: $api_result"
            health_status="api_failed"
        fi
    fi

    # Gateway is unhealthy - track how long
    current_time=$(date +%s)
    unhealthy_since=$(read_unhealthy_since)

    if [ "$unhealthy_since" = "0" ]; then
        # Just became unhealthy - record timestamp
        write_unhealthy_since "$current_time"
        log "Gateway became unhealthy, starting timer"
        send_alert "IB Gateway health check failed. Monitoring for recovery..."
        exit 0
    fi

    # Calculate how long it's been unhealthy
    unhealthy_duration=$(( (current_time - unhealthy_since) / 60 ))
    log "Gateway has been unhealthy for ${unhealthy_duration} minutes"

    # Check if we've exceeded the threshold
    if [ "$unhealthy_duration" -ge "$UNHEALTHY_THRESHOLD_MINUTES" ]; then
        log "Unhealthy threshold exceeded (${unhealthy_duration}m >= ${UNHEALTHY_THRESHOLD_MINUTES}m) - triggering restart"
        send_alert "*AUTO-RECOVERY*: Gateway unhealthy for ${unhealthy_duration} minutes. Triggering automatic restart..."

        # Clear state before restart
        clear_unhealthy_state

        # Trigger the full restart procedure
        "$SCRIPT_DIR/restart_gateway.sh"

        log "Restart procedure completed"
    else
        remaining=$((UNHEALTHY_THRESHOLD_MINUTES - unhealthy_duration))
        log "Waiting ${remaining} more minutes before auto-restart"
    fi
}

# Run main function
main "$@"
