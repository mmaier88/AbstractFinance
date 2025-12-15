# AbstractFinance - European Decline Macro Fund

> **📋 THIS README IS THE SOURCE OF TRUTH** for all configuration, deployment, and operational documentation. All other docs are supplementary.

A production-grade automated trading system implementing a multi-sleeve macro hedge fund strategy expressing a structural view on US vs European economic performance.

## Strategy Overview

The **European Decline Macro Fund** expresses the thesis that US companies and balance sheets will outperform European ones over the next decade due to:

- Stronger US GDP and job growth
- US dominance in R&D and technology leadership
- Europe's aging demographics and high structural social spending
- European regulatory burden and fiscal rigidity
- Higher energy costs in Europe

### Multi-Sleeve Architecture

The strategy is implemented across six sleeves:

| Sleeve | Target Weight | Description |
|--------|--------------|-------------|
| **Core Index RV** | 35% | Long US (ES/SPY) vs Short EU (FESX/FEZ), FX-hedged |
| **Sector RV** | 25% | Long US innovation sectors vs Short EU old-economy |
| **Single Name** | 15% | US quality growth vs EU "zombies" |
| **Credit & Carry** | 15% | Long US credit, underweight EU credit |
| **Crisis Alpha** | 5% | Options, volatility, sovereign stress hedges |
| **Cash Buffer** | 5% | Margin reserve |

### Expected Performance

Based on historical calibration:
- **Unlevered Expected Return**: ~7-9% annually
- **Spread Volatility**: ~8%
- **Target Sharpe Ratio**: ~1.0-1.1
- **Tail Hedge Budget**: 2-3% NAV/year

## Paper Trading Status

> **LIVE since December 3, 2025** - 60-day burn-in period in progress

| Metric | Current | Target |
|--------|---------|--------|
| **Days Elapsed** | 5 / 60 | 60 trading days |
| **NAV** | $10,843,730 | N/A |
| **Total Return** | +8.4% | Positive |
| **Max Drawdown** | -4.21% | < 10% |
| **Target End Date** | Feb 1, 2026 | - |

**Active Sleeve**: core_index_rv only (Phase 1)

For detailed tracking, see [`docs/PAPER_TRADING.md`](docs/PAPER_TRADING.md).

```bash
# Quick status check
ssh root@94.130.228.55 "cat /srv/abstractfinance/state/portfolio_state.json | python3 -m json.tool"
```

## Architecture

```
                       +----------------------+
                       |      GitHub Repo     |
                       +----------+-----------+
                                  |
                        CI/CD: Build & Push
                                  |
                   +--------------v----------------+
                   |        Staging Server        |
                   |   94.130.228.55 (CX33)       |
                   +--------------+----------------+
                                  |
                Docker Compose: Full Infra Stack
                                  |
     +--------------------+   +-----------------------+
     | IB Gateway (paper) |   | Trading Engine (paper)|
     +--------------------+   +-----------------------+
               |                        |
               |<-- ib_insync API ----->|
               |
        +------+--------+
        | Reconnect/HC  |
        +---------------+
               |
               v
      +------------------+
      | Monitoring Stack |
      | Grafana/Prom/Loki|
      +------------------+

                   +--------------+----------------+
                   |      Production Server       |
                   | 91.99.116.196 (CX43)        |
                   +--------------+----------------+
                                  |
                       Same Stack, Mode=live
```

## Project Structure

```
AbstractFinance/
├── config/
│   ├── settings.yaml          # All tunable parameters
│   ├── instruments.yaml       # Symbol mappings & contract specs (76 instruments)
│   └── credentials.env.template
├── src/
│   ├── __init__.py
│   ├── data_feeds.py          # Market data (IB primary + yfinance fallback)
│   ├── portfolio.py           # Positions, NAV, P&L, 6-sleeve tracking
│   ├── risk_engine.py         # Vol targeting, 4-regime detection, DD control
│   ├── strategy_logic.py      # Sleeve construction + UCITS compliance
│   ├── tail_hedge.py          # Tail hedges + crisis playbook
│   ├── execution_ibkr.py      # IBKR via ib_insync (810 lines)
│   ├── reconnect.py           # Watchdog, heartbeat, auto-reconnect
│   ├── scheduler.py           # Continuous loop orchestrator (713 lines)
│   ├── futures_rollover.py    # Automatic futures rollover detection & execution
│   ├── stock_screener.py      # Multi-factor stock selection (589 lines)
│   ├── backtest.py            # Historical + Monte Carlo (20+ metrics)
│   ├── paper_trading.py       # 60-day burn-in with validation gates
│   ├── alerts.py              # Telegram/email notifications
│   ├── metrics.py             # Prometheus metrics (20+ metric types)
│   ├── healthcheck.py         # HTTP health endpoints
│   └── logging_utils.py       # Structured JSON logging
├── scripts/
│   ├── setup_cron.sh          # Install cron job
│   ├── run_daily.sh           # Daily run wrapper
│   ├── restart_gateway.sh     # Sunday maintenance restart
│   ├── cross_monitor.py       # Cross-server auto-remediation
│   ├── backup_postgres.sh     # Automated database backups
│   ├── setup_pg_replication.sh # PostgreSQL streaming replication setup
│   ├── rollover_futures.py    # Manual futures rollover script
│   ├── failover.sh            # Automated failover to standby server
│   └── sync_state.sh          # State sync for warm standby
├── tests/
│   ├── test_portfolio.py
│   ├── test_risk_engine.py
│   ├── test_strategy_logic.py
│   └── test_tail_hedge.py
├── infra/
│   ├── prometheus.yml
│   ├── alert-rules.yml        # Trading-specific alerts
│   ├── alertmanager.yml       # Alert routing to Telegram
│   ├── loki-config.yml
│   ├── Caddyfile              # SSL/TLS reverse proxy config
│   └── grafana/
├── docs/
│   ├── IBKR_SETUP.md
│   ├── PRODUCTION_HARDENING.md
│   ├── FAILOVER.md            # HA failover procedure
│   └── ROLLBACK.md            # Deployment rollback procedure
├── .github/workflows/
│   ├── ci.yml
│   ├── deploy-staging.yml
│   └── deploy-production.yml
├── docker-compose.yml
├── docker-compose.caddy.yml   # SSL/TLS overlay with Caddy
├── Dockerfile
├── requirements.txt
└── README.md
```

## Quick Start

### Prerequisites

- Python 3.11+
- Interactive Brokers account with API access
- Docker & Docker Compose (for containerized deployment)
- Hetzner server (for production)

### Local Development

1. **Clone the repository**
```bash
git clone https://github.com/yourusername/AbstractFinance.git
cd AbstractFinance
```

2. **Create virtual environment**
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

3. **Configure credentials**
```bash
cp config/credentials.env.template config/credentials.env
# Edit credentials.env with your IBKR credentials
```

4. **Run backtests**
```bash
python -m src.backtest
```

5. **Start paper trading**
```bash
# Ensure IB Gateway is running
python -m src.scheduler
```

### Docker Deployment

1. **Configure environment**
```bash
cp config/credentials.env.template .env
# Edit .env with your credentials
```

2. **Start all services**
```bash
docker compose up -d
```

3. **View logs**
```bash
docker compose logs -f trading-engine
```

4. **Access monitoring**
- Grafana: http://localhost:3000
- Prometheus: http://localhost:9090

## Configuration

### settings.yaml

Key parameters in `config/settings.yaml`:

```yaml
mode: "paper"                    # "paper" or "live"
vol_target_annual: 0.12          # 12% target volatility
gross_leverage_max: 2.0          # 200% max gross exposure
hedge_budget_annual_pct: 0.025   # 2.5% for tail hedges
max_drawdown_pct: 0.10           # 10% emergency de-risk

sleeves:
  core_index_rv: 0.35
  sector_rv: 0.25
  single_name: 0.15
  credit_carry: 0.15
  crisis_alpha: 0.05
  cash_buffer: 0.05
```

### instruments.yaml

Defines all tradeable instruments. All instruments must be IBKR-executable:

- **Equity Indices**: ES, SPY, FESX, FEZ
- **FX**: 6E (EUR/USD futures)
- **Sectors**: XLK, QQQ, EUFN, etc.
- **Credit**: LQD, HYG, JNK
- **Volatility**: VIX options, VX futures
- **Sovereign**: FOAT, FGBL futures

## IB Gateway Setup (IBGA - Headless 2FA)

> **IMPORTANT**: This system uses [IBGA (heshiming/ibga)](https://heshiming.github.io/ibga/) for fully automated headless IB Gateway with TOTP 2FA support. No manual phone taps required!

### How IBGA Works

IBGA automates the entire IB Gateway login process:
1. Launches IB Gateway in a virtual X display (Xvfb)
2. Automatically fills in username and password
3. Detects 2FA prompt and generates TOTP code using `oathtool`
4. Enters the 6-digit code and clicks OK
5. Configures API settings (port, logging, etc.)
6. Exposes API on port 4000 via socat proxy

### Required Environment Variables

Add these to your `.env` file on the server:

```bash
# IBKR Credentials (REQUIRED)
IBKR_USERNAME=your_username          # IBKR username
IBKR_PASSWORD=your_password          # IBKR password
IBKR_TOTP_KEY=YOUR32CHARBASE32KEY   # TOTP secret from Mobile Authenticator setup

# Trading Mode
TRADING_MODE=paper                   # "paper" or "live"
```

### Getting Your TOTP Key

The TOTP key is a 32-character Base32 secret obtained when setting up Mobile Authenticator:

1. Log into IBKR Account Management
2. Go to **Settings → Security → Secure Login System**
3. Enable **Mobile Authenticator** (IB Key app)
4. When shown the QR code, click "Can't scan?" to reveal the secret key
5. Copy the 32-character secret (e.g., `JBSWY3DPEHPK3PXPEXAMPLEKEY1234`)
6. Save this as `IBKR_TOTP_KEY` in your `.env` file

**Note**: You can verify your TOTP key works with:
```bash
oathtool --base32 --totp "YOUR32CHARBASE32KEY"
```

### Docker Compose Configuration

The `docker-compose.yml` configures IBGA with these key settings:

```yaml
ibgateway:
  image: heshiming/ibga:latest
  environment:
    - IB_USERNAME=${IBKR_USERNAME}
    - IB_PASSWORD=${IBKR_PASSWORD}
    - TOTP_KEY=${IBKR_TOTP_KEY}
    - IB_REGION=europe
    - IB_TIMEZONE=Europe/Berlin
    - IB_LOGINTAB=IB API           # IMPORTANT: Use "IB API", NOT "FIX CTCI"
    - IB_LOGINTYPE=${TRADING_MODE:-paper}
    - IB_LOGOFF=11:55 PM Europe/Berlin
    - IB_APILOG=data
    - IB_LOGLEVEL=INFO
  ports:
    - "4000:4000"    # IBGA API port (socat proxy to internal port 9000)
    - "5900:5900"    # VNC for debugging
    - "6080:5800"    # noVNC web interface
  volumes:
    - ibgateway-data:/home/ibg
    - ibgateway-settings:/home/ibg_settings  # Persists login state
```

### Port Configuration

| External Port | Internal Port | Purpose |
|--------------|---------------|---------|
| 4000 | 4000 | IB Gateway API (socat → 9000) |
| 5900 | 5900 | VNC server (for debugging) |
| 6080 | 5800 | noVNC web interface |

**Trading Engine connects to**: `ibgateway:4000` (inside Docker network)

### Debugging via noVNC

If login fails, connect to noVNC to see the IB Gateway GUI:

```
http://<server-ip>:6080/vnc.html
```

Common issues visible in noVNC:
- **"Order routing login failed"**: Wrong credentials or wrong login tab
- **Black screen**: Container restarting, wait for it to stabilize
- **2FA prompt stuck**: TOTP key incorrect or clock sync issue

### Hetzner Server Setup

```bash
# Install dependencies
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-venv python3-pip git tmux

# Install Docker
curl -fsSL https://get.docker.com | sudo bash
sudo usermod -aG docker $(whoami)

# Clone and setup
git clone https://github.com/mmaier88/AbstractFinance.git /srv/abstractfinance
cd /srv/abstractfinance

# Configure credentials
nano .env
# Add: IBKR_USERNAME, IBKR_PASSWORD, IBKR_TOTP_KEY, TRADING_MODE, etc.

# Start services
docker compose up -d
```

### Verifying Connection

After startup, verify IB Gateway is connected:

```bash
# Check logs for successful login
docker logs ibgateway | grep -E "TOTP|connected|welcome"

# Test API connection
docker exec trading-engine python3 -c "
from ib_insync import IB
ib = IB()
ib.connect('ibgateway', 4000, clientId=99, timeout=10)
print('Connected! Accounts:', ib.managedAccounts())
ib.disconnect()
"
```

Expected output:
```
Connected! Accounts: ['U12345678']
```

### Troubleshooting

| Issue | Cause | Solution |
|-------|-------|----------|
| "Order routing login failed" | Wrong login tab (FIX CTCI) | Ensure `IB_LOGINTAB=IB API` |
| "Not able to wait for connect request on port 4000" | Port conflict | Restart container: `docker compose restart ibgateway` |
| TOTP not entered | Wrong TOTP key | Verify key with `oathtool --base32 --totp "KEY"` |
| Black screen in noVNC | Container starting | Wait 60s, check `docker logs ibgateway` |
| Connection refused on 4002 | Wrong port | Use port 4000, not 4001/4002 |

### Volume Persistence

The `ibgateway-settings` volume preserves:
- Login form state (skips timezone selection on restart)
- API configuration (port, logging settings)
- Option check skip flag

To reset and reconfigure from scratch:
```bash
docker compose down
docker volume rm abstractfinance_ibgateway-settings
docker compose up -d ibgateway
```

## Risk Management

### Volatility Targeting

The system implements volatility targeting:
- Computes 20-day realized volatility
- Scales positions to maintain target volatility (12% annual target)
- Caps at maximum gross leverage (200%)

### Regime Detection

Monitors SPX/SX5E ratio momentum with four regimes:

| Regime | Trigger | Position Scaling |
|--------|---------|------------------|
| **NORMAL** | VIX < 25, positive momentum | 100% |
| **ELEVATED** | VIX > 25 or negative momentum | 50% |
| **CRISIS** | VIX > 40 or DD > 10% | 25% (emergency de-risk) |
| **RECOVERY** | Mean-reverting after crisis | Gradual restoration |

### Tail Hedges

Hedge budget (2.5% NAV/year) allocated across:
- **40%**: Equity puts (SPY, FEZ)
- **20%**: VIX calls
- **15%**: Credit puts (HYG)
- **15%**: Sovereign spread (OAT-Bund)
- **10%**: European bank puts

**Profit-Taking**: Sells 60% of ITM hedges when profitable. Alert triggered at 90% budget usage.

## Stock Screening (Single Name Sleeve)

The `src/stock_screener.py` module implements quantitative stock selection:

### US Longs (Quality Growth)
Multi-factor scoring:
- **Quality (50%)**: ROE > 15%, debt/equity < 1, positive earnings growth, strong FCF
- **Momentum (30%)**: 12-month returns (excluding last month)
- **Size (20%)**: Market cap > $50B, daily volume > $50M

### EU Shorts (Zombies)
- **Zombie Score (50%)**: Interest coverage < 3x, negative revenue growth, high debt
- **Weakness (30%)**: Negative momentum vs Euro STOXX 50
- **Sector (20%)**: Banks, Autos, Utilities, Industrials preferred

**Rebalancing**: Monthly. Max 5% per single name. Fallback to AAPL, MSFT, GOOGL, NVDA, AMZN if screening fails.

## Testing

### Run Tests

```bash
# Run all tests
pytest tests/ -v

# Run with coverage
pytest tests/ -v --cov=src --cov-report=html
```

### Paper Trading Burn-In

Before going live, complete a 60-day paper trading period:

```bash
python -m src.paper_trading
```

Validation checks:
- Sharpe ratio > 0.5
- Max drawdown > -15%
- Order rejection rate < 5%
- Minimum 50 trades executed

## Health Check Endpoints

The trading engine exposes HTTP health endpoints on port 8080:

| Endpoint | Purpose | Response |
|----------|---------|----------|
| `/health` | Liveness check | `{"status": "healthy", "uptime": 12345}` |
| `/health/detailed` | Component status | IB connection, portfolio age, mode |
| `/health/ib` | IB Gateway status | Connection state, last heartbeat |

Used by external uptime monitors (e.g., UptimeRobot, Healthchecks.io).

## Cross-Server Monitoring

The `scripts/cross_monitor.py` implements automated server health monitoring with graduated escalation:

| Failure Count | Action |
|---------------|--------|
| 1-3 | Restart Docker containers |
| 4-6 | Reboot entire server |
| 7+ | Alert and wait for manual intervention |

Runs on standby server, monitors primary via `/health` endpoint.

## Monitoring & Alerts

### Daily Alerts

Sent via Telegram/email:
- NAV and daily P&L
- Exposure metrics
- Risk warnings
- Hedge budget usage

### Alert Thresholds

- Daily loss > 3%
- Daily gain > 5%
- Drawdown > 5%
- Hedge budget > 90% used
- Connection failures

## CI/CD Pipeline

### GitHub Actions

- **CI**: Runs tests and linting on every push
- **Deploy Staging**: Auto-deploy to staging on main branch
- **Deploy Production**: Manual trigger or on release tag

### Server Architecture

| Server | IP | Type | Purpose | noVNC |
|--------|-----|------|---------|-------|
| Staging | 94.130.228.55 | CX33 | Paper trading, testing | http://94.130.228.55:6080/vnc.html |
| Production | 91.99.116.196 | CX43 | Live trading | http://91.99.116.196:6080/vnc.html |

**Current Account**: U23203300 (abstractbot)

## Development

### Adding New Instruments

1. Add to `config/instruments.yaml`
2. Map symbol in `DataFeed.YFINANCE_MAPPING`
3. Add to appropriate sleeve in strategy

### Adding New Sleeves

1. Add to `Sleeve` enum in `portfolio.py`
2. Configure weight in `settings.yaml`
3. Implement builder method in `Strategy` class
4. Add tests

## Documentation

Additional documentation:

- **[IBKR Setup Guide](docs/IBKR_SETUP.md)** - IB Gateway configuration, paper trading credentials, exchange mappings
- **[Production Hardening](docs/PRODUCTION_HARDENING.md)** - HA architecture, security, deployment hygiene, monitoring completeness

## EU Compliance (PRIIPs/KID)

EU retail accounts cannot trade US-listed ETFs without Key Information Documents. The system uses UCITS-compliant alternatives:

| US ETF | UCITS Alternative | Exchange |
|--------|------------------|----------|
| SPY | CSPX | LSE |
| QQQ | CNDX | LSE |
| LQD | LQDE | LSE |
| IEF | IDTL | LSE |
| XLK | IUIT | XETRA |

Exchange mappings for IBKR: `LSE` → `LSEETF`, `XETRA` → `IBIS`

## Telegram Alerts Setup

To receive disconnect and trading alerts:

1. **Create a Bot**: Message `@BotFather` on Telegram, send `/newbot`
2. **Get Chat ID**: Message your bot, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. **Configure Server**:
```bash
ssh root@94.130.228.55
nano /srv/abstractfinance/.env

# Add:
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id

docker compose restart trading-engine
```

Alerts include: disconnects, reconnection status, daily PnL, drawdown warnings.

## Automatic Futures Rollover

The system automatically detects and rolls expiring futures positions before expiry. This eliminates manual intervention for:

### Supported Futures

| Category | Contracts | Expiry Cycle | Rolls/Year |
|----------|-----------|--------------|------------|
| **FX** | M6E, 6E, M6B, 6B, M6J, 6J | Quarterly | 4 |
| **Equity Index** | ES, MES, NQ, MNQ, FESX, FDAX | Quarterly | 4 |
| **Bonds** | FGBL, FOAT, FBTP, ZN, ZB | Quarterly | 4 |
| **Volatility** | VX, FVS | **Monthly** | **12** |
| **CAC** | FCE | Monthly | 12 |

### How It Works

1. **Daily Check**: The scheduler scans positions for contracts expiring within `days_before_expiry` (default: 3)
2. **Auto-Roll**: Closes the expiring position and opens equivalent position in next contract
3. **Alerts**: Sends Telegram notification before and after each rollover

### Configuration

In `config/settings.yaml`:

```yaml
futures_rollover:
  days_before_expiry: 3    # Roll 3 days before expiry
  dry_run: false           # Set true to test without executing
```

### Manual Rollover

For immediate rollover (e.g., if a contract is expiring today):

```bash
# Dry run (test)
ssh root@94.130.228.55 "docker exec trading-engine python3 /app/scripts/rollover_futures.py --symbol M6E"

# Execute
ssh root@94.130.228.55 "docker exec trading-engine python3 /app/scripts/rollover_futures.py --symbol M6E --execute"
```

### Alternative: Spot FX

For FX hedging specifically, consider using **spot FX** (`EUR.USD` on IDEALPRO) instead of futures:
- **No expiry** - no rollover needed
- **Flexible sizing** - not limited to contract sizes
- **Lower complexity** - no operational overhead

## Scheduled Maintenance & IBKR Windows

### Automatic Maintenance

- **Sunday 22:00 UTC**: Automatic IB Gateway restart (cron job)
- **Daily 3:00 UTC**: PostgreSQL backup with 30-day retention

Cron job location: `/etc/cron.d/abstractfinance-maintenance`

### IBKR Maintenance Windows

The scheduler automatically detects IBKR maintenance windows and **skips order execution** during these periods:

| Window | Schedule (UTC) | Duration | Action |
|--------|---------------|----------|--------|
| **Weekly Restart** | Sunday 23:45 - Monday 00:45 | 60 min | Orders skipped |
| **Daily Disconnect** | Mon-Fri 22:00 - 22:15 | 15 min | Orders skipped |

During maintenance windows:
- Position sync and NAV calculation continue normally
- Risk metrics are computed but orders are not placed
- Run summary includes `maintenance_window: true`
- Skipped orders appear as `orders_skipped` in logs

This is implemented in `src/scheduler.py` via the `is_maintenance_window()` function.

## SSL/TLS with Caddy (Optional)

For production with HTTPS, use the Caddy reverse proxy overlay:

```bash
# Enable HTTPS for Grafana, Prometheus, Alertmanager
docker compose -f docker-compose.yml -f docker-compose.caddy.yml up -d
```

Requirements:
- Domain name pointing to server IP
- Ports 80/443 open in firewall
- Set `GRAFANA_DOMAIN`, `PROMETHEUS_DOMAIN` in `.env`

Caddy automatically obtains and renews Let's Encrypt certificates.

Configuration: `infra/Caddyfile`

## Database Operations

### Automated Backups

PostgreSQL backups run daily at 3:00 UTC via cron:

```bash
# Manual backup
/srv/abstractfinance/scripts/backup_postgres.sh

# View backups
ls -la /srv/abstractfinance/backups/
```

Features:
- Gzipped dumps with timestamps
- 30-day retention policy
- Optional sync to standby server via WireGuard

### PostgreSQL Replication (Optional)

For production with >$1M AUM, streaming replication is available:

```bash
# On primary server
./scripts/setup_pg_replication.sh primary

# On standby server
./scripts/setup_pg_replication.sh standby
```

For paper trading, the backup-based approach is recommended:
```bash
./scripts/setup_pg_replication.sh backup
```

## Deployment Rollback

Rollback procedures documented in [docs/ROLLBACK.md](docs/ROLLBACK.md).

### Quick Rollback Commands

```bash
# Rollback to previous git commit
ssh root@94.130.228.55 "cd /srv/abstractfinance && git checkout HEAD~1 && docker compose build trading-engine && docker compose up -d trading-engine"

# Restore database from backup
BACKUP=$(ls -t /srv/abstractfinance/backups/*.sql.gz | head -1)
gunzip -c $BACKUP | docker exec -i postgres psql -U postgres -d abstractfinance

# Emergency stop
ssh root@94.130.228.55 "cd /srv/abstractfinance && docker compose down"
```

## Security & Production Hardening

### Implemented Security Measures

| Category | Measure | Status |
|----------|---------|--------|
| **Authentication** | Headless TOTP 2FA (IBGA) | ✅ Implemented |
| **File Security** | .env permissions (chmod 600) | ✅ Implemented |
| **User Isolation** | Non-root service user `abstractfinance` | ✅ Implemented |
| **Secrets Audit** | Git history checked for leaks | ✅ Clean |
| **Version Pinning** | Docker images pinned | ✅ Implemented |
| **Version Pinning** | Python packages pinned | ✅ Implemented |
| **CI/CD** | GitHub Actions with service user | ✅ Implemented |
| **Maintenance** | Sunday gateway restart cron | ✅ Implemented |
| **Alerts** | Telegram disconnect notifications | ✅ Implemented |
| **Monitoring** | Prometheus metrics endpoint | ✅ Implemented |
| **Monitoring** | Alertmanager with trading-specific rules | ✅ Implemented |
| **Monitoring** | Grafana dashboard provisioned | ✅ Implemented |
| **High Availability** | Standby VPS (fsn1 datacenter) | ✅ Provisioned |
| **High Availability** | WireGuard tunnel between servers | ✅ Active |
| **High Availability** | Failover procedure documented | ✅ Complete |
| **High Availability** | Rollback procedure documented | ✅ Complete |
| **Operations** | IBKR maintenance window awareness | ✅ Implemented |
| **Operations** | Automated PostgreSQL backups | ✅ Implemented |
| **Operations** | PostgreSQL replication scripts | ✅ Available |
| **SSL/TLS** | Caddy reverse proxy config | ✅ Available |
| **Metrics** | Execution metrics (orders/fills/latency) | ✅ Wired |
| **Secrets** | 1Password Business integration | ✅ Configured |
| **Secrets** | Service Account + op CLI | ✅ Installed |

### Pinned Versions

**Docker Images:**
- `heshiming/ibga:latest` (only tag available - no versioned releases)
- `postgres:14-alpine`
- `prom/prometheus:v2.54.1`
- `prom/alertmanager:v0.27.0`
- `grafana/grafana:11.3.0`
- `grafana/loki:3.2.0`
- `grafana/promtail:3.2.0`

**Python:** `3.11.11-slim`

See `requirements.txt` for pinned Python package versions.

### Monitoring Stack

The system includes a comprehensive monitoring stack:

**Prometheus Metrics** (`src/metrics.py`):
- IB Gateway connection state and heartbeat
- Order counts (submitted, filled, rejected) by sleeve
- Portfolio metrics (NAV, exposure, drawdown)
- Risk metrics (regime, VIX, volatility)
- Scheduler status and timing

**Alert Rules** (`infra/alert-rules.yml`):
| Alert | Trigger | Severity |
|-------|---------|----------|
| IBGatewayDisconnected | Connection lost > 2m | Critical |
| DrawdownCritical | DD > 8% | Critical |
| CrisisRegime | Regime = 2 | Critical |
| HighVIX | VIX > 35 | Warning |
| VeryLargeDailyLoss | Daily < -5% | Critical |

**Grafana Dashboard**:
- Real-time connection status
- NAV and drawdown charts
- Order execution metrics
- Risk regime visualization

Access: http://94.130.228.55:3000

### High Availability Architecture

**Server Infrastructure**:
| Role | Hostname | IP | Location | WireGuard IP |
|------|----------|-----|----------|--------------|
| Primary | AbstractFinance-staging | 94.130.228.55 | nbg1 | 10.0.0.1 |
| Standby | abstractfinance-standby | 46.224.46.117 | fsn1 | 10.0.0.2 |

**WireGuard Tunnel**: Active between both servers on UDP 51820

**Failover**: See [docs/FAILOVER.md](docs/FAILOVER.md) for detailed procedure

### Security Best Practices

- Never commit `.env` or `credentials.env`
- Use environment variables for all secrets
- SSH key authentication only (password disabled)
- Service user `abstractfinance` for all operations
- Firewall rules restrict IB Gateway ports

### 1Password Secrets Management

**Status**: ACTIVE - All secrets managed via 1Password

The system uses **1Password Business** with Service Accounts for all secrets management. No plaintext `.env` files are stored in git - the server `.env` is dynamically fetched from 1Password.

**How It Works**:
```
┌─────────────────────┐
│  1Password Cloud    │  ← Source of truth for all secrets
│  (abstractfinance   │
│   .1password.eu)    │
└─────────┬───────────┘
          │
          │ Service Account Token (OP_SERVICE_ACCOUNT_TOKEN)
          │
          ▼
┌─────────────────────┐     ┌─────────────────────┐
│  GitHub Actions     │────▶│   Hetzner Servers   │
│  (auto-refresh on   │     │   /srv/.env fetched │
│   each deploy)      │     │   from 1Password    │
└─────────────────────┘     └─────────────────────┘
          │                           │
          │                           ▼
          │                 ┌─────────────────────┐
          │                 │  Docker Compose     │
          │                 │  reads .env file    │
          └────────────────▶│  injects into       │
                            │  containers         │
                            └─────────────────────┘
```

**Vaults**:
| Vault | Purpose | Items |
|-------|---------|-------|
| `AF - Trading Infra - Staging` | Paper trading | IBKR creds, DB, Telegram, .env |
| `AF - Trading Infra - Prod` | Live trading (future) | Empty - ready for production |

**Items in Staging Vault**:
| Item | Type | Usage |
|------|------|-------|
| `ibkr.staging` | Login | IBKR username/password |
| `ibkr.staging.totp-key` | Password | TOTP secret for headless 2FA |
| `db.staging.password` | Password | PostgreSQL password |
| `grafana.admin.password` | Password | Grafana admin |
| `telegram.bot-token` | API Credential | Telegram alerts |
| `telegram.chat-id` | Password | Telegram chat ID |
| `hetzner.ssh-key.staging` | Secure Note | SSH private + public key |
| `abstractfinance.staging.env` | Secure Note | Complete .env file |

**Manual Refresh** (if needed):
```bash
# On server
export OP_SERVICE_ACCOUNT_TOKEN="ops_eyJ..."
op read "op://AF - Trading Infra - Staging/abstractfinance.staging.env/notesPlain" > /srv/abstractfinance/.env
chmod 600 /srv/abstractfinance/.env
docker compose down && docker compose up -d
```

**Updating Secrets**:
1. Edit the item in 1Password (web or desktop app)
2. If changing .env values, also update the `abstractfinance.staging.env` Secure Note
3. Re-run the manual refresh command above, or trigger a GitHub Actions deploy

**Available Scripts**:
| Script | Purpose |
|--------|---------|
| `scripts/op_validate.sh` | Validate 1Password access |
| `scripts/op_fetch_secret.sh` | Fetch single secret |
| `scripts/op_fetch_env.sh` | Fetch full .env file |
| `scripts/op_bootstrap_server.sh` | Bootstrap new server |
| `scripts/op_inject_env.sh` | Inject secrets as env vars |

**GitHub Secret**: `OP_SERVICE_ACCOUNT_TOKEN` - Already configured

## License

Proprietary - All rights reserved

## Support

For issues and questions:
- Create a GitHub issue
- Contact: support@abstractfinance.io
