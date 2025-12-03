# AbstractFinance - Implementation Documentation

## Project Overview

**AbstractFinance** is a production-grade automated trading system implementing a multi-sleeve macro hedge fund strategy expressing a structural view on US vs European economic performance (the "European Decline" thesis).

**Repository**: https://github.com/mmaier88/AbstractFinance

---

## Infrastructure

### Servers (Hetzner Cloud)

| Server | Hostname | IP | Type | Location | Purpose |
|--------|----------|-----|------|----------|---------|
| Staging | AbstractFinance-staging | 94.130.228.55 | CX33 | nbg1 | Paper trading, testing |
| Production | AbstractFinance-prod | 91.99.116.196 | CX43 | fsn1 | Live trading (ready) |

### SSH Access
- SSH Key: `maier-ssh-key` (id_ed25519)
- User: `root`
- Example: `ssh root@94.130.228.55`

### Firewall Configuration
Firewall `abstractfinance-firewall` (ID: 10255499) applied to both servers:

| Port | Service | Status |
|------|---------|--------|
| 22 | SSH | Open |
| 3000 | Grafana | Open |
| 9090 | Prometheus | Open |
| ICMP | Ping | Open |
| 4001/4002 | IB Gateway | Blocked |
| 5432 | PostgreSQL | Blocked |
| 5900 | VNC | Blocked |

---

## Docker Stack

All services run via Docker Compose (`docker-compose.yml`):

| Container | Image | Purpose | Ports |
|-----------|-------|---------|-------|
| ibgateway | ghcr.io/gnzsnz/ib-gateway:latest | IB Gateway headless | 4001, 4002, 5900 |
| trading-engine | abstractfinance-trading-engine | Main trading logic | - |
| postgres | postgres:14-alpine | Database | 5432 |
| prometheus | prom/prometheus:latest | Metrics | 9090 |
| grafana | grafana/grafana:latest | Dashboards | 3000 |
| loki | grafana/loki:latest | Log aggregation | 3100 |
| promtail | grafana/promtail:latest | Log collection | - |

### Access URLs (Staging)
- Grafana: http://94.130.228.55:3000
  - User: `admin`
  - Password: `AbstractFinance_Grafana_2024!`
- Prometheus: http://94.130.228.55:9090

---

## IBKR Configuration

### Credentials
- Username: `abstractcapital`
- Account ID: `U23203300`
- Paper Account: `DUO775682`

### Ports
- Paper trading: 4002
- Live trading: 4001

### IB Gateway Features
- Automatic login via IBController
- Daily auto-restart at 11:59 PM
- 2FA timeout action: restart
- Reconnection on connection loss

---

## CI/CD Pipeline

### GitHub Actions Workflows

1. **CI** (`.github/workflows/ci.yml`)
   - Triggers: Push to main/develop, PRs to main
   - Jobs: test, lint, build
   - Runs pytest, flake8, black, Docker build

2. **Deploy Staging** (`.github/workflows/deploy-staging.yml`)
   - Triggers: Push to main
   - Actions: SSH to staging, git pull, docker compose up

3. **Deploy Production** (`.github/workflows/deploy-production.yml`)
   - Triggers: Release published, manual workflow_dispatch
   - Actions: Creates backup, deploys tagged version, verifies IB connection
   - Includes rollback on failure

### GitHub Secrets
| Secret | Value |
|--------|-------|
| STAGING_HOST | 94.130.228.55 |
| PRODUCTION_HOST | 91.99.116.196 |
| HETZNER_SSH_KEY | SSH private key (id_ed25519) |

---

## Project Structure

```
AbstractFinance/
├── config/
│   ├── settings.yaml          # All tunable parameters
│   ├── instruments.yaml       # Symbol mappings & contract specs
│   └── credentials.env.template
├── src/
│   ├── __init__.py
│   ├── data_feeds.py          # Market data abstraction (IB + yfinance fallback)
│   ├── portfolio.py           # Positions, NAV, P&L, sleeves
│   ├── risk_engine.py         # Vol targeting, DD, hedge budget
│   ├── strategy_logic.py      # Sleeve construction + regime filter
│   ├── tail_hedge.py          # Tail hedge & crisis management
│   ├── execution_ibkr.py      # IBKR integration via ib_insync
│   ├── reconnect.py           # Watchdog & reconnection layer
│   ├── scheduler.py           # Daily run orchestrator (main entrypoint)
│   ├── backtest.py            # Historical + Monte Carlo backtester
│   ├── paper_trading.py       # 60-day burn-in orchestrator
│   ├── alerts.py              # Telegram/email alerts (disabled)
│   └── logging_utils.py       # Structured JSON logging
├── scripts/
│   ├── setup_cron.sh          # Install cron job
│   └── run_daily.sh           # Daily run wrapper
├── tests/
│   ├── test_portfolio.py
│   ├── test_risk_engine.py
│   ├── test_strategy_logic.py
│   └── test_tail_hedge.py
├── infra/
│   ├── prometheus.yml
│   ├── loki-config.yml
│   ├── promtail-config.yml
│   └── grafana/provisioning/
├── .github/workflows/
│   ├── ci.yml
│   ├── deploy-staging.yml
│   └── deploy-production.yml
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── README.md
```

---

## Environment Variables

### .env File (on servers at `/srv/abstractfinance/.env`)

**Staging:**
```
IBKR_USERNAME=abstractcapital
IBKR_PASSWORD=<redacted>
IBKR_ACCOUNT_ID=U23203300
IBKR_PORT=4004                    # Paper trading via socat relay
TRADING_MODE=paper
MODE=paper
DB_PASSWORD=AbstractFinance_Staging_2024!
GRAFANA_PASSWORD=AbstractFinance_Grafana_2024!
ENVIRONMENT=staging
```

**Production:**
```
IBKR_USERNAME=abstractcapital
IBKR_PASSWORD=<redacted>
IBKR_ACCOUNT_ID=U23203300
IBKR_PORT=4003                    # Live trading via socat relay
TRADING_MODE=live
MODE=live
DB_PASSWORD=AbstractFinance_Prod_2024_Secure!
GRAFANA_PASSWORD=AbstractFinance_Grafana_Prod_2024!
ENVIRONMENT=production
```

---

## Key Implementation Details

### Fixes Applied During Deployment

1. **Loki Config** (`infra/loki-config.yml`)
   - Added `limits_config.allow_structured_metadata: false` for compatibility with latest Loki

2. **Docker Compose Health Check**
   - Changed from `nc -z` to `echo > /dev/tcp/localhost/4004` (nc not available in container)
   - Added `start_period: 120s` to allow IB Gateway login time
   - Changed trading-engine dependency from `service_healthy` to `service_started`

3. **Scheduler Environment Variables** (`src/scheduler.py`)
   - Added environment variable priority over settings.yaml
   - `IBKR_HOST`, `IBKR_PORT`, `MODE` now read from environment for Docker networking

4. **IB Gateway Socat Relay Ports** (`docker-compose.yml`)
   - The gnzsnz/ib-gateway image uses socat to relay API connections:
     - Internal ports 4001/4002 are bound to 127.0.0.1 only
     - Socat exposes 4003 (live) and 4004 (paper) for Docker network access
   - Port mapping: host:4001→container:4003, host:4002→container:4004
   - Trading-engine connects to port 4004 (paper via socat relay)

5. **Continuous Scheduler with Startup Delay** (`src/scheduler.py`)
   - Added 120-second startup delay to wait for IB Gateway
   - Added retry logic (5 attempts with 60s delay) for initialization failures
   - Scheduler runs continuously, executing daily job at 06:00 UTC

### Disabled Features
- Telegram alerts (disabled in `config/settings.yaml`)
- Email alerts (disabled in `config/settings.yaml`)

---

## Server Setup Commands (Reference)

### Initial Server Setup
```bash
# Install Docker
curl -fsSL https://get.docker.com | sh
systemctl enable docker && systemctl start docker

# Clone repository
cd /srv
git clone https://github.com/mmaier88/AbstractFinance.git abstractfinance
cd abstractfinance

# Create .env file (see above for contents)
nano .env

# Create directories with proper permissions
mkdir -p state/logs logs
chmod -R 777 state logs

# Start all services
docker compose up -d
```

### Common Operations
```bash
# View all containers
docker compose ps -a

# View logs
docker compose logs trading-engine --tail=50
docker compose logs ibgateway --tail=50

# Restart a service
docker compose restart trading-engine

# Update from GitHub
git pull origin main
docker compose build trading-engine
docker compose up -d

# Full restart
docker compose down && docker compose up -d
```

---

## Monitoring

### Log Locations
- Trading engine logs: `/srv/abstractfinance/state/logs/`
- Docker logs: `docker compose logs <service>`
- Loki aggregation: http://94.130.228.55:3100

### Health Checks
- IB Gateway: Check `docker logs ibgateway` for "Login has completed"
- Trading Engine: Check for "scheduler_init" in logs
- PostgreSQL: `docker compose exec postgres pg_isready`

---

## Deployment Timeline

| Date | Action |
|------|--------|
| 2025-12-03 | Initial code implementation |
| 2025-12-03 | GitHub repository created |
| 2025-12-03 | Servers provisioned on Hetzner |
| 2025-12-03 | Docker stack deployed to staging |
| 2025-12-03 | IB Gateway connected (paper account) |
| 2025-12-03 | CI/CD pipeline configured |
| 2025-12-03 | Firewall configured |
| 2025-12-03 | Paper trading burn-in started |

---

## Next Steps

1. **Monitor paper trading** for 60 days
2. **Validate performance** against expected metrics:
   - Sharpe ratio > 0.5
   - Max drawdown > -15%
   - Order rejection rate < 5%
   - Minimum 50 trades executed
3. **Deploy to production** after validation passes
4. **Set up database backups** for production

---

## Contacts & Support

- Repository: https://github.com/mmaier88/AbstractFinance
- Issues: https://github.com/mmaier88/AbstractFinance/issues
