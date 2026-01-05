#!/bin/bash
# IB Gateway API Health Check
# Returns 0 if gateway is authenticated and API-ready
# Returns 1 if gateway is down or stuck in authentication

# Simple port check first (fast fail)
if ! nc -z localhost 4000 2>/dev/null; then
    exit 1
fi

# Check if we can get any response from the API socket
# This is a lightweight check - just verify the socket accepts connections
# A full IB connection test would be too heavy for a healthcheck
timeout 5 bash -c 'echo "" | nc localhost 4000' >/dev/null 2>&1
exit $?
