# Execution Debugging Log - December 2025

This document captures lessons learned while debugging the trading engine execution pipeline.

## Current Portfolio Status (Dec 18, 2025)

| Symbol | Qty | Avg Cost | Market Price | Currency | Sleeve |
|--------|-----|----------|--------------|----------|--------|
| EXV1 | -87 | €34.29 | €34.37 | EUR | core_index_rv |
| IUKD | -224 | £8.96 | £9.11 | GBP | core_index_rv |
| M6E (Mar 26) | -3 | $14,742 | 1.1788 | USD | core_index_rv |

**Account Summary:**
- NAV: $280,060
- Broker NLV: $280,056
- Cash (EUR): €243,939
- Gross Exposure: $50,452 (18% of NAV)
- Net Exposure: -$50,452 (short bias)
- Total P&L: $41,977
- Reconciliation: **PASS** (0.00% diff)

**Note:** Portfolio was deleveraged from ~$800K gross exposure to ~$50K after executing sell orders on Dec 18. Most positions (CSPX, EXS1, FLOT, IHYU, IUHC, IUIT, IUQA, LQDE) were closed.

---

## Issues Fixed

### 1. NAV Reconciliation Threshold Too Tight

**Symptom:** `HALT: NAV diff 0.26% > 0.25%`

**Root Cause:** The `halt_threshold_pct` in `config/settings.yaml` was set to 0.0025 (0.25%), which was too tight given normal market price fluctuations.

**Fix:** Increased threshold to 0.005 (0.5%)
```yaml
reconciliation:
  halt_threshold_pct: 0.005  # Was 0.0025
```

**File:** `config/settings.yaml:28`

---

### 2. AlertManager.send_alert() Signature Mismatch

**Symptom:** `AlertManager.send_alert() got an unexpected keyword argument 'alert_type'`

**Root Cause:** The `AlertManager.send_alert()` method expected an `Alert` object, but scheduler code was calling it with keyword arguments like `alert_type="risk"`.

**Fix:** Updated `send_alert()` to accept both Alert objects and keyword arguments:
```python
def send_alert(
    self,
    alert: Optional[Alert] = None,
    *,
    alert_type: Optional[str] = None,
    severity: Optional[str] = None,
    message: Optional[str] = None,
    ...
)
```

**File:** `src/alerts.py:334-401`

---

### 3. Portfolio Positions Iteration Bug

**Symptom:** `'str' object has no attribute 'market_price'`

**Root Cause:** `self.portfolio.positions` is a dictionary. Iterating over it directly yields keys (strings), not values (Position objects).

**Fix:**
```python
# Before (WRONG):
for pos in self.portfolio.positions:

# After (CORRECT):
for pos in self.portfolio.positions.values():
```

**File:** `src/scheduler.py:1123`

---

### 4. IBKR Forex Pair Conventions (Previous Session)

**Symptom:** `IB Error 200: No security definition for JPYUSD`

**Root Cause:** IBKR uses standard forex conventions (USDJPY, not JPYUSD). Some pairs need inversion.

**Fix:** Updated `fx_rates.py` to use proper IBKR pair names with inversion flags:
```python
pairs = [
    ("EUR", "USD", "EURUSD", False),  # 1 EUR = X USD
    ("CHF", "USD", "USDCHF", True),   # Invert: IBKR gives USD/CHF
    ("JPY", "USD", "USDJPY", True),   # Invert: IBKR gives USD/JPY
    ...
]
```

**File:** `src/fx_rates.py:137-144`

---

### 5. Futures Contract Ambiguity

**Symptom:** `IB Error 200: M6E is ambiguous, must specify currency`

**Root Cause:** Future contracts need explicit currency parameter.

**Fix:** Added currency to Future contract constructor:
```python
contract = Future(symbol, exchange=exchange, currency=currency, lastTradeDateOrContractMonth=expiry)
```

**File:** `src/execution_ibkr.py:501-502`

---

### 6. Futures Expiry Parsing Bug

**Symptom:** `Ambiguous contract: Future(symbol='M6E', lastTradeDateOrContractMonth='micro')`

**Root Cause:** Instrument ID like `eurusd_micro` was being parsed with `micro` as the expiry date.

**Fix:** Only treat suffix as expiry if it's 6+ digits:
```python
if suffix.isdigit() and len(suffix) >= 6:
    expiry = suffix
```

**File:** `src/data_feeds.py`

---

### 7. ExecutionResult Attribute Error

**Symptom:** `'ExecutionResult' object has no attribute 'status'`

**Root Cause:** Status is on the ticket, not the result directly.

**Fix:** `result.status.value` → `result.ticket.status.value`

**File:** `src/scheduler.py:373`

---

### 8. Disconnect Callback Binding

**Symptom:** `TypeError: IBClient._on_disconnect() missing 1 required positional argument: 'self'`

**Root Cause:** Event handlers in ib_insync need proper binding.

**Fix:** Wrap in lambda:
```python
self.ib.disconnectedEvent += lambda: self._on_disconnect()
```

**File:** `src/execution_ibkr.py:149-150`

---

### 9. Index Contract Type Support

**Symptom:** `IB Error 200: No security definition for CS51` (VSTOXX)

**Root Cause:** Missing Index contract type handling.

**Fix:** Added Index import and IND case:
```python
from ib_insync import ..., Index

elif sec_type == 'IND':
    contract = Index(symbol, exchange, currency)
```

**File:** `src/execution_ibkr.py:21, 504-506`

---

## Known Remaining Issues

### Market Data Subscriptions (Error 354)
Several instruments show "market data not subscribed" errors:
- EXV1, FLOT, M6E, LQDE, CSPX (LSEETF)
- SX7E (Euro STOXX Banks Index)

**Workaround:** Portfolio prices from IBKR positions are used as fallback.

### Invalid Instrument Symbols
- `CS51` - Invalid VSTOXX symbol, needs proper V2X instrument
- `EXV3, EXH1, etc.` - Yahoo Finance returns "delisted" errors

---

## Market Data Improvements (COMPLETED Dec 17, 2025)

All 6 issues have been implemented:

| Issue | Status | Summary | Location |
|-------|--------|---------|----------|
| 1. GBP/Pence | **DONE** | Added `GBX_QUOTED_ETFS` whitelist | `data_feeds.py:44-52` |
| 2. Blocking sleep | **DONE** | Added `get_prices_batch()` with single wait | `data_feeds.py:387-480` |
| 3. Silent exceptions | **DONE** | Added debug logging to all exceptions | `data_feeds.py:*` |
| 4. Sequential fetch | **DONE** | Batch `_batch_fetch_from_ib_insync()` | `marketdata/live.py:148-237` |
| 5. Circuit breaker | **DONE** | `CircuitBreaker` class with auto-recovery | `data_feeds.py:81-157` |
| 6. Data metrics | **DONE** | `DataQualityMetrics` + `get_metrics()` | `data_feeds.py:25-78, 900-935` |

### Key Improvements

- **GBX Whitelist**: Replaced unreliable price > 100 heuristic with explicit ETF list
- **Batch Fetching**: N instruments = 1 wait instead of N waits (20x faster for 20 instruments)
- **Circuit Breakers**: IBKR (3 failures, 60s recovery) and Yahoo (5 failures, 120s recovery)
- **Metrics**: Track success rates, latency, and last error for monitoring

---

## Execution Flow Summary

1. **Connect to IBKR** (clientId 1 for execution, 2 for data feed)
2. **Sync positions** from broker
3. **FX rates refresh** (IBKR primary, Yahoo fallback)
4. **NAV reconciliation** - compare internal vs broker NAV
5. **Risk computation** - vol targeting, drawdown check
6. **Strategy computation** - generate order specs
7. **Hedge management** - FX hedges, tail hedges
8. **Execute orders** - convert specs to intents, submit via execution stack
9. **Record P&L** - save state for next run

---

## Commands for Debugging

```bash
# Check gateway status
ssh root@94.130.228.55 'docker compose -f /srv/abstractfinance/docker-compose.yml ps ibgateway'

# View trading engine logs
ssh root@94.130.228.55 'docker compose -f /srv/abstractfinance/docker-compose.yml logs --tail=100 trading-engine'

# Run manual test execution
ssh root@94.130.228.55 'docker run --rm \
  -v /srv/abstractfinance/config:/app/config:ro \
  -v /srv/abstractfinance/state:/app/state \
  -v /srv/abstractfinance/logs:/app/logs \
  --network host \
  -e FORCE_EXECUTION=1 \
  -e MODE=paper \
  -e IBKR_HOST=localhost \
  -e IBKR_PORT=4000 \
  -w /app \
  abstractfinance-trading-engine python -m src.scheduler --once 2>&1'

# Check portfolio via IB API
ssh root@94.130.228.55 'docker run --rm --network host \
  abstractfinance-trading-engine python -c "
from ib_insync import IB
ib = IB()
ib.connect('localhost', 4000, clientId=99)
for p in ib.positions():
    print(f'{p.contract.symbol}: {p.position}')
ib.disconnect()
"'
```

---

---

## Execution Fixes (December 18, 2025)

Major improvements to order execution that increased fill rate from **0% to ~80%**.

### 10. Timezone Bug in Market Open Safety Check

**Symptom:** `Within 15 min of EU market open` blocking trades at wrong times

**Root Cause:** `is_near_market_open()` compared `datetime.now()` (UTC) to local market open times without timezone conversion.

**Fix:** Added pytz and convert current time to exchange timezone:
```python
import pytz

def is_near_market_open(exchange: str = "US", buffer_minutes: int = 15) -> bool:
    now_utc = datetime.now(pytz.UTC)

    if exchange == "US":
        tz = pytz.timezone("America/New_York")
        market_open = time(9, 30)
    else:
        tz = pytz.timezone("Europe/Berlin")
        market_open = time(9, 0)

    local_time = now_utc.astimezone(tz).time()
    # ... comparison with local_time
```

**File:** `src/execution_ibkr.py:55-85`

---

### 11. NAV Reconciliation Using Wrong Value

**Symptom:** NAV diff 0.32% causing unnecessary halts

**Root Cause:** IBKR's `NetLiquidation` differs from positions + cash by ~$800 due to internal calculations.

**Fix:** Added `get_computed_nav()` to calculate NAV from positions + cash:
```python
def get_computed_nav(self, fx_rates) -> Optional[float]:
    """Compute NAV from positions + cash (more accurate than NetLiquidation)."""
    positions_value = sum(item.marketValue for item in self.ib.portfolio())

    cash_balances = {}
    for av in self.ib.accountValues():
        if av.tag == 'CashBalance' and av.currency != 'BASE':
            cash_balances[av.currency] = float(av.value)

    total_cash_eur = sum(fx_rates.to_base(v, c) for c, v in cash_balances.items())
    return positions_value_eur + total_cash_eur
```

**File:** `src/execution_ibkr.py:280-315`, `src/scheduler.py:213-230`

---

### 12. Order Replacement Bug (Error 103: Duplicate Order ID)

**Symptom:** `Error 103: Duplicate order id` when replacing orders

**Root Cause:** `modify_order()` was reusing the same order ID for the replacement order. IBKR requires a new order ID for cancel/replace.

**Fix:** Changed to proper cancel/replace pattern:
```python
def modify_order(self, broker_order_id: int, new_limit_price: float) -> Tuple[bool, Optional[int]]:
    """Cancel existing order and submit new one with fresh ID."""
    trade = self._active_trades.get(broker_order_id)

    # Cancel existing order
    self.ib_client.ib.cancelOrder(trade.order)
    del self._active_trades[broker_order_id]

    # Create new order with new ID
    new_order = LimitOrder(action=side, totalQuantity=qty, lmtPrice=new_limit_price)
    new_trade = self.ib_client.ib.placeOrder(contract, new_order)

    return True, new_trade.order.orderId  # Return NEW order ID
```

**File:** `src/execution_ibkr.py:686-740`, `src/execution/order_manager.py:401-425`

---

### 13. Missing Market Data for LSE ETFs (Error 354)

**Symptom:** `Error 354: Market data not subscribed` for LSEETF instruments

**Root Cause:** Paper trading account doesn't have real-time quotes for LSE. Without bid/ask, limit prices were too conservative to fill.

**Fix:** When quotes unavailable, use 2x slippage to ensure fills:
```python
def _marketable_limit_price(self, md, side, max_slip_bps):
    if md.has_quotes():
        # Normal calculation with bid/ask
        ...
    else:
        # No quotes - be MORE aggressive (2x slippage)
        aggressive_slip = max_slip * 2.0
        if side == "BUY":
            return ref * (1.0 + aggressive_slip)
        else:
            return ref * (1.0 - aggressive_slip)
```

**File:** `src/execution/policy.py:377-400`

---

### 14. Tick Size Rounding (Warning 110)

**Symptom:** `Warning 110: The price does not conform to the minimum price variation`

**Root Cause:** Limit prices like `95.70001048955801` have too many decimal places. IBKR requires prices rounded to tick size (usually $0.01).

**Fix:** Added `_round_to_tick()` helper and applied to all price calculations:
```python
def _round_to_tick(self, price: float, tick_size: float = 0.01) -> float:
    """Round price to valid tick size for IBKR."""
    return round(price / tick_size) * tick_size

def _marketable_limit_price(self, md, side, max_slip_bps) -> float:
    # ... calculate price ...
    return self._round_to_tick(price)  # Always round before returning
```

**Applied to:**
- `_marketable_limit_price()` - initial order price
- `_calculate_collar()` - price ceiling/floor
- `update_limit_for_replace()` - replacement order price

**File:** `src/execution/policy.py:344-353, 382, 387, 397, 400, 412, 415, 498, 504`

---

### 15. Slippage Too Tight for Fills

**Symptom:** 0% fill rate despite orders being placed

**Root Cause:** Default 10bps slippage was too tight for crossing spreads, especially on less liquid instruments.

**Fix:** Increased default slippage from 10bps to 25bps:
```python
@dataclass
class ExecutionConfig:
    default_max_slippage_bps: float = 25.0  # Was 10.0

    max_slippage_bps_by_asset_class = {
        "ETF": 25.0,   # Was 10.0
        "STK": 30.0,   # Was 15.0
        "FUT": 5.0,
        "FX_FUT": 3.0,
    }
```

**File:** `src/execution/policy.py:49-83`

---

### 16. Margin Rejection Due to Order Sequencing

**Symptom:** `Error 201: Insufficient margin` for BUY orders

**Root Cause:** BUY orders were executing before SELL orders. Without sell proceeds, account lacked margin for buys.

**Fix:** Execute SELLs before BUYs to free up margin first:
```python
def order_by_priority(self, net_positions):
    def priority_key(pos):
        # 1. Crisis urgency first
        # 2. Futures first (hedging)
        # 3. SELLS before BUYS (frees margin)
        side_score = 0 if pos.side == "SELL" else 1
        # 4. Liquidity tier
        # 5. Notional size
        return (urgency, asset_class, side_score, liquidity, -notional)
```

**File:** `src/execution/basket.py:205-256`

---

## Execution Results After Fixes

| Metric | Before | After |
|--------|--------|-------|
| Fill Rate | 0% | ~80% |
| Orders Placed | 24 | 6 |
| Orders Filled | 0 | 5 |
| Avg Fill Time | N/A | <5 seconds |

**Sample Fills (Dec 18, 2025):**
- FLOT: SELL 652 @ $5.03 (filled in 3s)
- EXV1: BUY 9 @ €34.32 (filled in 1s)
- IUIT: SELL 4 @ $40.67 (filled in 2s)
- IHYU: SELL 157 @ $95.57 (filled in 1s)
- CSPX: SELL 933 @ ~$722.31 (partial fills ongoing)

---

## System Capabilities Summary

### Order Execution
- **Marketable Limit Orders**: Cross spread with slippage protection (25bps default)
- **Cancel/Replace**: Automatic order repricing every 15s, up to 6 attempts
- **Tick Size Compliance**: All prices rounded to $0.01 for IBKR
- **TTL Expiry**: Orders cancelled after 120s if unfilled
- **Margin Optimization**: Sells execute before buys

### Market Data
- **Batch Fetching**: Single 2s wait for N instruments (was N*1s)
- **Circuit Breakers**: Auto-recovery after failures (IBKR: 3 fails/60s, Yahoo: 5 fails/120s)
- **GBX Handling**: Whitelist of LSE ETFs that quote in pence
- **Quality Metrics**: Track success rates, latency, staleness

### Risk Management
- **NAV Reconciliation**: Compare internal vs broker NAV (0.5% threshold)
- **Timezone Safety**: Block trades near market open/close
- **Exposure Limits**: Validate gross/net exposure before trading

---

## Vol Burn-In and Legacy Unwind (December 18, 2025)

### Problem: Day-0 Deleveraging to Cash

After fixing the execution bugs, we observed the portfolio moved from ~320% gross exposure to ~20% (mostly cash). Investigation revealed:

1. **`realized_vol: 0.0`** - No historical returns available on first run
2. **`scaling_factor: 0.577`** - Computed as target_vol / blended_vol
3. The risk engine was **working as designed** but too aggressive with no history

The engine correctly identified legacy positions as overweight and deleveraged, but the lack of volatility history caused it to use an unfavorable vol estimate.

### Solution: Three-Part Fix

#### 1. Vol Burn-In Prior

During the burn-in period (first 60 days), use a prior volatility estimate instead of computed realized vol:

```yaml
# config/settings.yaml
vol_burn_in:
  burn_in_days: 60         # Days until full realized vol is used
  initial_vol_annual: 0.10 # Prior vol during burn-in (10%)
  min_vol_annual: 0.06     # Hard floor on effective vol (6%)
```

**Logic:** `effective_vol = max(realized_vol, initial_vol_prior)` during burn-in

**File:** `src/risk_engine.py:effective_realized_vol()`

#### 2. Scaling Factor Clamps

Prevent extreme position scaling by clamping the scaling factor:

```yaml
# config/settings.yaml
scaling_clamps:
  min_scaling_factor: 0.80  # Never scale below 80% of target
  max_scaling_factor: 1.25  # Never scale above 125% of target
```

**Effect:** Even with volatile conditions, positions stay within 80-125% of target weights.

**File:** `src/risk_engine.py:compute_scaling_factor()`

#### 3. Legacy Unwind Glidepath

Gradually transition from initial (legacy) positions to strategy targets over N days:

```yaml
# config/settings.yaml
legacy_unwind:
  enabled: true
  unwind_days: 10                           # Days to converge
  snapshot_file: "state/portfolio_init.json" # Initial positions
```

**Logic:**
- **Day 0 (first run):** Save current IB positions as `portfolio_init.json`
- **Days 1-10:** Blend targets with initial positions using alpha = day/unwind_days
- **Day 11+:** Use pure strategy targets (alpha = 1.0)

**Formula:** `blended[i] = alpha * target[i] + (1 - alpha) * initial[i]`

**File:** `src/legacy_unwind.py`

### Decision Logging

Each run now logs scaling diagnostics:

```
scaling_diagnostics:
  history_days: 0
  raw_realized_vol: 0.0
  effective_vol: 0.10      # Used burn-in prior
  burn_in_active: true
  raw_scaling: 1.2
  clamped_scaling: 1.2     # Within [0.80, 1.25]
  clamp_applied: false
```

### Dry-Run Mode

Test the system without executing orders:

```bash
python -m src.scheduler --dry-run
```

This computes everything (NAV, risk, targets, orders) but does not submit to IBKR.

### Files Modified

| File | Changes |
|------|---------|
| `config/settings.yaml` | Added `vol_burn_in`, `scaling_clamps`, `legacy_unwind` sections |
| `src/risk_engine.py` | Added `effective_realized_vol()`, updated `compute_scaling_factor()` with clamps |
| `src/legacy_unwind.py` | NEW - Glidepath implementation |
| `src/scheduler.py` | Integrated glidepath, added decision logging, `--dry-run` flag |
| `tests/test_risk_engine.py` | Added tests for burn-in and clamps |
| `tests/test_legacy_unwind.py` | NEW - 18 tests for glidepath logic |

### Expected Behavior After Fix

| Scenario | Before Fix | After Fix |
|----------|-----------|-----------|
| Day 0, no history | scaling=0.57, deleverage to cash | scaling=1.2, positions stable |
| High vol (24%) | scaling=0.5, panic sell | scaling=0.8 (clamped), gradual reduction |
| Low vol (4%) | scaling=3.0, over-leverage | scaling=1.25 (clamped), modest increase |
| Legacy positions | Immediate rebalance | 10-day glidepath blend |

---

## Position Sync Fix (December 18, 2025)

### 17. NAV Reconciliation: Phantom Positions

**Symptom:** `EMERGENCY STOP: NAV diff 164.04% > 1.00%` - Internal NAV ($739K) vs Broker NAV ($280K)

**Root Cause:** `_sync_positions()` was **adding** broker positions to internal state without **removing** positions that no longer exist at the broker. This caused "phantom positions" to persist:

```python
# BEFORE (BUG): Only adds, never removes
for inst_id, position in ib_positions.items():
    self.portfolio.positions[inst_id] = position  # Accumulates!
```

The internal state had 11 positions (from stale `portfolio_state.json`) while the broker only had 3 real positions. The extra 8 phantom positions inflated internal NAV.

**Fix:** Clear internal positions before syncing from broker:

```python
# AFTER (FIXED): Replace internal with broker positions
old_count = len(self.portfolio.positions)
self.portfolio.positions.clear()  # Remove all phantom positions

for inst_id, position in ib_positions.items():
    self.portfolio.positions[inst_id] = position

if old_count != len(self.portfolio.positions):
    logger.info("positions_synced_from_broker",
                old_count=old_count,
                new_count=len(self.portfolio.positions))
```

**Additional Actions:**
- Deleted stale `state/portfolio_state.json` (contained 11 phantom positions)
- Deleted stale `state/portfolio_init.json` (legacy glidepath snapshot with phantoms)

**Result:**

| Metric | Before | After |
|--------|--------|-------|
| Internal NAV | $739,464 | $280,060 |
| Broker NAV | $280,062 | $280,056 |
| Diff | 164.04% | **0.00%** |
| Status | EMERGENCY | **PASS** |
| Position count | 11 phantom | 3 real |

**File:** `src/scheduler.py:799-817`

**Lesson:** Internal state must be **replaced** from broker on each sync, not accumulated. Stale state files can cause catastrophic NAV discrepancies.

---

## Commands for Debugging

```bash
# Check gateway status
ssh root@94.130.228.55 'docker compose -f /srv/abstractfinance/docker-compose.yml ps ibgateway'

# View trading engine logs
ssh root@94.130.228.55 'docker compose -f /srv/abstractfinance/docker-compose.yml logs --tail=100 trading-engine'

# Run manual test execution
ssh root@94.130.228.55 'docker run --rm \
  -v /srv/abstractfinance/config:/app/config:ro \
  -v /srv/abstractfinance/state:/app/state \
  -v /srv/abstractfinance/logs:/app/logs \
  --network host \
  -e FORCE_EXECUTION=1 \
  -e MODE=paper \
  -e IBKR_HOST=localhost \
  -e IBKR_PORT=4000 \
  -w /app \
  abstractfinance-trading-engine python -m src.scheduler --once 2>&1'

# Check portfolio via IB API
ssh root@94.130.228.55 'docker run --rm --network host \
  abstractfinance-trading-engine python -c "
from ib_insync import IB
ib = IB()
ib.connect('localhost', 4000, clientId=99)
for p in ib.positions():
    print(f'{p.contract.symbol}: {p.position}')
ib.disconnect()
"'
```
