# 1Password Service Accounts

Service Accounts provide secure, non-interactive access to 1Password for CI/CD and server automation.

## Service Account Configuration

### 1. svc-af-staging

**Purpose**: Staging server and CI/CD pipeline access

**Vault Access**:
- AF - Trading Infra - Staging (Read)
- AF - Servers & SSH (Read)

**Token Storage**:
- GitHub Secret: `OP_SERVICE_ACCOUNT_TOKEN_STAGING`
- Server env: `OP_SERVICE_ACCOUNT_TOKEN`

---

### 2. svc-af-prod

**Purpose**: Production server access (create when going live)

**Vault Access**:
- AF - Trading Infra - Prod (Read)
- AF - Servers & SSH (Read)

**Token Storage**:
- GitHub Secret: `OP_SERVICE_ACCOUNT_TOKEN_PROD`
- Server env: `OP_SERVICE_ACCOUNT_TOKEN`

---

## Creating a Service Account

1. Go to **1Password Business Console** > **Integrations** > **Directory**
2. Click **Infrastructure Secrets Management**
3. Click **Create a Service Account**
4. Configure:
   - **Name**: `svc-af-staging` or `svc-af-prod`
   - **Vault Access**: Select vaults as listed above
   - **Permissions**: Read only
5. Click **Create Service Account**
6. **IMPORTANT**: Copy the token immediately - it's shown only once!

The token looks like:
```
ops_eyJzaWduSW5BZGRyZXNzIjo...
```

---

## Required Environment Variables

Scripts and CI expect these environment variables:

```bash
# Required for all 1Password operations
OP_SERVICE_ACCOUNT_TOKEN="ops_eyJ..."  # Service account token

# Vault references (use exact vault names)
OP_VAULT_TRADING_INFRA="AF - Trading Infra - Staging"
OP_VAULT_SERVERS="AF - Servers & SSH"
```

---

## GitHub Secrets Configuration

Add these secrets to your GitHub repository (Settings > Secrets and variables > Actions):

| Secret Name | Value | Description |
|-------------|-------|-------------|
| `OP_SERVICE_ACCOUNT_TOKEN` | `ops_eyJ...` | Current service account token |
| `OP_VAULT_TRADING_INFRA` | `AF - Trading Infra - Staging` | Trading infra vault name |
| `OP_VAULT_SERVERS` | `AF - Servers & SSH` | Servers vault name |

---

## Server Configuration

On Hetzner servers, add the service account token to `/etc/environment` or use systemd environment files:

```bash
# /etc/environment (system-wide)
OP_SERVICE_ACCOUNT_TOKEN="ops_eyJ..."

# Or in systemd service file
# /etc/systemd/system/abstractfinance.service.d/1password.conf
[Service]
Environment="OP_SERVICE_ACCOUNT_TOKEN=ops_eyJ..."
```

**Security Note**: Restrict file permissions:
```bash
chmod 600 /etc/systemd/system/abstractfinance.service.d/1password.conf
```

---

## Token Rotation

Rotate service account tokens:
1. Every 90 days (recommended)
2. After any suspected compromise
3. When team members with access leave

**Rotation Process**:
1. Generate new token in 1Password Business Console
2. Update GitHub Secret
3. Update server environment
4. Restart affected services
5. Verify functionality
6. Revoke old token

---

## Troubleshooting

### "Invalid token" error
- Verify token is complete (no truncation during copy/paste)
- Check token hasn't expired or been revoked
- Ensure vault access is configured for the service account

### "Vault not found" error
- Use exact vault name including spaces and dashes
- Verify service account has access to the vault

### "Item not found" error
- Check item name matches exactly (case-sensitive)
- Verify item exists in the specified vault
