# AbstractFinance Investment Strategy

## "Insurance for Europeans" - European Decline Macro Fund

**Version:** 2.0 (Strategy Evolution)
**Last Updated:** December 2025
**Status:** Paper Trading (Staging)

---

## Executive Summary

AbstractFinance implements a systematic macro strategy designed to provide European investors with:

1. **Decent risk-adjusted returns** in normal market conditions (~8-12% CAGR target)
2. **Asymmetric payoff during European stress** - the portfolio should profit when Europe declines
3. **Convex protection** via options structures that pay off in crisis scenarios

The core thesis: Europe faces structural headwinds (demographics, energy dependence, regulatory burden, banking fragility) that will manifest as periodic stress events. This strategy monetizes that view while maintaining positive expected returns in normal years.

---

## Strategy Architecture

### Investment Channels (Three Pillars)

| Channel | Description | Target Allocation |
|---------|-------------|-------------------|
| **(A) Equity Relative Value** | Long US / Short EU equities | 50% of risk |
| **(B) FX Structural** | USD strength vs EUR in stress | 10-15% of risk |
| **(C) Europe Vol Convexity** | VSTOXX calls + SX5E put spreads | 25% of risk |

The key insight: **Equity L/S alone won't reliably pay off in stress** because US/EU correlation spikes to ~0.95 during crises. The strategy therefore anchors insurance on **(C) Europe Vol Convexity**, lets **(B) FX** run as a secondary channel, and treats **(A) Equity RV** as alpha generation, not insurance.

---

## Sleeve Breakdown

### Current Allocation (Strategy Evolution v2.0)

```
Sleeve Weights (% of NAV at full scaling):
├── Core Index RV:       20%  (reduced from 35%)
├── Sector RV:           20%  (factor-neutral)
├── Single Name L/S:     10%  (trend-gated)
├── Credit & Carry:      15%  (regime-adaptive)
├── Europe Vol Convex:   15%  (NEW - primary insurance)
├── Crisis Alpha:        10%  (increased from 5%)
└── Cash Buffer:         10%  (safety margin)
```

### Sleeve Details

#### 1. Core Index RV (20%)
- **Long:** iShares Core S&P 500 UCITS ETF (CSPX)
- **Short:** iShares Core Euro STOXX 50 UCITS ETF (CS51)
- **Hedge:** Portfolio-level FX hedging via M6E micro futures
- **Trend-Gated:** Position scales 0-100% based on US/EU relative momentum

#### 2. Sector RV (20%)
- **Long US:** Technology (IUIT), NASDAQ (CNDX), Semiconductors (SEMI), Healthcare (IUHC), Quality Factor (IUQA)
- **Short EU:** Banks (EXV1), DAX (EXS1), UK Dividend (IUKD)
- **Factor-Neutral:** Beta and value/growth exposure neutralized
- **Trend-Gated:** Same filter as Core Index RV

#### 3. Single Name L/S (10%)
- **Long US:** Quality growth stocks (AAPL, MSFT, GOOGL, NVDA, etc.)
- **Short EU:** "Zombie" companies with weak fundamentals
- **Quantitative Screening:** Factor-based selection (Quality 50%, Momentum 30%, Size 20%)
- **Trend-Gated:** Disabled when US/EU momentum very negative

#### 4. Credit & Carry (15%)
- **Long US Credit:** IG (LQDE), HY (IHYU), Floating Rate (FLOT), BDCs (ARCC)
- **Regime-Adaptive:** Reduced in ELEVATED/CRISIS regimes
- **Carry Target:** ~7% annual from credit spread + financing

#### 5. Europe Vol Convexity (15%) - NEW
Primary insurance channel using VSTOXX and SX5E structures:

| Structure | Allocation | Description |
|-----------|------------|-------------|
| VSTOXX Call Spreads | 50% | Buy +5pt OTM calls, sell +15pt (cap upside, reduce premium) |
| SX5E Put Spreads | 35% | Buy 10% OTM puts, sell 15% OTM (1x2 ratio spreads) |
| EU Banks Puts | 15% | SX7E or EXV1 puts for financial stress |

- **Target DTE:** 60-90 days
- **Roll:** At 21-30 DTE
- **Premium Budget:** 2.5% NAV annually

#### 6. Crisis Alpha (10%)
Secondary insurance (US-focused, complements Europe Vol):

| Hedge Type | Allocation | Instrument |
|------------|------------|------------|
| VSTOXX Calls | 30% | FVS options (EUREX) |
| SX5E Puts | 20% | Euro STOXX 50 options |
| EU Bank Puts | 10% | SX7E options |
| VIX Calls | 15% | VIX index options |
| SPY Puts | 10% | S&P 500 ETF options |
| Sovereign Spread | 10% | Short FOAT / Long FGBL |

#### 7. Cash Buffer (10%)
- Margin for futures positions
- Dry powder for crisis deployment
- Financing cost optimization

---

## Risk Management

### Regime Detection (Europe-First)

The strategy uses a multi-factor stress score to determine regime:

```
stress_score = 0.4 * V2X_component + 0.3 * VIX_component
             + 0.2 * EURUSD_trend + 0.1 * drawdown_component

Where:
  V2X_component = clip((V2X - 20) / 20, 0, 1)
  VIX_component = clip((VIX - 20) / 25, 0, 1)
  EURUSD_trend = clip(-annualized_trend / 0.10, 0, 1)
  drawdown_component = clip(-drawdown / 0.10, 0, 1)
```

| Regime | Stress Score | Scaling | Actions |
|--------|--------------|---------|---------|
| NORMAL | < 0.3 | 100% | Full positioning |
| ELEVATED | 0.3 - 0.6 | 70% | Reduce equity, increase hedges |
| CRISIS | > 0.6 | 30% | Minimal equity, max hedges, no FX hedge |

### Trend Filter (Prevents Thesis Bleed)

Equity L/S sleeves are gated by US/EU relative momentum:

```python
momentum = US_return_60d - EU_return_60d

if momentum >= +2%:    sizing = 100%  (thesis working)
if momentum <= -5%:    sizing = 25%   (thesis challenged)
if momentum <= -10%:   sizing = 0%    (options only mode)
```

This prevents the strategy from bleeding during cyclical EU outperformance periods.

### FX Hedge Policy

| Mode | Residual FX Exposure | When Used |
|------|---------------------|-----------|
| FULL | < 2% of NAV | Rarely (pure RV) |
| PARTIAL | ~25% of NAV | NORMAL, ELEVATED regimes |
| NONE | 100% (no hedge) | CRISIS regime - let USD pay off |

### Execution Safety Guards

- Max single order: 10% of NAV
- Max daily turnover: 50%
- Min time between trades: 5 seconds
- Marketable limits only (no market orders)
- Hard slippage collars: 10bps ETF, 12bps STK, 3bps FUT, 2bps FX

### Circuit Breakers

| Threshold | Action |
|-----------|--------|
| NAV vs Broker NLV > 0.25% | HALT trading |
| NAV vs Broker NLV > 1% | EMERGENCY mode |
| Daily loss > 3% | Alert + manual review |
| Hedge budget > 90% used | Alert |

---

## Backtest Results

### Period: January 2010 - December 2025

#### Summary Metrics

| Metric | Original Strategy | Evolved Strategy | Change |
|--------|-------------------|------------------|--------|
| **Total Return** | 311.5% | 674.8% | +117% |
| **CAGR** | 9.3% | 13.7% | +47% |
| **Sharpe Ratio** | 1.36 | 3.41* | +151% |
| **Sortino Ratio** | 2.07 | 5.29 | +156% |
| **Max Drawdown** | -11.6% | -6.8% | +41% better |
| **Calmar Ratio** | 0.80 | 2.02 | +153% |
| **Realized Vol** | 6.7% | 3.8% | Lower risk |
| **Insurance Score** | +4.5% | +22.5% | **5x better** |

*Note: Backtest Sharpe is optimistic. Expect ~1.5-2.0 in live trading due to execution costs and liquidity constraints.

#### Stress Period Performance

| Crisis | Period | Original | Evolved | Improvement |
|--------|--------|----------|---------|-------------|
| **Euro Crisis 2011** | Jul-Dec 2011 | +20.9% | +32.8% | +57% |
| **COVID Crash** | Feb-Apr 2020 | +5.6% | +9.6% | +71% |
| **Rate Shock 2022** | Jan-Oct 2022 | +22.5% | +43.7% | +94% |

#### Stress Period Details

**Euro Crisis 2011:**
- Total Return: +32.8%
- Max Drawdown: -1.9%
- Hedge Payoff: +26.6%
- *Strategy profited from EU banking stress and EUR weakness*

**COVID 2020:**
- Total Return: +9.6%
- Max Drawdown: -0.4%
- Hedge Payoff: +9.2%
- *Vol convexity paid off during global panic*

**Rate Shock 2022:**
- Total Return: +43.7%
- Max Drawdown: -1.6%
- Hedge Payoff: +36.4%
- *EUR weakness + EU growth concerns drove strong returns*

#### Cost Analysis (15-year period)

| Cost Category | Amount | % of Final NAV |
|---------------|--------|----------------|
| Transaction Costs | $285,529 | 3.7% |
| Carry Costs | $144,754 | 1.9% |
| **Total Costs** | $430,283 | 5.6% |

Average daily turnover: 4.6%

#### Risk Metrics

| Metric | Value |
|--------|-------|
| VaR (95%) | -0.31% daily |
| VaR (99%) | -0.57% daily |
| Expected Shortfall | -0.47% daily |
| Downside Vol | 2.4% annualized |

---

## Implementation Details

### Instrument Universe

#### Equity Indices (UCITS ETFs for EU PRIIPs compliance)
- US: CSPX (iShares S&P 500), CNDX (NASDAQ 100)
- EU: CS51 (Euro STOXX 50), SMEA (MSCI Europe)

#### Volatility Products
- VSTOXX Mini Future (FVS) - EUREX
- VSTOXX Options on Futures - EUREX
- VIX Future (VX) - CFE
- VIX Options - CBOE

#### Europe Vol Structures
- SX5E Index Options - EUREX (multiplier: 10)
- SX5E Mini Options (OXXP) - EUREX (multiplier: 1)
- DAX Options - EUREX (multiplier: 5)
- SX7E (Euro Banks) Options - EUREX

#### FX Hedging
- EUR/USD Micro Futures (M6E) - CME
- GBP/USD Micro Futures (M6B) - CME

#### Sovereign Futures
- Euro-Bund (FGBL) - EUREX (long for hedge)
- French OAT (FOAT) - EUREX (short for spread)

### Execution Stack

1. **Session-Aware Scheduling:** Trades execute during optimal windows (avoid first 15min, last 10min)
2. **Marketable Limits:** All orders use limit prices at bid/ask, never market orders
3. **Pair Execution:** Legs protected with temporary hedges if one side fills first
4. **Cost Gating:** Trade only if expected alpha > 1.5x predicted cost
5. **Self-Tuning Slippage:** Model calibrated on last 200 trades

### Exactly-Once Execution

SQLite-backed run ledger ensures:
- No duplicate orders on restart
- Deterministic client order IDs
- Full audit trail of all trading decisions

---

## Configuration Reference

### Key Settings (settings.yaml)

```yaml
# Risk Parameters
vol_target_annual: 0.12
gross_leverage_max: 2.0
max_drawdown_pct: 0.10

# Sleeve Weights (Strategy Evolution v2.0)
sleeves:
  core_index_rv: 0.20
  sector_rv: 0.20
  single_name: 0.10
  credit_carry: 0.15
  europe_vol_convex: 0.15
  crisis_alpha: 0.10
  cash_buffer: 0.10

# Trend Filter
trend_filter:
  enabled: true
  short_lookback_days: 60
  long_lookback_days: 252
  positive_momentum_threshold: 0.02
  negative_momentum_threshold: -0.05
  options_only_threshold: -0.10

# Europe Vol Convexity
europe_vol_convex:
  vstoxx_calls_pct: 0.50
  sx5e_puts_pct: 0.35
  eu_banks_puts_pct: 0.15

# FX Hedge Policy
fx_hedge:
  mode: "PARTIAL"
  target_residual_pct_nav:
    FULL: 0.02
    PARTIAL: 0.25
    NONE: 1.00
  regime_overrides:
    NORMAL: "PARTIAL"
    ELEVATED: "PARTIAL"
    CRISIS: "NONE"

# Europe-First Regime Detection
europe_regime:
  v2x_weight: 0.4
  vix_weight: 0.3
  eurusd_trend_weight: 0.2
  drawdown_weight: 0.1
```

---

## Risk Warnings

### Strategy Risks

1. **Thesis Risk:** Europe may outperform US for extended periods (trend filter mitigates but doesn't eliminate)
2. **Correlation Risk:** US/EU correlation may stay high even outside crisis periods
3. **Vol Premium Decay:** Option structures have negative expected value in normal times
4. **Liquidity Risk:** VSTOXX options less liquid than VIX options
5. **Execution Risk:** Slippage on options can be significant

### Operational Risks

1. **IBKR Connection:** Single broker dependency
2. **Data Quality:** Yahoo Finance fallback for research data
3. **FX Conversion:** Errors can compound across multi-currency positions

### What Could Go Wrong

| Scenario | Impact | Mitigation |
|----------|--------|------------|
| US crashes harder than EU | Equity L/S loses, but vol convexity pays | Size equity L/S conservatively |
| EUR strengthens significantly | FX channel loses | Trend filter cuts exposure |
| Extended low vol period | Option decay without payoff | Spread structures reduce bleed |
| VSTOXX illiquidity in crisis | Can't exit at fair value | Use exchange-traded structures only |

---

## Appendix: File Structure

```
AbstractFinance/
├── config/
│   ├── settings.yaml          # All strategy parameters
│   └── instruments.yaml       # Tradable instruments
├── src/
│   ├── strategy_logic.py      # Sleeve construction + trend filter
│   ├── tail_hedge.py          # Europe-centric crisis alpha
│   ├── risk_engine.py         # Regime detection + scaling
│   ├── scheduler.py           # Daily orchestration
│   ├── execution/             # Order execution stack
│   ├── marketdata/            # Live data feeds (IBKR)
│   ├── research/
│   │   └── backtest.py        # Historical validation
│   └── state/
│       └── run_ledger.py      # Exactly-once execution
├── tests/
│   └── test_roadmap_features.py
└── docs/
    ├── ROADMAP.md
    └── TRADING_ENGINE_ARCHITECTURE.md
```

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | Nov 2025 | Initial implementation |
| 1.1 | Dec 2025 | ENGINE_FIX_PLAN (FX, risk scaling, reconciliation) |
| 1.2 | Dec 2025 | Execution Stack Upgrade (slippage, cost gating) |
| **2.0** | Dec 2025 | **Strategy Evolution** (Europe-centric, trend filter, vol convexity) |

---

*Document generated: December 2025*
*Strategy validated via backtest 2010-2025*
*Paper trading on staging server: 94.130.228.55*
