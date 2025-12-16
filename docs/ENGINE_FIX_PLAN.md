# Trading Engine Fix Plan (MANDATORY)

## Objective

Make the trading engine numerically correct, risk-safe, and reconciled to broker reality before any further strategy optimization.

**This fixes:**
- Silent Sharpe destruction
- NAV blow-ups
- Cross-currency bugs

---

## Phase 0 — Rules (Read First)

1. **Broker NetLiquidation (NLV) is the source of truth**
   - Internal NAV is a check, not an authority.

2. **Exposure ≠ NAV**
   - Exposure is for risk.
   - NAV is for capital.

3. **Every number has a currency**
   - No implicit USD.

4. **If reconciliation fails → STOP TRADING**
   - No partial execution.

---

## Phase 1 — Base Currency Accounting (Non-Negotiable)

### 1.1 Introduce Base Currency Layer

Add a global constant:

```python
BASE_CCY = "USD"
```

Create a centralized FX service:

```python
class FXRates:
    # keyed by (from_ccy, to_ccy)
    rates: dict[tuple[str, str], float]
    timestamp: datetime
```

**Rules:**
- Every valuation uses the same FX snapshot
- No inline FX conversions scattered in code

### 1.2 Cash Accounting by Currency

Replace:

```python
cash: float
```

With:

```python
cash_by_ccy: dict[str, float]  # {"USD": x, "EUR": y, "GBP": z}
```

Add:

```python
def cash_in_base_ccy(cash_by_ccy: dict[str, float], fx_rates: FXRates) -> float:
    """Convert all cash balances to base currency."""
    pass
```

### 1.3 Position Object Must Carry Currency

Enforce:

```python
@dataclass
class Position:
    symbol: str
    quantity: float
    price: float
    currency: str  # REQUIRED - no default
    instrument_type: Literal["STK", "ETF", "FUT", "OPT"]
    multiplier: float | None
    avg_cost: float
```

**Prohibited:**
- No default currency
- No price without currency

---

## Phase 2 — Correct NAV vs Exposure Logic

### 2.1 Split Valuation Paths (CRITICAL)

Implement two separate functions:

```python
def position_nav_value(position: Position, fx_rates: FXRates) -> float:
    """Calculate position's contribution to NAV."""
    pass

def position_exposure(position: Position, fx_rates: FXRates) -> float:
    """Calculate position's risk exposure."""
    pass
```

**Rules by Instrument:**

| Instrument | NAV Value | Exposure |
|------------|-----------|----------|
| Stocks/ETFs | `qty × price` | `qty × price` |
| Futures | **Unrealized P&L ONLY** | `qty × price × multiplier` |
| Options | Model value | `delta × underlying_price × multiplier` |

**CRITICAL:**
- Never add futures notional to NAV
- Ever.

### 2.2 Futures P&L Calculation

For futures:

```python
unrealized_pnl = (price - avg_cost) * quantity * multiplier
```

Cash already includes variation margin → do not double count

### 2.3 NAV Calculation

```python
NAV = cash_in_base_ccy + sum(position_nav_value for all positions)
```

All components converted to `BASE_CCY` before summation.

---

## Phase 3 — Broker Reconciliation Circuit Breaker

### 3.1 Pull Broker NLV

From IBKR:

```python
broker_nlv: float = ib.accountSummary()["NetLiquidation"]
```

### 3.2 Reconciliation Guardrail

```python
diff = abs(internal_nav - broker_nlv) / broker_nlv
```

**Rules:**

| Difference | Action |
|------------|--------|
| `> 0.25%` | **HALT** - Do not place orders |
| `> 1.00%` | **EMERGENCY STOP** + Alert |

Trading logic MUST refuse to place orders if guardrail is breached.

---

## Phase 4 — Currency-Correct Position Sizing

### 4.1 Target Notional Pipeline

Correct pipeline (MANDATORY):

```
NAV (BASE_CCY)
    → sleeve weight
    → scaling factor
    → target notional (BASE_CCY)
    → convert to instrument currency
    → divide by instrument price
    → quantity
```

### 4.2 Example (CS51 – EUR instrument)

```python
leg_notional_usd = NAV * sleeve_weight * scaling
leg_notional_eur = leg_notional_usd / fx_rates[("EUR", "USD")]
qty = round(leg_notional_eur / cs51_price)
```

**Prohibited:**
- Never divide USD notional by EUR price
- Never floor large positions (use `round`)

---

## Phase 5 — Portfolio-Level FX Hedging

### 5.1 Central FX Book (Required)

Delete per-sleeve FX hedges.

Create:

```python
def compute_net_fx_exposure(
    positions: dict[str, Position],
    cash_by_ccy: dict[str, float]
) -> dict[str, float]:
    """
    Returns net exposure per currency.

    Example output:
    {"EUR": -1_400_000, "GBP": 250_000}
    """
    pass
```

### 5.2 Hedge to Target Ratio

Config:

```python
FX_HEDGE_RATIO = 1.0  # allow 0.0–1.0
```

Compute hedge:

```python
hedge_notional = net_fx * FX_HEDGE_RATIO
```

**Rules:**
- Round futures to nearest contract, not floor
- Residual FX exposure must be `<= 2% of NAV`

---

## Phase 6 — Volatility Targeting Fix

Replace:

```python
scaling = target_vol / realized_vol_20d
```

With:

```python
realized_vol = max(vol_floor, ewma_vol)
scaling = clip(target_vol / realized_vol, min=0.0, max=max_leverage)
```

**Requirements:**
- EWMA or blended 20d/60d
- Explicit volatility floor
- `scaling_factor` doc updated to allow `>1.0`

---

## Phase 7 — Regime System Fix

### 7.1 Use Spread Momentum (Mandatory)

When spread momentum ≤ 0:

> Scale Core RV sleeve down or disable entirely

Minimal implementation:

```python
rv_scaler = max(0.0, spread_momentum)
```

### 7.2 Add Hysteresis

**Rules:**
- Regime must persist N days to switch
- Separate enter vs exit thresholds

**Prohibited:**
- No single-day flip-flopping

---

## Phase 8 — Emergency De-Risk Rewrite

Replace multiplicative stacking with state machine:

```python
class RiskState(Enum):
    NORMAL = 1.0
    ELEVATED = 0.7
    CRISIS = 0.3

scaling = RiskState.current.value
```

**Drawdown rule:**
- Sets a floor, not a multiplier

---

## Phase 9 — Execution Safety

**Order Placement Rules:**
- No market orders near open
- Use:
  - Limit-at-open
  - Auction participation

**Block order placement if:**
- NAV reconciliation fails
- FX data missing
- Vol estimate invalid

---

## Phase 10 — Tests (Mandatory)

Add unit tests:

| Test | Description |
|------|-------------|
| **FX Sanity** | EUR position + EURUSD change moves NAV correctly |
| **Futures Sanity** | 1 tick move = correct P&L |
| **Reconciliation** | Simulated mismatch halts trading |
| **Sizing** | USD NAV → EUR instrument produces correct qty |
| **Exposure** | Gross exposure = sum(notional) / NAV |

---

## Definition of Done

This refactor is **NOT complete** unless:

- [ ] NAV matches IBKR NLV within tolerance (0.25%)
- [ ] Futures never contribute notional to NAV
- [ ] All sizing is currency-correct
- [ ] FX hedge is portfolio-level
- [ ] Risk metrics reconcile across logs, storage, and alerts

---

## Important Notes

> **This is a correctness refactor, not an optimization.**
>
> Do not proceed to strategy improvements until this passes end-to-end.

---

*Document added: 2025-12-15*
*Status: PENDING IMPLEMENTATION*
