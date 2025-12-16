# AbstractFinance Trading Engine - Complete Technical Documentation

## Executive Summary

AbstractFinance is a **production-grade automated trading engine** implementing a "European Decline Macro" hedge fund strategy. It runs on Hetzner cloud infrastructure, connects to Interactive Brokers via IB Gateway, and executes a multi-sleeve long/short equity strategy with FX hedging and tail risk management.

**Key Stats:**
- ~9,000 lines of Python across 17 modules
- 76 tradeable instruments across 14 asset classes
- 6 strategy sleeves with distinct alpha sources
- Daily execution at 06:00 UTC
- Full monitoring stack (Prometheus, Grafana, Telegram alerts)

---

## 1. Architecture Overview

### System Topology

```
┌─────────────────────────────────────────────────────────────────────┐
│                        HETZNER CLOUD SERVER                          │
│                         (94.130.228.55)                              │
│                                                                      │
│  ┌──────────────┐     ┌──────────────────┐     ┌─────────────────┐ │
│  │  IB Gateway  │────►│  Trading Engine  │────►│   PostgreSQL    │ │
│  │ (heshiming/  │     │  (Python 3.12)   │     │   (State DB)    │ │
│  │   ibga)      │     │                  │     │                 │ │
│  │  Port 4000   │     │  Ports 8000/8080 │     │   Port 5432     │ │
│  └──────────────┘     └──────────────────┘     └─────────────────┘ │
│         │                      │                                     │
│         │              ┌───────┴───────┐                            │
│         │              │               │                            │
│  ┌──────┴──────┐  ┌────┴────┐   ┌─────┴─────┐                      │
│  │  VNC Debug  │  │Prometheus│   │  Grafana  │                      │
│  │  Port 5900  │  │Port 9090 │   │ Port 3000 │                      │
│  └─────────────┘  └─────────┘   └───────────┘                      │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                    ┌─────────────────┐
                    │ Interactive     │
                    │ Brokers         │
                    │ (Order Routing) │
                    └─────────────────┘
```

### Docker Compose Services

| Service | Image | Purpose | Ports |
|---------|-------|---------|-------|
| `ibgateway` | heshiming/ibga:latest | IBKR API Gateway with TOTP | 4000, 5900 |
| `trading-engine` | custom | Core trading logic | 8000, 8080 |
| `postgres` | postgres:14-alpine | State persistence | 5432 |
| `prometheus` | prom/prometheus:v2.54.1 | Metrics collection | 9090 |
| `grafana` | grafana/grafana:11.3.0 | Dashboards | 3000 |
| `loki` | grafana/loki:3.2.0 | Log aggregation | 3100 |
| `alertmanager` | prom/alertmanager:v0.27.0 | Alert routing | 9093 |

---

## 2. Daily Execution Flow

### Scheduler (`src/scheduler.py`)

The trading engine runs on a continuous scheduler that executes at **06:00 UTC daily**:

```python
class ContinuousScheduler:
    def run(self):
        # 1. Start health check server (port 8080)
        # 2. Start Prometheus metrics server (port 8000)
        # 3. Wait for IB Gateway API ready (up to 10 minutes)
        # 4. Enter main loop
        while True:
            if is_run_time(hour=6, minute=0):
                self.run_daily()
            sleep(60)  # Check every minute
```

### Daily Run Steps (14 Steps with ENGINE_FIX_PLAN)

```
06:00 UTC - Daily Run Triggered
     │
     ▼
┌────────────────────────────────────────┐
│ Step 1: Check Maintenance Windows      │
│   - Skip orders if IBKR maintenance    │
│   - Weekly: Sun 23:45 - Mon 00:45 UTC  │
│   - Daily: 22:00 - 22:15 UTC           │
└────────────────────────────────────────┘
     │
     ▼
┌────────────────────────────────────────┐
│ Step 2: Initialize (First Run Only)    │
│   - Load instruments config            │
│   - Initialize portfolio state         │
│   - Connect to IB Gateway              │
│   - Initialize FX rates service        │ ← ENGINE_FIX_PLAN Phase 1
└────────────────────────────────────────┘
     │
     ▼
┌────────────────────────────────────────┐
│ Step 3: Refresh FX Rates              │ ← ENGINE_FIX_PLAN Phase 1
│   - Fetch live rates from IB           │
│   - Fallback to yfinance if IB fails   │
│   - Check staleness (>24h = warning)   │
└────────────────────────────────────────┘
     │
     ▼
┌────────────────────────────────────────┐
│ Step 4: Sync Positions from IBKR       │
│   - Query all positions via API        │
│   - Update portfolio.positions dict    │
│   - Normalize futures avgCost          │
│   - Handle GBP pence conversion        │
│   - Pass FX rates for exposure calc    │ ← ENGINE_FIX_PLAN Phase 2
└────────────────────────────────────────┘
     │
     ▼
┌────────────────────────────────────────┐
│ Step 5: Broker Reconciliation         │ ← ENGINE_FIX_PLAN Phase 3
│   - Get broker NLV from IBKR           │
│   - Compare internal NAV vs broker     │
│   - HALT if diff > 0.25%               │
│   - EMERGENCY if diff > 1.0%           │
│   - Block all trading until resolved   │
└────────────────────────────────────────┘
     │
     ▼
┌────────────────────────────────────────┐
│ Step 6: Auto-Roll Expiring Futures     │
│   - Check each futures position        │
│   - If <= 3 days to expiry: roll       │
│   - Close old contract, open new       │
└────────────────────────────────────────┘
     │
     ▼
┌────────────────────────────────────────┐
│ Step 7: Compute NAV                    │ ← ENGINE_FIX_PLAN Phase 2
│   - Futures: unrealized P&L only       │
│   - Stocks/ETFs: full market value     │
│   - All values converted to USD        │
│   - NAV = cash_in_base + Σ(nav_value)  │
└────────────────────────────────────────┘
     │
     ▼
┌────────────────────────────────────────┐
│ Step 8: Evaluate Risk                  │ ← ENGINE_FIX_PLAN Phase 6-8
│   - EWMA volatility with floor (8%)    │
│   - Regime hysteresis (3-day persist)  │
│   - Risk state machine:                │
│     NORMAL (1.0) → ELEVATED (0.7)      │
│     ELEVATED → CRISIS (0.3)            │
│   - Calculate scaling factor           │
└────────────────────────────────────────┘
     │
     ▼
┌────────────────────────────────────────┐
│ Step 9: Compute Strategy Targets       │ ← ENGINE_FIX_PLAN Phase 4-5
│   - Currency-correct position sizing   │
│   - Convert USD target to local ccy    │
│   - Portfolio-level FX hedging         │
│   - Single EUR/GBP hedge calculation   │
└────────────────────────────────────────┘
     │
     ▼
┌────────────────────────────────────────┐
│ Step 10: Manage Tail Hedges            │
│   - Check hedge budget remaining       │
│   - Roll expiring hedges               │
│   - Increase hedges if regime elevated │
└────────────────────────────────────────┘
     │
     ▼
┌────────────────────────────────────────┐
│ Step 11: Check Crisis Conditions       │
│   - If VIX >= 40 OR daily loss >= 10%: │
│     • Close 60% of ITM hedges          │
│     • Reduce core exposure             │
│     • Execute crisis playbook          │
└────────────────────────────────────────┘
     │
     ▼
┌────────────────────────────────────────┐
│ Step 12: Execution Safety Check       │ ← ENGINE_FIX_PLAN Phase 9
│   - Verify reconciliation passed       │
│   - Check FX rates valid               │
│   - Max order value < 10% NAV          │
│   - Max daily turnover < 50%           │
│   - Block execution if ANY fails       │
└────────────────────────────────────────┘
     │
     ▼
┌────────────────────────────────────────┐
│ Step 13: Execute Orders                │
│   - Generate orders (target - current) │
│   - Skip if maintenance window         │
│   - Place market orders via IBKR       │
│   - Wait for fills                     │
└────────────────────────────────────────┘
     │
     ▼
┌────────────────────────────────────────┐
│ Step 14: Record & Alert                │
│   - Calculate daily P&L                │
│   - Update drawdown metrics            │
│   - Save state to JSON/DB              │
│   - Send Telegram summary              │
└────────────────────────────────────────┘
```

---

## 3. Strategy Architecture (6 Sleeves)

### Sleeve Allocation

| Sleeve | Target Weight | Strategy |
|--------|---------------|----------|
| Core Index RV | 35% | Long US (CSPX), Short EU (CS51), FX hedged |
| Sector RV | 25% | Long US Tech/Healthcare, Short EU Old Economy |
| Single Name | 15% | Factor-based stock selection (Quality/Momentum) |
| Credit & Carry | 15% | Long US IG/HY bonds, BDCs |
| Crisis Alpha | 5% | Tail hedges (puts, VIX calls) |
| Cash Buffer | 5% | Liquidity reserve |

### Sleeve 1: Core Index RV (35%)

**Thesis:** Long US equities vs Short European equities with FX hedge

```python
def construct_core_index_rv(nav, scaling_factor, data_feed):
    sleeve_notional = nav * 0.35 * scaling_factor
    leg_notional = sleeve_notional / 2

    # LONG LEG: US Equities via UCITS ETF
    cspx_price = data_feed.get_last_price("CSPX")  # ~$730
    cspx_qty = int(leg_notional / cspx_price)

    # SHORT LEG: EU Equities via UCITS ETF
    cs51_price = data_feed.get_last_price("CS51")  # ~€50
    cs51_qty = -int(leg_notional / cs51_price)

    # FX HEDGE: Short EUR via Micro Futures
    eur_exposure = abs(cs51_qty) * cs51_price  # EUR notional
    m6e_contracts = -int(eur_exposure / 12500)  # 12,500 EUR per contract

    return {
        "CSPX": cspx_qty,      # Long ~932 shares
        "CS51": cs51_qty,      # Short ~28,000 shares
        "M6E": m6e_contracts   # Short ~4 contracts
    }
```

**Instruments Used:**
- `CSPX` - iShares Core S&P 500 UCITS ETF (LSE, USD)
- `CS51` - iShares Euro STOXX 50 UCITS ETF (XETRA, EUR)
- `M6E` - Micro EUR/USD Futures (CME, multiplier 12,500)

### Sleeve 2: Sector RV (25%)

**Thesis:** Long US growth sectors, Short EU old economy

**Long Basket (US):**
- `IUIT` - S&P 500 IT Sector UCITS
- `CNDX` - NASDAQ 100 UCITS
- `SEMI` - VanEck Semiconductor UCITS
- `IUHC` - S&P 500 Healthcare UCITS
- `IUQA` - MSCI USA Quality Factor UCITS

**Short Basket (EU):**
- `EXV1` - STOXX Europe 600 Banks UCITS
- `EXS1` - Core DAX UCITS
- `IUKD` - UK Dividend UCITS

### Sleeve 3: Single Name (15%)

**Thesis:** Factor-based stock selection

```python
def construct_single_name(nav, scaling_factor, stock_screener):
    sleeve_notional = nav * 0.15 * scaling_factor

    # US LONGS: Quality + Momentum + Size factors
    us_longs = stock_screener.screen_us_longs(
        quality_weight=0.50,
        momentum_weight=0.30,
        size_weight=0.20,
        top_n=10
    )

    # EU SHORTS: Zombie + Weakness + Sector factors
    eu_shorts = stock_screener.screen_eu_shorts(
        zombie_weight=0.50,
        weakness_weight=0.30,
        sector_weight=0.20,
        top_n=10
    )

    # Fallback if screening fails
    if not us_longs:
        us_longs = ["AAPL", "MSFT", "GOOGL", "NVDA", "AMZN"]
    if not eu_shorts:
        eu_shorts = ["EXV1"]  # EU Financials ETF
```

### Sleeve 4: Credit & Carry (15%)

**Thesis:** Harvest credit risk premium

**Allocation:**
- 40% `LQDE` - iShares $ Corp Bond UCITS (IG)
- 25% `IHYU` - iShares $ High Yield UCITS
- 20% `FLOT` - iShares Floating Rate UCITS
- 15% `ARCC` - Ares Capital (BDC, individual stock)

### Sleeve 5: Crisis Alpha (5%)

**Thesis:** Tail hedge protection

**Budget:** 2.5% of NAV annually for option premiums

**Hedge Types:**
- 40% - Equity puts (SPX/SPY/SX5E)
- 20% - VIX calls
- 15% - Credit puts (HYG/JNK)
- 15% - Sovereign spreads (OAT-Bund)
- 10% - Bank puts (French banks)

---

## 4. Risk Management System

### Risk Engine (`src/risk_engine.py`)

```python
@dataclass
class RiskDecision:
    scaling_factor: float       # 0.0 - 1.0 (position size multiplier)
    emergency_derisk: bool      # True = reduce to 25%
    regime: RiskRegime          # NORMAL, ELEVATED, CRISIS, RECOVERY
    reduce_core_exposure: bool  # Apply regime reduction
    reduce_factor: float        # 0.3 - 1.0
    increase_hedges: bool       # Increase tail hedge budget
    warnings: List[str]         # Risk warnings to log
```

### Risk Regime Detection

```python
def detect_regime(vix_level, spread_momentum, current_drawdown):
    # CRISIS: VIX >= 40 OR Drawdown <= -10%
    if vix_level >= 40 or current_drawdown <= -0.10:
        return RiskRegime.CRISIS

    # ELEVATED: VIX >= 25 OR Drawdown <= -5%
    if vix_level >= 25 or current_drawdown <= -0.05:
        return RiskRegime.ELEVATED

    # RECOVERY: Improving from drawdown
    if -0.05 <= current_drawdown <= -0.03 and vix_level < 20:
        return RiskRegime.RECOVERY

    # NORMAL: All clear
    return RiskRegime.NORMAL
```

### Volatility Targeting

**Target:** 12% annualized volatility

```python
def compute_scaling_factor(realized_vol, target_vol=0.12, max_leverage=2.0):
    # Scale inversely to realized volatility
    raw_scaling = target_vol / realized_vol

    # Cap at maximum leverage
    return min(raw_scaling, max_leverage)

# Example:
# realized_vol = 18%, target = 12%
# scaling = 0.12 / 0.18 = 0.67
# Result: Reduce all positions to 67% of target
```

### Drawdown Control

```python
# Emergency de-risk trigger: 10% drawdown
if current_drawdown <= -0.10:
    scaling_factor = 0.25  # Reduce to 25% of target
    emergency_derisk = True

# Regime-based reduction
if regime == RiskRegime.CRISIS:
    scaling_factor *= 0.30  # Additional 70% reduction
elif regime == RiskRegime.ELEVATED:
    scaling_factor *= 0.75  # Additional 25% reduction
```

### Key Risk Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `vol_target_annual` | 12% | Target portfolio volatility |
| `gross_leverage_max` | 2.0x | Maximum gross exposure |
| `net_leverage_max` | 1.0x | Maximum net exposure |
| `max_drawdown_pct` | 10% | Emergency de-risk trigger |
| `vix_threshold` | 40 | Crisis regime trigger |
| `rebalance_threshold_pct` | 2% | Drift before rebalancing |

---

## 5. Order Execution

### IBKR Client (`src/execution_ibkr.py`)

```python
class IBClient:
    def __init__(self, host="ibgateway", port=4000, client_id=1):
        self.ib = IB()  # ib_insync library
        self._connected = False
        self._reconnect_attempts = 0
        self._max_reconnect_attempts = 5

    def get_positions(self) -> Dict[str, Position]:
        """Fetch all positions from IBKR."""
        positions = {}
        for ib_pos in self.ib.positions():
            # Normalize futures avgCost (divide by multiplier)
            if contract.secType == "FUT":
                avg_cost = ib_pos.avgCost / multiplier
            else:
                avg_cost = ib_pos.avgCost

            # Handle GBP pence conversion
            if contract.currency == "GBP" and price > 100:
                price = price / 100.0

            positions[instrument_id] = Position(...)
        return positions

    def place_order(self, order_spec, instruments_config):
        """Place a single order."""
        contract = self.build_contract(order_spec.instrument_id)

        if order_spec.order_type == "MKT":
            order = MarketOrder(order_spec.side, order_spec.quantity)
        elif order_spec.order_type == "LMT":
            order = LimitOrder(order_spec.side, order_spec.quantity,
                             order_spec.limit_price)

        trade = self.ib.placeOrder(contract, order)
        self.ib.sleep(1)  # Wait for fill

        return ExecutionReport(
            filled_qty=trade.orderStatus.filled,
            avg_fill_price=trade.orderStatus.avgFillPrice,
            status=trade.orderStatus.status
        )
```

### Contract Building

```python
def build_contract(self, instrument_id, spec):
    if spec['sec_type'] == 'STK':
        # Stock with exchange routing
        exchange_map = {'LSE': 'LSEETF', 'XETRA': 'IBIS'}
        return Stock(spec['symbol'], 'SMART', spec['currency'],
                    primaryExchange=exchange_map.get(spec['exchange']))

    elif spec['sec_type'] == 'FUT':
        # Future with expiry
        if '_' in instrument_id:
            expiry = instrument_id.split('_')[1]  # M6E_20251215 -> 20251215
        else:
            expiry = calculate_front_month()
        return Future(spec['symbol'], exchange=spec['exchange'],
                     lastTradeDateOrContractMonth=expiry)
```

### Reconnection Logic

```python
def _attempt_reconnect(self):
    """Reconnect with exponential backoff."""
    delays = [30, 60, 90, 120, 120]  # seconds

    for attempt, delay in enumerate(delays, 1):
        time.sleep(delay)
        try:
            self.ib.connect(self.host, self.port, self.client_id)
            if self.ib.isConnected():
                self._connected = True
                send_alert("IB Gateway reconnected!")
                return True
        except Exception:
            continue

    send_alert("CRITICAL: Reconnection failed!")
    return False
```

---

## 6. Data Feeds

### Market Data Sources

**Primary:** Interactive Brokers API (real-time)
**Fallback:** Yahoo Finance (yfinance library)

```python
def get_last_price(self, instrument_id):
    # 1. Check cache (TTL: 60 seconds)
    if cached and cache_age < 60:
        return cached_price

    # 2. Try IBKR
    if self.ib.isConnected():
        contract = self._get_ib_contract(instrument_id)
        ticker = self.ib.reqMktData(contract)
        self.ib.sleep(0.5)
        price = ticker.last or ticker.close
        if price:
            return price

    # 3. Fallback to Yahoo Finance
    yf_ticker = YFINANCE_MAPPING.get(instrument_id, instrument_id)
    df = yf.download(yf_ticker, period='1d')
    return df['Close'].iloc[-1]
```

### Yahoo Finance Mapping

```python
YFINANCE_MAPPING = {
    # UCITS ETFs on LSE
    "CSPX": "CSPX.L",
    "CNDX": "CNDX.L",
    "IUIT": "IUIT.L",
    "IUKD": "IUKD.L",

    # UCITS ETFs on XETRA
    "CS51": "SXRT.DE",
    "EXS1": "EXS1.DE",

    # Futures (use underlying)
    "M6E": "EURUSD=X",
    "ES": "^GSPC",

    # VIX
    "^VIX": "^VIX",
}
```

---

## 7. Position & NAV Calculation

### Position Data Structure

```python
@dataclass
class Position:
    instrument_id: str      # e.g., "CSPX" or "M6E_20251215"
    quantity: float         # Signed: + long, - short
    avg_cost: float         # Per-unit cost basis
    market_price: float     # Current price
    multiplier: float       # 1.0 for stocks, 12500 for M6E
    currency: str           # USD, EUR, GBP
    sleeve: Sleeve          # Which sleeve owns this position

    @property
    def market_value(self) -> float:
        return self.quantity * self.market_price * self.multiplier

    @property
    def unrealized_pnl(self) -> float:
        cost_basis = self.quantity * self.avg_cost * self.multiplier
        return self.market_value - cost_basis
```

### NAV Calculation

```python
def compute_nav(portfolio_state, data_feed):
    total = portfolio_state.cash

    for inst_id, position in portfolio_state.positions.items():
        # Update market price
        position.market_price = data_feed.get_last_price(inst_id)

        # Add to NAV
        total += position.market_value

    portfolio_state.nav = total
    return total

# Example:
# Cash: $10,000,000
# CSPX: 932 shares × $734 × 1.0 = $684,088
# CS51: -28,000 shares × €50 × 1.0 = -€1,400,000 = -$1,470,000
# M6E: -4 contracts × 1.17 × 12,500 = -$58,500
# NAV = $10,000,000 + $684,088 - $1,470,000 - $58,500 + ... = $X,XXX,XXX
```

### Exposure Calculation

```python
def compute_exposures(portfolio_state):
    long_exposure = sum(
        abs(pos.market_value)
        for pos in positions.values()
        if pos.quantity > 0
    )

    short_exposure = sum(
        abs(pos.market_value)
        for pos in positions.values()
        if pos.quantity < 0
    )

    gross_exposure = long_exposure + short_exposure
    net_exposure = long_exposure - short_exposure

    return gross_exposure, net_exposure
```

---

## 8. State Persistence

### Portfolio State File (`state/portfolio_state.json`)

```json
{
  "nav": 12587099.48,
  "cash": 10000000,
  "initial_capital": 10000000,
  "gross_exposure": 3197893.63,
  "net_exposure": 2750547.30,
  "long_exposure": 2974220.47,
  "short_exposure": 223673.17,
  "realized_vol_annual": 0.142,
  "current_drawdown": -0.013,
  "max_drawdown": -0.057,
  "daily_pnl": 45230.12,
  "daily_return": 0.00417,
  "total_pnl": 2587099.48,
  "last_update": "2025-12-15T09:32:24.926433",
  "inception_date": "2025-12-03",
  "positions": {
    "CSPX": {
      "quantity": 932,
      "avg_cost": 738.48,
      "market_price": 734.13,
      "multiplier": 1.0,
      "currency": "USD",
      "sleeve": "core_index_rv"
    },
    "M6E_20251215": {
      "quantity": -4,
      "avg_cost": 1.1718,
      "market_price": 1.1736,
      "multiplier": 12500.0,
      "currency": "USD",
      "sleeve": "core_index_rv"
    }
  },
  "pnl_history": {
    "2025-12-15": 0.00417,
    "2025-12-14": 0.00312,
    "2025-12-13": -0.00189
  }
}
```

---

## 9. Monitoring & Alerting

### Prometheus Metrics (Port 8000)

```
# Connection
abstractfinance_ib_connection_state 1

# Portfolio
abstractfinance_portfolio_nav_usd 12587099.48
abstractfinance_portfolio_gross_exposure_usd 3197893.63
abstractfinance_portfolio_drawdown_pct -0.013

# Risk
abstractfinance_risk_realized_vol_annual 0.142
abstractfinance_risk_scaling_factor 0.85
abstractfinance_risk_regime 0  # 0=NORMAL

# Orders
abstractfinance_orders_filled_total{sleeve="core_index_rv"} 156
```

### Telegram Alerts

**Daily Summary:**
```
📊 Daily Summary - 2025-12-15

NAV: $12,587,099 (+0.42%)
Daily P&L: +$45,230

Exposures:
  Gross: 3.20x
  Net: 2.75x

Risk:
  Vol: 14.2% (target 12%)
  Drawdown: -1.3%
  Regime: NORMAL

Next run: 2025-12-16 06:00 UTC
```

**Crisis Alert:**
```
🚨 CRISIS ALERT

VIX spiked to 42.5!
Regime: CRISIS

Actions taken:
- Scaling reduced to 30%
- Hedge budget increased 50%
- Core exposure reduced

Manual review recommended.
```

---

## 10. Key Algorithms

### Realized Volatility (Annualized)

```python
def compute_realized_vol_annual(returns, window=20):
    daily_vol = returns.tail(window).std()
    annual_vol = daily_vol * np.sqrt(252)  # 252 trading days
    return annual_vol

# Example:
# Daily returns std = 0.91%
# Annual vol = 0.0091 × 15.87 = 14.5%
```

### Drawdown Calculation

```python
def compute_drawdown(nav_history):
    rolling_max = nav_history.cummax()
    drawdown = (nav_history - rolling_max) / rolling_max
    current_dd = drawdown.iloc[-1]
    max_dd = drawdown.min()
    return current_dd, max_dd

# Example:
# Peak NAV: $10.5M, Current NAV: $9.2M
# Drawdown = (9.2 - 10.5) / 10.5 = -12.4%
```

### US/EU Spread Momentum

```python
def compute_spread_momentum(spx_sx5e_ratio, short_window=50, long_window=200):
    ma_short = ratio.rolling(short_window).mean()
    ma_long = ratio.rolling(long_window).mean()

    slope = (ma_short.iloc[-1] - ma_short.iloc[-20]) / ma_short.iloc[-20]

    if ma_short.iloc[-1] > ma_long.iloc[-1] and slope > 0:
        return 1.0   # Strong US outperformance
    elif ma_short.iloc[-1] > ma_long.iloc[-1]:
        return 0.5   # Moderate US outperformance
    elif slope < 0:
        return -1.0  # Strong EU outperformance (reduce positions)
    else:
        return -0.5  # Moderate EU outperformance
```

---

## 11. ENGINE_FIX_PLAN - Mandatory System Fixes (December 2025)

A comprehensive 10-phase refactor was implemented to make the trading engine numerically correct, risk-safe, and reconciled to broker reality.

### Phase 1: Base Currency Accounting

**Problem:** Cash was stored as a single float, ignoring multi-currency positions.

**Solution:** New centralized FX service (`src/fx_rates.py`):

```python
# Global base currency
BASE_CCY = "USD"

@dataclass
class FXRates:
    rates: Dict[Tuple[str, str], float]  # (from_ccy, to_ccy) -> rate
    timestamp: Optional[datetime] = None

    def get_rate(self, from_ccy: str, to_ccy: str) -> float
    def convert(self, amount: float, from_ccy: str, to_ccy: str) -> float
    def to_base(self, amount: float, from_ccy: str) -> float
    def refresh(self, ib: Optional[object] = None) -> bool

# Portfolio now uses multi-currency cash
cash_by_ccy: Dict[str, float] = {"USD": 0.0, "EUR": 0.0, "GBP": 0.0}

# Total cash in base currency
def cash_in_base_ccy(cash_by_ccy: Dict[str, float], fx_rates: FXRates) -> float
```

### Phase 2: NAV vs Exposure Logic

**Problem:** Futures notional was added to NAV, when only unrealized P&L should contribute.

**Solution:** Separate functions for NAV and exposure:

```python
class InstrumentType(Enum):
    STK = "STK"  # Stock
    ETF = "ETF"  # ETF
    FUT = "FUT"  # Futures
    OPT = "OPT"  # Options

def position_nav_value(position: Position, fx_rates: FXRates) -> float:
    """Futures: unrealized P&L only. Stocks/ETFs: market value."""
    if position.instrument_type == InstrumentType.FUT:
        pnl = position.unrealized_pnl  # (price - avg_cost) * qty * mult
        return fx_rates.to_base(pnl, position.currency)
    else:
        return fx_rates.to_base(position.market_value, position.currency)

def position_exposure(position: Position, fx_rates: FXRates) -> float:
    """Full notional for risk calculations."""
    exposure = position.quantity * position.market_price * position.multiplier
    return fx_rates.to_base(exposure, position.currency)

# NAV = cash + sum(position_nav_value)
# Exposure = sum(position_exposure)  # For risk limits
```

### Phase 3: Broker Reconciliation Circuit Breaker

**Problem:** No validation that internal NAV matches broker's NLV.

**Solution:** Reconciliation check before any trading:

```python
def reconcile_with_broker(
    self,
    broker_nlv: float,
    halt_threshold_pct: float = 0.0025,  # 0.25%
    emergency_threshold_pct: float = 0.01  # 1.0%
) -> Tuple[bool, str]:
    diff_pct = abs(self.nav - broker_nlv) / broker_nlv

    if diff_pct > emergency_threshold_pct:
        self.reconciliation_status = "EMERGENCY"  # Full stop
        return False, "EMERGENCY"

    if diff_pct > halt_threshold_pct:
        self.reconciliation_status = "HALT"  # Stop trading
        return False, "HALT"

    self.reconciliation_status = "PASS"
    return True, "PASS"

def can_trade(self) -> bool:
    return self.reconciliation_status == "PASS"
```

### Phase 4: Currency-Correct Position Sizing

**Problem:** EUR notional divided by EUR price when it should use USD target.

**Solution:** Convert target notional to instrument currency BEFORE dividing:

```python
def _build_core_index_targets(self, nav, scaling, fx_rates):
    notional_per_leg_usd = nav * 0.35 * scaling / 2

    # EU Short Leg - CS51 trades in EUR
    notional_eur = fx_rates.convert(notional_per_leg_usd, "USD", "EUR")
    cs51_price = data_feed.get_last_price("CS51")  # EUR price
    cs51_qty = round(notional_eur / cs51_price)  # Use round(), not int()

    # UK Positions - IUKD trades in GBP
    notional_gbp = fx_rates.convert(notional_per_leg_usd, "USD", "GBP")
    iukd_price = data_feed.get_last_price("IUKD")  # GBP price
    iukd_qty = round(notional_gbp / iukd_price)
```

### Phase 5: Portfolio-Level FX Hedging

**Problem:** Per-sleeve FX hedging was redundant and complex.

**Solution:** Single portfolio-level hedge calculation:

```python
def compute_net_fx_exposure(positions, cash_by_ccy, fx_rates) -> Dict[str, float]:
    """Compute net FX exposure by currency."""
    exposure_by_ccy = {}

    # Add position exposures
    for position in positions.values():
        ccy = position.currency
        value = position.market_value
        exposure_by_ccy[ccy] = exposure_by_ccy.get(ccy, 0) + value

    # Add cash balances
    for ccy, amount in cash_by_ccy.items():
        exposure_by_ccy[ccy] = exposure_by_ccy.get(ccy, 0) + amount

    # Remove USD (base currency - no hedge needed)
    exposure_by_ccy.pop("USD", None)
    return exposure_by_ccy  # e.g., {"EUR": -1_400_000, "GBP": 250_000}

def compute_fx_hedge_quantities(net_fx_exposure, fx_rates, hedge_ratio=1.0):
    """Calculate FX futures contracts needed."""
    contract_sizes = {"EUR": 12500, "GBP": 6250, "CHF": 12500}
    hedges = {}

    for ccy, exposure in net_fx_exposure.items():
        if ccy in contract_sizes:
            contracts = round(exposure * hedge_ratio / contract_sizes[ccy])
            hedges[ccy] = -contracts  # Negative = short the currency

    return hedges
```

### Phase 6: Volatility Targeting (EWMA, Floor, Clip)

**Problem:** Simple rolling vol was noisy and allowed dangerous scaling.

**Solution:** EWMA with floor and max leverage cap:

```python
# Settings
vol_floor = 0.08  # 8% minimum vol assumption
ewma_span = 20
vol_blend_weight = 0.7  # 70% EWMA, 30% rolling

def compute_blended_vol(self, returns, short_window=20, long_window=60) -> float:
    """Blend EWMA and rolling volatility."""
    ewma_vol = returns.ewm(span=self.ewma_span).std().iloc[-1] * np.sqrt(252)
    rolling_vol = returns.tail(long_window).std() * np.sqrt(252)

    blended = self.vol_blend_weight * ewma_vol + (1 - self.vol_blend_weight) * rolling_vol

    # Apply floor
    return max(self.vol_floor, blended)

def compute_scaling_factor(self, realized_vol):
    floored_vol = max(self.vol_floor, realized_vol)
    raw_scaling = self.vol_target_annual / floored_vol
    # Allow scaling > 1.0, but cap at max_leverage
    return np.clip(raw_scaling, 0.0, self.gross_leverage_max)
```

### Phase 7: Regime System Hysteresis

**Problem:** Single-day VIX spikes caused flip-flopping between regimes.

**Solution:** Persistence requirement and separate enter/exit thresholds:

```python
# Hysteresis settings
regime_persistence_days = 3  # Days before regime change
vix_enter_elevated = 25  # VIX to enter ELEVATED
vix_exit_elevated = 20   # VIX to exit ELEVATED (different threshold!)
vix_enter_crisis = 40
vix_exit_crisis = 35

def detect_regime(self, vix_level, spread_momentum, current_drawdown) -> RiskRegime:
    raw_regime = self._detect_raw_regime(vix_level, spread_momentum, current_drawdown)

    # Exception: Always enter CRISIS immediately (no delay)
    if raw_regime == RiskRegime.CRISIS:
        self._current_regime = RiskRegime.CRISIS
        return RiskRegime.CRISIS

    # Track pending regime changes
    if raw_regime != self._current_regime:
        if raw_regime == self._pending_regime:
            self._pending_regime_days += 1
        else:
            self._pending_regime = raw_regime
            self._pending_regime_days = 1

        # Only switch after N days persistence
        if self._pending_regime_days >= self.regime_persistence_days:
            self._current_regime = self._pending_regime

    return self._current_regime
```

### Phase 8: Emergency De-Risk State Machine

**Problem:** Multiplicative scaling (0.25 × 0.30 × 0.75) caused extreme over-reduction.

**Solution:** Explicit state machine with fixed scaling factors:

```python
class RiskState(Enum):
    NORMAL = 1.0    # Full target allocation
    ELEVATED = 0.7  # 30% reduction
    CRISIS = 0.3    # 70% reduction

def get_risk_state_scaling(self) -> float:
    """Returns 1.0, 0.7, or 0.3 - no multiplication."""
    return self._risk_state.value

def update_risk_state(self, regime: RiskRegime, current_drawdown: float) -> RiskState:
    # Drawdown floor overrides regime
    if current_drawdown <= -self.max_drawdown_pct:
        self._risk_state = RiskState.CRISIS
        return self._risk_state

    # Map regime to state
    if regime == RiskRegime.CRISIS:
        self._risk_state = RiskState.CRISIS
    elif regime == RiskRegime.ELEVATED:
        self._risk_state = RiskState.ELEVATED
    else:
        self._risk_state = RiskState.NORMAL

    return self._risk_state
```

### Phase 9: Execution Safety Guards

**Problem:** Orders could execute with stale data or failed reconciliation.

**Solution:** Pre-execution safety checks:

```python
def check_execution_safety(
    portfolio_state: PortfolioState,
    fx_rates_valid: bool = True,
    vol_estimate_valid: bool = True,
    exchange: str = "US"
) -> Tuple[bool, List[str]]:
    reasons = []
    safe = True

    # Check 1: NAV reconciliation must pass
    if not portfolio_state.can_trade():
        safe = False
        reasons.append(f"NAV reconciliation failed: {portfolio_state.reconciliation_status}")

    # Check 2: FX rates must be valid
    if not fx_rates_valid:
        safe = False
        reasons.append("FX rates are stale or invalid")

    # Check 3: Volatility estimate must be valid
    if not vol_estimate_valid:
        safe = False
        reasons.append("Volatility estimate is invalid")

    # Check 4: No market orders in first 15 min after open
    if is_near_market_open(exchange):
        reasons.append("Warning: Near market open - use limit orders")

    return safe, reasons
```

### Phase 10: Mandatory Unit Tests

All changes covered by comprehensive tests in `tests/test_engine_fixes.py`:

```
tests/test_engine_fixes.py::TestFXRates - 8 tests
tests/test_engine_fixes.py::TestNAVvsExposure - 3 tests
tests/test_engine_fixes.py::TestBrokerReconciliation - 4 tests
tests/test_engine_fixes.py::TestCurrencyCorrectSizing - 2 tests
tests/test_engine_fixes.py::TestFXHedging - 3 tests
tests/test_engine_fixes.py::TestVolatilityTargeting - 3 tests
tests/test_engine_fixes.py::TestRegimeHysteresis - 3 tests
tests/test_engine_fixes.py::TestRiskStateMachine - 4 tests
tests/test_engine_fixes.py::TestExecutionSafety - 3 tests
tests/test_engine_fixes.py::TestExposureCalculation - 2 tests
tests/test_engine_fixes.py::TestIntegration - 2 tests

Total: 39 tests, all passing
```

### Scheduler Integration (Complete)

All ENGINE_FIX_PLAN phases are now fully integrated into `src/scheduler.py`:

| Phase | Function | Integration Point |
|-------|----------|-------------------|
| Phase 1 | FX Rates Refresh | `_refresh_fx_rates()` called in `run_daily()` |
| Phase 2 | NAV/Exposure with FX | `compute_nav(fx_rates=...)`, `compute_exposures(fx_rates=...)` |
| Phase 3 | Broker Reconciliation | `_reconcile_with_broker()` with HALT/EMERGENCY circuit breaker |
| Phase 4 | Currency Sizing | `_compute_strategy_targets(fx_rates=...)` |
| Phase 5 | Portfolio FX Hedging | Integrated via `compute_all_sleeve_targets()` |
| Phase 6-8 | Risk Engine | `_compute_risk()` now uses EWMA, hysteresis, state machine |
| Phase 9 | Execution Safety | `check_execution_safety()` called in `_execute_orders()` |

New `settings.yaml` configuration sections:
- `fx:` - Base currency, staleness thresholds
- `reconciliation:` - HALT/EMERGENCY thresholds
- `volatility:` - Floor, cap, EWMA weight
- `hysteresis:` - Persistence days, enter/exit thresholds
- `risk_states:` - Scaling factors per state
- `execution_safety:` - Max order size, turnover limits

---

## 11.5. Execution Stack Upgrade - Alpha Capture & Slippage Reduction

A comprehensive execution layer upgrade to capture "execution alpha" by reducing slippage, avoiding legging risk, and controlling partial fills.

### New Module: `src/execution/`

```
src/execution/
  __init__.py          # Package exports
  types.py             # MarketDataSnapshot, OrderIntent, OrderPlan, OrderTicket
  policy.py            # ExecutionPolicy - order parameterization
  order_manager.py     # OrderManager - state machine for order lifecycle
  basket.py            # BasketExecutor - trade netting, priority ordering
  pair.py              # PairExecutor - legging protection
  slippage.py          # Slippage models, collar enforcement, tracking
  analytics.py         # ExecutionAnalytics - metrics recording
  calendars.py         # MarketCalendar - session timing
  liquidity.py         # LiquidityEstimator - ADV, size thresholds
```

### Key Design Principles

1. **No Market Orders**: All orders are marketable limits with price collars
2. **Hard Collars**: Max slippage enforced (10 bps ETF, 3 bps FUT, 2 bps FX)
3. **TTL + Replace**: Orders expire after 120s, reprice every 15s up to 6 attempts
4. **Trade Netting**: Aggregate trades across sleeves before execution
5. **Legging Protection**: Temporary hedge if one leg fills >30% while other is 0%

### ExecutionPolicy Flow

```python
# Convert strategy intent to executable plan
intent = OrderIntent(
    instrument_id="CSPX",
    side="BUY",
    quantity=100,
    reason="rebalance",
    sleeve="core_index_rv",
    urgency=Urgency.NORMAL,
)

# Policy generates plan based on market data
plan, warning = policy.create_plan(intent, market_data)

# Plan includes:
# - order_type: LMT (never MKT unless allowed)
# - limit_price: marketable limit with collar
# - ttl_seconds: 120
# - replace_interval_seconds: 15
# - price_ceiling / price_floor: hard limits
```

### Marketable Limit Pricing

```python
def marketable_limit_price(md: MarketDataSnapshot, side: str, max_slip_bps: float) -> float:
    ref = md.mid or md.last or md.close
    max_slip = max_slip_bps / 10_000.0

    if md.has_quotes():
        micro_buffer = md.spread * 0.25
        if side == "BUY":
            return min(md.ask + micro_buffer, ref * (1.0 + max_slip))
        else:
            return max(md.bid - micro_buffer, ref * (1.0 - max_slip))
    else:
        # No quotes - use reference with collar
        if side == "BUY":
            return ref * (1.0 + max_slip)
        else:
            return ref * (1.0 - max_slip)
```

### Trade Netting Example

```
Before netting (4 orders):
  Sleeve A: BUY 100 CSPX
  Sleeve B: SELL 40 CSPX
  Sleeve A: SELL 200 CS51
  Sleeve B: SELL 50 CS51

After netting (2 orders):
  BUY 60 CSPX  (100 - 40)
  SELL 250 CS51 (200 + 50)

Savings: 80 shares of turnover avoided
```

### Legging Protection

For paired trades (e.g., US long + EU short):

1. Submit both legs concurrently
2. Monitor fill percentages
3. If one leg fills >30% while other is 0%:
   - Deploy temporary hedge (index future)
   - Aggressively reprice lagging leg
4. Unwind hedge after both legs filled

### Session Timing

```python
# Avoid trading near open/close
avoid_first_minutes_after_open: 15
avoid_last_minutes_before_close: 10

# Use closing auction for daily rebalance
default_policy: "auction_close"  # LOC orders
```

### Execution Analytics (Daily Summary)

```
📊 Execution Summary - 2025-12-15

Orders: 12 total, 11 filled
Traded: $1,250,000

Costs:
  Slippage: 4.2 bps avg ($525)
  Commission: $35

Worst execution:
  IUKD: 12.3 bps on 500 shares

Netting saved: 18% of turnover
```

### Configuration (`settings.yaml`)

```yaml
execution:
  default_policy: "marketable_limit"
  allow_market_orders: false
  order_ttl_seconds: 120
  replace_interval_seconds: 15
  max_replace_attempts: 6
  default_max_slippage_bps: 10
  max_slippage_bps_by_asset_class:
    ETF: 10
    STK: 12
    FUT: 3
    FX_FUT: 2
  min_trade_notional_usd: 2500
  pair_max_legging_seconds: 60
  pair_hedge_enabled: true
  adv_fraction_threshold: 0.01
  max_participation_rate: 0.10
  avoid_first_minutes_after_open: 15
  avoid_last_minutes_before_close: 10
```

### Test Coverage (25 tests)

```
tests/test_execution.py::TestMarketableLimit - 6 tests
tests/test_execution.py::TestOrderManagerStateMachine - 4 tests
tests/test_execution.py::TestBasketNetting - 5 tests
tests/test_execution.py::TestSlippageCalculation - 4 tests
tests/test_execution.py::TestSlippageTracker - 1 test
tests/test_execution.py::TestMarketCalendar - 3 tests
tests/test_execution.py::TestPairLegging - 2 tests

Total: 25 tests, all passing
```

### Scheduler Integration

The execution stack is integrated into `scheduler.py` via `_execute_orders_new_stack()`:

**Initialization (`scheduler.py:initialize()`):**
```python
# Load config from settings.yaml
self.execution_config = load_execution_config(self.settings)

# Initialize components
self.ibkr_transport = IBKRTransport(ib_client, instruments)
self.execution_policy = ExecutionPolicy(self.execution_config)
self.execution_analytics = ExecutionAnalytics()
self.liquidity_estimator = get_liquidity_estimator()
self.market_calendar = get_market_calendar()
self.order_manager = OrderManager(
    transport=self.ibkr_transport,
    policy=self.execution_policy,
    on_fill=on_fill,
    on_complete=on_complete,
)
self.basket_executor = BasketExecutor(min_trade_notional=2500)
self.pair_executor = PairExecutor(
    order_manager=self.order_manager,
    max_legging_seconds=60,
    hedge_trigger_fill_pct=0.30,
)
```

**Execution Flow (`_execute_orders_new_stack()`):**
1. **Session timing check** - Skip if too close to open/close
2. **Convert OrderSpec → OrderIntent** - Prepare for execution stack
3. **Trade netting via BasketExecutor** - Reduce turnover
4. **Get ADV from LiquidityEstimator** - For slicing decisions
5. **Get session phase from MarketCalendar** - For policy selection
6. **Create OrderPlan via ExecutionPolicy.create_plan()** - Parameterize order
7. **Submit via OrderManager** - Track lifecycle
8. **Polling loop** - Process every 5s, handle TTL/replace
9. **Record metrics in ExecutionAnalytics** - Via on_complete callback
10. **Finalize analytics** - Generate daily summary

**Callbacks:**
```python
def on_fill(ticket):
    # Log fill progress during order lifecycle
    logger.info("order_fill", extra={...})

def on_complete(result):
    # Record in ExecutionAnalytics for reporting
    self.execution_analytics.record_order_complete(result, asset_class)
```

**IBKRTransport (`execution_ibkr.py`):**

Implements `BrokerTransport` interface for IBKR communication:
- `submit_order()` - Place order via ib_insync
- `cancel_order()` - Cancel active order
- `modify_order()` - Cancel/replace for limit price updates
- `get_order_status()` - Return `OrderUpdate` with fill info
- `get_market_data()` - Return `MarketDataSnapshot` for pricing

---

## 12. Known Issues & Recent Fixes

### Fixed (December 2025)

1. **M6E Futures Price Bug**
   - Issue: avgCost stored pre-multiplied ($14,648 instead of $1.17)
   - Fix: Normalize avgCost by dividing by multiplier for futures
   - Impact: NAV was showing -$700M instead of ~$12M

2. **IUKD GBP Pence Bug**
   - Issue: IBKR returned price in pence (895) instead of pounds (8.95)
   - Fix: Convert prices > 100 for GBP instruments on LSE
   - Impact: Short exposure was overstated by 100x

3. **Futures ID Mismatch**
   - Issue: Position ID `M6E_20251215` didn't match config `M6E`
   - Fix: Added `_get_instrument_spec()` to match by base symbol

4. **ENGINE_FIX_PLAN Implementation**
   - 10-phase comprehensive refactor completed
   - See Section 11 for full details

---

## 13. Configuration Files

### `config/settings.yaml`

```yaml
mode: "paper"

# Risk
vol_target_annual: 0.12
gross_leverage_max: 2.0
net_leverage_max: 1.0
max_drawdown_pct: 0.10

# Sleeves
sleeves:
  core_index_rv: 0.35
  sector_rv: 0.25
  single_name: 0.15
  credit_carry: 0.15
  crisis_alpha: 0.05
  cash_buffer: 0.05

# Momentum
momentum:
  short_window_days: 50
  long_window_days: 200
  regime_reduce_factor: 0.5

# Crisis
crisis:
  vix_threshold: 40
  pnl_spike_threshold_pct: 0.10
```

### `config/instruments.yaml`

76 instruments across 14 categories including:
- Equity indices (ES, MES, FESX, CSPX, CS51)
- FX futures (M6E, 6E, EUR.USD)
- Sector ETFs (IUIT, CNDX, SEMI, IUHC, EXV1, EXS1, IUKD)
- Credit (LQDE, IHYU, FLOT, ARCC)
- Volatility (VX, FVS)
- Sovereign bonds (FGBL, FOAT, FBTP)

---

## 14. Questions for Sanity Check

1. **Position Sizing:** Is the volatility-targeting approach (scaling inversely to realized vol) sound?

2. **Regime Detection:** Are the VIX/drawdown thresholds appropriate for regime classification?

3. **FX Hedging:** Is hedging the EUR notional of short EU positions via M6E futures correct?

4. **NAV Calculation:** Is `NAV = Cash + Σ(quantity × price × multiplier)` correct for a long/short portfolio?

5. **Drawdown Formula:** Is `(current - peak) / peak` the standard approach?

6. **Futures Multiplier Handling:** Should avgCost be stored normalized (per-unit) or raw (per-contract)?

7. **GBP/GBX Conversion:** Is the heuristic of "price > 100 = pence" reasonable for LSE ETFs?

8. **Maintenance Windows:** Is skipping orders during IBKR maintenance the right approach?

9. **Emergency De-risk:** Is reducing to 25% at 10% drawdown too aggressive or appropriate?

10. **Sleeve Independence:** Should sleeves be truly independent or share risk budget?

---

*Document generated: 2025-12-15*
*Codebase version: main branch*
*ENGINE_FIX_PLAN: All 10 phases implemented with 39 passing tests*
