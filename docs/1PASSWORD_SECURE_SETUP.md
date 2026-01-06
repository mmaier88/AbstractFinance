# Secure 1Password Integration for Servers

This guide describes how to securely manage secrets on Linux servers using 1Password's `op run` pattern. This approach ensures credentials are never stored in plaintext and are only available in memory during runtime.

## Why This Approach?

| Old Pattern (Insecure) | New Pattern (Secure) |
|------------------------|----------------------|
| Plaintext `.env` files on disk | `.env.template` with `op://` references |
| Secrets visible in files, logs, to anyone with access | Secrets resolved in memory at runtime |
| Credentials persist indefinitely | Credentials purged when process stops |
| No audit trail | Full 1Password audit logging |

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         1Password Cloud                          │
│                    (Vault: "Ai" or your vault)                   │
└─────────────────────────────────────────────────────────────────┘
                                 │
                                 │ Service Account Token
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│                         Linux Server                             │
│                                                                  │
│  /etc/profile.d/1password.sh                                    │
│  └── export OP_SERVICE_ACCOUNT_TOKEN=<YOUR_TOKEN>               │
│                                                                  │
│  /srv/myapp/.env.template                                       │
│  └── DB_PASSWORD=op://Ai/database/password                      │
│  └── API_KEY=op://Ai/api-service/key                            │
│                                                                  │
│  /srv/myapp/dc (wrapper script)                                 │
│  └── op run --env-file=.env.template -- docker compose "$@"     │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
                                 │
                                 │ Secrets injected into process memory
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Docker Containers                           │
│         (receive secrets as environment variables)               │
└─────────────────────────────────────────────────────────────────┘
```

## Prerequisites

1. **1Password Business or Teams account** with service accounts enabled
2. **Service account** created in 1Password with access to relevant vault(s)
3. **Linux server** with root/sudo access

## Step-by-Step Setup

### Step 1: Create a Service Account in 1Password

1. Go to 1Password web console → Settings → Service Accounts
2. Create a new service account (e.g., "Server Infrastructure")
3. Grant access to the vault(s) containing your secrets
4. Copy the service account token (starts with `ops_`)

**Important:** Store this token securely - it's the only time you'll see it!

### Step 2: Install 1Password CLI on Server

```bash
# SSH into your server
ssh root@your-server-ip

# Download and install the 1Password CLI
curl -sSfLo /tmp/op.zip "https://cache.agilebits.com/dist/1P/op2/pkg/v2.32.0/op_linux_amd64_v2.32.0.zip"
cd /tmp && unzip -o op.zip
mv op /usr/local/bin/
chmod +x /usr/local/bin/op

# Verify installation
op --version
# Should output: 2.32.0 (or newer)
```

### Step 3: Configure Service Account Token

```bash
# Create a secure file for the service account token
# Replace <YOUR_TOKEN> with your actual service account token
echo 'export OP_SERVICE_ACCOUNT_TOKEN=<YOUR_TOKEN>' > /etc/profile.d/1password.sh

# Secure the file (readable only by root)
chmod 600 /etc/profile.d/1password.sh

# Test the connection
source /etc/profile.d/1password.sh
op vault list

# Expected output:
# ID                            NAME
# abc123...                     Ai
```

### Step 4: Create .env.template File

Create a template file with secret references instead of actual values:

```bash
# /srv/myapp/.env.template

# Database credentials
DB_HOST=postgres
DB_PORT=5432
DB_NAME=myapp
DB_USER=postgres
DB_PASSWORD=op://Ai/database/password

# API keys
STRIPE_API_KEY=op://Ai/stripe/secret-key
SENDGRID_API_KEY=op://Ai/sendgrid/api-key

# Third-party service credentials
AWS_ACCESS_KEY_ID=op://Ai/aws/access-key-id
AWS_SECRET_ACCESS_KEY=op://Ai/aws/secret-access-key

# Non-secret values can be plaintext
NODE_ENV=production
LOG_LEVEL=info
```

**Secret Reference Format:** `op://<vault>/<item>/<field>`

### Step 5: Create Docker Compose Wrapper Script

Create a wrapper script that injects secrets at runtime:

```bash
cat > /srv/myapp/dc << 'EOF'
#!/bin/bash
# Docker Compose wrapper that injects secrets from 1Password
# Usage: ./dc up -d, ./dc logs, ./dc ps, etc.

set -e

# Source 1Password service account token
source /etc/profile.d/1password.sh

# Change to app directory
cd /srv/myapp

# Use op run to inject secrets and execute docker compose
exec op run --env-file=.env.template -- docker compose "$@"
EOF

# Make it executable
chmod +x /srv/myapp/dc
```

### Step 6: Usage

```bash
# Start services (secrets are injected at runtime)
cd /srv/myapp
./dc up -d

# View logs
./dc logs --tail=50 myservice

# Check status
./dc ps

# Stop services
./dc down

# Restart a specific service
./dc restart myservice
```

## Adding Secrets to 1Password

### Via 1Password Web/Desktop App

1. Open 1Password
2. Navigate to your vault (e.g., "Ai")
3. Create a new item (Login, Password, or API Credential)
4. Name it clearly (e.g., "database", "stripe", "aws")
5. Add fields for each secret value

### Via CLI (if needed)

```bash
source /etc/profile.d/1password.sh

# Create a new password item
# Replace <YOUR_API_KEY> with the actual value
op item create \
  --vault="Ai" \
  --category=password \
  --title="my-api-service" \
  'password=<YOUR_API_KEY>'

# Create an item with multiple fields
# Replace placeholders with actual values
op item create \
  --vault="Ai" \
  --category=login \
  --title="database" \
  'username=<YOUR_DB_USER>' \
  'password=<YOUR_DB_PASSWORD>' \
  'host=<YOUR_DB_HOST>'
```

## Finding the Correct Field Names

If you're unsure what field names an item has:

```bash
source /etc/profile.d/1password.sh

# List all fields in an item
op item get "my-item" --vault="Ai" --format=json | jq -r '.fields[] | .label'

# Test reading a specific field
op read "op://Ai/my-item/password"
```

## Common Field Names

| Item Type | Common Fields |
|-----------|---------------|
| Login | `username`, `password` |
| Password | `password` |
| API Credential | `credential`, `username`, `password` |
| Database | `username`, `password`, `host`, `port`, `database` |
| SSH Key | `private key`, `public key` |
| Notes | `notesPlain` |

## Systemd Integration (Optional)

If you need services to start automatically on boot with secrets:

```bash
# /etc/systemd/system/myapp.service
[Unit]
Description=My Application
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/srv/myapp
EnvironmentFile=/etc/profile.d/1password.sh
ExecStart=/usr/local/bin/op run --env-file=/srv/myapp/.env.template -- /usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable myapp
systemctl start myapp
```

## Troubleshooting

### "Secret not found" Error

```bash
# Check the exact item name
op item list --vault="Ai"

# Check available fields
op item get "item-name" --vault="Ai" --format=json | jq '.fields[] | {label, id}'

# Test the secret reference
op read "op://Ai/item-name/field-name"
```

### "Invalid token" Error

```bash
# Verify the token is set
echo $OP_SERVICE_ACCOUNT_TOKEN | head -c 20

# Re-source the file
source /etc/profile.d/1password.sh

# Test connection
op vault list
```

### "Vault not found" Error

The service account may not have access to the vault. Check:
1. Go to 1Password web console → Settings → Service Accounts
2. Edit the service account
3. Ensure it has access to the required vault

## Security Best Practices

1. **Never create plaintext `.env` files** - Always use `.env.template` with `op://` references

2. **Secure the service account token file**
   ```bash
   chmod 600 /etc/profile.d/1password.sh
   chown root:root /etc/profile.d/1password.sh
   ```

3. **Use least-privilege vault access** - Only grant the service account access to vaults it needs

4. **Rotate service account tokens periodically** - Create a new token and update servers

5. **Audit access** - Review 1Password audit logs for service account activity

6. **Don't log environment variables** - Ensure your application doesn't log secrets

## Quick Reference

| Task | Command |
|------|---------|
| List vaults | `op vault list` |
| List items in vault | `op item list --vault="Ai"` |
| Get item details | `op item get "item-name" --vault="Ai"` |
| Read a secret | `op read "op://Ai/item/field"` |
| Run with secrets | `op run --env-file=.env.template -- command` |
| Check CLI version | `op --version` |

## Example .env.template for Common Stacks

### Node.js/Express
```bash
NODE_ENV=production
PORT=3000
DATABASE_URL=op://Ai/postgres/connection-string
JWT_SECRET=op://Ai/jwt/secret
REDIS_URL=op://Ai/redis/url
```

### Python/Django
```bash
DEBUG=False
SECRET_KEY=op://Ai/django/secret-key
DATABASE_URL=op://Ai/postgres/connection-string
ALLOWED_HOSTS=example.com
```

### Docker Compose with Multiple Services
```bash
# Database
POSTGRES_USER=postgres
POSTGRES_PASSWORD=op://Ai/postgres/password
POSTGRES_DB=myapp

# Application
APP_SECRET=op://Ai/myapp/secret
API_KEY=op://Ai/external-api/key

# Redis
REDIS_PASSWORD=op://Ai/redis/password

# Monitoring
GRAFANA_ADMIN_PASSWORD=op://Ai/grafana/password
```

---

## Support

- 1Password CLI Documentation: https://developer.1password.com/docs/cli
- Service Accounts: https://developer.1password.com/docs/service-accounts
- Secret References: https://developer.1password.com/docs/cli/secret-references
