#!/usr/bin/env python3
"""
MCP Secret Fetching Module - Secure 1Password Integration

Fetches secrets from the 1Password MCP server via HTTP API.
Used by the dc-mcp wrapper to inject secrets into Docker Compose.
"""
import os
import sys
import json
import urllib.request
import urllib.error

# MCP Server Configuration
MCP_SERVER_URL = "http://91.99.97.249:8000"
MCP_SECRET_ENDPOINT = f"{MCP_SERVER_URL}/secret"
MCP_API_KEY = os.getenv("MCP_API_KEY")


def fetch_secret(vault: str, item: str, field: str = "password") -> str:
    """Fetch a secret from the 1Password MCP server."""
    if not MCP_API_KEY:
        raise RuntimeError("MCP_API_KEY not set")

    headers = {
        "Authorization": f"Bearer {MCP_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = json.dumps({
        "vault": vault,
        "item": item,
        "field": field
    }).encode('utf-8')

    req = urllib.request.Request(
        MCP_SECRET_ENDPOINT,
        data=payload,
        headers=headers,
        method='POST'
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode('utf-8'))
            return data.get("value", "")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError(f"Secret not found: op://{vault}/{item}/{field}")
        raise RuntimeError(f"MCP error ({e.code}): {e.read().decode()}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Failed to connect to MCP server: {e}")


def resolve_op_reference(ref: str) -> str:
    """Resolve an op:// reference to its value."""
    if not ref.startswith("op://"):
        return ref

    # Parse op://vault/item/field
    parts = ref[5:].split("/")
    if len(parts) < 2:
        raise RuntimeError(f"Invalid op:// reference: {ref}")

    vault = parts[0]
    item = parts[1]
    field = parts[2] if len(parts) > 2 else "password"

    return fetch_secret(vault, item, field)


def process_env_template(template_path: str) -> dict:
    """Process a .env.template file and resolve all op:// references."""
    env_vars = {}

    with open(template_path, 'r') as f:
        for line in f:
            line = line.strip()
            # Skip comments and empty lines
            if not line or line.startswith('#'):
                continue

            # Parse KEY=VALUE
            if '=' not in line:
                continue

            key, value = line.split('=', 1)
            key = key.strip()
            value = value.strip()

            # Resolve op:// references
            if value.startswith("op://"):
                try:
                    value = resolve_op_reference(value)
                except Exception as e:
                    print(f"Warning: Failed to resolve {key}: {e}", file=sys.stderr)
                    continue

            env_vars[key] = value

    return env_vars


def main():
    """Main entry point for CLI usage."""
    if len(sys.argv) < 2:
        print("Usage: mcp_secrets.py <template_path>", file=sys.stderr)
        sys.exit(1)

    template_path = sys.argv[1]

    if not os.path.exists(template_path):
        print(f"Error: Template file not found: {template_path}", file=sys.stderr)
        sys.exit(1)

    try:
        env_vars = process_env_template(template_path)
        # Output as shell-compatible export statements
        for key, value in env_vars.items():
            # Escape single quotes in value
            escaped = value.replace("'", "'\\''")
            print(f"export {key}='{escaped}'")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
