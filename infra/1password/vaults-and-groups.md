# 1Password Vaults & Groups Configuration

This document describes the RBAC model and vault layout for AbstractFinance.

## Groups

Create these groups in 1Password Business (Team & Business Console > Groups):

| Group | Description | Members |
|-------|-------------|---------|
| **AF-Owners** | Full access to everything, break-glass scenarios | Fund principals |
| **AF-Infra** | Infrastructure & trading system access | DevOps, system admins |
| **AF-Quants** | Strategy code access, no production credentials | Quant researchers |
| **AF-ReadOnly** | View-only access to non-sensitive vaults | Auditors, compliance |

## Vaults

Create these vaults in 1Password Business (Vaults > New Vault):

### 1. AF - Admin / Breakglass

**Purpose**: Emergency access credentials, master keys, recovery codes

**Access**:
- AF-Owners: Manage
- All others: No access

**Items to store**:
```
af.admin.master-recovery-key     # Master recovery key
af.admin.2fa-backup-codes        # All 2FA backup codes
hetzner.root-password            # Hetzner root credentials
```

---

### 2. AF - Trading Infra - Prod

**Purpose**: Production trading system credentials

**Access**:
- AF-Owners: Manage
- AF-Infra: Read
- All others: No access

**Items to store**:
```
ibkr.prod.username               # IBKR paper/live username
ibkr.prod.password               # IBKR password
ibkr.prod.totp-key               # IBKR 2FA TOTP secret
db.prod.password                 # PostgreSQL password
telegram.bot-token               # Telegram bot token
email.smtp.password              # Email alert password
grafana.admin.password           # Grafana admin password
abstractfinance.prod.env         # Full .env file (in notesPlain field)
```

---

### 3. AF - Trading Infra - Staging

**Purpose**: Staging/paper trading credentials (currently same as prod for paper trading)

**Access**:
- AF-Owners: Manage
- AF-Infra: Read
- AF-Quants: Read
- All others: No access

**Items to store**:
```
ibkr.staging.username
ibkr.staging.password
ibkr.staging.totp-key
db.staging.password
abstractfinance.staging.env
```

---

### 4. AF - Servers & SSH

**Purpose**: Server access credentials, SSH keys

**Access**:
- AF-Owners: Manage
- AF-Infra: Read
- All others: No access

**Items to store**:
```
hetzner.api-token                # Hetzner Cloud API token
ssh.staging.private-key          # SSH key for staging server
ssh.production.private-key       # SSH key for production server
server.staging.ip                # 94.130.228.55
server.production.ip             # Production IP when ready
wireguard.staging.config         # WireGuard VPN config
```

---

### 5. AF - Automation Tokens

**Purpose**: CI/CD and automation tokens

**Access**:
- AF-Owners: Manage
- AF-Infra: Read
- All others: No access

**Items to store**:
```
github.deploy-token              # GitHub personal access token
github.actions.ssh-key           # SSH key for GitHub Actions deployment
op.service-account.staging       # 1Password SA token for staging
op.service-account.prod          # 1Password SA token for production
```

---

## Vault Access Matrix

| Vault | AF-Owners | AF-Infra | AF-Quants | AF-ReadOnly |
|-------|-----------|----------|-----------|-------------|
| AF - Admin / Breakglass | Manage | - | - | - |
| AF - Trading Infra - Prod | Manage | Read | - | - |
| AF - Trading Infra - Staging | Manage | Read | Read | - |
| AF - Servers & SSH | Manage | Read | - | - |
| AF - Automation Tokens | Manage | Read | - | - |

---

## Setup Instructions

1. **Create Groups** (Team & Business Console > Groups)
   - Click "New Group" for each group above
   - Add appropriate team members

2. **Create Vaults** (Vaults > New Vault)
   - Name each vault exactly as shown above
   - Set "Who has access" according to the matrix

3. **Add Items** (within each vault)
   - Use "Login" type for username/password combos
   - Use "Secure Note" type for API keys and tokens
   - Use "Secure Note" with full .env content in the notes field for env files

4. **Verify Access**
   - Have a non-owner test access to confirm RBAC works
