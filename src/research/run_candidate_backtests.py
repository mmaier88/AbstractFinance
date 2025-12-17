#!/usr/bin/env python3
"""
Candidate Engine Backtest Runner.

Phase N/O: Run backtests on all candidate engines and evaluate against implementation gates.

Usage:
    python -m src.research.run_candidate_backtests

Output:
    - Console report with pass/fail for each engine
    - JSON results file with detailed metrics
"""

import json
import logging
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Tuple
import numpy as np
import pandas as pd

from src.research.candidate_engines import (
    EUSovereignSpreadsEngine,
    EnergyShockEngine,
    ConditionalDurationEngine,
    BacktestResult,
    compute_sharpe,
    compute_max_drawdown,
    compute_insurance_score,
)
from src.research.institutional_backtest import (
    InstitutionalBacktest,
    StressAwareCostModel,
    StressAwareCostConfig,
    WalkForwardConfig,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class HistoricalDataGenerator:
    """
    Generate realistic historical data for backtesting.

    Uses known historical events to create realistic scenarios:
    - 2008: Global Financial Crisis
    - 2010-2012: EU Sovereign Debt Crisis
    - 2015-2016: Oil price collapse
    - 2020: COVID crash
    - 2022: Inflation shock + energy crisis
    """

    def __init__(self, start_date: str = "2008-01-01", end_date: str = "2024-12-31"):
        self.dates = pd.date_range(start=start_date, end=end_date, freq="B")
        np.random.seed(42)  # Reproducibility

    def generate_vix_series(self) -> pd.Series:
        """Generate VIX with crisis spikes."""
        n = len(self.dates)
        base_vix = 18 + np.random.normal(0, 2, n)

        # Add crisis spikes
        for dt, spike in [
            ("2008-09-15", 60),  # Lehman
            ("2008-10-15", 70),  # Peak GFC
            ("2010-05-06", 40),  # Flash crash
            ("2011-08-08", 45),  # US downgrade
            ("2011-11-01", 35),  # EU crisis peak
            ("2015-08-24", 40),  # China deval
            ("2020-03-16", 82),  # COVID peak
            ("2022-03-07", 35),  # Ukraine
            ("2022-06-13", 35),  # Inflation
        ]:
            idx = self.dates.get_indexer([pd.Timestamp(dt)], method='nearest')[0]
            # Create spike with decay
            for i in range(min(60, n - idx)):
                decay = np.exp(-i / 20)
                base_vix[idx + i] = max(base_vix[idx + i], spike * decay + 15)

        return pd.Series(np.clip(base_vix, 10, 90), index=self.dates, name="VIX")

    def generate_v2x_series(self, vix: pd.Series) -> pd.Series:
        """Generate V2X (VSTOXX) correlated with VIX but EU-focused."""
        # V2X typically 5-10% higher than VIX, more reactive to EU events
        v2x = vix * 1.05 + np.random.normal(0, 2, len(vix))

        # Extra EU crisis spikes
        for dt, spike in [
            ("2010-05-01", 50),   # Greece bailout
            ("2011-07-01", 45),   # Italy/Spain contagion
            ("2011-11-15", 55),   # EU crisis peak
            ("2012-06-01", 40),   # Spain banks
            ("2015-07-01", 35),   # Greece crisis
            ("2022-02-24", 45),   # Ukraine invasion
            ("2022-09-01", 35),   # Energy crisis
        ]:
            idx = self.dates.get_indexer([pd.Timestamp(dt)], method='nearest')[0]
            for i in range(min(40, len(v2x) - idx)):
                decay = np.exp(-i / 15)
                v2x.iloc[idx + i] = max(v2x.iloc[idx + i], spike * decay + 18)

        return pd.Series(np.clip(v2x.values, 12, 95), index=self.dates, name="V2X")

    def generate_btp_spread(self, v2x: pd.Series) -> pd.Series:
        """Generate BTP-Bund spread (Italy risk)."""
        # Base spread around 120 bps
        base = 120 + np.random.normal(0, 10, len(self.dates))

        # EU crisis: spreads blow out
        crisis_periods = [
            ("2010-05-01", "2010-07-01", 250),   # Greece -> contagion
            ("2011-07-01", "2012-01-01", 500),   # Italy crisis peak
            ("2012-01-01", "2012-08-01", 450),   # Draghi "whatever it takes"
            ("2018-05-01", "2018-08-01", 280),   # Italy populist govt
            ("2020-03-01", "2020-04-15", 280),   # COVID
            ("2022-06-01", "2022-10-01", 250),   # ECB rate hikes
        ]

        spread = pd.Series(base, index=self.dates)
        for start, end, level in crisis_periods:
            mask = (spread.index >= start) & (spread.index <= end)
            spread[mask] = level + np.random.normal(0, 20, mask.sum())

        return spread.clip(50, 600)

    def generate_oat_spread(self, btp_spread: pd.Series) -> pd.Series:
        """Generate OAT-Bund spread (France risk). Usually 1/3 of BTP."""
        oat = btp_spread * 0.3 + np.random.normal(0, 5, len(btp_spread))
        return pd.Series(np.clip(oat.values, 10, 200), index=self.dates, name="OAT_spread")

    def generate_oil_prices(self) -> pd.Series:
        """Generate oil prices with major moves."""
        n = len(self.dates)

        # Start at $90, random walk with mean reversion
        prices = [90]
        for i in range(1, n):
            drift = 0.0001 * (80 - prices[-1])  # Mean revert to $80
            shock = np.random.normal(0, 0.015)
            prices.append(prices[-1] * (1 + drift + shock))

        oil = pd.Series(prices, index=self.dates)

        # Major events
        events = [
            ("2008-07-01", 145),   # Peak before GFC
            ("2008-12-01", 35),    # GFC collapse
            ("2011-04-01", 115),   # Arab Spring
            ("2014-06-01", 105),   # Pre-shale crash
            ("2016-02-01", 26),    # Shale glut bottom
            ("2018-10-01", 85),    # Pre-COVID
            ("2020-04-01", 20),    # COVID crash
            ("2022-03-01", 130),   # Ukraine spike
            ("2022-06-01", 120),   # Energy crisis
        ]

        for dt, level in events:
            idx = self.dates.get_indexer([pd.Timestamp(dt)], method='nearest')[0]
            oil.iloc[max(0, idx-5):min(n, idx+30)] = level + np.random.normal(0, 5, min(35, n-idx+5))

        return oil.clip(15, 150)

    def generate_cpi_series(self) -> pd.Series:
        """Generate YoY CPI inflation."""
        n = len(self.dates)

        # Base inflation around 2%
        cpi = np.full(n, 2.0) + np.random.normal(0, 0.3, n)

        # Deflationary periods
        deflation_periods = [
            ("2009-01-01", "2009-12-01", -0.5),  # Post-GFC deflation
            ("2015-01-01", "2015-12-01", 0.2),   # Oil-driven low inflation
            ("2020-04-01", "2020-08-01", 0.3),   # COVID deflation
        ]

        for start, end, level in deflation_periods:
            mask = (self.dates >= start) & (self.dates <= end)
            cpi[mask] = level + np.random.normal(0, 0.2, mask.sum())

        # Inflation shock 2021-2023
        inflation_shock = [
            ("2021-06-01", "2021-12-01", 5.0),
            ("2022-01-01", "2022-06-01", 8.0),
            ("2022-07-01", "2022-12-01", 7.5),
            ("2023-01-01", "2023-06-01", 5.0),
            ("2023-07-01", "2024-01-01", 3.5),
        ]

        for start, end, level in inflation_shock:
            mask = (self.dates >= start) & (self.dates <= end)
            cpi[mask] = level + np.random.normal(0, 0.3, mask.sum())

        return pd.Series(cpi, index=self.dates, name="CPI_YoY")

    def generate_pmi_series(self) -> pd.Series:
        """Generate PMI (50 = neutral)."""
        n = len(self.dates)

        # Base PMI around 52 (slight expansion)
        pmi = np.full(n, 52.0) + np.random.normal(0, 2, n)

        # Recessions
        recessions = [
            ("2008-09-01", "2009-06-01", 35),  # GFC
            ("2011-08-01", "2012-01-01", 46),  # EU recession
            ("2020-03-01", "2020-05-01", 30),  # COVID
            ("2022-09-01", "2023-03-01", 47),  # Mild recession
        ]

        for start, end, level in recessions:
            mask = (self.dates >= start) & (self.dates <= end)
            pmi[mask] = level + np.random.normal(0, 2, mask.sum())

        return pd.Series(np.clip(pmi, 25, 65), index=self.dates, name="PMI")

    def generate_bund_returns(self, cpi: pd.Series) -> pd.Series:
        """Generate Bund daily returns (inverse to rates/inflation)."""
        n = len(self.dates)

        # Base return with slight positive drift (carry)
        returns = np.random.normal(0.0001, 0.005, n)

        # Flight to quality during crises (positive returns)
        crisis_periods = [
            ("2008-09-15", "2008-11-15", 0.003),  # GFC flight to quality
            ("2011-07-01", "2012-01-01", 0.002),  # EU crisis
            ("2020-03-01", "2020-04-01", 0.004),  # COVID
        ]

        returns_series = pd.Series(returns, index=self.dates)
        for start, end, boost in crisis_periods:
            mask = (self.dates >= start) & (self.dates <= end)
            returns_series[mask] += boost

        # 2022 disaster - rates up = bonds down
        inflation_period = (self.dates >= "2022-01-01") & (self.dates <= "2022-10-01")
        returns_series[inflation_period] = np.random.normal(-0.002, 0.008, inflation_period.sum())

        return returns_series

    def generate_all(self) -> Dict[str, pd.Series]:
        """Generate all historical series."""
        vix = self.generate_vix_series()
        v2x = self.generate_v2x_series(vix)
        btp_spread = self.generate_btp_spread(v2x)
        cpi = self.generate_cpi_series()

        return {
            "vix": vix,
            "v2x": v2x,
            "btp_spread": btp_spread,
            "oat_spread": self.generate_oat_spread(btp_spread),
            "oil_prices": self.generate_oil_prices(),
            "cpi": cpi,
            "pmi": self.generate_pmi_series(),
            "bund_returns": self.generate_bund_returns(cpi),
        }


def run_eu_sovereign_backtest(data: Dict[str, pd.Series]) -> BacktestResult:
    """Run backtest for EU Sovereign Spreads engine."""
    logger.info("Running EU Sovereign Spreads backtest...")

    engine = EUSovereignSpreadsEngine()

    # Generate spread change series (daily changes in bps)
    btp_changes = data["btp_spread"].diff().fillna(0)
    oat_changes = data["oat_spread"].diff().fillna(0)

    # Build returns manually with proper signal-based sizing
    returns = []
    for i, dt in enumerate(data["v2x"].index):
        v2x = data["v2x"].iloc[i]
        btp_spread = data["btp_spread"].iloc[i]
        oat_spread = data["oat_spread"].iloc[i]

        signal = engine.compute_signal(v2x, btp_spread, oat_spread, 1_000_000)

        daily_return = 0.0

        # Return from spread positions
        # Long Bund / Short BTP: profit when BTP spread NARROWS (negative change)
        if signal.target_allocation > 0 and i > 0:
            allocation = signal.target_allocation / 100

            # BTP spread trade
            if "FGBL_long_vs_FBTP" in signal.positions:
                btp_change = btp_changes.iloc[i]
                # Spread widening = loss, narrowing = profit
                # Scale: 1 bp change = ~0.01% return on allocated capital
                daily_return -= allocation * 0.5 * btp_change * 0.0002

            # OAT spread trade
            if "FGBL_long_vs_FOAT" in signal.positions:
                oat_change = oat_changes.iloc[i]
                daily_return -= allocation * 0.5 * oat_change * 0.0002

        returns.append(daily_return)

    returns = pd.Series(returns, index=data["v2x"].index)

    # Apply transaction costs
    cost_model = StressAwareCostModel(StressAwareCostConfig())
    for i, dt in enumerate(returns.index):
        if returns.iloc[i] != 0:
            vix = data["vix"].get(dt, 20)
            cost = cost_model.compute_transaction_cost(10000, "futures", vix) / 10000
            returns.iloc[i] -= cost * 0.1  # Assume 10% turnover

    # Define stress periods
    stress_mask = data["btp_spread"] > 200

    # Compute metrics
    sharpe = compute_sharpe(returns)
    max_dd = compute_max_drawdown(returns)
    total_return = (1 + returns).prod() - 1
    insurance_score = compute_insurance_score(returns, stress_mask)

    # Walk-forward (simplified)
    mid_point = len(returns) // 2
    is_returns = returns.iloc[:mid_point]
    oos_returns = returns.iloc[mid_point:]
    is_sharpe = compute_sharpe(is_returns)
    oos_sharpe = compute_sharpe(oos_returns)

    # Calculate average allocation
    allocations = []
    for dt in returns.index:
        signal = engine.compute_signal(
            data["v2x"].get(dt, 20),
            data["btp_spread"].get(dt, 100),
            data["oat_spread"].get(dt, 30),
            1_000_000,
        )
        allocations.append(signal.target_allocation)
    avg_allocation = np.mean(allocations)

    result = BacktestResult(
        engine_name="eu_sovereign_spreads",
        sharpe_ratio=sharpe,
        max_drawdown=max_dd,
        total_return=total_return,
        insurance_score=insurance_score,
        avg_allocation=avg_allocation,
        in_sample_sharpe=is_sharpe,
        out_of_sample_sharpe=oos_sharpe,
        parameter_stability=min(is_sharpe, oos_sharpe) / max(is_sharpe, oos_sharpe) if max(is_sharpe, oos_sharpe) > 0 else 0,
        portfolio_sharpe_with=0.0,  # Will be filled in ablation
        portfolio_sharpe_without=0.0,
        marginal_contribution=0.0,
    )
    result.evaluate_gates()

    return result


def run_energy_shock_backtest(data: Dict[str, pd.Series]) -> BacktestResult:
    """Run backtest for Energy Shock Hedge engine."""
    logger.info("Running Energy Shock Hedge backtest...")

    engine = EnergyShockEngine()

    # Compute oil returns
    oil_returns = data["oil_prices"].pct_change().fillna(0)

    # Simulate returns
    returns = engine.simulate_returns(
        oil_returns,
        data["v2x"],
        data["oil_prices"],
    )

    # Apply transaction costs
    cost_model = StressAwareCostModel(StressAwareCostConfig())
    for i, dt in enumerate(returns.index):
        if returns.iloc[i] != 0:
            vix = data["vix"].get(dt, 20)
            cost = cost_model.compute_transaction_cost(10000, "futures", vix) / 10000
            returns.iloc[i] -= cost * 0.05  # Lower turnover for trend following

    # Define stress periods (EU stress)
    stress_mask = data["v2x"] > 30

    # Compute metrics
    sharpe = compute_sharpe(returns)
    max_dd = compute_max_drawdown(returns)
    total_return = (1 + returns).prod() - 1
    insurance_score = compute_insurance_score(returns, stress_mask)

    # Walk-forward
    mid_point = len(returns) // 2
    is_sharpe = compute_sharpe(returns.iloc[:mid_point])
    oos_sharpe = compute_sharpe(returns.iloc[mid_point:])

    # Average allocation
    allocations = []
    for i, dt in enumerate(returns.index):
        if i >= 25:  # Need lookback
            prices_to_date = data["oil_prices"].iloc[:i+1]
            signal = engine.compute_signal(prices_to_date, data["v2x"].iloc[i])
            allocations.append(signal.target_allocation)
    avg_allocation = np.mean(allocations) if allocations else 0

    result = BacktestResult(
        engine_name="energy_shock",
        sharpe_ratio=sharpe,
        max_drawdown=max_dd,
        total_return=total_return,
        insurance_score=insurance_score,
        avg_allocation=avg_allocation,
        in_sample_sharpe=is_sharpe,
        out_of_sample_sharpe=oos_sharpe,
        parameter_stability=min(is_sharpe, oos_sharpe) / max(is_sharpe, oos_sharpe) if max(is_sharpe, oos_sharpe) > 0 else 0,
        portfolio_sharpe_with=0.0,
        portfolio_sharpe_without=0.0,
        marginal_contribution=0.0,
    )
    result.evaluate_gates()

    return result


def run_conditional_duration_backtest(data: Dict[str, pd.Series]) -> BacktestResult:
    """Run backtest for Conditional Duration engine."""
    logger.info("Running Conditional Duration backtest...")

    engine = ConditionalDurationEngine()

    # Simulate returns
    returns = engine.simulate_returns(
        data["bund_returns"],
        data["cpi"],
        data["pmi"],
    )

    # Apply transaction costs (very low turnover)
    cost_model = StressAwareCostModel(StressAwareCostConfig())
    for i, dt in enumerate(returns.index):
        if returns.iloc[i] != 0:
            vix = data["vix"].get(dt, 20)
            cost = cost_model.compute_transaction_cost(10000, "futures", vix) / 10000
            returns.iloc[i] -= cost * 0.02  # Very low turnover

    # Define stress periods (deflationary recessions)
    stress_mask = (data["cpi"] < 1.0) & (data["pmi"] < 49)

    # Compute metrics
    sharpe = compute_sharpe(returns)
    max_dd = compute_max_drawdown(returns)
    total_return = (1 + returns).prod() - 1
    insurance_score = compute_insurance_score(returns, stress_mask)

    # Walk-forward
    mid_point = len(returns) // 2
    is_sharpe = compute_sharpe(returns.iloc[:mid_point])
    oos_sharpe = compute_sharpe(returns.iloc[mid_point:])

    # Check 2022 behavior (should NOT be active during inflation)
    inflation_2022 = (data["cpi"].index >= "2022-01-01") & (data["cpi"].index <= "2022-12-31")
    returns_2022 = returns[inflation_2022]
    active_2022 = (returns_2022 != 0).sum() / len(returns_2022) if len(returns_2022) > 0 else 0

    # Average allocation
    avg_allocation = (returns != 0).mean() * 15  # 15% max allocation

    result = BacktestResult(
        engine_name="conditional_duration",
        sharpe_ratio=sharpe,
        max_drawdown=max_dd,
        total_return=total_return,
        insurance_score=insurance_score,
        avg_allocation=avg_allocation,
        in_sample_sharpe=is_sharpe,
        out_of_sample_sharpe=oos_sharpe,
        parameter_stability=min(is_sharpe, oos_sharpe) / max(is_sharpe, oos_sharpe) if max(is_sharpe, oos_sharpe) > 0 else 0,
        portfolio_sharpe_with=0.0,
        portfolio_sharpe_without=0.0,
        marginal_contribution=0.0,
    )
    result.evaluate_gates()

    logger.info(f"2022 activity rate: {active_2022:.1%} (should be near 0%)")

    return result


def run_ablation_analysis(
    data: Dict[str, pd.Series],
    results: List[BacktestResult],
) -> List[BacktestResult]:
    """
    Run ablation analysis to measure marginal contribution of each engine.

    Simulates adding each engine to a base portfolio and measures improvement.
    """
    logger.info("Running ablation analysis...")

    # Create base portfolio returns (existing sleeves)
    # Simulate realistic equity RV + vol hedge portfolio
    np.random.seed(42)
    n = len(data["vix"])

    # Base equity returns with positive drift
    equity_returns = np.random.normal(0.0004, 0.012, n)  # ~10% annual, 19% vol

    # Add crisis periods - equity drops
    for crisis_start, crisis_end, daily_loss in [
        ("2008-09-15", "2008-11-15", -0.02),
        ("2011-08-01", "2011-10-01", -0.01),
        ("2020-03-01", "2020-03-23", -0.04),
        ("2022-01-01", "2022-10-01", -0.003),
    ]:
        mask = (data["vix"].index >= crisis_start) & (data["vix"].index <= crisis_end)
        equity_returns[mask] = daily_loss + np.random.normal(0, 0.02, mask.sum())

    base_returns = pd.Series(equity_returns, index=data["vix"].index)
    base_sharpe = compute_sharpe(base_returns)

    for result in results:
        # Get engine returns using same methodology as individual backtests
        if result.engine_name == "eu_sovereign_spreads":
            engine = EUSovereignSpreadsEngine()
            btp_changes = data["btp_spread"].diff().fillna(0)
            oat_changes = data["oat_spread"].diff().fillna(0)

            engine_returns = []
            for i in range(len(data["v2x"])):
                v2x = data["v2x"].iloc[i]
                btp_spread = data["btp_spread"].iloc[i]
                oat_spread = data["oat_spread"].iloc[i]
                signal = engine.compute_signal(v2x, btp_spread, oat_spread, 1_000_000)

                daily_return = 0.0
                if signal.target_allocation > 0 and i > 0:
                    allocation = signal.target_allocation / 100
                    if "FGBL_long_vs_FBTP" in signal.positions:
                        daily_return -= allocation * 0.5 * btp_changes.iloc[i] * 0.0002
                    if "FGBL_long_vs_FOAT" in signal.positions:
                        daily_return -= allocation * 0.5 * oat_changes.iloc[i] * 0.0002
                engine_returns.append(daily_return)
            engine_returns = pd.Series(engine_returns, index=data["v2x"].index)

        elif result.engine_name == "energy_shock":
            engine = EnergyShockEngine()
            oil_returns = data["oil_prices"].pct_change().fillna(0)
            engine_returns = engine.simulate_returns(oil_returns, data["v2x"], data["oil_prices"])
        else:  # conditional_duration
            engine = ConditionalDurationEngine()
            engine_returns = engine.simulate_returns(data["bund_returns"], data["cpi"], data["pmi"])

        # Combined portfolio (80% base + 20% new engine)
        combined_returns = base_returns * 0.8 + engine_returns * 0.2
        combined_sharpe = compute_sharpe(combined_returns)

        result.portfolio_sharpe_without = base_sharpe
        result.portfolio_sharpe_with = combined_sharpe
        result.marginal_contribution = combined_sharpe - base_sharpe

        # Re-evaluate gates with ablation
        result.passes_portfolio_improvement = result.marginal_contribution > 0.1
        result.passes_all_gates = (
            result.passes_standalone_sharpe and
            result.passes_portfolio_improvement and
            result.passes_insurance_score and
            result.passes_walk_forward
        )

    return results


def print_report(results: List[BacktestResult]):
    """Print formatted backtest report."""
    print("\n" + "=" * 80)
    print("CANDIDATE ENGINE BACKTEST RESULTS")
    print("=" * 80)

    for result in results:
        status = "✅ APPROVED" if result.passes_all_gates else "❌ REJECTED"
        print(f"\n{'─' * 80}")
        print(f"Engine: {result.engine_name.upper()} {status}")
        print(f"{'─' * 80}")

        print(f"\nPerformance Metrics:")
        print(f"  Sharpe Ratio:     {result.sharpe_ratio:>8.2f}  {'✓' if result.passes_standalone_sharpe else '✗'} (threshold: > 0.3)")
        print(f"  Max Drawdown:     {result.max_drawdown:>8.1%}")
        print(f"  Total Return:     {result.total_return:>8.1%}")
        print(f"  Avg Allocation:   {result.avg_allocation:>8.1f}%")

        print(f"\nInsurance Quality:")
        print(f"  Insurance Score:  {result.insurance_score:>8.2f}  {'✓' if result.passes_insurance_score else '✗'} (threshold: > 0)")

        print(f"\nWalk-Forward Validation:")
        print(f"  In-Sample Sharpe: {result.in_sample_sharpe:>8.2f}")
        print(f"  OOS Sharpe:       {result.out_of_sample_sharpe:>8.2f}  {'✓' if result.passes_walk_forward else '✗'} (threshold: > 0)")
        print(f"  Stability:        {result.parameter_stability:>8.1%}")

        print(f"\nAblation Analysis:")
        print(f"  Portfolio w/o:    {result.portfolio_sharpe_without:>8.2f}")
        print(f"  Portfolio with:   {result.portfolio_sharpe_with:>8.2f}")
        print(f"  Contribution:     {result.marginal_contribution:>+8.2f}  {'✓' if result.passes_portfolio_improvement else '✗'} (threshold: > 0.1)")

        print(f"\nGate Summary:")
        gates = [
            ("Standalone Sharpe > 0.3", result.passes_standalone_sharpe),
            ("Insurance Score > 0", result.passes_insurance_score),
            ("OOS Sharpe > 0", result.passes_walk_forward),
            ("Portfolio Improvement > 0.1", result.passes_portfolio_improvement),
        ]
        for gate_name, passed in gates:
            print(f"  {'✓' if passed else '✗'} {gate_name}")

    print("\n" + "=" * 80)
    print("IMPLEMENTATION RECOMMENDATIONS")
    print("=" * 80)

    approved = [r for r in results if r.passes_all_gates]
    rejected = [r for r in results if not r.passes_all_gates]

    if approved:
        print("\nApproved for Phase O implementation:")
        for r in approved:
            print(f"  ✅ {r.engine_name}")
    else:
        print("\n⚠️  No engines passed all gates.")

    if rejected:
        print("\nRejected (archived for future review):")
        for r in rejected:
            failed_gates = []
            if not r.passes_standalone_sharpe:
                failed_gates.append("low Sharpe")
            if not r.passes_insurance_score:
                failed_gates.append("negative insurance")
            if not r.passes_walk_forward:
                failed_gates.append("OOS failed")
            if not r.passes_portfolio_improvement:
                failed_gates.append("no portfolio benefit")
            print(f"  ❌ {r.engine_name}: {', '.join(failed_gates)}")

    print("\n" + "=" * 80)


def main():
    """Run all candidate backtests."""
    logger.info("Generating historical data...")
    generator = HistoricalDataGenerator("2008-01-01", "2024-12-31")
    data = generator.generate_all()

    logger.info(f"Generated {len(data['vix'])} trading days of data")

    # Run individual backtests
    results = []

    eu_result = run_eu_sovereign_backtest(data)
    results.append(eu_result)

    energy_result = run_energy_shock_backtest(data)
    results.append(energy_result)

    duration_result = run_conditional_duration_backtest(data)
    results.append(duration_result)

    # Run ablation analysis
    results = run_ablation_analysis(data, results)

    # Print report
    print_report(results)

    # Save results to JSON
    output_path = Path("backtest_results.json")
    results_dict = {
        "run_date": str(date.today()),
        "data_range": "2008-01-01 to 2024-12-31",
        "engines": [
            {
                "name": r.engine_name,
                "sharpe": float(r.sharpe_ratio),
                "max_dd": float(r.max_drawdown),
                "total_return": float(r.total_return),
                "insurance_score": float(r.insurance_score),
                "oos_sharpe": float(r.out_of_sample_sharpe),
                "marginal_contribution": float(r.marginal_contribution),
                "passes_all_gates": bool(r.passes_all_gates),
            }
            for r in results
        ],
        "approved_for_implementation": [r.engine_name for r in results if r.passes_all_gates],
    }

    with open(output_path, "w") as f:
        json.dump(results_dict, f, indent=2)

    logger.info(f"Results saved to {output_path}")

    return results


if __name__ == "__main__":
    main()
