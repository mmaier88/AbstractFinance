#!/bin/bash
#
# AbstractFinance State Sync Script
# Syncs state from primary to standby for warm failover readiness
#
# Usage:
#   ./sync_state.sh
#
# Recommended cron (every 4 hours):
#   0 */4 * * * /srv/abstractfinance/scripts/sync_state.sh >> /var/log/abstractfinance/state_sync.log 2>&1
#

set -e

# Configuration
PRIMARY_IP="94.130.228.55"
STANDBY_IP="46.224.46.117"
PRIMARY_WG="10.0.0.1"
STANDBY_WG="10.0.0.2"
APP_DIR="/srv/abstractfinance"

TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

log() {
    echo "[$TIMESTAMP] $1"
}

# Determine which server we're running on
MY_IP=$(hostname -I | awk '{print $1}')

if [[ "$MY_IP" == "$PRIMARY_IP" ]] || [[ "$MY_IP" == "$PRIMARY_WG" ]] || [[ "$MY_IP" == "10.0.0.1" ]]; then
    # Running on primary - sync TO standby
    log "Running on PRIMARY, syncing state to STANDBY..."

    # Check if standby is reachable via WireGuard
    if ping -c 1 -W 5 $STANDBY_WG > /dev/null 2>&1; then
        TARGET="root@$STANDBY_WG"
        log "Using WireGuard tunnel ($STANDBY_WG)"
    elif ping -c 1 -W 5 $STANDBY_IP > /dev/null 2>&1; then
        TARGET="root@$STANDBY_IP"
        log "Using public IP ($STANDBY_IP)"
    else
        log "ERROR: Standby server unreachable"
        exit 1
    fi

    # Sync state directory
    log "Syncing state/ directory..."
    rsync -avz --delete $APP_DIR/state/ $TARGET:$APP_DIR/state/

    # Sync .env (careful - don't overwrite if standby has customizations)
    log "Syncing .env file..."
    rsync -avz $APP_DIR/.env $TARGET:$APP_DIR/.env

    # Update code on standby
    log "Updating code on standby..."
    ssh $TARGET "cd $APP_DIR && git fetch origin && git reset --hard origin/main" || true

    # Pull latest Docker images on standby (so failover is faster)
    log "Pre-pulling Docker images on standby..."
    ssh $TARGET "cd $APP_DIR && docker compose pull" || true

    log "State sync completed successfully"

elif [[ "$MY_IP" == "$STANDBY_IP" ]] || [[ "$MY_IP" == "$STANDBY_WG" ]] || [[ "$MY_IP" == "10.0.0.2" ]]; then
    # Running on standby - pull FROM primary
    log "Running on STANDBY, pulling state from PRIMARY..."

    # Check if primary is reachable via WireGuard
    if ping -c 1 -W 5 $PRIMARY_WG > /dev/null 2>&1; then
        SOURCE="root@$PRIMARY_WG"
        log "Using WireGuard tunnel ($PRIMARY_WG)"
    elif ping -c 1 -W 5 $PRIMARY_IP > /dev/null 2>&1; then
        SOURCE="root@$PRIMARY_IP"
        log "Using public IP ($PRIMARY_IP)"
    else
        log "ERROR: Primary server unreachable"
        exit 1
    fi

    # Sync state directory
    log "Syncing state/ directory..."
    rsync -avz --delete $SOURCE:$APP_DIR/state/ $APP_DIR/state/

    # Sync .env
    log "Syncing .env file..."
    rsync -avz $SOURCE:$APP_DIR/.env $APP_DIR/.env

    # Update code
    log "Updating local code..."
    cd $APP_DIR && git fetch origin && git reset --hard origin/main || true

    # Pull latest Docker images
    log "Pre-pulling Docker images..."
    docker compose pull || true

    log "State sync completed successfully"

else
    log "ERROR: Cannot determine which server this is (IP: $MY_IP)"
    exit 1
fi
