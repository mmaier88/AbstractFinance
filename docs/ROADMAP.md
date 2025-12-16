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

## Strategy Evolution v2.1 Integration (Dec 2025)

The following phases integrate the new strategy modules (`europe_vol.py`, `sector_pairs.py`) into the live execution path. These modules exist in research/backtest but are NOT YET connected to live trading.

**CRITICAL:** Do NOT deploy to staging until all phases pass local validation.

---

### Phase G: Integrate Europe Vol Engine into Tail Hedge (Status: COMPLETE)

**Goal:** Replace static tail_hedge.py allocations with dynamic EuropeVolEngine.

**Why:** The EuropeVolEngine provides:
- Term structure signal (contango/backwardation for entry timing)
- Vol-of-vol jump detection (monetization triggers)
- Vol regime-based structure selection (spreads vs outrights)

**Deliverables:**
1. Update `src/tail_hedge.py`:
   - Import `EuropeVolEngine` from `src/europe_vol.py`
   - Replace static `HEDGE_ALLOCATION` with dynamic signal-based allocation
   - Add `compute_dynamic_hedge_targets()` using EuropeVolEngine
2. Add V2X history tracking for vol-of-vol calculation
3. Config integration:
   - Read `term_structure` settings from settings.yaml
   - Read `vol_of_vol` settings from settings.yaml
   - Read `vol_regime` settings from settings.yaml

**Acceptance Criteria:**
- [x] EuropeVolEngine.compute_signal() called on each hedge update
- [x] Term structure influences sizing multiplier
- [x] Vol-of-vol jump triggers monetization flag
- [x] Vol regime determines spread vs outright preference
- [x] Fallback to static allocation if engine fails

**Files Modified:**
- `src/tail_hedge.py` (dynamic targeting integration)
- `src/europe_vol.py` (engine implementation)

---

### Phase H: Integrate Sector Pairs into Strategy Logic (Status: COMPLETE)

**Goal:** Replace current sector ETF selection with factor-neutral matched pairs.

**Why:** Current Sector RV sleeve has hidden factor bets (long growth, short value). Matched sector pairs (US Banks vs EU Banks, etc.) isolate regional beta.

**Deliverables:**
1. Update `src/strategy_logic.py`:
   - Import `SectorPairEngine` from `src/sector_pairs.py`
   - Replace `_build_sector_rv_targets()` with pair-based targeting
   - Add beta adjustment for EU legs
   - Add factor neutralization logic
2. Update instruments loading:
   - Load sector pair definitions from `instruments.yaml`
   - Map to IBKR contract specifications
3. Config integration:
   - Read `sector_pairs` settings from settings.yaml

**Acceptance Criteria:**
- [x] Sector RV targets use matched sector pairs
- [x] EU leg sized with beta adjustment
- [x] Factor exposure stays within bounds (±10% growth/value)
- [x] Fallback to original sector selection if engine fails

**Files Modified:**
- `src/strategy_logic.py` (sector pair integration)
- `src/sector_pairs.py` (engine implementation)

---

### Phase I: Add VSTOXX Data Feed (Status: COMPLETE)

**Goal:** Add live V2X/FVS data from IBKR for term structure signal.

**Why:** EuropeVolEngine needs:
- V2X spot level (or FVS front month as proxy)
- V2X front month future price
- V2X back month future price (for term structure)

**Deliverables:**
1. Update `src/marketdata/live.py`:
   - `get_vstoxx_spot()` - V2X index or FVS front month
   - `get_vstoxx_futures()` - Front and back month FVS prices
   - `get_vstoxx_term_spread()` - Back - Front
   - `get_vstoxx_all()` - All data for EuropeVolEngine
2. Add contract definitions:
   - FVS (VSTOXX Mini Future) contract resolution
   - Handle monthly expiry rollover via `_get_fvs_expiry()`
3. Cache and fallback:
   - Cache recent values for vol-of-vol history
   - Fallback estimate from spot when futures unavailable

**Acceptance Criteria:**
- [x] FVS front/back month prices retrieved from IBKR
- [x] Term spread calculated correctly
- [x] Graceful fallback when VSTOXX unavailable
- [x] Data methods exported via `src/marketdata/__init__.py`

**Files Modified:**
- `src/marketdata/live.py` (VSTOXX methods)
- `src/marketdata/__init__.py` (exports)

---

### Phase J: Integration Testing & Validation (Status: COMPLETE)

**Goal:** Comprehensive testing before staging deployment.

**Why:** The integration changes affect live order generation. Must validate thoroughly.

**Deliverables:**
1. Unit tests in `tests/test_strategy_evolution.py`:
   - EuropeVolEngine integration with tail_hedge (5 tests)
   - SectorPairEngine integration with strategy_logic (4 tests)
   - VSTOXX data feed mocking (2 tests)
   - TailHedgeManager integration (5 tests)
   - Strategy integration (3 tests)
   - Full integration tests (2 tests)
2. All 21 tests passing

**Acceptance Criteria:**
- [x] All unit tests pass (21/21)
- [x] Integration tests pass
- [ ] 1 week paper trading without anomalies (PENDING - Phase K)
- [ ] Manual review of generated orders vs expectations (PENDING - Phase K)

**Files Created:**
- `tests/test_strategy_evolution.py` (21 tests)

---

### Phase K: Staging Deployment (Status: READY - Awaiting User Approval)

**Goal:** Deploy validated v2.1 integration to staging server.

**Prerequisites:** ALL of phases G, H, I, J complete and passing. ✅ SATISFIED

**Deliverables:**
1. Git commit with all integration changes
2. Push to main branch
3. SSH deploy to 94.130.228.55
4. Restart trading-engine container
5. Monitor first daily run
6. Verify positions match expectations

**Acceptance Criteria:**
- [ ] Staging server running v2.1 code
- [ ] First daily run completes without errors
- [ ] Positions include sector pairs (if triggered)
- [ ] Hedge targeting uses EuropeVolEngine
- [ ] No duplicate orders

**Rollback Plan:**
- Git revert to previous commit
- Redeploy via docker compose

---

## Strategy Evolution v2.1 Execution Order

| Order | Phase | Priority | Dependency |
|-------|-------|----------|------------|
| 1 | **G: Europe Vol Engine Integration** | HIGH | None |
| 2 | **H: Sector Pairs Integration** | HIGH | None (can parallel with G) |
| 3 | **I: VSTOXX Data Feed** | HIGH | G (needed for engine) |
| 4 | **J: Testing & Validation** | CRITICAL | G, H, I |
| 5 | **K: Staging Deployment** | FINAL | J (must pass all tests) |

**Recommended Execution:**
- Day 1-2: G + H in parallel
- Day 3: I (VSTOXX data feed)
- Day 4-5: J (testing)
- Day 6+: K (deploy only after validation)

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
