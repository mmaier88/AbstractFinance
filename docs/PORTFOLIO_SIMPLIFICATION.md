# Portfolio Simplification Recommendations

> Based on comprehensive ablation analysis of all sleeves (2010-2024 backtest)

## Executive Summary

**Current portfolio has 37% unnecessary complexity.** Two sleeves have negative marginal contribution, one has near-zero value. Removing these would:
- Improve portfolio Sharpe by ~0.4
- Reduce codebase by ~900 lines
- Simplify maintenance and reduce bug surface

## Ablation Results

| Sleeve | Weight | LOC | Sharpe | Marginal Contribution | Insurance | VERDICT |
|--------|--------|-----|--------|----------------------|-----------|---------|
| europe_vol_convex | 15% | 596 | 0.70 | **+0.368** | +4.24 | ✅ KEEP |
| sector_rv | 20% | 525 | 0.51 | **+0.179** | -0.07 | ✅ KEEP |
| core_index_rv | 20% | 400 | 0.28 | **+0.078** | -0.09 | ✅ KEEP |
| credit_carry | 15% | 200 | 0.15 | +0.023 | -0.55 | ⚠️ REDUCE |
| cash_buffer | 10% | 0 | - | 0.000 | 0.00 | ⚠️ REVIEW |
| crisis_alpha | 10% | 400 | -0.90 | **-0.082** | +0.37 | ❌ MERGE |
| single_name | 10% | 300 | -0.41 | **-0.334** | -4.64 | ❌ REMOVE |

---

## Detailed Recommendations

### 1. 🔴 REMOVE: single_name (10% allocation, 300 LOC)

**Problem:** Single stock picking has -0.334 marginal Sharpe and -82% max drawdown.

**Root Cause Analysis:**
- Stock screening (quality/momentum/zombie) doesn't reliably generate alpha
- Individual stock variance overwhelms the signal
- Short EU "zombies" requires difficult stock borrow
- High execution costs on individual names

**Action:** Remove entirely. Reallocate weight to proven sleeves.

**Code Impact:**
- Remove `stock_screener.py` (589 lines)
- Remove `_build_single_name_targets()` from strategy_logic.py
- Remove single_name config from settings.yaml

---

### 2. 🔴 MERGE: crisis_alpha into europe_vol_convex (10% → 0%)

**Problem:** Overlaps with europe_vol_convex but performs worse.

**Evidence:**
- crisis_alpha: -0.082 marginal Sharpe, +0.37 insurance
- europe_vol_convex: +0.368 marginal Sharpe, +4.24 insurance

**Root Cause:**
- Both sleeves trade VSTOXX, SX5E options
- crisis_alpha is a "shell" managed by TailHedgeManager
- europe_vol_convex has better signal timing (term structure, vol-of-vol)

**Action:**
1. Increase europe_vol_convex allocation to 25% (absorbs crisis_alpha function)
2. Remove crisis_alpha sleeve
3. TailHedgeManager continues to manage europe_vol_convex positions

**Code Impact:**
- Remove CRISIS_ALPHA from Sleeve enum
- Remove `_build_crisis_alpha_targets()` from strategy_logic.py
- Update TailHedgeManager to work solely with europe_vol_convex

---

### 3. 🟡 REDUCE: credit_carry (15% → 10%)

**Problem:** Very low marginal contribution (+0.023) with negative insurance (-0.55).

**Analysis:**
- Adds positive carry in normal markets
- Loses money during stress (opposite of insurance)
- Not terrible, but low value per complexity

**Action:**
- Reduce allocation from 15% to 10%
- Keep for diversification benefit

---

### 4. 🟢 KEEP: europe_vol_convex (15% → 25%)

**Why:** Clear winner with +0.368 marginal Sharpe and +4.24 insurance score.

**Strengths:**
- Best insurance profile (pays during stress)
- Sophisticated signal (term structure, vol-of-vol)
- Well-implemented engine (europe_vol.py)

**Action:** Increase to 25% (primary insurance sleeve)

---

### 5. 🟢 KEEP: sector_rv (20%)

**Why:** Solid +0.179 marginal contribution with factor neutralization.

**Strengths:**
- True beta isolation (US vs EU sectors)
- Low correlation to other sleeves
- Reasonable complexity for value

---

### 6. 🟢 KEEP: core_index_rv (20%)

**Why:** Positive +0.078 contribution, core thesis of the fund.

**Considerations:**
- Trend filter prevents bleeding during EU outperformance
- Consider if 400 LOC is justified for +0.078

---

## Proposed New Allocation

| Sleeve | Current | Proposed | Change |
|--------|---------|----------|--------|
| core_index_rv | 20% | **20%** | - |
| sector_rv | 20% | **20%** | - |
| europe_vol_convex | 15% | **25%** | +10% |
| credit_carry | 15% | **10%** | -5% |
| cash_buffer | 10% | **15%** | +5% |
| crisis_alpha | 10% | **0%** | REMOVED |
| single_name | 10% | **0%** | REMOVED |
| **Total** | 100% | **90%** | 10% freed |

**Note:** 10% freed weight goes to larger cash buffer for safety.

---

## Expected Impact

### Performance
- Marginal Sharpe improvement: ~+0.4 (removing negative contributors)
- Insurance profile: Improved (consolidating into best performer)
- Max drawdown: Likely improved (removing -82% DD sleeve)

### Complexity
- LOC removed: ~900 lines (37% of sleeve code)
- Files removed: stock_screener.py, parts of strategy_logic.py
- Sleeves reduced: 7 → 5 (29% simpler)

### Maintenance
- Fewer failure modes
- Simpler reconciliation
- Less stock borrow management

---

## Implementation Plan

### Phase 1: Immediate (Low Risk)
1. Set single_name weight to 0 in settings.yaml
2. Set crisis_alpha weight to 0 in settings.yaml
3. Increase europe_vol_convex to 25%
4. Reduce credit_carry to 10%
5. Increase cash_buffer to 15%
6. Deploy and monitor for 1 week

### Phase 2: Code Cleanup (After Validation)
1. Remove _build_single_name_targets() from strategy_logic.py
2. Remove _build_crisis_alpha_targets() from strategy_logic.py
3. Remove stock_screener.py
4. Update TailHedgeManager
5. Remove unused configs
6. Update tests

### Phase 3: Validation
1. Run 2-week paper trading
2. Verify positions match expectations
3. Confirm no regressions in core functionality

---

## Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| Concentrated in europe_vol | Still have sector_rv + core_index_rv |
| Loss of single stock alpha | Backtest shows negative alpha anyway |
| Crisis playbook disruption | TailHedgeManager unaffected |

---

## Conclusion

The portfolio has grown complex without proportional value. **Two sleeves actively hurt performance** and should be removed. The v2.2 evidence-gate framework showed no approved new engines; we should apply the same rigor to existing sleeves.

**Recommended immediate action:** Zero out single_name and crisis_alpha weights, redeploy to simpler configuration.

---

*Analysis Date: 2024-12-17*
*Backtest Period: 2010-2024*
*Framework: Institutional Backtest Harness (Phase L)*
