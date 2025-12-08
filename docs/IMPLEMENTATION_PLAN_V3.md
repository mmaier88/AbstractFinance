# AbstractFinance Infra v3 - Implementation Plan

> **Goal**: Fully headless 24/7 operation with no manual 2FA intervention

---

## Research Summary: Headless 2FA Options

### Option A: IBGA with TOTP Automation (RECOMMENDED)
**Repository**: [heshiming/ibga](https://github.com/heshiming/ibga)

| Aspect | Details |
|--------|---------|
| **How it works** | Uses `oathtool` to generate TOTP codes from secret key |
| **Env var** | `TOTP_KEY=XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX` (32-char secret) |
| **Requirement** | Account must use **Mobile Authenticator App** (not IB Key) |
| **Availability** | New accounts since late 2023 default to this |
| **Limitation** | Cannot switch from IB Key to Mobile Auth (as of Nov 2024) |

**Steps to get TOTP secret:**
1. Use **2FAS** app (open-source, allows export)
2. During IBKR Mobile Auth setup, scan QR code with 2FAS
3. Export secret key from 2FAS (32-character base32 string)
4. Set `TOTP_KEY` in environment

### Option B: gnzsnz/ib-gateway + IB Key (CURRENT)
| Aspect | Details |
|--------|---------|
| **How it works** | IBC handles login, sends push to phone for approval |
| **Manual step** | Tap "Approve" on phone within 2 min window |
| **Frequency** | Weekly (after Sunday restart) + any unexpected restarts |
| **Settings** | `EXISTING_SESSION_DETECTED_ACTION=primary`, `TWOFA_TIMEOUT_ACTION=restart` |

### Option C: Request 2FA Opt-Out from IBKR
| Aspect | Details |
|--------|---------|
| **How it works** | IBKR can relax 2FA requirement for API-only logins |
| **Request via** | Account Management portal |
| **Trade-off** | Reduced security, limited liability protection |
| **Availability** | Not guaranteed; depends on account type |

### Decision Matrix

| Criteria | IBGA + TOTP | gnzsnz + IB Key | 2FA Opt-Out |
|----------|-------------|-----------------|-------------|
| Fully headless | **YES** | No (phone tap) | **YES** |
| Security | Medium | Higher | Lower |
| Setup complexity | Medium | Low (current) | Low |
| IBKR compliance | Gray area | Supported | Supported |
| Account requirement | Mobile Auth | Any 2FA | Special request |

**Recommendation**: Switch to **IBGA with TOTP** for fully headless operation.

---

## Phase 1: Headless 2FA Gateway (Priority: CRITICAL)
**Timeline: 2-3 days**

### 1.1 Check Current 2FA Method
```bash
# Log into IBKR Account Management
# Settings → Security → Secure Login System
# Determine if using: IB Key, Mobile Auth, or Security Device
```

**If using IB Key**: You may need a new paper trading account with Mobile Auth

### 1.2 Setup Mobile Authenticator (if not already)
1. Install **2FAS** app (iOS/Android) - open source, exportable
2. In IBKR portal: Settings → Secure Login → Add Mobile Authenticator
3. Scan QR code with 2FAS
4. Complete verification
5. **Export TOTP secret** from 2FAS settings

### 1.3 Extract TOTP Secret
```
# In 2FAS app:
# Settings → Export → Export to file
# Or: Long-press entry → Edit → Show secret key

# Secret format: 32-character base32 string
# Example: JBSWY3DPEHPK3PXP...
```

### 1.4 Switch to IBGA Docker Image
```yaml
# docker-compose.yml
services:
  ibgateway:
    image: heshiming/ibga:latest
    container_name: ibgateway
    restart: always
    environment:
      - TZ=Europe/Berlin
      - IB_USERNAME=${IBKR_USERNAME}
      - IB_PASSWORD=${IBKR_PASSWORD}
      - TOTP_KEY=${IBKR_TOTP_KEY}           # 32-char secret
      - IB_REGION=europe
      - IB_TIMEZONE=Europe/Berlin
      - IB_LOGINTYPE=paper                   # or 'live'
      - IB_LOGOFF=11:55 PM Europe/Berlin     # Before IBKR maintenance
      - IB_APILOG=data
      - IB_LOGLEVEL=INFO
    ports:
      - "4001:4001"  # Live API
      - "4002:4002"  # Paper API
      - "5900:5900"  # VNC (debugging)
      - "6080:6080"  # noVNC web interface
    volumes:
      - ibgateway-data:/home/ibg
    networks:
      - trading-network

volumes:
  ibgateway-data:
```

### 1.5 Update .env with TOTP
```bash
# /srv/abstractfinance/.env
IBKR_USERNAME=your_paper_username
IBKR_PASSWORD=your_paper_password
IBKR_TOTP_KEY=JBSWY3DPEHPK3PXP...  # 32-char secret from 2FAS
IBKR_ACCOUNT_ID=DUxxxxxxx
```

### 1.6 Test IBGA Login
```bash
# On server
cd /srv/abstractfinance
docker compose down ibgateway
docker compose up -d ibgateway

# Watch logs for successful auto-login
docker compose logs -f ibgateway

# Expected: "Login successful" without manual intervention
```

### 1.7 Update Trading Engine Connection
```python
# src/execution_ibkr.py - Update default port if needed
# IBGA exposes API on 4001 (live) / 4002 (paper)
```

**Deliverables:**
- [ ] TOTP secret extracted from 2FAS
- [ ] IBGA Docker image configured
- [ ] Fully automated login verified (no phone tap)
- [ ] Trading engine connects successfully

---

## Phase 2: Security Hardening (Priority: HIGH)
**Timeline: 1 day**

### 2.1 Create Non-Root Service User
```bash
ssh root@94.130.228.55

# Create user
useradd -m -s /bin/bash abstract
usermod -aG docker abstract

# Setup SSH
mkdir -p /home/abstract/.ssh
cp /root/.ssh/authorized_keys /home/abstract/.ssh/
chown -R abstract:abstract /home/abstract/.ssh
chmod 700 /home/abstract/.ssh
chmod 600 /home/abstract/.ssh/authorized_keys

# Transfer app ownership
chown -R abstract:abstract /srv/abstractfinance
```

### 2.2 Secret Segmentation
```bash
# Create separate secret directories
mkdir -p /srv/abstractfinance/secrets/gateway
mkdir -p /srv/abstractfinance/secrets/engine

# Gateway secrets (HIGHEST sensitivity - contains TOTP)
cat > /srv/abstractfinance/secrets/gateway/.env << 'EOF'
IBKR_USERNAME=your_username
IBKR_PASSWORD=your_password
IBKR_TOTP_KEY=your_32_char_secret
EOF

# Engine secrets
cat > /srv/abstractfinance/secrets/engine/.env << 'EOF'
POSTGRES_PASSWORD=your_db_password
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
EOF

# Lock down permissions
chmod 600 /srv/abstractfinance/secrets/gateway/.env
chmod 600 /srv/abstractfinance/secrets/engine/.env
chown abstract:abstract /srv/abstractfinance/secrets -R
```

### 2.3 Update docker-compose for Secret Segmentation
```yaml
services:
  ibgateway:
    env_file:
      - ./secrets/gateway/.env
    # Engine cannot see gateway secrets

  trading-engine:
    env_file:
      - ./secrets/engine/.env
    # Gateway secrets not mounted here
```

### 2.4 Host Firewall
```bash
# Enable UFW
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
ufw allow 3000/tcp   # Grafana
ufw allow 51820/udp  # WireGuard (for standby)
ufw enable
```

**Deliverables:**
- [ ] `abstract` user created and owns all files
- [ ] Secrets segmented (gateway vs engine)
- [ ] File permissions locked (600)
- [ ] UFW enabled

---

## Phase 3: Prometheus Trading Metrics (Priority: HIGH)
**Timeline: 1-2 days**

### 3.1 Create `src/metrics.py`
```python
"""Prometheus metrics for AbstractFinance trading engine."""

from prometheus_client import Counter, Gauge, Histogram, start_http_server
import time

# Connection metrics
ib_connection_up = Gauge(
    'ib_connection_up',
    'IB Gateway connection status (1=connected, 0=disconnected)'
)
ib_last_reconnect_unix = Gauge(
    'ib_last_reconnect_unix',
    'Unix timestamp of last successful reconnect'
)
ib_reconnect_total = Counter(
    'ib_reconnect_total',
    'Total reconnection attempts'
)
ib_reconnect_failures = Counter(
    'ib_reconnect_failures_total',
    'Failed reconnection attempts'
)

# Order metrics
ib_orders_submitted = Counter(
    'ib_orders_submitted_total',
    'Orders submitted',
    ['instrument', 'side', 'sleeve']
)
ib_orders_filled = Counter(
    'ib_orders_filled_total',
    'Orders filled',
    ['instrument', 'side', 'sleeve']
)
ib_orders_rejected = Counter(
    'ib_orders_rejected_total',
    'Orders rejected',
    ['instrument', 'reason']
)
ib_order_latency = Histogram(
    'ib_order_latency_seconds',
    'Order fill latency',
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0]
)

# Portfolio metrics
strategy_nav = Gauge('strategy_nav_usd', 'Current NAV in USD')
strategy_daily_pnl = Gauge('strategy_daily_pnl_usd', 'Daily P&L in USD')
strategy_daily_return = Gauge('strategy_daily_return_pct', 'Daily return percentage')
strategy_drawdown_pct = Gauge('strategy_drawdown_pct', 'Current drawdown percentage')
strategy_high_water_mark = Gauge('strategy_hwm_usd', 'High water mark NAV')

# Exposure metrics
portfolio_gross_exposure = Gauge('portfolio_gross_exposure_usd', 'Gross exposure USD')
portfolio_net_exposure = Gauge('portfolio_net_exposure_usd', 'Net exposure USD')
sleeve_exposure_pct = Gauge('sleeve_exposure_pct', 'Sleeve exposure', ['sleeve'])
sleeve_pnl_usd = Gauge('sleeve_pnl_usd', 'Sleeve P&L', ['sleeve'])

# Risk metrics
portfolio_realized_vol = Gauge('portfolio_realized_vol_20d', '20-day realized volatility')
portfolio_beta_spx = Gauge('portfolio_beta_spx', 'Portfolio beta to SPX')
hedge_budget_used_usd = Gauge('hedge_budget_used_usd', 'Hedge budget used YTD')
hedge_budget_remaining_pct = Gauge('hedge_budget_remaining_pct', 'Hedge budget remaining %')

# Regime metrics
market_regime = Gauge('market_regime', 'Current regime (0=normal, 1=elevated, 2=crisis)')
vix_level = Gauge('vix_level', 'Current VIX level')

def start_metrics_server(port: int = 8000):
    """Start Prometheus metrics HTTP server."""
    start_http_server(port)

def update_connection_metrics(connected: bool, reconnect_attempt: bool = False):
    """Update connection-related metrics."""
    ib_connection_up.set(1 if connected else 0)
    if connected:
        ib_last_reconnect_unix.set(time.time())
    if reconnect_attempt:
        ib_reconnect_total.inc()
        if not connected:
            ib_reconnect_failures.inc()

def update_portfolio_metrics(portfolio_state):
    """Update portfolio metrics from PortfolioState."""
    strategy_nav.set(portfolio_state.nav)
    strategy_drawdown_pct.set(portfolio_state.current_drawdown * 100)
    strategy_high_water_mark.set(portfolio_state.high_water_mark)
    portfolio_gross_exposure.set(portfolio_state.gross_exposure)
    portfolio_net_exposure.set(portfolio_state.net_exposure)

    for sleeve_name, weight in portfolio_state.sleeve_weights.items():
        sleeve_exposure_pct.labels(sleeve=sleeve_name).set(weight * 100)
```

### 3.2 Integrate Metrics into Scheduler
Update `scheduler.py` to:
- Call `start_metrics_server(8000)` on init
- Call `update_portfolio_metrics()` after each daily run
- Call `update_connection_metrics()` on connect/disconnect

### 3.3 Expose Port in Docker Compose
```yaml
trading-engine:
  ports:
    - "8000:8000"  # Prometheus metrics
```

### 3.4 Update Prometheus Scrape Config
```yaml
# prometheus/prometheus.yml
scrape_configs:
  - job_name: 'trading-engine'
    static_configs:
      - targets: ['trading-engine:8000']
    scrape_interval: 15s
```

**Deliverables:**
- [ ] `src/metrics.py` with full trading metrics
- [ ] Metrics integrated into scheduler
- [ ] Prometheus scraping verified
- [ ] Basic Grafana dashboard created

---

## Phase 4: Alertmanager & Alert Rules (Priority: HIGH)
**Timeline: 0.5 days**

### 4.1 Add Alertmanager to Docker Compose
```yaml
alertmanager:
  image: prom/alertmanager:v0.26.0
  container_name: alertmanager
  restart: always
  volumes:
    - ./alertmanager/alertmanager.yml:/etc/alertmanager/alertmanager.yml:ro
  ports:
    - "9093:9093"
  networks:
    - trading-network
```

### 4.2 Create Alert Rules
```yaml
# prometheus/alerts.yml
groups:
  - name: connectivity
    rules:
      - alert: IBGatewayDown
        expr: ib_connection_up == 0
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "IB Gateway connection down"
          description: "Trading engine disconnected from IB Gateway for 5+ minutes"

      - alert: FrequentReconnects
        expr: increase(ib_reconnect_total[1h]) > 5
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Frequent reconnection attempts"
          description: "{{ $value }} reconnects in the past hour"

  - name: risk
    rules:
      - alert: DrawdownWarning
        expr: strategy_drawdown_pct > 5
        for: 15m
        labels:
          severity: warning
        annotations:
          summary: "Drawdown exceeds 5%"
          description: "Current drawdown: {{ $value }}%"

      - alert: DrawdownCritical
        expr: strategy_drawdown_pct > 10
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "CRITICAL: Drawdown exceeds 10%"
          description: "Emergency de-risk may be triggered. Drawdown: {{ $value }}%"

      - alert: HighVIX
        expr: vix_level > 30
        for: 30m
        labels:
          severity: warning
        annotations:
          summary: "VIX elevated"
          description: "VIX at {{ $value }}, consider reducing exposure"

  - name: orders
    rules:
      - alert: HighOrderRejections
        expr: rate(ib_orders_rejected_total[10m]) > 0.1
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "High order rejection rate"
          description: "Order rejections elevated over past 10 minutes"

      - alert: NoOrdersPlaced
        expr: increase(ib_orders_submitted_total[24h]) == 0
        for: 1h
        labels:
          severity: info
        annotations:
          summary: "No orders in 24h"
          description: "Trading engine has not placed any orders"
```

### 4.3 Alertmanager Config (Telegram)
```yaml
# alertmanager/alertmanager.yml
global:
  resolve_timeout: 5m

route:
  receiver: 'telegram'
  group_by: ['alertname', 'severity']
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  routes:
    - match:
        severity: critical
      receiver: 'telegram'
      repeat_interval: 30m

receivers:
  - name: 'telegram'
    telegram_configs:
      - bot_token_file: /etc/alertmanager/telegram_token
        chat_id: <your_chat_id>
        parse_mode: 'Markdown'
        message: |
          *{{ .Status | toUpper }}* {{ if eq .Status "firing" }}🔥{{ else }}✅{{ end }}
          *Alert:* {{ .CommonLabels.alertname }}
          *Severity:* {{ .CommonLabels.severity }}
          {{ range .Alerts }}
          {{ .Annotations.summary }}
          {{ .Annotations.description }}
          {{ end }}
```

**Deliverables:**
- [ ] Alertmanager container running
- [ ] Alert rules for connectivity, risk, orders
- [ ] Telegram notifications working
- [ ] Test alert fired and received

---

## Phase 5: Position Reconciliation (Priority: HIGH)
**Timeline: 1 day**

### 5.1 Add Reconciliation Method
```python
# In src/execution_ibkr.py

def reconcile_positions(
    self,
    local_positions: Dict[str, Position],
    tolerance: float = 0.01
) -> Tuple[bool, List[str]]:
    """
    Compare IB positions with local state.

    Args:
        local_positions: Dict of instrument_id -> Position from Postgres
        tolerance: Acceptable quantity difference (default 1%)

    Returns:
        Tuple of (reconciled: bool, mismatches: List[str])
    """
    ib_positions = self.get_positions()
    mismatches = []

    # Check local positions exist in IB
    for inst_id, local_pos in local_positions.items():
        ib_pos = ib_positions.get(inst_id)
        if ib_pos is None and abs(local_pos.quantity) > tolerance:
            mismatches.append(f"{inst_id}: local={local_pos.quantity:.2f}, IB=0")
        elif ib_pos and abs(ib_pos.quantity - local_pos.quantity) > tolerance:
            mismatches.append(
                f"{inst_id}: local={local_pos.quantity:.2f}, "
                f"IB={ib_pos.quantity:.2f}"
            )

    # Check IB positions not in local
    for inst_id, ib_pos in ib_positions.items():
        if inst_id not in local_positions and abs(ib_pos.quantity) > tolerance:
            mismatches.append(f"{inst_id}: local=0, IB={ib_pos.quantity:.2f}")

    if mismatches:
        self.logger.log_alert(
            alert_type="position_mismatch",
            severity="critical",
            message="Position reconciliation failed:\n" + "\n".join(mismatches)
        )
        if self.alert_manager:
            self.alert_manager.send_connection_error(
                f"⚠️ POSITION MISMATCH DETECTED!\n\n" +
                "\n".join(mismatches) +
                "\n\nTrading paused until resolved."
            )
        return False, mismatches

    self.logger.logger.info("position_reconciliation_success")
    return True, []
```

### 5.2 Call on Every Reconnect
```python
# In _on_disconnect handler or reconnect success
def _attempt_reconnect(self) -> bool:
    # ... existing reconnect logic ...

    if self.ib.isConnected():
        # Reconcile before resuming trading
        if hasattr(self, 'local_positions'):
            reconciled, mismatches = self.reconcile_positions(self.local_positions)
            if not reconciled:
                self.trading_halted = True
                return False

    return True
```

**Deliverables:**
- [ ] `reconcile_positions()` method implemented
- [ ] Called on every reconnect
- [ ] Alert sent on mismatch
- [ ] Trading halted until manual review

---

## Phase 6: Standby VPS & WireGuard (Priority: MEDIUM)
**Timeline: 1-2 days**

### 6.1 Provision Standby
- Hetzner CX21 (4 vCPU, 8GB RAM) - ~€7/month
- Same datacenter (FSN1 or NBG1)

### 6.2 WireGuard Setup
```bash
# Primary server (94.130.228.55)
apt install wireguard
wg genkey | tee /etc/wireguard/private.key | wg pubkey > /etc/wireguard/public.key

cat > /etc/wireguard/wg0.conf << 'EOF'
[Interface]
Address = 10.100.0.1/24
PrivateKey = <primary_private_key>
ListenPort = 51820

[Peer]
PublicKey = <standby_public_key>
AllowedIPs = 10.100.0.2/32
EOF

systemctl enable wg-quick@wg0
systemctl start wg-quick@wg0
```

### 6.3 Clone Stack to Standby
```bash
# On standby
git clone https://github.com/mmaier88/AbstractFinance.git /srv/abstractfinance
# Copy secrets (via encrypted transfer over WireGuard)
scp -r 10.100.0.1:/srv/abstractfinance/secrets /srv/abstractfinance/

# DO NOT start ibgateway on standby (avoid conflicting sessions)
# Only start monitoring stack
docker compose up -d prometheus grafana
```

### 6.4 Cross-Monitoring
Configure standby Prometheus to monitor primary via WireGuard:
```yaml
# On standby prometheus.yml
scrape_configs:
  - job_name: 'primary-trading-engine'
    static_configs:
      - targets: ['10.100.0.1:8000']
```

**Deliverables:**
- [ ] Standby VPS provisioned
- [ ] WireGuard tunnel operational
- [ ] Stack cloned (gateway OFF)
- [ ] Cross-monitoring configured

---

## Phase 7: Failover Runbook (Priority: MEDIUM)
**Timeline: 0.5 days**

Create `docs/RUNBOOK_FAILOVER.md` with:
1. Primary failure detection criteria
2. Pre-failover checklist
3. Step-by-step failover procedure
4. Post-failover verification
5. Primary restoration procedure

**Deliverables:**
- [ ] Failover runbook documented
- [ ] Quarterly drill scheduled
- [ ] Incident log template created

---

## Phase 8: Version Pinning (Priority: LOW)
**Timeline: 0.5 days**

Pin all images and packages:
```yaml
# docker-compose.yml
ibgateway:
  image: heshiming/ibga:2024.11  # Specific version
postgres:
  image: postgres:14.10-alpine
prometheus:
  image: prom/prometheus:v2.48.0
grafana:
  image: grafana/grafana:10.2.3
alertmanager:
  image: prom/alertmanager:v0.26.0
```

```txt
# requirements.txt
ib_insync==0.9.86
pandas==2.1.4
prometheus_client==0.19.0
```

---

## Revised Timeline

### Week 1: Headless 2FA + Security
| Day | Task |
|-----|------|
| 1-2 | Phase 1: IBGA + TOTP setup, verify headless login |
| 3 | Phase 2: Non-root user, secret segmentation, UFW |

### Week 2: Monitoring + Safety
| Day | Task |
|-----|------|
| 1-2 | Phase 3: Prometheus metrics implementation |
| 3 | Phase 4: Alertmanager + alert rules |
| 4 | Phase 5: Position reconciliation |

### Week 3: Resilience
| Day | Task |
|-----|------|
| 1-2 | Phase 6: Standby VPS + WireGuard |
| 3 | Phase 7: Failover runbook |
| 4 | Phase 8: Version pinning |

---

## Success Criteria

Before going live:

- [ ] **Headless 2FA**: Gateway logs in automatically with TOTP (no phone tap)
- [ ] **Security**: Non-root user, secrets segmented, UFW enabled
- [ ] **Monitoring**: All trading metrics in Prometheus, dashboards in Grafana
- [ ] **Alerting**: Critical alerts → Telegram within 5 minutes
- [ ] **Reconciliation**: Position check on every reconnect
- [ ] **Resilience**: Standby VPS ready, failover tested once
- [ ] **Paper trading**: 60 days completed with <5% order rejection rate
