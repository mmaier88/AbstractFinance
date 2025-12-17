# Execution Debugging Log - December 2025

This document captures lessons learned while debugging the trading engine execution pipeline.

## Current Portfolio Status (Dec 17, 2025)

| Symbol | Qty | Avg Cost | Market Price | Currency | Sleeve |
|--------|-----|----------|--------------|----------|--------|
| CSPX | 933 | $738.48 | $727.99 | USD | core_index_rv |
| EXS1 | -1,005 | €201.60 | €199.49 | EUR | core_index_rv |
| EXV1 | -9 | €33.13 | €34.23 | EUR | core_index_rv |
| FLOT | 652 | $5.05 | $5.03 | USD | core_index_rv |
| IHYU | 157 | $95.80 | $95.59 | USD | core_index_rv |
| IUHC | 15 | $12.86 | $12.32 | USD | core_index_rv |
| IUIT | 4 | $43.73 | $41.03 | USD | core_index_rv |
| IUKD | -224 | £8.96 | £9.15 | GBP | core_index_rv |
| IUQA | 11 | $17.51 | $16.67 | USD | core_index_rv |
| LQDE | 2 | $106.68 | $102.63 | USD | core_index_rv |
| M6E (Mar 26) | -3 | $14,742 | 1.1794 | USD | core_index_rv |

**Account Summary:**
- NAV: $285,804
- Net Liquidation: $243,283
- Gross Exposure: $798,388
- Unrealized P&L: ~$-13,400

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

## Why Orders Show 0 Filled

Even with 24 orders placed, `orders_filled: 0` is expected because:

1. **Limit Orders**: The execution stack uses `marketable_limit` policy, not market orders
2. **Paper Trading**: Paper trading may have limited liquidity simulation
3. **Market Hours**: Orders placed outside market hours won't fill immediately
4. **Order Expiry**: Day orders expire at market close

To verify fills, check:
- `ib.executions()` for today's fills
- `ib.openOrders()` for pending orders
- Trading engine logs for order status updates
