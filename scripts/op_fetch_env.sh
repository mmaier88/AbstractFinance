#!/usr/bin/env bash
# op_fetch_env.sh - Fetch a full .env file from 1Password and write to disk
#
# Usage: ./op_fetch_env.sh <vault> <item> <output_path>
#
# Arguments:
#   vault       - Vault name (e.g., "AF - Trading Infra - Staging")
#   item        - Item name containing .env in notesPlain (e.g., "abstractfinance.staging.env")
#   output_path - Where to write the .env file (e.g., "/srv/abstractfinance/.env")
#
# Required environment variables:
#   OP_SERVICE_ACCOUNT_TOKEN - 1Password service account token
#
# The .env content should be stored in a Secure Note item with the full
# .env file contents in the "notesPlain" field.
#
# Example:
#   ./op_fetch_env.sh "AF - Trading Infra - Staging" "abstractfinance.staging.env" ".env"

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1" >&2
}

# Validate arguments
if [[ $# -lt 3 ]]; then
    log_error "Missing required arguments"
    echo ""
    echo "Usage: $0 <vault> <item> <output_path>"
    echo ""
    echo "Arguments:"
    echo "  vault       - Vault name (e.g., 'AF - Trading Infra - Staging')"
    echo "  item        - Item name (e.g., 'abstractfinance.staging.env')"
    echo "  output_path - Output file path (e.g., '.env')"
    echo ""
    echo "Example:"
    echo "  $0 'AF - Trading Infra - Staging' 'abstractfinance.staging.env' '/srv/abstractfinance/.env'"
    exit 1
fi

VAULT="$1"
ITEM="$2"
OUTPUT_PATH="$3"

# Check for required environment variable
if [[ -z "${OP_SERVICE_ACCOUNT_TOKEN:-}" ]]; then
    log_error "OP_SERVICE_ACCOUNT_TOKEN environment variable is not set"
    exit 1
fi

# Check if op CLI is installed
if ! command -v op &> /dev/null; then
    log_error "1Password CLI 'op' is not installed"
    exit 1
fi

# Create parent directory if it doesn't exist
OUTPUT_DIR=$(dirname "${OUTPUT_PATH}")
if [[ ! -d "${OUTPUT_DIR}" ]]; then
    log_info "Creating directory: ${OUTPUT_DIR}"
    mkdir -p "${OUTPUT_DIR}"
fi

# Fetch the .env content from notesPlain field
SECRET_REF="op://${VAULT}/${ITEM}/notesPlain"

log_info "Fetching .env from 1Password: ${VAULT}/${ITEM}"

if ! op read "${SECRET_REF}" > "${OUTPUT_PATH}" 2>&1; then
    log_error "Failed to fetch .env from 1Password"
    rm -f "${OUTPUT_PATH}"  # Clean up partial file
    exit 1
fi

# Set secure permissions
chmod 600 "${OUTPUT_PATH}"

# Verify file was written
if [[ -s "${OUTPUT_PATH}" ]]; then
    LINE_COUNT=$(wc -l < "${OUTPUT_PATH}")
    log_info "Successfully wrote ${LINE_COUNT} lines to ${OUTPUT_PATH}"
    log_info "Permissions set to 600 (owner read/write only)"
else
    log_error "Output file is empty - check item name and field"
    exit 1
fi
