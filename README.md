# AbstractFinance - European Decline Macro Fund

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
│   ├── instruments.yaml       # Symbol mappings & contract specs
│   └── credentials.env.template
├── src/
│   ├── __init__.py
│   ├── data_feeds.py          # Market data abstraction (IB + fallbacks)
│   ├── portfolio.py           # Positions, NAV, P&L, sleeves
│   ├── risk_engine.py         # Vol targeting, DD, hedge budget
│   ├── strategy_logic.py      # Sleeve construction + regime filter
│   ├── tail_hedge.py          # Tail hedge & crisis management
│   ├── execution_ibkr.py      # IBKR integration via ib_insync
│   ├── reconnect.py           # Watchdog & reconnection layer
│   ├── scheduler.py           # Daily run orchestrator
│   ├── backtest.py            # Historical + Monte Carlo backtester
│   ├── paper_trading.py       # 60-day burn-in orchestrator
│   ├── alerts.py              # Telegram/email alerts
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
│   └── grafana/
├── .github/workflows/
│   ├── ci.yml
│   ├── deploy-staging.yml
│   └── deploy-production.yml
├── docker-compose.yml
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

## IB Gateway Setup

### Hetzner Server Setup

```bash
# Install dependencies
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-venv python3-pip git tmux default-jre

# Install Docker
curl -fsSL https://get.docker.com | sudo bash
sudo usermod -aG docker $(whoami)

# Clone and setup
git clone https://github.com/yourusername/AbstractFinance.git /srv/abstractfinance
cd /srv/abstractfinance
cp config/credentials.env.template .env
```

### IB Gateway Configuration

The system uses the [gnzsnz/ib-gateway](https://github.com/gnzsnz/ib-gateway) Docker image for headless IB Gateway:

- **Paper trading port**: 4002
- **Live trading port**: 4001
- Automatic daily restarts
- Automatic reconnection

## Risk Management

### Volatility Targeting

The system implements volatility targeting:
- Computes 20-day realized volatility
- Scales positions to maintain target volatility
- Caps at maximum gross leverage

### Regime Detection

Monitors SPX/SX5E ratio momentum:
- **Normal**: Full position sizing
- **Elevated**: Reduced exposure (VIX > 25)
- **Crisis**: Emergency de-risk (VIX > 40 or DD > 10%)

### Tail Hedges

Hedge budget allocated across:
- **40%**: Equity puts (SPY, FEZ)
- **20%**: VIX calls
- **15%**: Credit puts (HYG)
- **15%**: Sovereign spread (OAT-Bund)
- **10%**: European bank puts

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

| Server | IP | Type | Purpose |
|--------|-----|------|---------|
| Staging | 94.130.228.55 | CX33 | Paper trading, testing |
| Production | 91.99.116.196 | CX43 | Live trading |

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

## Scheduled Maintenance

- **Sunday 22:00 UTC**: Automatic IB Gateway restart (cron job)
- **Weekly**: IBKR maintenance window (Sunday 23:45-Monday 00:45 UTC)

Cron job location: `/etc/cron.d/abstractfinance-maintenance`

## Security

- Never commit `credentials.env`
- Use environment variables for secrets
- Restrict IB login to server IPs
- Use Vault for production secrets
- SSH key authentication only

## License

Proprietary - All rights reserved

## Support

For issues and questions:
- Create a GitHub issue
- Contact: support@abstractfinance.io
