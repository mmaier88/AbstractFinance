# IBKR Gateway Configuration Guide

## Account Setup

AbstractFinance uses Interactive Brokers for execution via the `gnzsnz/ib-gateway` Docker image.

### Credentials (Staging)

| Setting | Value | Description |
|---------|-------|-------------|
| `IBKR_USERNAME` | `dnczyt810` | Paper trading username (NOT main account) |
| `IBKR_PASSWORD` | `[stored in .env]` | Paper trading specific password |
| `IBKR_ACCOUNT_ID` | `DUO775682` | Paper trading account number |
| `TRADING_MODE` | `paper` | Must be `paper` for paper trading |

### Key Lessons Learned

#### 1. Paper Trading Username vs Main Account

**Problem**: When your main IBKR account (`abstractbot`) has multiple paper trading sub-accounts, you'll get:
```
Connection to server failed: The specified user has multiple Paper Trading
users associated with it.
```

**Solution**: Use the **paper trading username directly** (e.g., `dnczyt810`), not the main account username.

To find your paper trading username:
1. Log into [IBKR Client Portal](https://portal.interactivebrokers.com)
2. Go to **Settings → Account Settings → Paper Trading Account**
3. Note the paper trading username (usually different from main account)

#### 2. Paper Trading Password

**Important**: Paper trading accounts have their **own password**, separate from the main account.

- Main account password: Used for `abstractbot`
- Paper account password: Used for `dnczyt810`

You can reset the paper trading password in Client Portal → Settings → Paper Trading Account.

#### 3. Trading Mode Setting

When using a paper trading username directly:
- Set `TRADING_MODE=paper`
- The IBC controller will click "Paper Log In" button
- This works correctly when using the paper username + paper password combo

#### 4. Session Conflicts

Each IBKR session (paper or live) can only have **one active login** at a time.

If you're logged in via:
- TWS desktop
- Client Portal
- Another IB Gateway instance

...you'll need to log out before the Docker container can connect.

## Environment Variables

Located in `/srv/abstractfinance/.env`:

```bash
# IBKR Credentials
IBKR_USERNAME=dnczyt810          # Paper trading username
IBKR_PASSWORD=your_paper_password # Paper trading password
IBKR_ACCOUNT_ID=DUO775682        # Paper account number
IBKR_PORT=4004                    # Paper trading port

# Trading Mode
TRADING_MODE=paper               # paper or live
```

## Docker Port Mapping

The `gnzsnz/ib-gateway` image uses socat to relay connections:

| Host Port | Container Port | Usage |
|-----------|----------------|-------|
| 4001 | 4003 | Live trading API |
| 4002 | 4004 | Paper trading API |
| 5900 | 5900 | VNC (debugging) |

The trading engine connects to `ibgateway:4004` (paper) or `ibgateway:4003` (live).

## Common Issues

### "Multiple Paper Trading users"
- Using main account username with paper mode
- Solution: Use paper trading username directly

### "Specified user is a Paper Trading user"
- Using paper username with `TRADING_MODE=live`
- Solution: Set `TRADING_MODE=paper`

### Connection Timeout
- IB Gateway still starting (wait 120s)
- Another session logged in (log out other sessions)
- Wrong password (verify paper trading password)

### Session Kicked
- Enable `EXISTING_SESSION_DETECTED_ACTION=primary` to auto-take over existing sessions

## Restarting After Config Changes

```bash
# SSH to server
ssh root@94.130.228.55

# Update .env file
nano /srv/abstractfinance/.env

# Recreate IB Gateway (clears cached config)
cd /srv/abstractfinance
docker compose down ibgateway
docker volume rm abstractfinance_ibgateway-data
docker compose up -d ibgateway

# Restart trading engine
docker compose restart trading-engine
```

## EU Regulatory Compliance (PRIIPs/KID)

EU retail accounts cannot trade US-listed ETFs without Key Information Documents.

**Solution**: Use UCITS-compliant ETFs on European exchanges (LSE, XETRA).

See `config/instruments.yaml` for the UCITS alternatives:
- SPY → CSPX (LSE)
- QQQ → CNDX (LSE)
- LQD → LQDE (LSE)
- etc.

Individual stocks (ARCC, MAIN, etc.) are not affected - no KID required.
