#!/bin/bash
#
# AbstractFinance Failover Script
# Automates failover from primary to standby server
#
# Usage:
#   ./failover.sh [--force]
#
# Options:
#   --force    Skip confirmation prompts
#

set -e

# Server configuration
PRIMARY_IP="94.130.228.55"
STANDBY_IP="46.224.46.117"
STANDBY_WG="10.0.0.2"  # WireGuard IP
APP_DIR="/srv/abstractfinance"
HEALTH_ENDPOINT="http://%s:8080/health"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Check if running with --force flag
FORCE=false
if [[ "$1" == "--force" ]]; then
    FORCE=true
fi

check_primary_health() {
    log_info "Checking primary server health..."

    # Try health endpoint
    if curl -sf --connect-timeout 5 "$(printf "$HEALTH_ENDPOINT" "$PRIMARY_IP")" > /dev/null 2>&1; then
        return 0  # healthy
    fi

    # Try SSH
    if ssh -o ConnectTimeout=5 -o BatchMode=yes root@$PRIMARY_IP "echo ok" > /dev/null 2>&1; then
        return 1  # SSH works but health check fails
    fi

    return 2  # completely unreachable
}

check_standby_ready() {
    log_info "Checking standby server readiness..."

    # Check SSH access
    if ! ssh -o ConnectTimeout=10 -o BatchMode=yes root@$STANDBY_IP "echo ok" > /dev/null 2>&1; then
        log_error "Cannot SSH to standby server"
        return 1
    fi

    # Check Docker is installed
    if ! ssh root@$STANDBY_IP "docker --version" > /dev/null 2>&1; then
        log_error "Docker not installed on standby"
        return 1
    fi

    # Check app directory exists
    if ! ssh root@$STANDBY_IP "test -d $APP_DIR"; then
        log_error "App directory $APP_DIR not found on standby"
        return 1
    fi

    # Check .env exists
    if ! ssh root@$STANDBY_IP "test -f $APP_DIR/.env"; then
        log_warn ".env file missing on standby - will need to copy"
        return 2
    fi

    log_info "Standby server is ready"
    return 0
}

stop_primary() {
    log_info "Stopping services on primary..."

    if ssh -o ConnectTimeout=10 root@$PRIMARY_IP "cd $APP_DIR && docker compose down" 2>/dev/null; then
        log_info "Primary services stopped gracefully"
        return 0
    else
        log_warn "Could not stop primary (may be unreachable)"
        return 1
    fi
}

sync_state_from_primary() {
    log_info "Syncing state from primary to standby..."

    # Try direct transfer via WireGuard first
    if ssh root@$STANDBY_IP "ping -c 1 10.0.0.1" > /dev/null 2>&1; then
        log_info "Using WireGuard tunnel for sync..."
        ssh root@$STANDBY_IP "rsync -avz --delete root@10.0.0.1:$APP_DIR/state/ $APP_DIR/state/"
        ssh root@$STANDBY_IP "rsync -avz root@10.0.0.1:$APP_DIR/.env $APP_DIR/.env"
    else
        # Fallback: sync through local machine
        log_info "WireGuard unavailable, syncing via local machine..."

        # Create temp directory
        TMP_DIR=$(mktemp -d)

        # Copy from primary
        if scp -r root@$PRIMARY_IP:$APP_DIR/state/ "$TMP_DIR/state/" 2>/dev/null; then
            # Copy to standby
            scp -r "$TMP_DIR/state/" root@$STANDBY_IP:$APP_DIR/
            log_info "State files synced successfully"
        else
            log_warn "Could not copy state from primary"
        fi

        # Copy .env if accessible
        if scp root@$PRIMARY_IP:$APP_DIR/.env "$TMP_DIR/.env" 2>/dev/null; then
            scp "$TMP_DIR/.env" root@$STANDBY_IP:$APP_DIR/.env
            log_info ".env synced successfully"
        fi

        rm -rf "$TMP_DIR"
    fi
}

fetch_env_from_1password() {
    log_info "Fetching .env from 1Password..."

    # Check if OP_SERVICE_ACCOUNT_TOKEN is set
    if [[ -z "$OP_SERVICE_ACCOUNT_TOKEN" ]]; then
        log_error "OP_SERVICE_ACCOUNT_TOKEN not set"
        log_info "Export it or run: source scripts/op_inject_env.sh"
        return 1
    fi

    # Fetch and deploy to standby
    ENV_CONTENT=$(op read "op://AF - Trading Infra - Staging/abstractfinance.staging.env/notesPlain")

    if [[ -n "$ENV_CONTENT" ]]; then
        echo "$ENV_CONTENT" | ssh root@$STANDBY_IP "cat > $APP_DIR/.env && chmod 600 $APP_DIR/.env"
        log_info ".env deployed from 1Password"
        return 0
    else
        log_error "Failed to fetch .env from 1Password"
        return 1
    fi
}

start_standby() {
    log_info "Starting services on standby..."

    # Update code to latest
    log_info "Pulling latest code..."
    ssh root@$STANDBY_IP "cd $APP_DIR && git fetch origin && git reset --hard origin/main"

    # Pull Docker images
    log_info "Pulling Docker images..."
    ssh root@$STANDBY_IP "cd $APP_DIR && docker compose pull"

    # Start services
    log_info "Starting Docker services..."
    ssh root@$STANDBY_IP "cd $APP_DIR && docker compose up -d"

    # Wait for services to start
    log_info "Waiting for services to initialize (90 seconds for IB Gateway TOTP)..."
    sleep 90

    # Check health
    if curl -sf --connect-timeout 10 "$(printf "$HEALTH_ENDPOINT" "$STANDBY_IP")" > /dev/null 2>&1; then
        log_info "Standby health check PASSED"
        return 0
    else
        log_warn "Standby health check failed - services may still be starting"
        return 1
    fi
}

verify_ib_connection() {
    log_info "Verifying IB Gateway connection..."

    # Wait a bit more for IB Gateway to fully connect
    sleep 30

    # Check IB Gateway logs
    IB_STATUS=$(ssh root@$STANDBY_IP "docker logs ibgateway 2>&1 | tail -20")

    if echo "$IB_STATUS" | grep -q "Authenticating"; then
        log_info "IB Gateway is authenticating..."
        return 0
    elif echo "$IB_STATUS" | grep -q "entered maintenance cycle"; then
        log_info "IB Gateway connected (in maintenance cycle)"
        return 0
    else
        log_warn "IB Gateway status unclear - check manually"
        echo "$IB_STATUS"
        return 1
    fi
}

print_summary() {
    echo ""
    echo "========================================"
    echo "         FAILOVER COMPLETE"
    echo "========================================"
    echo ""
    echo "New primary server: $STANDBY_IP"
    echo ""
    echo "Access points:"
    echo "  - Health:     http://$STANDBY_IP:8080/health"
    echo "  - Grafana:    http://$STANDBY_IP:3000"
    echo "  - Prometheus: http://$STANDBY_IP:9090"
    echo ""
    echo "Next steps:"
    echo "  1. Verify trading is working via Grafana"
    echo "  2. Update external monitors to point to new IP"
    echo "  3. Investigate why primary failed"
    echo "  4. Prepare primary for failback when ready"
    echo ""
}

# Main failover process
main() {
    echo "========================================"
    echo "    AbstractFinance Failover Script"
    echo "========================================"
    echo ""
    echo "Primary:  $PRIMARY_IP"
    echo "Standby:  $STANDBY_IP"
    echo ""

    # Check primary status
    check_primary_health
    PRIMARY_STATUS=$?

    case $PRIMARY_STATUS in
        0)
            log_warn "Primary server appears HEALTHY"
            if [[ "$FORCE" != "true" ]]; then
                read -p "Are you sure you want to failover? (yes/no): " CONFIRM
                if [[ "$CONFIRM" != "yes" ]]; then
                    log_info "Failover cancelled"
                    exit 0
                fi
            fi
            ;;
        1)
            log_warn "Primary SSH accessible but health check failing"
            ;;
        2)
            log_error "Primary server is UNREACHABLE"
            log_info "Proceeding with emergency failover..."
            ;;
    esac

    # Check standby readiness
    check_standby_ready
    STANDBY_STATUS=$?

    if [[ $STANDBY_STATUS -eq 1 ]]; then
        log_error "Standby server is not ready. Cannot proceed."
        exit 1
    fi

    # Confirmation
    if [[ "$FORCE" != "true" ]]; then
        echo ""
        read -p "Proceed with failover to $STANDBY_IP? (yes/no): " CONFIRM
        if [[ "$CONFIRM" != "yes" ]]; then
            log_info "Failover cancelled"
            exit 0
        fi
    fi

    echo ""
    log_info "Starting failover process..."

    # Step 1: Stop primary if accessible
    if [[ $PRIMARY_STATUS -ne 2 ]]; then
        stop_primary || true
    fi

    # Step 2: Sync state
    if [[ $PRIMARY_STATUS -ne 2 ]]; then
        sync_state_from_primary || true
    elif [[ $STANDBY_STATUS -eq 2 ]]; then
        # Need .env but primary is unreachable
        log_info "Primary unreachable and .env missing on standby"
        fetch_env_from_1password || {
            log_error "Cannot proceed without .env"
            exit 1
        }
    fi

    # Step 3: Wait for IBKR session to timeout (if primary unreachable)
    if [[ $PRIMARY_STATUS -eq 2 ]]; then
        log_warn "Waiting 5 minutes for old IBKR session to timeout..."
        log_info "(IBKR only allows one concurrent session per account)"
        sleep 300
    fi

    # Step 4: Start standby
    start_standby

    # Step 5: Verify IB connection
    verify_ib_connection || true

    # Print summary
    print_summary
}

main "$@"
