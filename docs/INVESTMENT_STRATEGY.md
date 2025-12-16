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

## Design Rationale: Why Each Sleeve Exists

This section explains the **reasoning** behind each design choice. Every sleeve has a specific job, and understanding these jobs prevents dangerous modifications.

### The Core Problem: Crash Correlation

**Why can't we just go long US / short EU and collect insurance payoff?**

During normal markets, US and EU equities have ~0.70 correlation. But in a crash, correlation spikes to **~0.95**. This means:

- If SX5E drops 30%, SPY likely drops 25%
- Your "long US / short EU" trade might make 5% on a massive move
- That's not insurance—that's a small alpha bet

**The key insight:** Equity L/S is an alpha-generation strategy, NOT an insurance strategy. We need *something else* to provide convex payoff in stress.

### Why We Changed Core Index RV: 35% → 20%

**Old thinking:** "More equity L/S = more exposure to US outperformance thesis"

**New thinking:** Core Index RV's job is to express a *directional view* on US vs EU at the index level. It's clean, liquid, and low-cost. But it has two problems:

1. **Crash correlation:** Won't reliably pay off when we need it most
2. **Thesis bleed:** When EU cyclically outperforms, this sleeve just bleeds

We reduced it from 35% to 20% because we realized it's an **alpha sleeve, not an insurance sleeve**. The capital freed up went to Europe Vol Convexity—which *does* pay off in stress.

### Why Sector RV is Factor-Neutral (Not Just "More Equity L/S")

This is the most misunderstood design choice. Let's be precise:

**What "reducing Core Index RV" does:**
- Less notional in CSPX long / CS51 short
- Less total US vs EU bet
- Still a clean regional exposure

**What "factor-neutral Sector RV" does:**
- Same amount of regional exposure
- BUT removes hidden factor bets (growth vs value, duration sensitivity)
- Isolates the *regional* signal from *style* signals

**Why this matters—a concrete example:**

Suppose EU banks rally 40% in a risk-on move. Your Sector RV sleeve is:
- Long US Tech (growth, high duration)
- Short EU Banks (value, low duration, financial sector)

Without factor neutralization, you're actually running:
- Long growth, short value
- Long duration, short duration
- Long US, short EU
- Long tech, short financials

When EU banks rally, you lose money. But is that loss from your *regional thesis* or from your hidden *value vs growth* bet? You can't tell.

**The sanity check question:** "If European banks rally 40% in a cyclical upturn, should your fund lose money?"

- If the answer is "yes, I want that short-banks exposure" → leave it
- If the answer is "no, I'm supposed to be long US vs EU, not short banks specifically" → factor-neutralize

We chose factor-neutral because our thesis is "US outperforms EU" not "growth outperforms value" or "tech outperforms financials."

### Why Europe Vol Convexity is the PRIMARY Insurance Channel

**The problem it solves:** Equity L/S doesn't provide reliable crisis payoff due to crash correlation.

**The solution:** Buy volatility directly. When European stress hits:
- VSTOXX spikes from 18 → 45+ (150%+ move)
- SX5E puts go from 2% to 15%+ of notional (7x+ payoff)
- EU bank vol explodes (banking crises are *European* crises)

This sleeve has **negative expected value** in normal times (options decay). But it has **massive positive expected value** conditional on European stress. That's the definition of insurance.

**Why VSTOXX over VIX?**
- We're insuring against *European* decline
- VSTOXX rises more than VIX in EU-specific stress (Euro crisis 2011, EU energy crisis 2022)
- VIX rises more in global stress—but we're not insuring against US problems

### Why Crisis Alpha is Europe-Centric (60% EU / 25% US)

**Old allocation:** ~50% VIX, ~50% SPY puts

**New allocation:** 60% Europe (VSTOXX 30%, SX5E 20%, EU banks 10%), 25% US (VIX 15%, SPY 10%)

**The reasoning:**

If we're building "Insurance for Europeans," we need to ask: what are we insuring against?

1. **EU banking crisis** → VSTOXX spikes, SX7E collapses, EUR weakens
2. **EU sovereign crisis** → Bund/OAT spreads widen, VSTOXX spikes
3. **EU energy crisis** → EUR weakens, European equities collapse
4. **Global risk-off** → VIX spikes, all equities fall

Three of these four scenarios are Europe-specific. The Crisis Alpha sleeve should reflect that. VIX calls are still useful (scenario 4), but they shouldn't dominate.

### Why Trend Filter Exists (Preventing Thesis Bleed)

**The problem:** Our thesis is "US outperforms EU over the cycle." But cycles exist. EU can outperform US for 6-18 months during:
- Value rotations
- Dollar weakness periods
- European cyclical recoveries

During these periods, all three equity sleeves (Core RV, Sector RV, Single Name) bleed. This is expected—but we don't want to bleed at full size.

**The solution:** Scale equity sleeve sizing based on 60-day US vs EU relative momentum:

| Momentum | Sizing | Interpretation |
|----------|--------|----------------|
| >= +2% | 100% | Thesis working, full size |
| 0% to +2% | 75% | Neutral, slightly reduced |
| -5% to 0% | 50% | Thesis challenged, half size |
| <= -5% | 25% | Thesis failing, minimal equity |
| <= -10% | 0% | Options only mode |

This isn't market timing—it's *thesis monitoring*. If US vs EU momentum is deeply negative, either:
1. Our thesis is wrong (temporarily or permanently)
2. We're in a cyclical counter-move

Either way, we should reduce equity L/S and let the options carry the insurance load.

### Why Credit & Carry is Regime-Adaptive

**The job:** Generate steady ~7% annual carry from credit spreads and financing.

**The problem:** Credit spreads blow out in stress. If we hold full credit exposure into a crisis, we give back months of carry in days.

**The solution:** Regime-adaptive scaling:
- NORMAL: 100% allocation
- ELEVATED: 70% allocation (trimming)
- CRISIS: 30% allocation (minimal, locked in)

This isn't perfect—we'll miss some spread compression during recoveries. But the asymmetry is favorable: missing 2% upside is better than eating 8% downside.

### Why FX Hedge is PARTIAL (Not FULL)

**Old thinking:** "FX adds noise, hedge it all away"

**New thinking:** USD strength vs EUR is *part of our thesis*. In European stress:
- EUR weakens as capital flees Europe
- Our USD-denominated longs appreciate in EUR terms
- This is free insurance

FULL hedge mode (< 2% residual FX) removes this channel. We use PARTIAL (25% residual) in normal regimes to capture some EUR weakness upside.

**In CRISIS regime:** We switch to NO hedge (100% USD exposure). When Europe is in crisis, we want maximum USD payoff.

### Why Cash Buffer is 10% (Not 0%)

**Three purposes:**
1. **Margin buffer:** Futures and options require margin. 10% cash prevents margin calls during vol spikes.
2. **Dry powder:** In a crisis, we want capital to deploy into dislocated assets.
3. **Rebalancing liquidity:** Avoids forced selling during rebalances.

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
