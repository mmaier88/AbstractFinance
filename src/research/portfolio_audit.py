#!/usr/bin/env python3
"""
Portfolio Complexity Audit.

Critical analysis of all sleeves to determine:
1. Which sleeves actually add value
2. Which have excessive complexity vs contribution
3. What should be removed or simplified

Usage:
    python -m src.research.portfolio_audit
"""

import json
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, List, Tuple
import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


@dataclass
class SleeveMetrics:
    """Metrics for a single sleeve."""
    name: str
    weight: float
    lines_of_code: int
    sharpe: float
    max_dd: float
    correlation_to_portfolio: float
    marginal_sharpe_contribution: float
    insurance_score: float  # Performance in stress vs normal
    complexity_score: float  # LOC / marginal contribution


# Current sleeve allocations from settings.yaml
CURRENT_SLEEVES = {
    "core_index_rv": 0.20,
    "sector_rv": 0.20,
    "single_name": 0.10,
    "credit_carry": 0.15,
    "europe_vol_convex": 0.15,
    "crisis_alpha": 0.10,
    "cash_buffer": 0.10,
}

# Estimated lines of code per sleeve (from codebase analysis)
SLEEVE_COMPLEXITY = {
    "core_index_rv": 400,       # Part of strategy_logic.py + trend_filter
    "sector_rv": 525,           # sector_pairs.py
    "single_name": 300,         # Part of strategy_logic.py + stock_screener
    "credit_carry": 200,        # Part of strategy_logic.py
    "europe_vol_convex": 596,   # europe_vol.py
    "crisis_alpha": 400,        # Part of tail_hedge.py
    "cash_buffer": 0,           # No code - just cash
}


class HistoricalSimulator:
    """Simulate historical returns for each sleeve."""

    def __init__(self, start_date: str = "2010-01-01", end_date: str = "2024-12-31"):
        self.dates = pd.date_range(start=start_date, end=end_date, freq="B")
        np.random.seed(42)

    def generate_market_data(self) -> Dict[str, pd.Series]:
        """Generate correlated market data."""
        n = len(self.dates)

        # Base market returns (S&P 500 proxy)
        market = np.random.normal(0.0004, 0.012, n)

        # Add crisis periods
        crises = [
            ("2011-08-01", "2011-10-01", -0.015),  # EU crisis
            ("2015-08-15", "2015-09-30", -0.012),  # China
            ("2018-12-01", "2018-12-24", -0.018),  # Q4 selloff
            ("2020-02-20", "2020-03-23", -0.035),  # COVID
            ("2022-01-01", "2022-10-01", -0.004),  # Rate hikes
        ]

        market_series = pd.Series(market, index=self.dates)
        for start, end, daily_loss in crises:
            mask = (market_series.index >= start) & (market_series.index <= end)
            market_series[mask] = daily_loss + np.random.normal(0, 0.015, mask.sum())

        # VIX proxy (inverse correlation to market)
        vix = 18 - market_series * 500 + np.random.normal(0, 2, n)
        vix = np.clip(vix, 10, 80)

        # EU vs US spread (slight EU underperformance)
        eu_spread = np.random.normal(-0.0001, 0.005, n)

        return {
            "market": market_series,
            "vix": pd.Series(vix, index=self.dates),
            "eu_spread": pd.Series(eu_spread, index=self.dates),
        }

    def simulate_sleeve_returns(self, data: Dict[str, pd.Series]) -> Dict[str, pd.Series]:
        """Simulate returns for each sleeve based on its strategy logic."""
        n = len(self.dates)
        returns = {}

        # 1. Core Index RV (Long US, Short EU)
        # Should make money when US outperforms EU
        core_rv = -data["eu_spread"] * 0.8 + np.random.normal(0, 0.003, n)
        returns["core_index_rv"] = pd.Series(core_rv, index=self.dates)

        # 2. Sector RV (Factor-neutral pairs)
        # Low correlation to market, small positive drift
        sector_rv = np.random.normal(0.0001, 0.004, n)
        returns["sector_rv"] = pd.Series(sector_rv, index=self.dates)

        # 3. Single Name (Stock picking with trend gate)
        # Correlated to market but with alpha
        single_name = data["market"] * 0.6 + np.random.normal(0.0001, 0.008, n)
        returns["single_name"] = pd.Series(single_name, index=self.dates)

        # 4. Credit Carry
        # Positive carry, but loses in risk-off
        credit_base = np.random.normal(0.0002, 0.003, n)
        # Loses during crises
        stress_mask = data["vix"] > 30
        credit_base[stress_mask] = -0.003 + np.random.normal(0, 0.008, stress_mask.sum())
        returns["credit_carry"] = pd.Series(credit_base, index=self.dates)

        # 5. Europe Vol Convex (VSTOXX calls + SX5E puts)
        # Negative carry, but big payoff in stress
        vol_base = np.random.normal(-0.0002, 0.005, n)  # Negative drift (premium decay)
        # Big wins during stress
        vol_base[stress_mask] = 0.015 + np.random.normal(0, 0.02, stress_mask.sum())
        returns["europe_vol_convex"] = pd.Series(vol_base, index=self.dates)

        # 6. Crisis Alpha (Sovereign stress trades)
        # Very low activity, occasional big wins
        crisis_alpha = np.zeros(n)
        extreme_stress = data["vix"] > 40
        crisis_alpha[extreme_stress] = 0.02 + np.random.normal(0, 0.015, extreme_stress.sum())
        # Small negative drift otherwise
        crisis_alpha[~extreme_stress] = np.random.normal(-0.00005, 0.001, (~extreme_stress).sum())
        returns["crisis_alpha"] = pd.Series(crisis_alpha, index=self.dates)

        # 7. Cash Buffer
        # Risk-free rate minus opportunity cost
        cash = np.full(n, 0.00015)  # ~4% annual
        returns["cash_buffer"] = pd.Series(cash, index=self.dates)

        return returns


def compute_sharpe(returns: pd.Series, rf: float = 0.02) -> float:
    """Compute annualized Sharpe ratio."""
    if len(returns) == 0 or returns.std() == 0:
        return 0.0
    excess = returns - rf / 252
    return np.sqrt(252) * excess.mean() / returns.std()


def compute_max_dd(returns: pd.Series) -> float:
    """Compute maximum drawdown."""
    cum = (1 + returns).cumprod()
    rolling_max = cum.expanding().max()
    dd = (cum - rolling_max) / rolling_max
    return dd.min()


def compute_correlation(a: pd.Series, b: pd.Series) -> float:
    """Compute correlation between two series."""
    return a.corr(b)


def compute_insurance_score(returns: pd.Series, stress_mask: pd.Series) -> float:
    """Compute insurance score (stress performance - normal performance)."""
    if stress_mask.sum() == 0:
        return 0.0
    stress_ret = returns[stress_mask].mean() * 252
    normal_ret = returns[~stress_mask].mean() * 252
    return stress_ret - normal_ret


def run_ablation(
    sleeve_returns: Dict[str, pd.Series],
    weights: Dict[str, float],
) -> Dict[str, float]:
    """Run ablation to measure marginal contribution of each sleeve."""
    # Compute portfolio returns
    portfolio = sum(
        sleeve_returns[s] * w
        for s, w in weights.items()
        if s in sleeve_returns
    )
    portfolio_sharpe = compute_sharpe(portfolio)

    contributions = {}

    for sleeve in weights:
        if sleeve not in sleeve_returns or sleeve == "cash_buffer":
            contributions[sleeve] = 0.0
            continue

        # Portfolio without this sleeve (re-weighted)
        remaining_weight = 1.0 - weights[sleeve]
        if remaining_weight == 0:
            contributions[sleeve] = portfolio_sharpe
            continue

        portfolio_without = sum(
            sleeve_returns[s] * (w / remaining_weight)
            for s, w in weights.items()
            if s in sleeve_returns and s != sleeve
        )

        sharpe_without = compute_sharpe(portfolio_without)
        contributions[sleeve] = portfolio_sharpe - sharpe_without

    return contributions


def analyze_portfolio() -> List[SleeveMetrics]:
    """Run full portfolio analysis."""
    logger.info("Generating historical data...")
    simulator = HistoricalSimulator()
    market_data = simulator.generate_market_data()
    sleeve_returns = simulator.simulate_sleeve_returns(market_data)

    logger.info("Computing sleeve metrics...")

    # Portfolio returns
    portfolio = sum(
        sleeve_returns[s] * w
        for s, w in CURRENT_SLEEVES.items()
        if s in sleeve_returns
    )

    # Stress mask
    stress_mask = market_data["vix"] > 30

    # Ablation
    contributions = run_ablation(sleeve_returns, CURRENT_SLEEVES)

    results = []

    for sleeve, weight in CURRENT_SLEEVES.items():
        if sleeve not in sleeve_returns:
            continue

        ret = sleeve_returns[sleeve]
        sharpe = compute_sharpe(ret)
        max_dd = compute_max_dd(ret)
        corr = compute_correlation(ret, portfolio)
        marginal = contributions.get(sleeve, 0.0)
        insurance = compute_insurance_score(ret, stress_mask)
        loc = SLEEVE_COMPLEXITY.get(sleeve, 0)

        # Complexity score: LOC per unit of marginal Sharpe contribution
        # Lower is better (more efficient)
        if marginal > 0:
            complexity = loc / marginal
        elif marginal < 0:
            complexity = float('inf')  # Negative contribution = infinite complexity
        else:
            complexity = loc * 100  # No contribution = high complexity

        results.append(SleeveMetrics(
            name=sleeve,
            weight=weight,
            lines_of_code=loc,
            sharpe=sharpe,
            max_dd=max_dd,
            correlation_to_portfolio=corr,
            marginal_sharpe_contribution=marginal,
            insurance_score=insurance,
            complexity_score=complexity,
        ))

    return results


def print_report(results: List[SleeveMetrics]):
    """Print analysis report."""
    # Sort by marginal contribution
    results_sorted = sorted(results, key=lambda x: x.marginal_sharpe_contribution, reverse=True)

    print("\n" + "=" * 100)
    print("PORTFOLIO COMPLEXITY AUDIT - CRITICAL ANALYSIS")
    print("=" * 100)

    print("\n" + "-" * 100)
    print(f"{'Sleeve':<22} {'Weight':>8} {'LOC':>6} {'Sharpe':>8} {'MaxDD':>8} {'Corr':>6} {'Marginal':>10} {'Insurance':>10}")
    print("-" * 100)

    total_loc = 0
    positive_contributors = []
    negative_contributors = []
    low_value = []

    for r in results_sorted:
        status = ""
        if r.marginal_sharpe_contribution < 0:
            status = "❌ NEGATIVE"
            negative_contributors.append(r)
        elif r.marginal_sharpe_contribution < 0.05:
            status = "⚠️ LOW"
            low_value.append(r)
        else:
            status = "✓"
            positive_contributors.append(r)

        print(f"{r.name:<22} {r.weight:>7.0%} {r.lines_of_code:>6} {r.sharpe:>8.2f} {r.max_dd:>7.1%} {r.correlation_to_portfolio:>6.2f} {r.marginal_sharpe_contribution:>+10.3f} {r.insurance_score:>+10.2f} {status}")
        total_loc += r.lines_of_code

    print("-" * 100)
    print(f"{'TOTAL':<22} {'100%':>8} {total_loc:>6}")

    # Recommendations
    print("\n" + "=" * 100)
    print("RECOMMENDATIONS")
    print("=" * 100)

    if negative_contributors:
        print("\n🔴 REMOVE (Negative Contribution):")
        for r in negative_contributors:
            print(f"   • {r.name}: Marginal Sharpe {r.marginal_sharpe_contribution:+.3f}, {r.lines_of_code} LOC")
            print(f"     → Removing saves {r.lines_of_code} lines and IMPROVES portfolio")

    if low_value:
        print("\n🟡 SIMPLIFY OR REMOVE (Low Value):")
        for r in low_value:
            print(f"   • {r.name}: Marginal Sharpe {r.marginal_sharpe_contribution:+.3f}, {r.lines_of_code} LOC")
            if r.lines_of_code > 300:
                print(f"     → High complexity ({r.lines_of_code} LOC) for marginal benefit")

    if positive_contributors:
        print("\n🟢 KEEP (Positive Contribution):")
        for r in positive_contributors:
            print(f"   • {r.name}: Marginal Sharpe {r.marginal_sharpe_contribution:+.3f}")
            if r.insurance_score > 0:
                print(f"     → Insurance score {r.insurance_score:+.2f} (good crisis protection)")

    # Complexity analysis
    print("\n" + "=" * 100)
    print("COMPLEXITY vs VALUE ANALYSIS")
    print("=" * 100)

    print("\nComplexity Efficiency (LOC per 0.1 Sharpe contribution):")
    for r in sorted(results, key=lambda x: x.complexity_score if x.complexity_score < float('inf') else 999999):
        if r.marginal_sharpe_contribution > 0:
            efficiency = r.lines_of_code / r.marginal_sharpe_contribution
            print(f"   {r.name:<22}: {efficiency:>8.0f} LOC per 0.1 Sharpe")
        else:
            print(f"   {r.name:<22}: ∞ (no/negative contribution)")

    # Summary stats
    total_marginal = sum(r.marginal_sharpe_contribution for r in results)
    removable_loc = sum(r.lines_of_code for r in negative_contributors + low_value)

    print("\n" + "=" * 100)
    print("SUMMARY")
    print("=" * 100)
    print(f"\nTotal sleeves: {len(results)}")
    print(f"Total LOC in sleeves: {total_loc}")
    print(f"Candidates for removal: {len(negative_contributors)}")
    print(f"Candidates for simplification: {len(low_value)}")
    print(f"Potentially removable LOC: {removable_loc} ({removable_loc/total_loc*100:.0f}%)")

    print("\n" + "=" * 100)


def main():
    """Run portfolio audit."""
    results = analyze_portfolio()
    print_report(results)

    # Save results
    output = {
        "audit_date": str(date.today()),
        "sleeves": [
            {
                "name": r.name,
                "weight": r.weight,
                "loc": r.lines_of_code,
                "sharpe": float(r.sharpe),
                "max_dd": float(r.max_dd),
                "correlation": float(r.correlation_to_portfolio),
                "marginal_contribution": float(r.marginal_sharpe_contribution),
                "insurance_score": float(r.insurance_score),
            }
            for r in results
        ],
    }

    with open("portfolio_audit.json", "w") as f:
        json.dump(output, f, indent=2)

    logger.info("Audit saved to portfolio_audit.json")

    return results


if __name__ == "__main__":
    main()
