# IBKR Gateway Configuration Guide

## Account Setup

AbstractFinance uses Interactive Brokers for execution via the `heshiming/ibga` Docker image (with native TOTP support).

### Current Staging Configuration (Paper Trading)

| Setting | Value | Description |
|---------|-------|-------------|
| `IBKR_USERNAME` | `dnczyt810` | Paper trading username |
| `IBKR_PASSWORD` | `[stored in 1Password]` | Paper trading specific password |
| `IBKR_ACCOUNT_ID` | `DUO775682` | Paper trading account number |
| `TRADING_MODE` | `Paper Trading` | Must include space and capitals for heshiming/ibga |

> **Security Note**: All credentials stored in 1Password vault `AF - Trading Infra - Staging`. Never commit credentials to Git.

---

## Key Lessons Learned (December 2025)

### 1. Paper Trading Username vs Main Account

**Problem**: When your main IBKR account has multiple paper trading sub-accounts, you'll get:
```
The specified user has multiple Paper Trading users associated with it.
```

**Solution**: Use the **paper trading username directly**, not the main account username.

To find your paper trading username:
1. Log into [IBKR Client Portal](https://portal.interactivebrokers.com) (EU: interactivebrokers.ie)
2. Go to **Settings → Account Settings → Paper Trading Account**
3. Note the paper trading username (format: `DUxxxxxxx` or similar)

**Our setup**:
- Main account: `abstractbot` (for live trading)
- Paper trading username: `dnczyt810` (for paper trading)
- Paper trading account ID: `DUO775682`

### 2. Paper Trading Has SEPARATE Password

**Critical**: Paper trading accounts have their **own password**, completely separate from the main account.

| Account Type | Username | Password |
|--------------|----------|----------|
| Live Trading | `abstractbot` | Main account password |
| Paper Trading | `dnczyt810` | Paper-specific password |

You can reset the paper trading password in:
- Client Portal → Settings → Paper Trading Account → Reset Password

**Symptom of wrong password**:
```
UNRECOGNIZED USERNAME OR PASSWORD
Passwords are case sensitive.
```

### 3. TRADING_MODE Format (heshiming/ibga specific)

**Important**: The `heshiming/ibga` image requires specific format for `IB_LOGINTYPE`:

| Correct | Incorrect |
|---------|-----------|
| `Paper Trading` | `paper` |
| `Live Trading` | `live` |

In `.env`:
```bash
TRADING_MODE=Paper Trading   # Note: with space and capitals
```

The docker-compose maps this: `IB_LOGINTYPE=${TRADING_MODE:-Paper Trading}`

### 4. Session Conflicts

Each IBKR account can only have **one active session** at a time.

**Symptoms**:
- Login loops showing "existing session detected, will kick it out"
- 28% authentication progress then reset

**Common causes**:
- TWS desktop app open
- Client Portal logged in
- Another IB Gateway running (check production server!)
- Previous gateway didn't shut down cleanly

**Solution for December 12, 2025 incident**:
We had a gateway running on production server (91.99.116.196) for 7 days using the same credentials. Had to stop it:
```bash
ssh root@91.99.116.196 "docker stop ibgateway && docker rm ibgateway"
```

### 5. VNC Debugging is Essential

When login fails, **always check VNC** to see what's actually on screen:
```
VNC: vnc://94.130.228.55:5900
Password: <from .env VNC_PASSWORD or 1Password>
```

Common screens you might see:
- "UNRECOGNIZED USERNAME OR PASSWORD" - wrong credentials
- "Multiple Paper Trading users" - using main account instead of paper username
- "Existing session" dialog - another session active
- 2FA/TOTP prompt - TOTP_KEY might be wrong
- Paper trading confirmation dialog - handled automatically by ibga

### 6. Clean Restart Procedure

When credentials change, you **must** clear the cached state:

```bash
ssh root@94.130.228.55

# Stop and remove container + volume (clears cached login state)
cd /srv/abstractfinance
docker compose down ibgateway
docker volume rm abstractfinance_ibgateway-data

# Start fresh
docker compose up -d ibgateway

# Watch logs
docker logs -f ibgateway

# Restart trading engine after gateway is healthy
docker compose restart trading-engine
```

**Note**: Simple `docker compose restart ibgateway` does NOT clear cached credentials.

---

## Environment Variables

Located in `/srv/abstractfinance/.env`:

```bash
# IBKR Credentials (Paper Trading)
IBKR_USERNAME=dnczyt810           # Paper trading username (NOT main account!)
IBKR_PASSWORD=<from 1Password>    # Paper trading specific password
IBKR_TOTP_KEY=<from 1Password>    # TOTP secret for 2FA
IBKR_ACCOUNT_ID=DUO775682         # Paper account number

# Trading Mode - note the format!
TRADING_MODE=Paper Trading        # "Paper Trading" or "Live Trading" (with space)
```

## Docker Image: heshiming/ibga

We use `heshiming/ibga:latest` which provides:
- Native TOTP automation (no need for IB Key mobile app)
- Automatic dialog handling (accepts paper trading warning, etc.)
- VNC access for debugging
- Auto-reconnect during IBKR maintenance windows

Key environment variables for this image:
```yaml
environment:
  - IB_USERNAME=${IBKR_USERNAME}
  - IB_PASSWORD=${IBKR_PASSWORD}
  - TOTP_KEY=${IBKR_TOTP_KEY}      # Note: TOTP_KEY not IBKR_TOTP_KEY
  - IB_LOGINTYPE=${TRADING_MODE:-Paper Trading}
  - IB_REGION=Europe
  - IB_TIMEZONE=Europe/Berlin
```

## Docker Port Mapping

| Host Port | Container Port | Usage |
|-----------|----------------|-------|
| 4000 | 4000 | Trading API (via socat relay) |
| 5900 | 5900 | VNC (debugging) |
| 5800 | 5800 | noVNC web interface |

The trading engine connects to `ibgateway:4000`.

---

## Common Issues & Solutions

### "UNRECOGNIZED USERNAME OR PASSWORD"
1. Verify you're using the paper trading username (not main account)
2. Verify you're using the paper trading password (separate from main)
3. Check VNC to see the actual error dialog
4. Reset password in IBKR Client Portal if needed

### "Multiple Paper Trading users"
- You're using the main account username with paper mode
- Solution: Use the paper trading username directly (e.g., `dnczyt810`)

### Login loops at 28% authentication
- Another session is active somewhere
- Check: TWS, Client Portal, other gateways on prod/staging
- Stop all other sessions and restart gateway

### Gateway shows "unhealthy" but port 4000 works
- The healthcheck might be too strict
- Test manually: `nc -zv localhost 4000`
- If port responds, trading engine should connect

### "Specified user is a Paper Trading user"
- Using paper username with `TRADING_MODE=Live Trading`
- Solution: Set `TRADING_MODE=Paper Trading`

### Container keeps restarting
- Check logs: `docker logs ibgateway`
- Usually a credential or configuration issue
- Clear volume and restart fresh

---

## 1Password Integration

Credentials are stored in 1Password vault `AF - Trading Infra - Staging`:

| Item | Contents |
|------|----------|
| `ibkr.staging` | Username + Password for paper trading |
| `ibkr.staging.totp-key` | TOTP secret key |
| `abstractfinance.staging.env` | Full .env file template |

Fetch .env from 1Password:
```bash
export OP_SERVICE_ACCOUNT_TOKEN="<token>"
op read "op://AF - Trading Infra - Staging/abstractfinance.staging.env/notesPlain" > .env
```

---

## EU Regulatory Compliance (PRIIPs/KID)

EU retail accounts cannot trade US-listed ETFs without Key Information Documents.

**Solution**: Use UCITS-compliant ETFs on European exchanges (LSE, XETRA).

See `config/instruments.yaml` for the UCITS alternatives:
- SPY → CSPX (LSE)
- QQQ → CNDX (LSE)
- LQD → LQDE (LSE)

Individual stocks (ARCC, MAIN, etc.) are not affected - no KID required.

## Exchange Mappings for European ETFs

IBKR uses specific exchange identifiers:

| Config Exchange | IBKR primaryExchange | Notes |
|-----------------|---------------------|-------|
| `LSE` | `LSEETF` | London Stock Exchange ETFs |
| `XETRA` | `IBIS` | German electronic exchange |
| `SBF` | `SBF` | Euronext Paris |

---

## Quick Reference: Full Restart Procedure

When things aren't working:

```bash
# 1. SSH to staging
ssh root@94.130.228.55

# 2. Check what's running
docker ps | grep ibgateway

# 3. Check VNC for visual debugging
# Open: vnc://94.130.228.55:5900 (password: see .env VNC_PASSWORD)

# 4. Full clean restart
cd /srv/abstractfinance
docker compose down ibgateway
docker volume rm abstractfinance_ibgateway-data
docker compose up -d ibgateway

# 5. Watch logs until "entered maintenance cycle" or success
docker logs -f ibgateway

# 6. Once gateway healthy, restart trading engine
docker compose restart trading-engine

# 7. Verify trading engine connects
docker logs trading-engine | tail -50
```

---

## Troubleshooting Timeline (December 12, 2025)

For reference, here's what we debugged:

1. **Initial symptom**: Gateway stuck in "maintenance cycle" loop
2. **Found**: Production server had old ibgateway running (7 days) - stopped it
3. **Found**: `TRADING_MODE=paper` should be `TRADING_MODE=Paper Trading`
4. **Found**: Container had stale env vars - needed full volume removal
5. **Found**: Username `abstractbot` is for live, `dnczyt810` for paper trading
6. **Found**: Password in 1Password was for live account, not paper
7. **Solution**: Used correct paper trading username + reset paper password
8. **Result**: Paper trading connected successfully, orders executing
