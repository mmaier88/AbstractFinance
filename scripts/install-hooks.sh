#!/bin/bash
# Install git hooks for AbstractFinance
# Run from repo root: ./scripts/install-hooks.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

echo "Installing git hooks..."

# Install pre-commit hook
if [ -f "$REPO_ROOT/git-hooks/pre-commit" ]; then
    cp "$REPO_ROOT/git-hooks/pre-commit" "$REPO_ROOT/.git/hooks/pre-commit"
    chmod +x "$REPO_ROOT/.git/hooks/pre-commit"
    echo "Installed pre-commit hook (secret detection)"
else
    echo "Warning: git-hooks/pre-commit not found"
fi

echo "Git hooks installed successfully!"
echo ""
echo "Installed hooks:"
ls -la "$REPO_ROOT/.git/hooks/" | grep -v sample | grep -v "^\." | tail -n +2
