# Claude Code Project Context

This file provides context for Claude Code when working on AbstractFinance.

---

## Implementation Status (Jan 5, 2026 - Phase R Complete)

> **All sleeves are now operational using available instruments.**

### Feature Status

| Feature | Status | Implementation |
|---------|--------|----------------|
| **Core Index RV (20%)** | ✅ WORKING | CSPX long / CS51 short |
| **Sector RV (20%)** | ✅ WORKING | 2 UCITS pairs: IUIT/EXV3 (Tech), IUHC/EXV4 (Healthcare) |
| **Europe Vol (18%)** | ✅ WORKING | VIX calls + FEZ/EUFN puts (US proxies) |
| **Credit Carry (8%)** | ✅ WORKING | LQDE, IHYU, FLOT, ARCC |
| **Money Market (34%)** | ✅ WORKING | Cash + short-dated |
| **Sovereign Overlay** | ✅ STANDBY | Activates on 25%+ drawdown |
| **FX Hedge** | ✅ WORKING | Short EUR positions provide natural hedge |

### Instrument Substitutions (PRIIPs Compliance)

| Original | Substitute | Reason |
|----------|------------|--------|
| VSTOXX calls | VIX calls | EUREX not in paper account |
| SX5E puts | FEZ puts | US-listed Euro STOXX 50 ETF |
| EU Bank puts | EUFN puts | US-listed EU financials |
| XLF, XLI | *Disabled* | No UCITS equivalent |
| XLK → IUIT | Technology pair active | UCITS available |
| XLV → IUHC | Healthcare pair active | UCITS available |

### What Paper Trading Validates

The 60-day burn-in validates:
- Full strategy execution (all sleeves active)
- Order execution reliability
- Position sizing correctness
- Risk scaling behavior
- Gateway auto-recovery
- Options order flow (via US proxies)

---

## 1Password Secrets Management (Secure)

Secrets are managed via 1Password using the **`op run`** pattern. This ensures credentials are:
- Never stored in plaintext on disk
- Never visible to Claude or in logs
- Injected directly into process memory at runtime
- Purged from memory when the process exits

### Architecture

```
.env.template (contains op:// references, NOT actual secrets)
         ↓
op run --env-file=.env.template -- docker compose up
         ↓
Secrets resolved in memory → injected into container environment
         ↓
Secrets purged when container stops
```

### Server Setup (For New Servers)

#### Step 1: Install 1Password CLI

```bash
# Download and install op CLI
curl -sSfLo /tmp/op.zip "https://cache.agilebits.com/dist/1P/op2/pkg/v2.32.0/op_linux_amd64_v2.32.0.zip"
cd /tmp && unzip -o op.zip && mv op /usr/local/bin/ && chmod +x /usr/local/bin/op
op --version
```

#### Step 2: Configure Service Account

```bash
# Get the service account token (stored securely, not shown here)
# Create /etc/profile.d/1password.sh with:
echo 'export OP_SERVICE_ACCOUNT_TOKEN=<token>' > /etc/profile.d/1password.sh
chmod 600 /etc/profile.d/1password.sh

# Verify access
source /etc/profile.d/1password.sh
op vault list  # Should show "Ai" vault
```

#### Step 3: Create .env.template

Create `/srv/abstractfinance/.env.template` with secret references:

```bash
# Example for staging (paper trading)
IBKR_USERNAME=op://Ai/ibkr.staging/username
IBKR_PASSWORD=op://Ai/ibkr.staging/password
IBKR_TOTP_KEY=op://Ai/ibkr.staging.totp-key/password
IBKR_ACCOUNT_ID=DUO775682
IBKR_PORT=4004
TRADING_MODE=Paper Trading

# Example for production (live trading)
IBKR_USERNAME=op://Ai/InteractivebrokersClaude/username
IBKR_PASSWORD=op://Ai/InteractivebrokersClaude/password
IBKR_TOTP_KEY=op://Ai/InteractivebrokersClaude/2fa
IBKR_PORT=4001
TRADING_MODE=Live
```

#### Step 4: Create Docker Compose Wrapper

Create `/srv/abstractfinance/dc`:

```bash
#!/bin/bash
# Docker Compose wrapper that injects secrets from 1Password
set -e
source /etc/profile.d/1password.sh
cd /srv/abstractfinance
exec op run --env-file=.env.template -- docker compose "$@"
```

```bash
chmod +x /srv/abstractfinance/dc
```

#### Step 5: Usage

```bash
# Start services (secrets injected at runtime)
./dc up -d ibgateway trading-engine

# View logs
./dc logs --tail=50 trading-engine

# Stop services
./dc down
```

### Available Secrets in "Ai" Vault

| Item | Field | Description |
|------|-------|-------------|
| `InteractivebrokersClaude` | `username`, `password`, `2fa` | Live IBKR credentials |
| `ibkr.staging` | `username`, `password` | Paper trading credentials |
| `ibkr.staging.totp-key` | `password` | Paper trading TOTP |
| `hetzner-cloud` | `token` | Hetzner Cloud API token |
| `telegram.bot-token` | `credential` | Telegram bot token |
| `telegram.chat-id` | `password` | Telegram chat ID |
| `db.staging.password` | `password` | Database password |
| `grafana.admin.password` | `password` | Grafana admin password |

### Adding New Secrets

1. Add the secret to 1Password in the "Ai" vault
2. Update `.env.template` with the `op://Ai/<item>/<field>` reference
3. Restart services with `./dc up -d`

### Security Notes

- **NEVER** create plaintext `.env` files on servers
- **NEVER** use the old MCP HTTP server (91.99.97.249:8080) - it exposes secrets in responses
- The `./dc` wrapper ensures secrets are only in memory during runtime
- Service account token in `/etc/profile.d/1password.sh` is the only sensitive file

## Vaults

| Vault | Purpose |
|-------|---------|
| `Ai` | All trading infrastructure secrets (IBKR, Telegram, DB, etc.) |

## Servers

| Server | IP | Purpose |
|--------|-----|---------|
| Staging | 94.130.228.55 | Paper trading (CX33) |
| Production | 91.99.116.196 | Live trading (CX43) - NOT YET ACTIVE |
| 1Password MCP | 91.99.97.249 | Secret management server (CX22) |

SSH access: `ssh root@94.130.228.55`

## Key Commands

```bash
# Check trading status (staging)
ssh root@94.130.228.55 "cd /srv/abstractfinance && ./dc ps"

# View logs (staging)
ssh root@94.130.228.55 "cd /srv/abstractfinance && ./dc logs --tail=50 trading-engine"

# Start services with secrets (staging)
ssh root@94.130.228.55 "cd /srv/abstractfinance && ./dc up -d ibgateway trading-engine"

# Check trading status (production)
ssh root@91.99.116.196 "cd /srv/abstractfinance && ./dc ps"
```

## Important Notes

- **NEVER** commit `.env` files or plaintext credentials
- **NEVER** create plaintext `.env` files on servers - use `.env.template` with `op://` references
- All secrets flow from 1Password via `op run` at runtime
- Use the `./dc` wrapper instead of `docker compose` directly

---

## Market Data Implementation Guidance

**STATUS: ALL 6 ISSUES COMPLETED (Dec 17, 2025)**

See `docs/EXECUTION_DEBUGGING_LOG.md` for full details. Summary:

| Issue | Status | Implementation |
|-------|--------|----------------|
| 1. GBP/Pence | ✅ DONE | `GBX_QUOTED_ETFS` whitelist in `data_feeds.py:44-52` |
| 2. Blocking sleep | ✅ DONE | `get_prices_batch()` with single wait in `data_feeds.py:387-480` |
| 3. Silent exceptions | ✅ DONE | Debug logging added to all exception handlers |
| 4. Sequential fetch | ✅ DONE | Batch `_batch_fetch_from_ib_insync()` in `marketdata/live.py:148-237` |
| 5. Circuit breaker | ✅ DONE | `CircuitBreaker` class in `data_feeds.py:81-157` |
| 6. Data metrics | ✅ DONE | `DataQualityMetrics` + `get_metrics()` in `data_feeds.py:25-78, 900-935` |

---

## Recent Fixes (Dec 18, 2025)

### Execution Engine Reliability

17 issues fixed to achieve ~80% fill rate. Key improvements:

| Category | Fixes |
|----------|-------|
| Timezone handling | Market open safety check now uses pytz |
| NAV reconciliation | Uses computed NAV from positions+cash, not NetLiquidation |
| Order management | Cancel/replace pattern instead of modify-in-place |
| Tick compliance | All prices rounded to $0.01 for IBKR |
| Slippage | Increased from 10bps to 25bps for fills |
| Margin | SELLs execute before BUYs to free margin |
| Position sync | Clear+replace internal positions from broker each run |

### Risk Engine Reliability

3-part fix to prevent day-0 deleveraging:

| Component | Description |
|-----------|-------------|
| Vol burn-in | Use 10% prior vol during first 60 days |
| Scaling clamps | Constrain scaling to [0.80, 1.25] range |
| Legacy glidepath | Blend to targets over 10 days |

**Files:** `src/risk_engine.py`, `src/legacy_unwind.py`, `src/scheduler.py`

---

## Recent Fixes (Jan 5, 2026)

### Issue #30: IUKD Double Price Conversion

**Problem:** IUKD orders placed at £9.00 were rejected by IBKR as "too far from market" because the limit price was being sent as 9 pence instead of 900 pence.

**Root Cause:** In `execution_ibkr.py:get_market_data()`, the portfolio fallback prices (which are already in GBP) were being incorrectly converted from pence→GBP, resulting in double conversion:
- Portfolio returns `9.25` (GBP)
- Code applies `from_broker()` → `0.0925` (wrong!)
- Order sent at `0.0925 * 100 = 9.25` pence instead of 925 pence

**Fix:** Track data source (`from_ticker` flag) and only apply pence conversion to real-time ticker data, not portfolio fallback prices.

**File:** `src/execution_ibkr.py:1216-1277`

### Gateway Auto-Recovery System

**Problem:** IB Gateway stuck in authentication loop Jan 1-5, 2026 (4+ days) with no automatic recovery.

**Solution:** Two-layer recovery system:

| Layer | Mechanism | Frequency | File |
|-------|-----------|-----------|------|
| **Autoheal** | Docker container auto-restarts unhealthy containers | Every 30s | `docker-compose.yml` |
| **Watchdog** | Deep API health check + full restart procedure | Every 15 min (cron) | `scripts/gateway_watchdog.sh` |

**Configuration:**
- Gateway has `autoheal=true` label
- Autoheal container monitors health status
- Watchdog tests actual API connectivity (not just port)
- Triggers full restart after 30 min unhealthy
- Sends Telegram alerts on issues

**Cron:** `*/15 * * * * /srv/abstractfinance/scripts/gateway_watchdog.sh`

---

## Security Hardening (Jan 6, 2026)

### Pre-commit Hook for Secret Detection

Installed git hooks to prevent accidental secret commits:

```bash
# Install hooks
./scripts/install-hooks.sh

# Hook detects:
# - Plaintext passwords/tokens
# - AWS access keys
# - Private keys
# - .env files (except .env.template)
```

**Files:**
- `git-hooks/pre-commit` - The hook script
- `scripts/install-hooks.sh` - Installation script

### Scheduler Module Refactoring

Split `scheduler.py` from 2,477 → 2,080 lines:

| New Module | Contents |
|------------|----------|
| `src/scheduler/maintenance.py` | IBKR maintenance window detection |
| `src/scheduler/continuous.py` | ContinuousScheduler class |
| `src/scheduler/__init__.py` | Re-exports for backward compatibility |

DailyScheduler remains in `scheduler.py` (tightly coupled, future migration planned).

### Exception Handler Improvements

Fixed 12 silent `except Exception: pass` handlers with proper logging:

| File | Handlers Fixed |
|------|----------------|
| `execution_ibkr.py` | 3 (contract qualification, order cancel, NAV fetch) |
| `portfolio.py` | 1 (price fetch fallback) |
| `scheduler.py` | 3 (alert sending) |
| `reconnect.py` | 4 (connection callbacks, heartbeat) |

### Security Audit Findings (Resolved)

| Finding | Status | Action |
|---------|--------|--------|
| Plaintext `.env` on staging | ✅ FIXED | Deleted file |
| No pre-commit secret detection | ✅ FIXED | Hook installed |
| Silent exception handlers | ✅ FIXED | Added logging |
| Example passwords in docs | ✅ FIXED | Replaced with placeholders |

---

## Paper Trading Burn-In Status

**Inception:** December 18, 2025

### Current Status (Jan 5, 2026)

| Metric | Value |
|--------|-------|
| NAV | $278,108 |
| Initial Capital | $238,097 |
| Total P&L | +$40,011 (+16.8%) |
| Daily Return | -0.36% |
| Days Active | 17 |
| Burn-in Progress | 28% (17/60 days) |
| Max Drawdown | -0.72% |

### Data Storage

Burn-in data is stored in **files only** (no database persistence implemented):

| File | Contents |
|------|----------|
| `state/portfolio_state.json` | Current positions, NAV, P&L |
| `state/returns_history.csv` | Daily returns for vol calculation |
| `state/portfolio_init.json` | Initial portfolio snapshot |

**Note:** Database schema exists (`scripts/init-db.sql`) but persistence layer not implemented.

### Positions

| Instrument | Quantity | Market Value | Sleeve |
|------------|----------|--------------|--------|
| us_index_etf (CSPX) | 38 | $28,004 | core_index_rv |
| bdc_arcc (ARCC) | 69 | $1,412 | core_index_rv |
| loans_bkln (FLOT) | 617 | $3,111 | core_index_rv |
| ig_lqd (LQDE) | 60 | $6,179 | core_index_rv |
| hy_hyg (IHYU) | 40 | $3,847 | core_index_rv |
| eu_index_etf (CS51) | -22 | -€4,519 | core_index_rv |
| financials_eufn (EXV1) | -246 | -€8,805 | core_index_rv |
| value_ewu (IUKD) | -179 | -£1,657 | core_index_rv |

### Known Issues

1. **Returns data gap (Jan 1-4)** - FIXED: Interpolated 7 trading days based on NAV delta
2. **Outlier return Dec 13** - FIXED: Cleaned returns_history.csv (was bogus test data)
3. **No database persistence** - All data in files only (by design for now)

---

## Original Implementation Details (Reference)

### Issue 1: GBP/Pence Heuristic (HIGH PRIORITY)

**Location:** `src/data_feeds.py:307-313`

**Problem:** Current heuristic `if price > 100` assumes ETFs never trade above £100. Some do.

**Fix:** Use IBKR contract details to determine currency unit. The `Contract` object from IBKR has enough info.

```python
# In _get_ib_contract() or after qualifyContracts():
# Check contract.secType and contract.currency
# LSE stocks: IBKR returns prices in GBX (pence) for secType='STK'
# LSE ETFs: Usually return in GBP but verify via contract details

def _needs_pence_conversion(self, contract, spec: InstrumentSpec) -> bool:
    """Determine if IBKR price needs pence->pounds conversion."""
    if spec.currency != 'GBP' or spec.exchange != 'LSE':
        return False
    # IBKR ETFs on LSE typically quote in GBP already
    # Individual stocks quote in GBX (pence)
    # Check primaryExchange or use a whitelist of known ETF symbols
    etf_symbols = {'CSPX', 'CNDX', 'IUIT', 'WTCH', 'SEMI', 'IUHC', 'SBIO',
                   'BTEK', 'IUQA', 'IUMO', 'SMEA', 'IUKD', 'LQDE', 'IHYU',
                   'HYLD', 'FLOT', 'FLOA', 'IHYG'}
    # ETFs don't need conversion; individual stocks do
    return spec.symbol not in etf_symbols
```

**Test:** Verify with ARCC (US stock), CSPX (LSE ETF), and a UK individual stock if available.

---

### Issue 2: Blocking Sleep (MEDIUM PRIORITY)

**Location:** `src/data_feeds.py:299-300`

**Problem:** `self.ib.sleep(1)` blocks 1 second per instrument. 20 instruments = 20 seconds.

**Fix:** Use streaming market data subscriptions instead of request-wait-cancel pattern.

```python
# Option A: Pre-subscribe to instruments at startup
def subscribe_instruments(self, instrument_ids: List[str]) -> None:
    """Subscribe to streaming data for instruments."""
    self._tickers: Dict[str, Ticker] = {}
    for inst_id in instrument_ids:
        contract = self._get_ib_contract(inst_id)
        if contract:
            self.ib.qualifyContracts(contract)
            ticker = self.ib.reqMktData(contract, '', False, False)
            self._tickers[inst_id] = ticker
    self.ib.sleep(2)  # Single wait for all subscriptions

def get_last_price(self, instrument_id: str) -> float:
    """Get price from pre-subscribed ticker."""
    ticker = self._tickers.get(instrument_id)
    if ticker and (ticker.last or ticker.close):
        return ticker.last or ticker.close
    # Fallback to on-demand fetch only if not subscribed
    ...

# Option B: Batch fetch with single sleep
def get_prices_batch(self, instrument_ids: List[str]) -> Dict[str, float]:
    """Fetch multiple prices with single wait."""
    tickers = {}
    for inst_id in instrument_ids:
        contract = self._get_ib_contract(inst_id)
        if contract:
            self.ib.qualifyContracts(contract)
            tickers[inst_id] = self.ib.reqMktData(contract, '', False, False)

    self.ib.sleep(2)  # Single wait for all

    results = {}
    for inst_id, ticker in tickers.items():
        if ticker.last and ticker.last > 0:
            results[inst_id] = ticker.last
        elif ticker.close and ticker.close > 0:
            results[inst_id] = ticker.close
        # Cancel subscription
        self.ib.cancelMktData(ticker.contract)
    return results
```

**Where to call:** `scheduler.py` should call `subscribe_instruments()` at startup with all portfolio instruments.

---

### Issue 3: Silent Exception Swallowing (HIGH PRIORITY)

**Location:** `src/data_feeds.py:314-315, 330-331`

**Problem:** `except Exception: pass` loses all debugging info.

**Fix:** Log at debug level, include instrument context.

```python
# Replace:
except Exception:
    pass

# With:
except Exception as e:
    logger.debug(f"IBKR price fetch failed for {instrument_id}: {e}")
    # Continue to fallback
```

**Apply to all silent exception handlers in:**
- `data_feeds.py:314-315` (IBKR fetch)
- `data_feeds.py:330-331` (Yahoo fallback)
- `data_feeds.py:417-418` (history fetch)
- `fx_rates.py` (similar patterns)

---

### Issue 4: Sequential Multi-Instrument Fetch (MEDIUM PRIORITY)

**Location:** `src/marketdata/live.py:127-130`

**Problem:** `get_snapshots()` fetches sequentially in a loop.

**Fix:** Use asyncio or batch IBKR requests.

```python
def get_snapshots(
    self,
    instrument_ids: List[str],
    require_quotes: bool = True,
) -> Dict[str, Optional[MarketDataSnapshot]]:
    """Get live snapshots for multiple instruments - batched."""
    # Request all at once
    pending = {}
    for inst_id in instrument_ids:
        contract = self._get_contract(inst_id)
        if contract:
            ticker = self.ib_client.ib.reqMktData(contract, '', False, False)
            pending[inst_id] = (contract, ticker)

    # Single wait
    self.ib_client.ib.sleep(self.timeout_ms / 1000.0)

    # Collect results
    results = {}
    for inst_id, (contract, ticker) in pending.items():
        snapshot = self._ticker_to_snapshot(inst_id, ticker)
        if snapshot and self._validate_quality(snapshot, require_quotes):
            results[inst_id] = snapshot
        else:
            results[inst_id] = None
        self.ib_client.ib.cancelMktData(contract)

    return results
```

---

### Issue 5: Circuit Breaker (LOW PRIORITY)

**Location:** New utility, used in `data_feeds.py` and `marketdata/live.py`

**Problem:** If IBKR is flaky, repeated failures hammer the connection.

**Fix:** Add simple circuit breaker pattern.

```python
# src/utils/circuit_breaker.py
from dataclasses import dataclass
from datetime import datetime, timedelta

@dataclass
class CircuitBreaker:
    """Simple circuit breaker for external services."""
    failure_threshold: int = 5
    reset_timeout_seconds: int = 60

    _failure_count: int = 0
    _last_failure: datetime = None
    _state: str = "closed"  # closed, open, half-open

    def record_success(self) -> None:
        self._failure_count = 0
        self._state = "closed"

    def record_failure(self) -> None:
        self._failure_count += 1
        self._last_failure = datetime.now()
        if self._failure_count >= self.failure_threshold:
            self._state = "open"
            logger.warning(f"Circuit breaker opened after {self._failure_count} failures")

    def can_execute(self) -> bool:
        if self._state == "closed":
            return True
        if self._state == "open":
            if datetime.now() - self._last_failure > timedelta(seconds=self.reset_timeout_seconds):
                self._state = "half-open"
                return True
            return False
        return True  # half-open: allow one attempt

# Usage in DataFeed:
class DataFeed:
    def __init__(self, ...):
        self._ibkr_circuit = CircuitBreaker(failure_threshold=5, reset_timeout_seconds=60)

    def get_last_price(self, instrument_id: str) -> float:
        if self._ibkr_circuit.can_execute() and self.ib and self.ib.isConnected():
            try:
                price = self._fetch_from_ibkr(instrument_id)
                if price:
                    self._ibkr_circuit.record_success()
                    return price
            except Exception as e:
                self._ibkr_circuit.record_failure()
                logger.debug(f"IBKR fetch failed: {e}")
        # Continue to fallback...
```

---

### Issue 6: Data Quality Metrics (LOW PRIORITY)

**Location:** New module `src/metrics/data_quality.py`

**Problem:** No visibility into data fetch performance.

**Fix:** Add simple counters, expose via healthcheck.

```python
# src/metrics/data_quality.py
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict

@dataclass
class DataQualityMetrics:
    """Track market data quality metrics."""
    ibkr_requests: int = 0
    ibkr_successes: int = 0
    ibkr_failures: int = 0
    stale_data_rejections: int = 0
    spread_rejections: int = 0

    latencies_ms: list = field(default_factory=list)
    _last_reset: datetime = field(default_factory=datetime.now)

    def record_request(self, source: str, success: bool, latency_ms: float = None):
        if source == "ibkr":
            self.ibkr_requests += 1
            if success:
                self.ibkr_successes += 1
            else:
                self.ibkr_failures += 1
        if latency_ms:
            self.latencies_ms.append(latency_ms)
            # Keep last 1000 only
            if len(self.latencies_ms) > 1000:
                self.latencies_ms = self.latencies_ms[-1000:]

    def to_dict(self) -> Dict:
        avg_latency = sum(self.latencies_ms) / len(self.latencies_ms) if self.latencies_ms else 0
        return {
            "ibkr_success_rate": self.ibkr_successes / max(self.ibkr_requests, 1),
            "ibkr_requests": self.ibkr_requests,
            "avg_latency_ms": avg_latency,
            "stale_rejections": self.stale_data_rejections,
            "spread_rejections": self.spread_rejections,
        }

# Add to healthcheck.py:
# Include data_quality_metrics.to_dict() in health response
```

---

### Implementation Order

1. **Issue 3** (silent exceptions) - Quick fix, immediate debugging benefit
2. **Issue 1** (GBP/pence) - High risk of wrong prices
3. **Issue 2** (blocking sleep) - Performance improvement
4. **Issue 4** (batch fetch) - Performance improvement
5. **Issue 5** (circuit breaker) - Resilience
6. **Issue 6** (metrics) - Observability

---

## Risk Parity + Sovereign Crisis Overlay (Jan 5, 2026)

### Overview

Added three-phase strategy enhancement for improved risk management:

1. **Phase 1: Risk Parity Allocator** - Inverse-vol weighting across sleeves
2. **Phase 2: Sovereign Crisis Overlay** - Put spreads on periphery exposure
3. **Phase 3: Integration** - Merges with existing strategy, applies constraints

### Files Added

| File | Description |
|------|-------------|
| `src/risk_parity.py` | Inverse-vol weight allocation with 12% vol target |
| `src/sovereign_overlay.py` | Put spreads on EWI/EWQ/FXE/EUFN (US-listed proxies) |
| `src/strategy_integration.py` | Combines risk parity + overlay with base strategy |

### Configuration (settings.yaml)

```yaml
# Risk Parity settings
risk_parity:
  enabled: true
  target_vol_annual: 0.12    # 12% portfolio vol target
  rebalance_frequency: monthly
  drift_threshold: 0.05      # 5% drift triggers rebalance

# Sovereign Overlay settings
sovereign_overlay:
  enabled: true
  annual_budget_pct: 0.0035  # 35bps budget
  country_allocations:
    italy: 0.35              # EWI
    france: 0.25             # EWQ
    eur_usd: 0.20            # FXE
    eu_banks: 0.20           # EUFN

# Integration settings
strategy_integration:
  use_risk_parity: true
  risk_parity_weight: 0.7    # 70% RP, 30% base
  use_sovereign_overlay: true
  max_gross_leverage: 2.0
```

### IBKR Options Access (Paper Account)

EUREX instruments NOT available. Using US-listed proxies:

| Proxy | Symbol | Options | Expirations |
|-------|--------|---------|-------------|
| Italy | EWI | Available | 4 dates |
| France | EWQ | Available | 4 dates |
| EUR/USD | FXE | Available | 4 dates |
| EU Banks | EUFN | Available | 4 dates |
| Germany | EWG | Available | 9 dates |
| SPY | SPY | Available | 32 dates |

### Usage

```python
from src.strategy_integration import create_integrated_strategy

# Create integrated strategy
strategy = create_integrated_strategy(
    settings=settings,
    instruments_config=instruments,
    risk_engine=risk_engine
)

# Compute strategy output
output = strategy.compute_strategy(
    portfolio=portfolio_state,
    data_feed=data_feed,
    risk_decision=risk_decision
)

# Access components
print(output.risk_parity_weights.to_dict())
print(output.sovereign_orders)
print(output.all_orders)
```

### Key Features

- **Inverse-Vol Weighting**: Lower-vol sleeves get higher allocation
- **Vol Targeting**: Scales to achieve 12% annual portfolio vol
- **Stress Detection**: Monitors ETF drawdowns as sovereign stress proxy
- **Put Spreads**: Cost-efficient protection using put spreads
- **Budget Control**: 35bps annual budget with monthly allocation
- **Constraint Enforcement**: Max leverage, single-country limits

---

## v2.4 Strategy Improvements (Phase S) - Jan 5, 2026

### Overview

Comprehensive strategy enhancement with regime-aware allocation, improved hedge structure, and better reporting.

### Critical Design Principle

> **Risk parity (inverse-vol weighting) is applied only to return-seeking sleeves (Core Index RV, Sector RV, Credit Carry). Insurance sleeves (Europe Vol, Money Market) are intentionally exempt to preserve convex crisis behavior.**

This is fundamental: insurance sleeves must maintain their hedge effectiveness regardless of their volatility profile.

### S.1: Regime-Aware Risk Parity

**File:** `src/risk_parity.py`

Dynamic allocation between BASE (return-seeking) and SAFE_HAVEN (insurance) sleeves:

| Regime | BASE Weight | SAFE_HAVEN Weight | Trigger |
|--------|-------------|-------------------|---------|
| NORMAL | 85% | 15% | VIX < 20 |
| ELEVATED | 65% | 35% | VIX 20-30 |
| CRISIS | 35% | 65% | VIX > 30 |

Key additions:
- `SleeveType` enum: BASE vs SAFE_HAVEN classification
- `Regime` enum: NORMAL, ELEVATED, CRISIS
- `RegimeBlendState`: EMA smoothing (α=0.15) for smooth transitions
- Insurance sleeves use FIXED weights (60/40) instead of inverse-vol

```python
# Sleeve classification
SleeveType.BASE = [CORE_INDEX_RV, SECTOR_RV, CREDIT_CARRY]
SleeveType.SAFE_HAVEN = [EUROPE_VOL_CONVEX, MONEY_MARKET]
```

### S.2: Hedge Ladder + Two Sub-Buckets

**File:** `src/hedge_ladder.py`

6-leg put spread structure across two buckets:

| Bucket | Allocation | Purpose | Legs |
|--------|------------|---------|------|
| CRASH_CONVEXITY | 40% | Deep OTM puts for tail events | 30/60/90 DTE |
| CRISIS_MONETIZERS | 60% | Closer-to-money puts for faster payoff | 30/60/90 DTE |

Features:
- VIX spike detection for roll timing
- Staggered expiries reduce gamma exposure
- 40bps annual budget with monthly allocation
- Automatic roll at 50% time decay or VIX spike >5pts

```yaml
# config/settings.yaml
hedge_ladder:
  annual_budget_pct: 0.004  # 40bps
  crash_convexity:
    allocation_pct: 0.40
    strike_pct_otm: 0.15    # 15% OTM
  crisis_monetizers:
    allocation_pct: 0.60
    strike_pct_otm: 0.08    # 8% OTM
```

### S.3: Sovereign Rates Fragmentation Overlay

**File:** `src/sovereign_overlay.py`

Simplified to focus on Bund-BTP spread as primary signal:

| Metric | Trigger | Action |
|--------|---------|--------|
| Bund-BTP spread | >200bps | Activate periphery puts |
| 10-day change | >50bps | Increase allocation |

```yaml
sovereign_overlay:
  mode: rates_fragmentation  # New mode (vs legacy)
  rates_config:
    bund_symbol: FGBL
    btp_symbol: FBTP
    spread_trigger_bps: 200
```

### S.4: Defensive Credit Sleeve (0-5%)

**File:** `config/settings.yaml`

Credit sleeve reduced and regime-gated:

| Regime | Max Allocation | Allowed Instruments |
|--------|----------------|---------------------|
| NORMAL | 5% | LQDE (IG), FLOT (floating) |
| ELEVATED | 2% | FLOT only |
| CRISIS | 0% | None |

```yaml
credit_sleeve:
  max_allocation_pct: 0.05
  regime_caps:
    normal: 0.05
    elevated: 0.02
    crisis: 0.0
  allowed_instruments:
    - LQDE  # IG corporate
    - FLOT  # Floating rate
```

### S.5: Attribution & Reporting

**File:** `src/attribution.py`

Daily sleeve-level P&L attribution and factor exposure:

| Component | Description |
|-----------|-------------|
| SleeveAttribution | P&L, gross/net exposure per sleeve |
| FactorExposure | Equity beta, duration, credit spread, FX |
| HedgeEffectiveness | Vol hedge offset ratio vs core drawdown |
| AttributionReport | Telegram-formatted daily summary |

```python
from src.attribution import create_attribution_engine

engine = create_attribution_engine()
report = engine.compute_attribution(portfolio, previous_nav)
print(report.to_telegram_format())
```

### Files Added/Modified

| File | Change |
|------|--------|
| `src/risk_parity.py` | Added regime-aware blending, sleeve types |
| `src/hedge_ladder.py` | NEW - 6-leg hedge structure |
| `src/sovereign_overlay.py` | Added rates fragmentation mode |
| `src/attribution.py` | NEW - Daily attribution engine |
| `config/settings.yaml` | Added hedge_ladder, credit_sleeve configs |
| `docs/INVESTMENT_STRATEGY.md` | Added design principle note |
| `docs/ROADMAP.md` | Added Phase S specification |
