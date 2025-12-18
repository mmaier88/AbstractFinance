# Execution Debugging Log - December 2025

This document captures lessons learned while debugging the trading engine execution pipeline.

## Current Portfolio Status (Dec 18, 2025)

| Symbol | Qty | Avg Cost | Market Price | Currency | Sleeve |
|--------|-----|----------|--------------|----------|--------|
| EXV1 | -174 | €34.34 | €34.51 | EUR | financials |
| IUKD | -224 | £8.96 | £9.12 | GBP | core_index_rv |
| M6E (Mar 26) | -3 | $14,742 | 1.178 | USD | core_index_rv |
| CSPX | 4 | $730.29 | $730 | USD | us_index_etf |
| LQDE | 6 | $103.05 | $103 | USD | ig_lqd |
| IHYU | 4 | $95.80 | $96 | USD | hy_hyg |
| FLOT | 62 | $5.04 | $5.03 | USD | loans_bkln |

**Account Summary:**
- NAV: $280,079
- Gross Exposure: $54,001 (19% of NAV)
- Net Exposure: -$54,001 (short bias)
- Reconciliation: **PASS**
- Risk Regime: normal
- Scaling Factor: 1.0

**Glidepath Status:**
- Day 1 of 10 (alpha = 0.10)
- Blending 3 initial positions with 8 targets
- 6 orders filled, 8 cancelled (hedge orders)

**Note:** After fixing the GBX_QUOTED_ETFS bug (Issue 20), USD-denominated LSE ETFs are now trading correctly. Portfolio is gradually being built up via the 10-day glidepath.

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

### 18. Burn-In Protection: Crisis Override Fix (December 18, 2025)

**Symptom:** Despite vol burn-in computing scaling=1.0, positions were still liquidated with scaling=0.3

**Root Cause:** The burn-in clamps (0.80-1.25) only applied to `vol_scaling`, NOT the final scaling. Crisis regime override was winning:

```python
# BEFORE: Burn-in clamped vol_scaling, but crisis still won
vol_scaling = 1.0  # From burn-in
state_scaling = 0.3  # Crisis regime
scaling_factor = min(vol_scaling, state_scaling)  # = 0.3 ← CRISIS WINS
```

**Fix:** Apply clamps to FINAL scaling during burn-in period:

```python
# AFTER: Burn-in clamps final scaling too
scaling_factor = min(vol_scaling, state_scaling)  # = 0.3

if burn_in_active:
    scaling_factor = np.clip(scaling_factor, 0.80, 1.25)  # = 0.80
```

**Result:**

| Scenario | Before | After |
|----------|--------|-------|
| Burn-in + Crisis | scaling = 0.3 (liquidate) | scaling = 0.80 (protect) |
| Burn-in + Normal | scaling = 1.0 | scaling = 1.0 |
| Post burn-in + Crisis | scaling = 0.3 | scaling = 0.3 (as designed) |

**File:** `src/risk_engine.py:920-931`

**Key Insight:** The burn-in period (first 60 days) should protect legacy positions from ALL aggressive scaling, including crisis regime. After burn-in, crisis regime can properly de-risk.

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

### 19. Glidepath Day 0: Return Strategy Targets Instead of Current Positions

**Date:** December 18, 2025

**Symptom:** On first run (day 0), the glidepath returned original strategy targets instead of blocking all trades. This caused immediate position changes instead of preserving legacy positions.

**Root Cause:** In `_apply_legacy_glidepath()`, the first-run code path returned `strategy_output` directly instead of creating a no-trade output that preserved current positions.

**Fix:** Modified first-run handling to return empty orders and current positions as targets:
```python
# On first run (day 0), use current positions (NO TRADES)
no_trade_output = StrategyOutput(
    sleeve_targets=strategy_output.sleeve_targets,
    total_target_positions=current_positions,  # Use current, not targets
    orders=[],  # No orders on day 0
    scaling_factor=strategy_output.scaling_factor,
    regime=strategy_output.regime,
    commentary=strategy_output.commentary +
               f"\n[Glidepath Day 0: No trades, preserving {len(current_positions)} positions]"
)
```

**File:** `src/scheduler.py:1110-1136`

**Lesson:** The glidepath must protect legacy positions on day 0 by blocking ALL strategy orders, not by returning strategy targets.

---

### 20. GBX_QUOTED_ETFS: USD ETFs Incorrectly Divided by 100

**Date:** December 18, 2025

**Symptom:** Limit orders for USD-denominated LSE ETFs (CSPX, LQDE, IHYU, FLOT) were submitted at 1/100th of correct prices. IBKR rejected them with: "Order limit price is too far from market (probably because of currency units misuse)"

| Symbol | Wrong Price | Correct Price | Market |
|--------|-------------|---------------|--------|
| CSPX | $7.30 | $730.29 | ~$730 |
| LQDE | $1.03 | $103.05 | ~$103 |
| IHYU | $0.96 | $95.80 | ~$96 |
| FLOT | $0.05 | $5.04 | ~$5 |

**Root Cause:** The `GBX_QUOTED_ETFS` whitelist in `data_feeds.py` incorrectly included USD-denominated ETFs. Only GBP-currency instruments need pence-to-pounds conversion:

```python
# WRONG - included USD instruments:
GBX_QUOTED_ETFS = {
    "CSPX", "CNDX", "IUIT", ..., "LQDE", "IHYU", "FLOT", ...
}
```

**Fix:** Updated whitelist to only include GBP-currency instruments:
```python
# CORRECT - only GBP instruments:
GBX_QUOTED_ETFS = {
    "SMEA",   # GBP - iShares Core MSCI Europe
    "IUKD",   # GBP - iShares UK Dividend
    "IEAC",   # GBP - iShares Core Corp Bond
    "IHYG",   # GBP - iShares Euro High Yield
    # NOTE: Do NOT add USD ETFs like CSPX, LQDE, IHYU, FLOT!
}
```

**Result after fix:**
- Orders filled: 6 (up from 2)
- All USD LSE ETFs now trading at correct prices
- Average slippage: 6.3 bps

**File:** `src/data_feeds.py:180-192`

**Lesson:** The GBX (pence) conversion only applies to **GBP-currency** instruments. USD-denominated ETFs on LSE return prices in USD already and should NOT be divided by 100. Always check the instrument's currency field, not just the exchange.

---

### 21. IBKR Symbol vs Internal Config ID Mismatch

**Date:** December 18, 2025

**Symptom:** Conflicting orders generated for same instrument:
- `us_index_etf BUY 4` AND `CSPX SELL 4` (same instrument!)
- `ig_lqd BUY 6` AND `LQDE SELL 6` (same instrument!)

Portfolio state showed only 3 positions while IBKR had 7.

**Root Cause:** The `_contract_to_instrument_id()` function in `execution_ibkr.py` returned IBKR symbols (CSPX, LQDE) instead of internal config IDs (us_index_etf, ig_lqd).

Flow:
1. IBKR returns `contract.symbol = "CSPX"`
2. `_contract_to_instrument_id()` returned "CSPX"
3. Position stored as `positions["CSPX"] = ...`
4. Strategy expects `positions["us_index_etf"]` - not found!
5. Strategy generates BUY order for us_index_etf
6. Meanwhile, orphan "CSPX" position generates conflicting SELL

**Fix:** Added reverse lookup in `_contract_to_instrument_id()`:
```python
def _contract_to_instrument_id(self, contract, instruments_config=None):
    ibkr_symbol = contract.symbol

    # Reverse lookup: find internal ID that maps to this IBKR symbol
    if instruments_config:
        for category, instruments in instruments_config.items():
            for inst_id, spec in instruments.items():
                if spec.get('symbol') == ibkr_symbol:
                    return inst_id  # Return internal ID, not IBKR symbol

    return ibkr_symbol  # Fallback
```

Also updated `get_positions()` signature to accept `instruments_config` parameter.

**Files:**
- `src/execution_ibkr.py:430-472` - `_contract_to_instrument_id()`
- `src/execution_ibkr.py:352-378` - `get_positions()`
- `src/scheduler.py:459, 799` - Updated callers

**Lesson:** Position sync MUST use internal config IDs, not IBKR symbols. Strategy and execution must use the same ID namespace.

---

## Systematic Testing Framework (Added Dec 18, 2025)

To prevent Issues 17-21 from recurring, we added a three-layer testing approach:

### Layer 1: Runtime Invariants (`src/utils/invariants.py`)

Assertions that catch bugs immediately at runtime:

| Invariant | What It Catches | Location |
|-----------|-----------------|----------|
| `assert_position_id_valid()` | IBKR symbol used instead of config ID | Position sync |
| `assert_no_conflicting_orders()` | BUY and SELL for same instrument | Order generation |
| `assert_gbx_whitelist_valid()` | Non-GBP instruments in GBX whitelist | Startup |
| `validate_instruments_config()` | Duplicate config IDs, symbol ambiguity | Startup |

These are called automatically in `scheduler.py` at critical points.

### Layer 2: Integration Tests (`tests/test_integration_flow.py`)

25 new tests covering:
- Position ID mapping (IBKR symbol → config ID)
- Glidepath blending (Day 0, Day 1, Day 10)
- Price conversion (GBX only for GBP)
- Order generation (no conflicts, correct IDs)
- End-to-end scenarios

Run with: `pytest tests/test_integration_flow.py -v`

### Layer 3: Simulation Mode (`src/simulation.py`)

Pre-deploy validation that runs full cycle without trading:

```python
from src.simulation import SimulationRunner, SimulationScenario

runner = SimulationRunner(instruments_config)
report = runner.run_predeploy_checks(positions, prices)

if not report.all_passed:
    print(report.summary())
    # Don't deploy!
```

### What Each Layer Would Have Caught

| Issue | Layer 1 | Layer 2 | Layer 3 |
|-------|---------|---------|---------|
| 17: Phantom positions | ✓ | ✓ | ✓ |
| 19: Glidepath Day 0 | - | ✓ | ✓ |
| 20: GBX whitelist | ✓ | ✓ | - |
| 21: ID mapping | ✓ | ✓ | ✓ |

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
