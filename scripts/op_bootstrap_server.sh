#!/usr/bin/env bash
# op_bootstrap_server.sh - Bootstrap a new server with secrets from 1Password
#
# Usage: ./op_bootstrap_server.sh [staging|prod]
#
# This script:
#   1. Installs 1Password CLI if not present
#   2. Validates service account access
#   3. Fetches .env file from 1Password
#   4. Sets up proper permissions
#
# Required environment variables:
#   OP_SERVICE_ACCOUNT_TOKEN - 1Password service account token
#
# Example:
#   export OP_SERVICE_ACCOUNT_TOKEN="ops_eyJ..."
#   ./op_bootstrap_server.sh staging

set -euo pipefail

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-/srv/abstractfinance}"
APP_USER="${APP_USER:-abstractfinance}"

# Vault configuration
VAULT_STAGING="AF - Trading Infra - Staging"
VAULT_PROD="AF - Trading Infra - Prod"
ENV_ITEM_STAGING="abstractfinance.staging.env"
ENV_ITEM_PROD="abstractfinance.prod.env"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1" >&2
}

# Parse arguments
ENVIRONMENT="${1:-staging}"

if [[ "${ENVIRONMENT}" != "staging" && "${ENVIRONMENT}" != "prod" ]]; then
    log_error "Invalid environment: ${ENVIRONMENT}"
    echo "Usage: $0 [staging|prod]"
    exit 1
fi

log_info "Bootstrapping server for environment: ${ENVIRONMENT}"

# Check for required environment variable
if [[ -z "${OP_SERVICE_ACCOUNT_TOKEN:-}" ]]; then
    log_error "OP_SERVICE_ACCOUNT_TOKEN environment variable is not set"
    echo ""
    echo "Set it with:"
    echo "  export OP_SERVICE_ACCOUNT_TOKEN='ops_eyJ...'"
    exit 1
fi

# Install 1Password CLI if not present
install_op_cli() {
    if command -v op &> /dev/null; then
        log_info "1Password CLI already installed: $(op --version)"
        return 0
    fi

    log_info "Installing 1Password CLI..."

    # Detect OS
    if [[ -f /etc/debian_version ]]; then
        # Debian/Ubuntu
        curl -sS https://downloads.1password.com/linux/keys/1password.asc | \
            gpg --dearmor --output /usr/share/keyrings/1password-archive-keyring.gpg

        echo "deb [arch=amd64 signed-by=/usr/share/keyrings/1password-archive-keyring.gpg] https://downloads.1password.com/linux/debian/amd64 stable main" | \
            tee /etc/apt/sources.list.d/1password.list

        apt-get update && apt-get install -y 1password-cli

    elif [[ -f /etc/redhat-release ]]; then
        # RHEL/CentOS
        rpm --import https://downloads.1password.com/linux/keys/1password.asc

        cat > /etc/yum.repos.d/1password.repo << 'EOF'
[1password]
name=1Password Stable Channel
baseurl=https://downloads.1password.com/linux/rpm/stable/$basearch
enabled=1
gpgcheck=1
repo_gpgcheck=1
gpgkey=https://downloads.1password.com/linux/keys/1password.asc
EOF

        yum install -y 1password-cli

    else
        log_error "Unsupported OS. Please install 1Password CLI manually."
        exit 1
    fi

    log_info "1Password CLI installed: $(op --version)"
}

# Validate 1Password access
validate_op_access() {
    log_info "Validating 1Password access..."

    if ! op vault list > /dev/null 2>&1; then
        log_error "Failed to authenticate with 1Password"
        exit 1
    fi

    log_info "1Password access validated"
}

# Fetch environment file
fetch_env_file() {
    local vault="$1"
    local item="$2"
    local output_path="${APP_DIR}/.env"

    log_info "Fetching .env from 1Password..."

    "${SCRIPT_DIR}/op_fetch_env.sh" "${vault}" "${item}" "${output_path}"

    # Set ownership if app user exists
    if id "${APP_USER}" &>/dev/null; then
        chown "${APP_USER}:${APP_USER}" "${output_path}"
        log_info "Set ownership to ${APP_USER}"
    fi
}

# Create application directory
setup_app_directory() {
    if [[ ! -d "${APP_DIR}" ]]; then
        log_info "Creating application directory: ${APP_DIR}"
        mkdir -p "${APP_DIR}"
    fi

    # Create app user if it doesn't exist
    if ! id "${APP_USER}" &>/dev/null; then
        log_info "Creating application user: ${APP_USER}"
        useradd -r -s /bin/false "${APP_USER}" || true
    fi

    # Set ownership
    chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"
}

# Main execution
main() {
    log_info "=== AbstractFinance Server Bootstrap ==="

    # Install op CLI
    install_op_cli

    # Validate access
    validate_op_access

    # Setup directories
    setup_app_directory

    # Determine vault and item based on environment
    if [[ "${ENVIRONMENT}" == "staging" ]]; then
        VAULT="${VAULT_STAGING}"
        ENV_ITEM="${ENV_ITEM_STAGING}"
    else
        VAULT="${VAULT_PROD}"
        ENV_ITEM="${ENV_ITEM_PROD}"
    fi

    # Fetch .env
    fetch_env_file "${VAULT}" "${ENV_ITEM}"

    log_info "=== Bootstrap Complete ==="
    echo ""
    log_info "Next steps:"
    echo "  1. Verify .env file: cat ${APP_DIR}/.env"
    echo "  2. Clone repo: git clone <repo> ${APP_DIR}"
    echo "  3. Start services: cd ${APP_DIR} && docker compose up -d"
}

main
