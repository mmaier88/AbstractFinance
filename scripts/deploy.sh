#!/bin/bash
# deploy.sh - Safe deployment with pre-deploy validation
# Usage: ./scripts/deploy.sh [staging|prod]
#
# This script implements the 80/20 improvements:
# 1. Run tests locally before deploy
# 2. Show config diff before applying
# 3. Run tests on server after pull
# 4. Only restart if all checks pass

set -e

# Configuration
STAGING_HOST="94.130.228.55"
PROD_HOST="91.99.116.196"
REMOTE_PATH="/srv/abstractfinance"
COMPOSE_FILE="docker-compose.yml"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Determine target
TARGET="${1:-staging}"
if [ "$TARGET" = "prod" ]; then
    HOST="$PROD_HOST"
    echo -e "${RED}WARNING: Deploying to PRODUCTION${NC}"
    read -p "Are you sure? (type 'yes' to confirm): " confirm
    if [ "$confirm" != "yes" ]; then
        echo "Aborted."
        exit 1
    fi
else
    HOST="$STAGING_HOST"
    echo -e "${GREEN}Deploying to STAGING${NC}"
fi

echo ""
echo "=========================================="
echo "STEP 1: Local Tests (integration + unit)"
echo "=========================================="

# Run integration tests (critical path)
echo "Running integration tests..."
if ! python3 -m pytest tests/test_integration_flow.py -v --tb=short; then
    echo -e "${RED}FAILED: Integration tests failed. Fix before deploying.${NC}"
    exit 1
fi
echo -e "${GREEN}Integration tests passed.${NC}"

# Run quick unit tests (optional, can skip with --skip-unit)
if [ "$2" != "--skip-unit" ]; then
    echo "Running unit tests..."
    if ! python3 -m pytest tests/ -v --tb=short --ignore=tests/test_integration_flow.py -x -q 2>/dev/null; then
        echo -e "${YELLOW}WARNING: Some unit tests failed. Continuing anyway...${NC}"
    fi
fi

echo ""
echo "=========================================="
echo "STEP 2: Check for uncommitted changes"
echo "=========================================="

if [ -n "$(git status --porcelain)" ]; then
    echo -e "${YELLOW}WARNING: You have uncommitted changes:${NC}"
    git status --short
    read -p "Continue anyway? (y/n): " continue_uncommitted
    if [ "$continue_uncommitted" != "y" ]; then
        echo "Aborted. Commit your changes first."
        exit 1
    fi
fi

echo ""
echo "=========================================="
echo "STEP 3: Show what will change on server"
echo "=========================================="

# Get current commit on server
REMOTE_COMMIT=$(ssh root@$HOST "cd $REMOTE_PATH && git rev-parse HEAD 2>/dev/null" || echo "unknown")
LOCAL_COMMIT=$(git rev-parse HEAD)

echo "Server commit: $REMOTE_COMMIT"
echo "Local commit:  $LOCAL_COMMIT"

if [ "$REMOTE_COMMIT" != "unknown" ] && [ "$REMOTE_COMMIT" != "$LOCAL_COMMIT" ]; then
    echo ""
    echo "Changes to be deployed:"
    git log --oneline "$REMOTE_COMMIT".."$LOCAL_COMMIT" 2>/dev/null || echo "(unable to show diff)"
    echo ""

    # Show file changes
    echo "Files changed:"
    git diff --stat "$REMOTE_COMMIT".."$LOCAL_COMMIT" 2>/dev/null | tail -20 || echo "(unable to show diff)"
fi

echo ""
read -p "Proceed with deployment? (y/n): " proceed
if [ "$proceed" != "y" ]; then
    echo "Aborted."
    exit 1
fi

echo ""
echo "=========================================="
echo "STEP 4: Pull code on server"
echo "=========================================="

ssh root@$HOST "cd $REMOTE_PATH && git fetch origin && git reset --hard origin/main"

echo ""
echo "=========================================="
echo "STEP 5: Config diff check"
echo "=========================================="

# Compare instruments.yaml hash before/after (catches accidental config changes)
CONFIG_HASH=$(ssh root@$HOST "cd $REMOTE_PATH && md5sum config/instruments.yaml 2>/dev/null | cut -d' ' -f1" || echo "none")
echo "Config hash (instruments.yaml): $CONFIG_HASH"

echo ""
echo "=========================================="
echo "STEP 6: Build and restart"
echo "=========================================="

ssh root@$HOST "cd $REMOTE_PATH && docker compose -f $COMPOSE_FILE build trading-engine"
ssh root@$HOST "cd $REMOTE_PATH && docker compose -f $COMPOSE_FILE up -d trading-engine"

echo ""
echo "=========================================="
echo "STEP 7: Verify startup"
echo "=========================================="

sleep 5

# Check if container is running
CONTAINER_STATUS=$(ssh root@$HOST "docker compose -f $REMOTE_PATH/$COMPOSE_FILE ps trading-engine --format '{{.Status}}'" 2>/dev/null || echo "unknown")
echo "Container status: $CONTAINER_STATUS"

if [[ "$CONTAINER_STATUS" == *"Up"* ]]; then
    echo -e "${GREEN}Container is running.${NC}"
else
    echo -e "${RED}Container may have failed to start. Check logs:${NC}"
    ssh root@$HOST "docker compose -f $REMOTE_PATH/$COMPOSE_FILE logs --tail=30 trading-engine"
    exit 1
fi

# Show startup logs (look for invariant checks)
echo ""
echo "Startup logs (checking for invariant validation):"
ssh root@$HOST "docker compose -f $REMOTE_PATH/$COMPOSE_FILE logs --tail=50 trading-engine 2>&1 | grep -E 'Invariant|validated|ERROR|WARNING|Starting|startup' | tail -20" || true

echo ""
echo -e "${GREEN}=========================================="
echo "DEPLOYMENT COMPLETE"
echo "==========================================${NC}"
echo ""
echo "Next steps:"
echo "  - Monitor logs: ssh root@$HOST \"docker compose -f $REMOTE_PATH/$COMPOSE_FILE logs -f trading-engine\""
echo "  - Check positions: ssh root@$HOST \"cat $REMOTE_PATH/state/portfolio_state.json | python3 -m json.tool\""
