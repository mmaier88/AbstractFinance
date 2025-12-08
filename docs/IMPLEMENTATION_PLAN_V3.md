# AbstractFinance Infra v3 - Implementation Plan

## Current State Assessment

### Already Implemented
- [x] Basic Docker Compose stack (trading-engine, ibgateway, postgres, prometheus, grafana)
- [x] Trading engine with `ib_insync`
- [x] Basic reconnection logic in `execution_ibkr.py`
- [x] Telegram alerts integration (awaiting credentials)
- [x] Sunday gateway restart cron job
- [x] CI/CD via GitHub Actions
- [x] Basic Prometheus/Grafana monitoring

### Not Yet Implemented
- [ ] Headless 2FA gateway (black-box component)
- [ ] Non-root service user
- [ ] Secret segmentation (gateway vs engine)
- [ ] Standby VPS with WireGuard
- [ ] Comprehensive Prometheus trading metrics
- [ ] Alertmanager rules
- [ ] Failover runbook
- [ ] Position reconciliation on reconnect

---

## Phase 1: Security Hardening (Priority: HIGH)
**Timeline: 1-2 days**

### 1.1 Create Non-Root Service User
```bash
# On server
useradd -m -s /bin/bash abstract
usermod -aG docker abstract
mkdir -p /home/abstract/.ssh
cp /root/.ssh/authorized_keys /home/abstract/.ssh/
chown -R abstract:abstract /home/abstract/.ssh
chmod 700 /home/abstract/.ssh
chmod 600 /home/abstract/.ssh/authorized_keys
```

### 1.2 Transfer Ownership
```bash
chown -R abstract:abstract /srv/abstractfinance
chmod 600 /srv/abstractfinance/.env
```

### 1.3 Secret Segmentation
Create directory structure:
```
/srv/abstractfinance/
├── secrets/
│   ├── gateway/          # IBKR credentials, 2FA config
│   │   └── .env.gateway
│   └── engine/           # DB, Telegram, internal keys
│       └── .env.engine
```

### 1.4 Host Firewall (UFW)
```bash
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
ufw allow 3000/tcp  # Grafana
ufw allow 9090/tcp  # Prometheus (consider restricting)
ufw enable
```

**Deliverables:**
- [ ] `abstract` user created and configured
- [ ] All files owned by `abstract`
- [ ] Secrets segmented into gateway/ and engine/
- [ ] UFW enabled with minimal ports

---

## Phase 2: Prometheus Trading Metrics (Priority: HIGH)
**Timeline: 1-2 days**

### 2.1 Create `src/metrics.py`
```python
from prometheus_client import Counter, Gauge, Histogram, start_http_server

# Connection metrics
ib_connection_up = Gauge('ib_connection_up', 'IB Gateway connection status')
ib_last_reconnect_unix = Gauge('ib_last_reconnect_unix', 'Last reconnect timestamp')
ib_reconnect_total = Counter('ib_reconnect_total', 'Total reconnection attempts')

# Order metrics
ib_orders_submitted = Counter('ib_orders_submitted_total', 'Orders submitted', ['instrument', 'side'])
ib_orders_filled = Counter('ib_orders_filled_total', 'Orders filled', ['instrument', 'side'])
ib_orders_rejected = Counter('ib_orders_rejected_total', 'Orders rejected', ['instrument', 'reason'])
ib_order_latency = Histogram('ib_order_latency_seconds', 'Order fill latency')

# Portfolio metrics
strategy_nav = Gauge('strategy_nav', 'Current NAV in USD')
strategy_drawdown_pct = Gauge('strategy_drawdown_pct', 'Current drawdown percentage')
sleeve_exposure_pct = Gauge('sleeve_exposure_pct', 'Sleeve exposure', ['sleeve'])
portfolio_gross_exposure = Gauge('portfolio_gross_exposure', 'Gross exposure USD')
portfolio_net_exposure = Gauge('portfolio_net_exposure', 'Net exposure USD')

# Risk metrics
portfolio_realized_vol = Gauge('portfolio_realized_vol', '20-day realized volatility')
hedge_budget_used = Gauge('hedge_budget_used', 'Hedge budget used YTD')

def start_metrics_server(port=8000):
    start_http_server(port)
```

### 2.2 Integrate with Scheduler
Update `scheduler.py` to:
- Start metrics server on init
- Update metrics after each daily run
- Update connection status on connect/disconnect

### 2.3 Update Prometheus Config
```yaml
# prometheus/prometheus.yml
scrape_configs:
  - job_name: 'trading-engine'
    static_configs:
      - targets: ['trading-engine:8000']
    scrape_interval: 15s
```

**Deliverables:**
- [ ] `src/metrics.py` with all trading metrics
- [ ] Metrics integrated into scheduler and execution
- [ ] Prometheus scraping trading-engine:8000
- [ ] Basic Grafana dashboard with trading metrics

---

## Phase 3: Alertmanager Rules (Priority: HIGH)
**Timeline: 0.5 days**

### 3.1 Create Alert Rules
```yaml
# prometheus/alerts.yml
groups:
  - name: trading
    rules:
      - alert: IBKRConnectionDown
        expr: ib_connection_up == 0
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "IBKR connection down for 5+ minutes"

      - alert: HighDrawdown
        expr: strategy_drawdown_pct > 5
        for: 15m
        labels:
          severity: warning
        annotations:
          summary: "Drawdown {{ $value }}% exceeds 5%"

      - alert: CriticalDrawdown
        expr: strategy_drawdown_pct > 10
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "CRITICAL: Drawdown {{ $value }}% exceeds 10%"

      - alert: HighOrderRejections
        expr: rate(ib_orders_rejected_total[5m]) > 0.1
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "High order rejection rate"

      - alert: FrequentReconnects
        expr: increase(ib_reconnect_total[1h]) > 5
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "More than 5 reconnects in the past hour"
```

### 3.2 Alertmanager Config
```yaml
# alertmanager/alertmanager.yml
route:
  receiver: 'telegram'
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h

receivers:
  - name: 'telegram'
    telegram_configs:
      - bot_token: '${TELEGRAM_BOT_TOKEN}'
        chat_id: ${TELEGRAM_CHAT_ID}
        parse_mode: 'Markdown'
```

**Deliverables:**
- [ ] `prometheus/alerts.yml` with trading rules
- [ ] Alertmanager container added to docker-compose
- [ ] Alerts routing to Telegram

---

## Phase 4: Position Reconciliation (Priority: HIGH)
**Timeline: 1 day**

### 4.1 Add Reconciliation to IBClient
On every reconnect:
1. Fetch positions from IB
2. Compare with local Postgres state
3. If mismatch > threshold: halt trading, raise alert
4. Log discrepancies

### 4.2 Implementation
```python
# In execution_ibkr.py
def reconcile_positions(self, local_positions: Dict) -> bool:
    """
    Compare IB positions with local state.
    Returns True if reconciled, False if mismatch requires intervention.
    """
    ib_positions = self.get_positions()

    mismatches = []
    for inst_id, local_pos in local_positions.items():
        ib_pos = ib_positions.get(inst_id)
        if ib_pos is None:
            mismatches.append(f"{inst_id}: local={local_pos.quantity}, IB=0")
        elif abs(ib_pos.quantity - local_pos.quantity) > 0.01:
            mismatches.append(f"{inst_id}: local={local_pos.quantity}, IB={ib_pos.quantity}")

    # Check for positions in IB not in local
    for inst_id in ib_positions:
        if inst_id not in local_positions:
            mismatches.append(f"{inst_id}: local=0, IB={ib_positions[inst_id].quantity}")

    if mismatches:
        self.logger.log_alert(
            alert_type="position_mismatch",
            severity="critical",
            message=f"Position reconciliation failed:\n" + "\n".join(mismatches)
        )
        if self.alert_manager:
            self.alert_manager.send_connection_error(
                f"POSITION MISMATCH DETECTED!\n\n" + "\n".join(mismatches)
            )
        return False

    return True
```

**Deliverables:**
- [ ] `reconcile_positions()` method in IBClient
- [ ] Called on every reconnect
- [ ] Alerts on mismatch
- [ ] Option to halt trading on critical mismatch

---

## Phase 5: Standby VPS & WireGuard (Priority: MEDIUM)
**Timeline: 1-2 days**

### 5.1 Provision Standby Server
- Hetzner CX21 (cheaper standby)
- Same region as primary for low latency

### 5.2 WireGuard Setup
```bash
# Primary (94.130.228.55)
[Interface]
Address = 10.100.0.1/24
PrivateKey = <primary_private_key>
ListenPort = 51820

[Peer]
PublicKey = <standby_public_key>
AllowedIPs = 10.100.0.2/32
Endpoint = <standby_ip>:51820

# Standby
[Interface]
Address = 10.100.0.2/24
PrivateKey = <standby_private_key>
ListenPort = 51820

[Peer]
PublicKey = <primary_public_key>
AllowedIPs = 10.100.0.1/32
Endpoint = 94.130.228.55:51820
```

### 5.3 Clone Stack to Standby
```bash
# On standby
git clone https://github.com/mmaier88/AbstractFinance.git /srv/abstractfinance
# Copy secrets (encrypted transfer)
# DO NOT start gateway on standby (avoid double login)
```

### 5.4 Cross-Monitoring
- Standby monitors primary via WireGuard
- If primary down for >10min, manual failover decision

**Deliverables:**
- [ ] Standby VPS provisioned
- [ ] WireGuard tunnel established
- [ ] Stack cloned to standby (gateway OFF)
- [ ] Cross-monitoring configured

---

## Phase 6: Failover Runbook (Priority: MEDIUM)
**Timeline: 0.5 days**

### 6.1 Create `docs/RUNBOOK_FAILOVER.md`
Document step-by-step:
1. How to detect primary failure
2. Pre-failover checks
3. Failover procedure
4. Post-failover verification
5. Restore primary procedure

### 6.2 Quarterly Drill Schedule
- Test failover every quarter
- Document results in `docs/incidents.md`

**Deliverables:**
- [ ] `docs/RUNBOOK_FAILOVER.md`
- [ ] `docs/incidents.md` template
- [ ] Quarterly drill calendar

---

## Phase 7: Headless 2FA Gateway (Priority: MEDIUM)
**Timeline: Research phase**

### 7.1 Options to Evaluate
1. **gnzsnz/ib-gateway with IBKR Mobile** (current)
   - Requires manual 2FA approval periodically
   - Use `EXISTING_SESSION_DETECTED_ACTION=primary`

2. **IBC + TOTP automation** (research needed)
   - Store TOTP secret on server
   - Automate 2FA entry
   - Security implications must be understood

3. **Commercial solutions**
   - Evaluate if any vendors offer compliant headless gateway

### 7.2 Interface Contract
Regardless of implementation, gateway must:
- Expose TWS API on port 4004 (paper) / 4003 (live)
- Handle its own 2FA internally
- Expose health metric: `gateway_logged_in{mode="paper|live"}`
- Restart cleanly on container restart

### 7.3 Secret Isolation
Gateway secrets (IBKR creds, TOTP) must be:
- Separate from engine secrets
- Mounted read-only
- Never logged

**Deliverables:**
- [ ] Decision document on 2FA approach
- [ ] Interface spec for headless gateway
- [ ] Security review sign-off

---

## Phase 8: Version Pinning (Priority: LOW)
**Timeline: 0.5 days**

### 8.1 Pin Docker Images
```yaml
# docker-compose.yml
services:
  ibgateway:
    image: ghcr.io/gnzsnz/ib-gateway:10.19.2j
  postgres:
    image: postgres:14.10-alpine
  prometheus:
    image: prom/prometheus:v2.48.0
  grafana:
    image: grafana/grafana:10.2.3
```

### 8.2 Pin Python Packages
```txt
# requirements.txt
ib_insync==0.9.86
pandas==2.1.4
numpy==1.26.3
prometheus_client==0.19.0
python-telegram-bot==20.7
```

**Deliverables:**
- [ ] All Docker images pinned to specific versions
- [ ] All Python packages pinned
- [ ] Documented upgrade procedure

---

## Implementation Priority Matrix

| Phase | Priority | Effort | Dependencies |
|-------|----------|--------|--------------|
| 1. Security Hardening | HIGH | 1-2 days | None |
| 2. Prometheus Metrics | HIGH | 1-2 days | None |
| 3. Alertmanager Rules | HIGH | 0.5 days | Phase 2 |
| 4. Position Reconciliation | HIGH | 1 day | None |
| 5. Standby VPS | MEDIUM | 1-2 days | Phase 1 |
| 6. Failover Runbook | MEDIUM | 0.5 days | Phase 5 |
| 7. Headless 2FA | MEDIUM | Research | Decision needed |
| 8. Version Pinning | LOW | 0.5 days | None |

---

## Recommended Execution Order

### Week 1: Security & Monitoring
1. Phase 1: Security Hardening
2. Phase 2: Prometheus Metrics
3. Phase 3: Alertmanager Rules

### Week 2: Reliability
4. Phase 4: Position Reconciliation
5. Phase 8: Version Pinning

### Week 3: Resilience
6. Phase 5: Standby VPS & WireGuard
7. Phase 6: Failover Runbook

### Ongoing: Research
8. Phase 7: Headless 2FA Gateway evaluation

---

## Success Criteria

Before going live with real capital:

- [ ] 60-day paper trading completed with <5% order rejection rate
- [ ] Non-root user running all services
- [ ] All secrets segmented and permissions hardened
- [ ] Prometheus metrics covering all trading operations
- [ ] Alertmanager routing critical alerts to Telegram
- [ ] Position reconciliation running on every reconnect
- [ ] Standby VPS ready for manual failover
- [ ] Failover tested at least once
- [ ] All versions pinned
- [ ] Headless 2FA approach decided and documented
