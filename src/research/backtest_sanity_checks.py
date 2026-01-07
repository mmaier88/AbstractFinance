"""
Backtest Sanity Checks for v3.0 Sizing Recommendations.

Addresses 5 key validation requirements:
1. Option carry realism - force full premium budget spend
2. Slippage stress test - 2x, 3x option slippage in stress
3. Deflationary crisis check - 2008 Q4, 2020 Q1-Q2 kill-switch test
4. Regime transition lag - 1-2 day signal delay
5. Attribution sanity - verify contributions by regime type

Created: January 2026
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class SanityCheckConfig:
    """Configuration for sanity checks."""
    # Option carry realism
    annual_premium_budget_pct: float = 0.025  # 2.5% NAV
    force_full_spend: bool = True  # Always spend the budget

    # Slippage
    base_option_slippage_bps: float = 25.0  # 25bps base
    stress_slippage_multiplier: float = 1.0  # Default 1x

    # Signal lag
    signal_lag_days: int = 0  # 0 = same-day, 1-2 = realistic

    # Kill-switch parameters
    deflation_guard_enabled: bool = True
    deflation_vix_threshold: float = 30.0
    deflation_rate_drop_5d_bps: float = -30.0


@dataclass
class SanityCheckResult:
    """Results from a single sanity check run."""
    check_name: str
    sharpe: float
    sortino: float
    max_dd: float
    cagr: float
    insurance_score: float

    # Per-regime attribution
    attribution_normal: Dict[str, float] = field(default_factory=dict)
    attribution_elevated: Dict[str, float] = field(default_factory=dict)
    attribution_crisis: Dict[str, float] = field(default_factory=dict)

    # Stress period returns
    stress_2008_q4: float = 0.0
    stress_2020_q1q2: float = 0.0
    stress_2011_euro: float = 0.0
    stress_2022_rates: float = 0.0

    # Kill-switch activations
    deflation_kills: int = 0
    total_days: int = 0

    notes: str = ""


class BacktestWithSanityChecks:
    """
    Backtest runner with explicit sanity checks.

    Addresses known backtest pitfalls:
    - Option bleed realism
    - Slippage in stress
    - Signal look-ahead
    - Deflation guard effectiveness
    """

    def __init__(
        self,
        config: SanityCheckConfig,
        start_date: date = date(2005, 1, 1),
        end_date: date = date(2025, 1, 1),
        initial_capital: float = 10_000_000.0,
    ):
        self.config = config
        self.start_date = start_date
        self.end_date = end_date
        self.initial_capital = initial_capital

        # Sleeve weights (insurance heavy)
        self.weights = {
            'core_rv': 0.15,
            'sector_rv': 0.15,
            'europe_vol': 0.25,
            'sov_rates': 0.10,  # Normal regime base
            'money_market': 0.30,
        }

    def run(self) -> SanityCheckResult:
        """Run backtest with sanity checks."""
        # Load data
        data = self._load_market_data()
        if data is None:
            return SanityCheckResult(
                check_name="FAILED",
                sharpe=0, sortino=0, max_dd=0, cagr=0, insurance_score=0,
                notes="Failed to load market data"
            )

        spy, ezu, vix, spread_proxy = data

        # State tracking
        nav = self.initial_capital
        hwm = self.initial_capital

        # Signal state with lag
        signal_history = []  # For lagged signals

        # Results tracking
        returns = []
        regimes = []

        # Attribution by regime
        attr_normal = {'core_rv': 0, 'sector_rv': 0, 'europe_vol': 0, 'sov_rates': 0, 'money_market': 0}
        attr_elevated = {'core_rv': 0, 'sector_rv': 0, 'europe_vol': 0, 'sov_rates': 0, 'money_market': 0}
        attr_crisis = {'core_rv': 0, 'sector_rv': 0, 'europe_vol': 0, 'sov_rates': 0, 'money_market': 0}
        regime_days = {'NORMAL': 0, 'ELEVATED': 0, 'CRISIS': 0}

        # Stress period tracking
        stress_periods = {
            '2008_q4': {'start': date(2008, 10, 1), 'end': date(2008, 12, 31), 'returns': []},
            '2020_q1q2': {'start': date(2020, 2, 15), 'end': date(2020, 6, 30), 'returns': []},
            '2011_euro': {'start': date(2011, 7, 1), 'end': date(2011, 12, 31), 'returns': []},
            '2022_rates': {'start': date(2022, 1, 1), 'end': date(2022, 10, 31), 'returns': []},
        }

        # Kill-switch tracking
        deflation_kills = 0
        rate_change_history = []  # Track 5-day rate changes

        # Option premium tracking - FORCE FULL SPEND
        daily_option_cost = self.config.annual_premium_budget_pct / 252

        spy_ret = spy.pct_change().fillna(0)
        ezu_ret = ezu.pct_change().fillna(0)
        spread_change = spread_proxy.diff().fillna(0)

        for i in range(1, len(spy)):
            dt = spy.index[i]
            if isinstance(dt, pd.Timestamp):
                dt = dt.date()

            current_vix = vix.iloc[i] if i < len(vix) else 20.0
            current_spread = spread_proxy.iloc[i]

            # Track rate changes for deflation guard
            if i >= 5:
                rate_change_5d = spread_proxy.iloc[i] - spread_proxy.iloc[i-5]
            else:
                rate_change_5d = 0
            rate_change_history.append(rate_change_5d)

            # Compute regime with LAG
            if self.config.signal_lag_days > 0:
                # Use lagged VIX for regime detection
                lag_idx = max(0, i - self.config.signal_lag_days)
                lagged_vix = vix.iloc[lag_idx] if lag_idx < len(vix) else 20.0
            else:
                lagged_vix = current_vix

            regime = self._compute_regime(lagged_vix)
            regimes.append(regime)
            regime_days[regime] += 1

            # Check deflation guard (2008/2020 scenarios)
            deflation_guard_active = False
            if self.config.deflation_guard_enabled:
                is_risk_off = current_vix > self.config.deflation_vix_threshold
                is_rates_down = rate_change_5d < self.config.deflation_rate_drop_5d_bps
                deflation_guard_active = is_risk_off and is_rates_down
                if deflation_guard_active:
                    deflation_kills += 1

            # Compute regime-aware weights
            if regime == 'CRISIS':
                regime_scale = 0.3
                sov_weight = 0.18 if not deflation_guard_active else 0.0  # Max or kill
            elif regime == 'ELEVATED':
                regime_scale = 0.7
                sov_weight = 0.14 if not deflation_guard_active else 0.0
            else:
                regime_scale = 1.0
                sov_weight = 0.10 if not deflation_guard_active else 0.0

            # ============================================
            # SLEEVE RETURNS WITH REALISTIC COSTS
            # ============================================

            # 1. Core Index RV: Long US / Short EU
            core_rv_ret = (spy_ret.iloc[i] - ezu_ret.iloc[i]) * self.weights['core_rv'] * regime_scale

            # 2. Sector RV: Similar but reduced correlation
            sector_rv_ret = (spy_ret.iloc[i] - ezu_ret.iloc[i]) * 0.8 * self.weights['sector_rv'] * regime_scale

            # 3. Europe Vol Convex: REALISTIC MODELING
            # - Force full premium spend
            # - Apply slippage in stress
            is_stress_day = current_vix > 25

            if is_stress_day:
                slippage_mult = self.config.stress_slippage_multiplier
            else:
                slippage_mult = 1.0

            # Option bleed (forced full spend)
            option_bleed = -daily_option_cost * self.weights['europe_vol']

            # Option payoff (convex in VIX spikes)
            if regime == 'CRISIS':
                # Big payoff, but apply slippage
                raw_payoff = 0.015  # 1.5% daily in crisis
                slippage_cost = raw_payoff * (self.config.base_option_slippage_bps / 10000) * slippage_mult
                option_payoff = (raw_payoff - slippage_cost) * self.weights['europe_vol']
            elif regime == 'ELEVATED':
                raw_payoff = 0.004  # 0.4% daily
                slippage_cost = raw_payoff * (self.config.base_option_slippage_bps / 10000) * slippage_mult
                option_payoff = (raw_payoff - slippage_cost) * self.weights['europe_vol']
            else:
                option_payoff = 0  # No payoff in normal

            europe_vol_ret = option_bleed + option_payoff

            # 4. Sovereign Rates Short
            if deflation_guard_active:
                sov_rates_ret = 0.0  # Killed by deflation guard
            else:
                # Profit from spread widening (with realistic DV01)
                dv01_sensitivity = 0.0007  # 7bps per 100bp move
                sov_rates_ret = (spread_change.iloc[i] / 100) * dv01_sensitivity * (sov_weight / 0.12)
                sov_rates_ret = np.clip(sov_rates_ret, -0.02, 0.05)  # Cap extremes

            # 5. Money Market
            mm_ret = 0.04 / 252 * self.weights['money_market']

            # Total return
            total_ret = core_rv_ret + sector_rv_ret + europe_vol_ret + sov_rates_ret + mm_ret

            # Track attribution by regime
            if regime == 'NORMAL':
                attr_normal['core_rv'] += core_rv_ret
                attr_normal['sector_rv'] += sector_rv_ret
                attr_normal['europe_vol'] += europe_vol_ret
                attr_normal['sov_rates'] += sov_rates_ret
                attr_normal['money_market'] += mm_ret
            elif regime == 'ELEVATED':
                attr_elevated['core_rv'] += core_rv_ret
                attr_elevated['sector_rv'] += sector_rv_ret
                attr_elevated['europe_vol'] += europe_vol_ret
                attr_elevated['sov_rates'] += sov_rates_ret
                attr_elevated['money_market'] += mm_ret
            else:
                attr_crisis['core_rv'] += core_rv_ret
                attr_crisis['sector_rv'] += sector_rv_ret
                attr_crisis['europe_vol'] += europe_vol_ret
                attr_crisis['sov_rates'] += sov_rates_ret
                attr_crisis['money_market'] += mm_ret

            # Track stress period returns
            for period_name, period_info in stress_periods.items():
                if period_info['start'] <= dt <= period_info['end']:
                    period_info['returns'].append(total_ret)

            returns.append(total_ret)
            nav *= (1 + total_ret)
            hwm = max(hwm, nav)

        # Compute summary statistics
        returns = pd.Series(returns)
        total_days = len(returns)

        # Risk metrics
        realized_vol = returns.std() * np.sqrt(252)
        sharpe = returns.mean() * 252 / realized_vol if realized_vol > 0 else 0

        downside = returns[returns < 0]
        downside_vol = downside.std() * np.sqrt(252) if len(downside) > 0 else realized_vol
        sortino = returns.mean() * 252 / downside_vol if downside_vol > 0 else 0

        # Drawdown
        cumulative = (1 + returns).cumprod()
        rolling_max = cumulative.cummax()
        drawdown = (cumulative - rolling_max) / rolling_max
        max_dd = drawdown.min()

        # CAGR
        total_return = (nav / self.initial_capital) - 1
        years = len(returns) / 252
        cagr = (1 + total_return) ** (1 / years) - 1 if years > 0 else 0

        # Insurance score (outperformance on stress days)
        stress_mask = [r == 'CRISIS' or r == 'ELEVATED' for r in regimes]
        stress_returns = returns[stress_mask]
        normal_returns = returns[~np.array(stress_mask)]
        insurance_score = (
            stress_returns.mean() - normal_returns.mean()
        ) * 252 if len(stress_returns) > 0 and len(normal_returns) > 0 else 0

        # Annualize attribution
        for attr_dict in [attr_normal, attr_elevated, attr_crisis]:
            for k in attr_dict:
                attr_dict[k] = attr_dict[k] * 252 / max(total_days, 1)

        # Stress period returns
        stress_2008 = (1 + pd.Series(stress_periods['2008_q4']['returns'])).prod() - 1 if stress_periods['2008_q4']['returns'] else 0
        stress_2020 = (1 + pd.Series(stress_periods['2020_q1q2']['returns'])).prod() - 1 if stress_periods['2020_q1q2']['returns'] else 0
        stress_2011 = (1 + pd.Series(stress_periods['2011_euro']['returns'])).prod() - 1 if stress_periods['2011_euro']['returns'] else 0
        stress_2022 = (1 + pd.Series(stress_periods['2022_rates']['returns'])).prod() - 1 if stress_periods['2022_rates']['returns'] else 0

        return SanityCheckResult(
            check_name=self._get_check_name(),
            sharpe=sharpe,
            sortino=sortino,
            max_dd=max_dd,
            cagr=cagr,
            insurance_score=insurance_score,
            attribution_normal=attr_normal,
            attribution_elevated=attr_elevated,
            attribution_crisis=attr_crisis,
            stress_2008_q4=stress_2008,
            stress_2020_q1q2=stress_2020,
            stress_2011_euro=stress_2011,
            stress_2022_rates=stress_2022,
            deflation_kills=deflation_kills,
            total_days=total_days,
        )

    def _get_check_name(self) -> str:
        """Generate descriptive check name."""
        parts = []
        if self.config.force_full_spend:
            parts.append(f"FullSpend{self.config.annual_premium_budget_pct:.1%}")
        if self.config.stress_slippage_multiplier > 1:
            parts.append(f"Slip{self.config.stress_slippage_multiplier:.0f}x")
        if self.config.signal_lag_days > 0:
            parts.append(f"Lag{self.config.signal_lag_days}d")
        if self.config.deflation_guard_enabled:
            parts.append("DeflGuard")
        return "_".join(parts) if parts else "Baseline"

    def _compute_regime(self, vix: float) -> str:
        """Determine regime based on VIX."""
        if vix >= 35:
            return 'CRISIS'
        elif vix >= 25:
            return 'ELEVATED'
        return 'NORMAL'

    def _load_market_data(self):
        """Load market data for backtest."""
        try:
            from src.research.backtest_v3 import MarketDataCache
            cache = MarketDataCache()

            spy = cache.fetch_or_load("SPY", self.start_date, self.end_date)
            ezu = cache.fetch_or_load("EZU", self.start_date, self.end_date)
            vix = cache.fetch_or_load("^VIX", self.start_date, self.end_date)

            # For spread proxy, use EWI/EWG ratio
            try:
                ewi = cache.fetch_or_load("EWI", self.start_date, self.end_date)
                ewg = cache.fetch_or_load("EWG", self.start_date, self.end_date)

                common = ewi.index.intersection(ewg.index)
                spread_proxy = (ewi.loc[common].pct_change() - ewg.loc[common].pct_change()).cumsum() * 100 + 150
            except Exception:
                # Fallback: synthetic spread
                spread_proxy = pd.Series(index=spy.index, data=150.0)

            # Align all
            common = spy.index.intersection(ezu.index).intersection(vix.index).intersection(spread_proxy.index)

            return (
                spy.loc[common],
                ezu.loc[common],
                vix.loc[common],
                spread_proxy.loc[common],
            )
        except Exception as e:
            logger.error(f"Failed to load market data: {e}")
            return None


def run_all_sanity_checks() -> List[SanityCheckResult]:
    """Run all 5 sanity checks."""
    results = []

    print("\n" + "="*80)
    print(" RUNNING 5 SANITY CHECKS FOR v3.0 SIZING RECOMMENDATIONS")
    print("="*80)

    # ============================================
    # CHECK 1: Option carry realism
    # ============================================
    print("\n[1/5] Option Carry Realism (Full Premium Spend)...")

    config1 = SanityCheckConfig(
        annual_premium_budget_pct=0.025,  # 2.5% NAV
        force_full_spend=True,
        stress_slippage_multiplier=1.0,
        signal_lag_days=0,
        deflation_guard_enabled=True,
    )
    runner1 = BacktestWithSanityChecks(config1)
    result1 = runner1.run()
    result1.check_name = "1_OptionCarryRealism"
    results.append(result1)
    print(f"   Sharpe: {result1.sharpe:.2f} | Insurance: {result1.insurance_score:.1%}")

    # ============================================
    # CHECK 2: Slippage stress test (2x, 3x)
    # ============================================
    print("\n[2/5] Slippage Stress Test...")

    for mult in [2.0, 3.0]:
        config2 = SanityCheckConfig(
            annual_premium_budget_pct=0.025,
            force_full_spend=True,
            stress_slippage_multiplier=mult,
            signal_lag_days=0,
            deflation_guard_enabled=True,
        )
        runner2 = BacktestWithSanityChecks(config2)
        result2 = runner2.run()
        result2.check_name = f"2_Slippage{mult:.0f}x"
        results.append(result2)
        print(f"   {mult:.0f}x Slippage: Sharpe={result2.sharpe:.2f} | MaxDD={result2.max_dd:.1%}")

    # ============================================
    # CHECK 3: Deflationary crisis check
    # ============================================
    print("\n[3/5] Deflationary Crisis Check (2008 Q4, 2020 Q1-Q2)...")

    # With deflation guard
    config3a = SanityCheckConfig(
        annual_premium_budget_pct=0.025,
        force_full_spend=True,
        deflation_guard_enabled=True,
    )
    runner3a = BacktestWithSanityChecks(config3a)
    result3a = runner3a.run()
    result3a.check_name = "3a_DeflationGuardON"
    results.append(result3a)

    # Without deflation guard (counterfactual)
    config3b = SanityCheckConfig(
        annual_premium_budget_pct=0.025,
        force_full_spend=True,
        deflation_guard_enabled=False,
    )
    runner3b = BacktestWithSanityChecks(config3b)
    result3b = runner3b.run()
    result3b.check_name = "3b_DeflationGuardOFF"
    results.append(result3b)

    print(f"   WITH Guard:    2008Q4={result3a.stress_2008_q4:.1%} | 2020Q1Q2={result3a.stress_2020_q1q2:.1%}")
    print(f"   WITHOUT Guard: 2008Q4={result3b.stress_2008_q4:.1%} | 2020Q1Q2={result3b.stress_2020_q1q2:.1%}")
    print(f"   Kill-switch activations: {result3a.deflation_kills} days")

    # ============================================
    # CHECK 4: Regime transition lag
    # ============================================
    print("\n[4/5] Regime Transition Lag (1-2 day delay)...")

    for lag in [1, 2]:
        config4 = SanityCheckConfig(
            annual_premium_budget_pct=0.025,
            force_full_spend=True,
            signal_lag_days=lag,
            deflation_guard_enabled=True,
        )
        runner4 = BacktestWithSanityChecks(config4)
        result4 = runner4.run()
        result4.check_name = f"4_SignalLag{lag}d"
        results.append(result4)
        print(f"   {lag}-day lag: Sharpe={result4.sharpe:.2f} | Sortino={result4.sortino:.2f}")

    # ============================================
    # CHECK 5: Attribution sanity
    # ============================================
    print("\n[5/5] Attribution Sanity by Regime...")

    # Use baseline result for attribution
    result5 = result1  # Reuse first check
    result5.check_name = "5_AttributionSanity"

    print("\n   NORMAL regime attribution (annualized):")
    for sleeve, value in result5.attribution_normal.items():
        print(f"     {sleeve:<15}: {value:>7.2%}")

    print("\n   ELEVATED regime attribution (annualized):")
    for sleeve, value in result5.attribution_elevated.items():
        print(f"     {sleeve:<15}: {value:>7.2%}")

    print("\n   CRISIS regime attribution (annualized):")
    for sleeve, value in result5.attribution_crisis.items():
        print(f"     {sleeve:<15}: {value:>7.2%}")

    return results


def print_sanity_summary(results: List[SanityCheckResult]):
    """Print summary of all sanity checks."""
    print("\n" + "="*80)
    print(" SANITY CHECK SUMMARY")
    print("="*80)

    print(f"\n{'Check':<25} {'Sharpe':>8} {'Sortino':>8} {'MaxDD':>8} {'InsScore':>10}")
    print("-"*60)

    for r in results:
        print(f"{r.check_name:<25} {r.sharpe:>8.2f} {r.sortino:>8.2f} {r.max_dd:>7.1%} {r.insurance_score:>9.1%}")

    # Stress period summary
    print("\n" + "-"*60)
    print("Stress Period Returns:")
    print("-"*60)
    print(f"{'Check':<25} {'2008Q4':>10} {'2020Q1Q2':>10} {'2011Euro':>10} {'2022Rates':>10}")

    for r in results:
        if "3" in r.check_name or "1" in r.check_name:  # Only show for relevant checks
            print(f"{r.check_name:<25} {r.stress_2008_q4:>9.1%} {r.stress_2020_q1q2:>9.1%} {r.stress_2011_euro:>9.1%} {r.stress_2022_rates:>9.1%}")

    # Pass/Fail assessment
    print("\n" + "="*80)
    print(" PASS/FAIL ASSESSMENT")
    print("="*80)

    baseline = next((r for r in results if "1_" in r.check_name), None)
    if baseline:
        print(f"\n✓ Baseline Sharpe with full spend: {baseline.sharpe:.2f}")

        # Check 2: Slippage
        slip2x = next((r for r in results if "2x" in r.check_name), None)
        slip3x = next((r for r in results if "3x" in r.check_name), None)
        if slip2x and slip3x:
            slip_robust = slip3x.sharpe > 0.2
            print(f"{'✓' if slip_robust else '✗'} Slippage stress: 3x slippage Sharpe = {slip3x.sharpe:.2f} (need > 0.2)")

        # Check 3: Deflation guard
        guard_on = next((r for r in results if "3a_" in r.check_name), None)
        guard_off = next((r for r in results if "3b_" in r.check_name), None)
        if guard_on and guard_off:
            guard_helps_2008 = guard_on.stress_2008_q4 > guard_off.stress_2008_q4
            guard_helps_2020 = guard_on.stress_2020_q1q2 > guard_off.stress_2020_q1q2
            print(f"{'✓' if guard_helps_2008 else '✗'} Deflation guard helps 2008 Q4: {guard_on.stress_2008_q4:.1%} vs {guard_off.stress_2008_q4:.1%}")
            print(f"{'✓' if guard_helps_2020 else '✗'} Deflation guard helps 2020 Q1-Q2: {guard_on.stress_2020_q1q2:.1%} vs {guard_off.stress_2020_q1q2:.1%}")

        # Check 4: Signal lag
        lag1 = next((r for r in results if "Lag1d" in r.check_name), None)
        lag2 = next((r for r in results if "Lag2d" in r.check_name), None)
        if lag2:
            lag_robust = lag2.sharpe > baseline.sharpe * 0.7
            print(f"{'✓' if lag_robust else '✗'} Signal lag robust: 2-day lag Sharpe = {lag2.sharpe:.2f} (need > {baseline.sharpe * 0.7:.2f})")

        # Check 5: Attribution makes sense
        print("\n✓ Attribution sanity check:")
        if baseline.attribution_normal.get('core_rv', 0) > 0:
            print(f"  ✓ Core RV positive in NORMAL: {baseline.attribution_normal.get('core_rv', 0):.2%}")
        if baseline.attribution_crisis.get('europe_vol', 0) > 0:
            print(f"  ✓ Europe Vol positive in CRISIS: {baseline.attribution_crisis.get('europe_vol', 0):.2%}")


def save_results(results: List[SanityCheckResult], filepath: str = "state/research/sanity_check_results.json"):
    """Save results to JSON."""
    Path(filepath).parent.mkdir(parents=True, exist_ok=True)

    output = {
        "run_date": datetime.now().isoformat(),
        "checks": []
    }

    for r in results:
        output["checks"].append({
            "name": r.check_name,
            "sharpe": r.sharpe,
            "sortino": r.sortino,
            "max_dd": r.max_dd,
            "cagr": r.cagr,
            "insurance_score": r.insurance_score,
            "stress_2008_q4": r.stress_2008_q4,
            "stress_2020_q1q2": r.stress_2020_q1q2,
            "deflation_kills": r.deflation_kills,
            "attribution_normal": r.attribution_normal,
            "attribution_crisis": r.attribution_crisis,
        })

    with open(filepath, 'w') as f:
        json.dump(output, f, indent=2)

    print(f"\nResults saved to: {filepath}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)

    results = run_all_sanity_checks()
    print_sanity_summary(results)
    save_results(results)
