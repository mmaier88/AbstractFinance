# AbstractFinance Roadmap

> Strategic priorities for the "European Decline Macro" trading engine.

---

## Executive Summary

This roadmap integrates the "Insurance-Grade Engine Upgrade" proposal with existing work to create a coherent path forward. Many items from the proposal are **already implemented** through ENGINE_FIX_PLAN and Execution Stack upgrades. This document identifies genuine gaps and prioritizes remaining work.

---

## Current State Assessment

### Completed (Dec 2025)

| Feature | Status | Reference |
|---------|--------|-----------|
| FX conversion correctness | **DONE** | ENGINE_FIX_PLAN Phase 1, `src/fx_rates.py`, 8 tests |
| Risk scaling factor fix | **DONE** | ENGINE_FIX_PLAN Phase 6-8, EWMA + floor + state machine |
| NAV vs Exposure split | **DONE** | ENGINE_FIX_PLAN Phase 2, futures P&L-only accounting |
| Broker reconciliation circuit breaker | **DONE** | ENGINE_FIX_PLAN Phase 3, HALT/EMERGENCY thresholds |
| Currency-correct position sizing | **DONE** | ENGINE_FIX_PLAN Phase 4 |
| Portfolio-level FX hedging | **DONE** | ENGINE_FIX_PLAN Phase 5 |
| Regime hysteresis (3-day persistence) | **DONE** | ENGINE_FIX_PLAN Phase 7 |
| Emergency de-risk state machine | **DONE** | ENGINE_FIX_PLAN Phase 8, NORMAL/ELEVATED/CRISIS |
| Execution safety guards | **DONE** | ENGINE_FIX_PLAN Phase 9, max order size, turnover limits |
| Marketable limits (no MKT orders) | **DONE** | Execution Stack, `ExecutionPolicy` |
| Hard slippage collars | **DONE** | Execution Stack, 10/12/3/2 bps by asset class |
| Trade netting across sleeves | **DONE** | Execution Stack, `BasketExecutor` |
| Legging protection | **DONE** | Execution Stack, `PairExecutor` |
| Session-aware job scheduling | **DONE** | Phase 2, `ExecutionJobStore` |
| Live/Research data separation | **DONE** | Phase 2, `LiveMarketData` (IBKR-only) |
| Cost-vs-benefit gating | **DONE** | Phase 2, `TradeGater` |
| Self-tuning slippage model | **DONE** | Phase 2, `SlippageModel` |
| Borrow/dividend/financing hooks | **DONE** | Phase 2, `src/carry/` package |
| Prometheus trading metrics | **DONE** | `src/metrics.py`, 20+ metrics |
| Alertmanager + Telegram | **DONE** | Infrastructure hardening |

### Partially Done

| Feature | Status | Gap |
|---------|--------|-----|
| Idempotent order emission | **PARTIAL** | ExecutionJobStore exists, but lacks run-level ledger with order IDs |
| VIX-based regime detection | **PARTIAL** | Uses VIX only; missing V2X, EURUSD trend |
| Research/backtest infrastructure | **PARTIAL** | Config exists, runner not implemented |

### Not Started (Genuine Gaps)

| Feature | Priority | Notes |
|---------|----------|-------|
| Europe-first regime detection (V2X + EURUSD) | **HIGH** | Critical for "insurance for Europeans" thesis |
| TradingRun ledger with broker order IDs | **HIGH** | Exactly-once guarantee across restarts |
| FX hedge policy modes (FULL/PARTIAL/NONE) | **MEDIUM** | Adds flexibility, aligns with regime |
| Tail hedge option lifecycle validator | **MEDIUM** | Prevents dumb fills on illiquid options |
| Backtest runner (2008-today) | **MEDIUM** | Strategy validation with cost realism |
| V2X data feed integration | **MEDIUM** | Required for Europe-first regime |

---

## Roadmap Phases

### Phase A: Exactly-Once Order Execution (Priority: CRITICAL)

**Goal:** Ensure restarts never cause duplicate orders.

**Why:** Current `ExecutionJobStore` tracks jobs but doesn't link to broker order IDs. A crash between order submission and acknowledgment could cause double-submission on restart.

**Deliverables:**
1. `src/state/run_ledger.py` - SQLite-backed run tracking
   - Fields: `run_id`, `date`, `inputs_hash`, `intents_hash`, `status`
   - Status: PLANNED → SUBMITTED → FILLED → DONE / ABORTED
2. Deterministic `client_order_id` generation
   - Formula: `sha1(run_id + instrument_id + side + qty + sleeve)`
3. Pre-submit check: query open orders, skip if matching client_order_id exists
4. Link broker order IDs to run ledger after submission

**Acceptance Criteria:**
- [ ] Restart mid-run does NOT double-submit orders
- [ ] Ledger correctly records all order states
- [ ] Recovery from partial fills works correctly

**Estimated Effort:** 2-3 days

---

### Phase B: Europe-First Regime Detection (Priority: HIGH)

**Goal:** Upgrade regime model from VIX-only to multi-factor European stress detection.

**Why:** Current regime uses VIX thresholds only. For a "European insurance" strategy, we need V2X (Euro volatility), EURUSD trend, and drawdown as regime inputs.

**Deliverables:**
1. Data feed extensions in `src/marketdata/live.py`:
   - `get_v2x_level()` - VSTOXX from IBKR (FVS futures or index)
   - `get_eurusd_spot()` - Current EURUSD rate
   - `get_eurusd_trend(lookback)` - Slope over N days
2. Updated regime model in `src/risk_engine.py`:
   ```
   stress_score = w1*clip((v2x-20)/20) + w2*clip((vix-20)/25)
                + w3*clip((-eurusd_trend)/k) + w4*clip(drawdown/0.1)
   ```
3. Fallback handling when V2X unavailable (degrade to VIX-only)
4. Config section for regime weights and thresholds

**Acceptance Criteria:**
- [ ] High V2X + EUR weakening → ELEVATED/CRISIS
- [ ] Missing V2X gracefully degrades (uses VIX only)
- [ ] Hysteresis preserved (3-day persistence except CRISIS)
- [ ] Unit tests for European stress scenarios

**Estimated Effort:** 3-4 days

---

### Phase C: FX Hedge Policy Modes (Priority: MEDIUM)

**Goal:** Allow configurable FX hedging (FULL/PARTIAL/NONE) based on regime.

**Why:** Current system fully hedges all EUR/GBP exposure. For "insurance" positioning, we may want to let USD exposure pay off during crisis (partial or no hedge).

**Deliverables:**
1. Config extension in `settings.yaml`:
   ```yaml
   fx_hedge:
     mode: "PARTIAL"  # FULL | PARTIAL | NONE
     target_residual_pct_nav:
       FULL: 0.02
       PARTIAL: 0.25
       NONE: 1.00
     regime_overrides:
       NORMAL: "PARTIAL"
       ELEVATED: "PARTIAL"
       CRISIS: "NONE"  # Let USD exposure pay off
   ```
2. Update `compute_fx_hedge_quantities()` in `src/strategy_logic.py`
3. Safety check: residual exposure within bounds per mode
4. Metrics: `fx_residual_exposure_pct_nav`

**Acceptance Criteria:**
- [ ] FULL mode: residual FX exposure <= 2% NAV
- [ ] PARTIAL mode: residual <= 25% NAV
- [ ] CRISIS override disables hedging correctly
- [ ] Over-hedge prevention (never hedge > 100% of exposure)

**Estimated Effort:** 2 days

---

### Phase D: Tail Hedge Option Validator (Priority: MEDIUM)

**Goal:** Prevent execution of illiquid or overpriced option hedges.

**Why:** Options can have wide bid/ask spreads and low liquidity. Need guardrails before submitting hedge orders.

**Deliverables:**
1. `src/options/validator.py`:
   - Check contract exists with correct multiplier
   - Check bid/ask spread < 8% of mid
   - Check volume/open interest thresholds
   - Check premium < max per leg (absolute + % of budget)
2. Integration in `src/tail_hedge.py`:
   - Validate before emitting orders
   - Skip + alert + propose alternatives if invalid
3. Metrics:
   - `tailhedge_orders_rejected_validator_total`
   - `tailhedge_budget_remaining_usd`
   - `tailhedge_effectiveness_last_30d`

**Acceptance Criteria:**
- [ ] Rejects orders with spread > threshold
- [ ] Rejects orders with insufficient liquidity
- [ ] Rejects orders exceeding premium budget
- [ ] Alerts sent on rejection with proposed alternatives

**Estimated Effort:** 2-3 days

---

### Phase E: Research/Backtest Harness (Priority: MEDIUM)

**Goal:** Validate strategy with historical data (2008-today) including realistic costs.

**Why:** Need to verify "insurance payoff" behavior during stress periods (2008, 2011, 2020, 2022) before committing more capital.

**Deliverables:**
1. `src/research/backtest.py`:
   - Load historical series (US proxy, EU proxy, EURUSD, VIX, V2X)
   - Run strategy logic daily
   - Apply transaction cost model
   - Produce returns series
2. Cost model with configurable parameters:
   ```yaml
   research_costs:
     equity_slippage_bps: 5
     etf_slippage_bps: 4
     futures_slippage_bps: 1
     commissions_per_trade_usd: 1.0
     short_dividend_bps_annual: 200
     borrow_bps_annual: 50
   ```
3. Output report:
   - Sharpe, max DD, CAGR
   - Crash-period payoff windows
   - Insurance payoff score (stress days vs normal)
   - Turnover and cost attribution
4. CLI entrypoint: `python -m src.research.backtest --start 2008-01-01`

**Acceptance Criteria:**
- [ ] Backtest runs 2008-today in < 2 minutes
- [ ] Produces deterministic JSON report
- [ ] Clearly shows stress-period payoffs
- [ ] Uses `ResearchMarketData` (NEVER `LiveMarketData`)

**Estimated Effort:** 4-5 days

---

### Phase F: Enhanced Metrics & Reporting (Priority: LOW)

**Goal:** Add remaining "insurance-grade" metrics and Telegram reporting fields.

**Note:** Many metrics already exist. This phase fills small gaps.

**Deliverables:**
1. Additional metrics in `src/metrics.py`:
   - `regime_inputs_v2x`, `regime_inputs_eurusd_trend`
   - `eur_stress_days_pnl_rolling`
   - `hedge_effectiveness_rolling` (% of drawdown offset by hedges)
2. Telegram daily summary additions:
   - Regime inputs (V2X, VIX, EURUSD trend)
   - Hedge budget remaining
   - Residual FX exposure (% NAV)
   - Turnover + predicted costs
   - Gating decisions count

**Acceptance Criteria:**
- [ ] New metrics visible in Grafana
- [ ] Telegram summary includes new fields
- [ ] No performance impact from additional metrics

**Estimated Effort:** 1-2 days

---

## Execution Order

| Order | Phase | Priority | Dependency |
|-------|-------|----------|------------|
| 1 | **A: Exactly-Once Orders** | CRITICAL | None |
| 2 | **B: Europe-First Regime** | HIGH | None (can parallel with A) |
| 3 | **C: FX Hedge Modes** | MEDIUM | B (needs regime for overrides) |
| 4 | **D: Tail Hedge Validator** | MEDIUM | None |
| 5 | **E: Backtest Harness** | MEDIUM | B (needs V2X for historical regime) |
| 6 | **F: Enhanced Metrics** | LOW | B, C (reports on new features) |

**Recommended Parallel Execution:**
- Week 1-2: A + B in parallel
- Week 3: C + D
- Week 4-5: E
- Week 6: F + integration testing

---

## Definition of Done (Full Roadmap)

All phases complete when:

- [x] Restart mid-run cannot double-submit orders (Phase A) - **IMPLEMENTED**
- [x] Regime uses V2X/EURUSD/VIX + hysteresis; missing data handled safely (Phase B) - **IMPLEMENTED**
- [x] FX hedge supports FULL/PARTIAL/NONE with regime overrides (Phase C) - **IMPLEMENTED**
- [x] Options validator prevents illiquid/insane hedge orders (Phase D) - **IMPLEMENTED**
- [x] Backtest produces metrics since 2008, including stress-period payoff (Phase E) - **IMPLEMENTED**
- [x] Grafana shows new metrics; Telegram includes new fields (Phase F) - **IMPLEMENTED**

**Already satisfied (from previous work):**
- [x] FX conversion passes unit tests and live self-checks
- [x] Scaling factor never < 0.1 unless in emergency mode
- [x] NAV reconciles with broker NLV within tolerance

---

## Implementation Status (Dec 2025)

| Phase | Feature | Status | Files |
|-------|---------|--------|-------|
| A | Run Ledger | **DONE** | `src/state/run_ledger.py` |
| B | Europe-First Regime | **DONE** | `src/risk_engine.py`, `src/marketdata/live.py` |
| C | FX Hedge Modes | **DONE** | `src/strategy_logic.py` |
| D | Option Validator | **DONE** | `src/options/validator.py`, `src/tail_hedge.py` |
| E | Backtest Harness | **DONE** | `src/research/backtest.py` |
| F | Enhanced Metrics | **DONE** | `src/metrics.py` |

### New Files Created

```
src/state/__init__.py
src/state/run_ledger.py          # SQLite-backed run tracking
src/options/__init__.py
src/options/validator.py          # Option lifecycle validation
src/research/__init__.py
src/research/backtest.py          # Historical backtest runner
tests/test_roadmap_features.py    # Unit tests for all phases
```

### Configuration Added to settings.yaml

- `europe_regime:` - V2X/VIX/EURUSD weights and thresholds
- `fx_hedge:` - FULL/PARTIAL/NONE modes with regime overrides
- `option_validator:` - Spread, volume, OI, premium thresholds
- `research_costs:` - Slippage and carry cost parameters
- `run_ledger:` - Database path and cleanup settings

---

## Not Included (Explicitly Excluded)

The following items from the original proposal are **not on this roadmap** because they're already done:

| Item | Why Excluded |
|------|--------------|
| FX rate convention + tests | Done in ENGINE_FIX_PLAN Phase 1 |
| Risk scaling factor fix | Done in ENGINE_FIX_PLAN Phases 6-8 |
| Risk scaling invariants | Done: `0.0 <= scaling <= 2.0` enforced |
| VIX-only regime + hysteresis | Done (but being enhanced in Phase B) |
| Position reconciliation on reconnect | Done in IMPLEMENTATION_PLAN_V3 Phase 5 |
| Session-aware scheduling | Done in Execution Phase 2 |
| Cost gating | Done in Execution Phase 2 |
| Slippage model | Done in Execution Phase 2 |
| Borrow/dividend/financing | Done in Execution Phase 2 |

---

## Risk Considerations

### Technical Risks

| Risk | Mitigation |
|------|------------|
| V2X data unavailable from IBKR | Fallback to VIX-only regime (already implemented) |
| SQLite run ledger performance | Use WAL mode, vacuum regularly |
| Backtest data quality for V2X pre-2010 | Use VIX proxy + note in results |

### Strategic Risks

| Risk | Mitigation |
|------|------------|
| FX hedge modes reduce hedging effectiveness | Backtest all modes before deployment |
| Europe-first regime over-reacts | Tune weights conservatively, monitor P&L attribution |
| Tail hedge validator too strict | Include manual override capability |

---

## Appendix: Deferred Items

These items may be valuable but are deferred to avoid scope creep:

1. **Portfolio insurance overlay** - Systematic VIX call buying during low-vol periods
2. **Cross-asset correlation monitoring** - Alert when US/EU correlation breaks
3. **Intraday regime updates** - Currently daily; intraday adds complexity
4. **Multi-account support** - Currently single IBKR account assumed

---

*Document created: 2025-12-16*
*Last updated: 2025-12-16*
