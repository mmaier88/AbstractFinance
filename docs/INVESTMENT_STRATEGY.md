# AbstractFinance Investment Strategy

## "Insurance for Europeans" - European Decline Macro Fund

**Version:** 2.2 (Portfolio Simplification)
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

Suppose EU banks rally 40% in a risk-on move. Your old Sector RV sleeve was:
- Long US Tech (growth, high duration)
- Short EU Banks (value, low duration, financial sector)

Without factor neutralization, you're actually running:
- Long growth, short value
- Long duration, short duration
- Long US, short EU
- Long tech, short financials

When EU banks rally, you lose money. But is that loss from your *regional thesis* or from your hidden *value vs growth* bet? You can't tell.

**The solution: Same-sector pairs**

New Sector RV uses matched sector pairs:
- US Financials (XLF) vs EU Banks (EXV1)
- US Tech (XLK) vs EU Tech (EXV3)
- US Industrials (XLI) vs EU Industrials (EXH1)
- US Healthcare (XLV) vs EU Healthcare (EXV4)

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

### ~~Why Crisis Alpha is Europe-Centric~~ (MERGED in v2.2)

**v2.2 Update:** Crisis Alpha has been merged into Europe Vol Convexity. Ablation analysis showed:
- crisis_alpha: -0.082 marginal Sharpe contribution
- europe_vol_convex: +0.368 marginal Sharpe contribution

Both sleeves traded overlapping instruments (VSTOXX calls, SX5E puts). The europe_vol_convex sleeve has better signal timing via term structure and vol-of-vol detection. Merging eliminates redundant complexity while preserving the Europe-centric insurance function.

The underlying thesis remains: we're insuring against European-specific scenarios (banking crisis, sovereign stress, energy shock), so our primary insurance channel should be VSTOXX and SX5E options, not VIX.

### Why Trend Filter Exists (Preventing Thesis Bleed)

**The problem:** Our thesis is "US outperforms EU over the cycle." But cycles exist. EU can outperform US for 6-18 months during:
- Value rotations
- Dollar weakness periods
- European cyclical recoveries

During these periods, equity sleeves (Core RV, Sector RV) bleed. This is expected—but we don't want to bleed at full size.

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

### Why Money Market is 34% (Not 0% or 10%)

**v2.2 Update:** Renamed from "Cash Buffer" and increased from 10% to 34%.

**Key insight:** "Cash" should never be idle. We invest in short-term money market funds (~4-5% annual return) instead of leaving cash uninvested.

**Four purposes:**
1. **Margin buffer:** Futures and options require margin. Substantial buffer prevents margin calls during vol spikes.
2. **Dry powder:** In a crisis, we have significant capital to deploy into dislocated assets.
3. **Rebalancing liquidity:** Avoids forced selling during rebalances.
4. **Return generation:** Money market funds earn ~4-5% vs 0% for idle cash.

**Why 34%?** The ablation analysis revealed single_name (-0.334 marginal Sharpe) and crisis_alpha (-0.082 marginal Sharpe) were actively hurting performance. Capital from these removed sleeves, plus credit_carry reduction, is better allocated to money market earning 4-5% than to strategies with negative contribution.

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

### Current Allocation (Portfolio Simplification v2.2)

```
Sleeve Weights (% of NAV at full scaling):
├── Core Index RV:       20%  (US vs EU index spread)
├── Sector RV:           20%  (factor-neutral same-sector pairs)
├── Europe Vol Convex:   18%  (PRIMARY insurance - absorbs crisis_alpha)
├── Credit & Carry:       8%  (NORMAL regime only)
└── Money Market:        34%  (short-term funds, not idle cash)
```

**v2.2 Changes (based on ablation analysis 2010-2024):**
- **REMOVED single_name:** -0.334 marginal Sharpe, -82% max DD (stock picking doesn't work)
- **REMOVED crisis_alpha:** Merged into europe_vol_convex (overlapping instruments)
- **REDUCED credit_carry:** 15% → 8%, gated to NORMAL regime only (negative insurance score)
- **INCREASED money_market:** 10% → 34% (invested in short-term funds, not idle)

### Sleeve Details

#### 1. Core Index RV (20%)
- **Long:** iShares Core S&P 500 UCITS ETF (CSPX)
- **Short:** iShares Core Euro STOXX 50 UCITS ETF (CS51)
- **Hedge:** Portfolio-level FX hedging via M6E micro futures
- **Trend-Gated:** Position scales 0-100% based on US/EU relative momentum

#### 2. Sector RV (20%) - Factor-Neutral Same-Sector Pairs
Matched sector pairs to isolate regional beta from style bets:

| Sector | US Long | EU Short | Beta Adjustment |
|--------|---------|----------|-----------------|
| Financials | XLF | EXV1 | 1.27x EU size |
| Technology | XLK | EXV3 | 0.92x EU size |
| Industrials | XLI | EXH1 | 1.04x EU size |
| Healthcare | XLV | EXV4 | 0.94x EU size |

- **Factor-Neutral:** Beta and value/growth exposure neutralized
- **Trend-Gated:** Same filter as Core Index RV

#### 3. Credit & Carry (8%) - NORMAL Regime Only
- **Long US Credit:** IG (LQDE), HY (IHYU), Floating Rate (FLOT), BDCs (ARCC)
- **NORMAL Regime Gate:** Only trades when regime == NORMAL (v2.2)
- **Rationale:** -0.55 insurance score means credit loses money in stress periods
- **Carry Target:** ~7% annual from credit spread + financing (when active)

**v2.2 Change:** Reduced from 15% to 8% and gated to NORMAL regime only. Ablation showed +0.023 marginal Sharpe but -0.55 insurance score - the sleeve hurts performance during the exact periods we need protection.

#### 4. Europe Vol Convexity (18%) - PRIMARY Insurance
Primary insurance channel using VSTOXX and SX5E structures:

| Structure | Allocation | Description |
|-----------|------------|-------------|
| VSTOXX Call Spreads | 50% | Buy +5pt OTM calls, sell +15pt (cap upside, reduce premium) |
| SX5E Put Spreads | 35% | Buy 10% OTM puts, sell 15% OTM (1x2 ratio spreads) |
| EU Banks Puts | 15% | SX7E or EXV1 puts for financial stress |

- **Target DTE:** 60-90 days
- **Roll:** At 21-30 DTE
- **Premium Budget:** 2.5% NAV annually

**NEW: Term Structure Signal**
- **Contango** (Front < Back): Vol is "cheap" → size up, use outrights
- **Backwardation** (Front > Back): Vol is "expensive" → size down, use spreads
- Z-score of term spread guides entry timing

**NEW: Vol-of-Vol Jump Detection**
- Detect large 1-3 day moves in V2X
- **Upward jump:** Monetize 30% of winners
- **Downward jump:** Add 20% on weakness (vol cheap)

#### 5. Money Market (34%)
**v2.2 Change:** Renamed from "Cash Buffer" and increased from 10% to 34%.

- **NOT idle cash:** Invested in short-term money market funds
- **Purpose 1:** Margin buffer for futures/options positions
- **Purpose 2:** Dry powder for crisis deployment
- **Purpose 3:** Generates ~4-5% annual return (vs 0% for idle cash)
- **Liquidity:** T+1 redemption for deployment

**Why 34%?** Capital freed from removing single_name (10%) and crisis_alpha (10%), plus reducing credit_carry (7%), redirected here. With 66% in active strategies and 34% in money market, the portfolio has substantial dry powder while still earning returns.

---

### Removed Sleeves (v2.2)

#### ~~Single Name L/S~~ (REMOVED)
**Ablation Results:** -0.334 marginal Sharpe, -82% max drawdown, -4.64 insurance score

**Why it failed:**
- Stock screening (quality/momentum/zombie) doesn't reliably generate alpha
- Individual stock variance overwhelms the signal
- Short EU "zombies" requires difficult stock borrow
- High execution costs on individual names

**Code removed:** `stock_screener.py` (589 lines)

#### ~~Crisis Alpha~~ (MERGED into Europe Vol Convex)
**Ablation Results:** -0.082 marginal Sharpe vs europe_vol_convex's +0.368

**Why merged:**
- Both sleeves traded overlapping instruments (VSTOXX, SX5E options)
- Crisis_alpha was a "shell" managed by TailHedgeManager
- Europe_vol_convex has better signal timing (term structure, vol-of-vol)
- Consolidation reduces complexity without losing functionality

---

## Europe Vol Convexity: Deep Dive

### VSTOXX Instruments (EUREX)

**VSTOXX Mini Futures (FVS)**
- Underlying: VSTOXX Index (30-day implied vol on EURO STOXX 50)
- Multiplier: EUR 100 per volatility point
- Tick size: 0.05 vol points (EUR 5)
- Expiries: Weekly and Monthly

**VSTOXX Options on Futures (OVS2)**
- Underlying: FVS futures (NOT the V2X index directly)
- Multiplier: EUR 100 per volatility point
- Settlement: Physical (into futures)
- Style: European
- Strike intervals: 0.5 vol points (near), 1.0 vol points (far)

### SX5E Instruments (EUREX)

**EURO STOXX 50 Index Options (OESX)**
- Underlying: EURO STOXX 50 Index
- Multiplier: EUR 10 per index point
- Settlement: Cash
- Style: European
- Strike intervals: 25 points (near), 50 points (far)

### Structure Selection by Vol Regime

| Vol Regime | V2X Level | Structure | Sizing |
|------------|-----------|-----------|--------|
| LOW | < 18 | Outrights | 130% (vol cheap) |
| NORMAL | 18-25 | Spreads | 100% |
| ELEVATED | 25-35 | Spreads + Tails | 120% |
| CRISIS | > 35 | Selective, monetize | 70% |

### Term Structure Trading Rules

```
term_spread = V2X_back - V2X_front

if term_spread > 0.5:    # Contango
    → Vol futures at premium = vol "cheap"
    → Size up, can use outrights
    → Good entry point

if term_spread < -0.5:   # Backwardation
    → Vol futures at discount = vol "expensive"
    → Size down, use spreads only
    → Wait for better entry
```

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

#### Summary Metrics Comparison

| Strategy | Total Return | CAGR | Sharpe | Max DD | Insurance |
|----------|-------------|------|--------|--------|-----------|
| v1.0 Original | 119% | 5.0% | 0.75 | -12.6% | -1.3% |
| v2.0 Evolved | 675% | 13.7% | 3.41 | -6.8% | +22.5% |
| v2.1 Aggressive | 1371% | 18.4% | 5.29 | -6.1% | +34.2% |

*Note: Backtest Sharpe is optimistic. Expect ~1.5-2.0 in live trading due to execution costs and liquidity constraints.*

**Key improvement:** Insurance score went from **-1.3%** (losing money on stress days) to **+22.5%** (profiting on stress days).

#### Stress Period Performance

| Crisis | v1.0 | v2.0 | v2.1 |
|--------|------|------|------|
| **Euro Crisis 2011** | +13.4% | +32.8% | +43.2% |
| **COVID Crash 2020** | +3.5% | +9.6% | +12.6% |
| **Rate Shock 2022** | +12.4% | +43.7% | +67.0% |

#### Stress Period Details

**Euro Crisis 2011 (Jul-Dec):**
- v2.0 Return: +32.8%
- Max Drawdown: -1.9%
- Hedge Payoff: +26.6%
- *Strategy profited from EU banking stress and EUR weakness*

**COVID 2020 (Feb-Apr):**
- v2.0 Return: +9.6%
- Max Drawdown: -0.4%
- Hedge Payoff: +9.2%
- *Vol convexity paid off during global panic*

**Rate Shock 2022 (Jan-Oct):**
- v2.0 Return: +43.7%
- Max Drawdown: -1.6%
- Hedge Payoff: +36.4%
- *EUR weakness + EU growth concerns drove strong returns*

---

## Implementation Details

### Instrument Universe

#### Equity Indices (UCITS ETFs for EU PRIIPs compliance)
- US: CSPX (iShares S&P 500), CNDX (NASDAQ 100)
- EU: CS51 (Euro STOXX 50), SMEA (MSCI Europe)

#### Sector Pair ETFs (Factor-Neutral)
- US: XLF, XLK, XLI, XLV, XLE, XLU
- EU: EXV1, EXV3, EXH1, EXV4, EXH2, EXH9

#### Volatility Products
- VSTOXX Mini Future (FVS) - EUREX
- VSTOXX Options on Futures (OVS2) - EUREX
- VIX Future (VX) - CFE
- VIX Options - CBOE

#### Europe Vol Structures
- SX5E Index Options (OESX) - EUREX (multiplier: 10)
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

# Sleeve Weights (Portfolio Simplification v2.2)
sleeves:
  core_index_rv: 0.20       # US vs EU index spread
  sector_rv: 0.20           # Factor-neutral sector pairs
  europe_vol_convex: 0.18   # Primary insurance (absorbed crisis_alpha)
  credit_carry: 0.08        # NORMAL regime only
  money_market: 0.34        # Short-term funds (not idle cash)
  # REMOVED: single_name, crisis_alpha, cash_buffer

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

# Term Structure Signal
term_structure:
  enabled: true
  contango_threshold: 0.5
  backwardation_threshold: -0.5
  zscore_lookback_days: 60

# Vol-of-Vol Jump Detection
vol_of_vol:
  enabled: true
  lookback_days: 20
  jump_window_days: 3
  jump_threshold_std: 2.0

# Factor-Neutral Sector Pairs
sector_pairs:
  enabled: true
  included_sectors: [financials, technology, industrials, healthcare]
  beta_adjust: true
  neutralize_growth_value: true

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
│   └── instruments.yaml       # Tradable instruments + sector pairs
├── src/
│   ├── strategy_logic.py      # Sleeve construction + trend filter
│   ├── tail_hedge.py          # Europe-centric crisis alpha
│   ├── europe_vol.py          # Europe vol convexity engine (NEW)
│   ├── sector_pairs.py        # Factor-neutral sector pairs (NEW)
│   ├── risk_engine.py         # Regime detection + scaling
│   ├── scheduler.py           # Daily orchestration
│   ├── execution/             # Order execution stack
│   ├── marketdata/            # Live data feeds (IBKR)
│   ├── research/
│   │   ├── backtest.py        # Historical validation
│   │   └── backtest_compare.py # Strategy comparison
│   └── state/
│       └── run_ledger.py      # Exactly-once execution
├── tests/
│   └── test_roadmap_features.py
└── docs/
    ├── INVESTMENT_STRATEGY.md  # This document
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
| 2.0 | Dec 2025 | Strategy Evolution (Europe-centric, trend filter, vol convexity) |
| 2.1 | Dec 2025 | Full Implementation (term structure, vol-of-vol, sector pairs) |
| **2.2** | Dec 2025 | **Portfolio Simplification** (removed single_name, merged crisis_alpha, NORMAL regime gate for credit) |

---

*Document generated: December 2025*
*Strategy validated via backtest 2010-2025*
*Paper trading on staging server: 94.130.228.55*
