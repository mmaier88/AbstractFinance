"""
Institutional-Grade Backtest Harness for AbstractFinance.

Phase L: v2.2 Roadmap - Evidence-based testing before any new sleeve implementation.

Key upgrades over basic backtest:
1. Stress-aware transaction costs (2-5x spread widening)
2. Futures roll simulation with basis and gap risk
3. Walk-forward validation (rolling 3yr train / 1yr test)
4. Ablation framework (measure each sleeve's marginal value)

Usage:
    harness = InstitutionalBacktest(config)
    results = harness.run_walk_forward()
    ablation = harness.run_ablation_suite()
"""

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple, Any, Callable
import numpy as np
import pandas as pd
from enum import Enum

logger = logging.getLogger(__name__)


class StressLevel(Enum):
    """Market stress levels for cost modeling."""
    CALM = "calm"           # VIX < 15
    NORMAL = "normal"       # 15 <= VIX < 25
    ELEVATED = "elevated"   # 25 <= VIX < 35
    CRISIS = "crisis"       # VIX >= 35


@dataclass
class StressAwareCostConfig:
    """
    Transaction cost model with stress-dependent spread widening.

    Real-world observation: bid-ask spreads widen 2-5x during stress.
    """
    # Base spreads (calm markets, VIX < 15)
    base_equity_spread_bps: float = 3.0
    base_etf_spread_bps: float = 2.0
    base_futures_spread_bps: float = 0.5
    base_fx_spread_bps: float = 1.0
    base_bond_futures_spread_bps: float = 0.3

    # Spread multipliers by stress level
    spread_multipliers: Dict[str, float] = field(default_factory=lambda: {
        "calm": 1.0,
        "normal": 1.5,
        "elevated": 3.0,
        "crisis": 5.0,
    })

    # Market impact (additional to spread, for larger orders)
    # Impact = base_impact * sqrt(order_size / ADV)
    equity_impact_bps_per_pct_adv: float = 5.0
    futures_impact_bps_per_pct_adv: float = 1.0

    # Fixed costs
    commission_per_trade: float = 1.0

    # Carry costs (annual)
    short_borrow_bps: float = 50.0
    short_dividend_bps: float = 200.0
    futures_margin_rate: float = 0.05


@dataclass
class FuturesRollConfig:
    """Configuration for futures roll simulation."""
    # Roll timing (days before expiry)
    roll_days_before_expiry: int = 5

    # Basis cost (contango/backwardation)
    # Positive = contango (roll costs money), negative = backwardation
    avg_equity_index_basis_bps_monthly: float = 3.0   # ~0.4% annual carry cost
    avg_bond_futures_basis_bps_monthly: float = -2.0  # Slightly negative (convenience yield)
    avg_vol_futures_basis_bps_monthly: float = 15.0   # VIX/V2X contango is steep

    # Basis volatility (stress can flip term structure)
    basis_vol_multiplier_stress: float = 3.0

    # Gap risk at roll (slippage during roll period)
    roll_slippage_bps: float = 2.0
    roll_slippage_stress_multiplier: float = 2.0


@dataclass
class WalkForwardConfig:
    """Configuration for walk-forward validation."""
    train_years: int = 3
    test_years: int = 1
    step_months: int = 6  # Roll forward every 6 months
    min_train_days: int = 500  # Minimum training data


@dataclass
class AblationConfig:
    """Configuration for ablation testing."""
    # Which sleeves to test removing
    sleeves_to_ablate: List[str] = field(default_factory=lambda: [
        "core_index_rv",
        "sector_rv",
        "europe_vol_convex",
        "crisis_alpha",
        "credit_carry",
    ])

    # Significance threshold
    sharpe_improvement_threshold: float = 0.1
    drawdown_improvement_threshold: float = 0.05


class StressAwareCostModel:
    """
    Transaction cost model that accounts for stress-dependent spread widening.

    Key insight: During 2008, 2020, 2022 - spreads widened 3-5x.
    A backtest using calm-market spreads will overestimate performance.
    """

    def __init__(self, config: StressAwareCostConfig):
        self.config = config

    def get_stress_level(self, vix: float) -> StressLevel:
        """Determine stress level from VIX."""
        if vix < 15:
            return StressLevel.CALM
        elif vix < 25:
            return StressLevel.NORMAL
        elif vix < 35:
            return StressLevel.ELEVATED
        else:
            return StressLevel.CRISIS

    def get_spread_multiplier(self, vix: float) -> float:
        """Get spread multiplier based on current VIX."""
        level = self.get_stress_level(vix)
        return self.config.spread_multipliers.get(level.value, 1.5)

    def compute_transaction_cost(
        self,
        notional: float,
        asset_class: str,
        vix: float,
        adv: Optional[float] = None,  # Average daily volume
    ) -> float:
        """
        Compute realistic transaction cost including stress widening.

        Args:
            notional: Trade size in USD
            asset_class: "equity", "etf", "futures", "fx", "bond_futures"
            vix: Current VIX level
            adv: Average daily volume (for market impact)

        Returns:
            Transaction cost in USD
        """
        # Base spread by asset class
        base_spreads = {
            "equity": self.config.base_equity_spread_bps,
            "etf": self.config.base_etf_spread_bps,
            "futures": self.config.base_futures_spread_bps,
            "fx": self.config.base_fx_spread_bps,
            "bond_futures": self.config.base_bond_futures_spread_bps,
        }
        base_spread = base_spreads.get(asset_class, self.config.base_etf_spread_bps)

        # Apply stress multiplier
        multiplier = self.get_spread_multiplier(vix)
        effective_spread = base_spread * multiplier

        # Spread cost (half spread each way)
        spread_cost = abs(notional) * effective_spread / 10000

        # Market impact (if ADV provided)
        impact_cost = 0.0
        if adv and adv > 0:
            pct_of_adv = abs(notional) / adv
            if asset_class in ["equity", "etf"]:
                impact_bps = self.config.equity_impact_bps_per_pct_adv * np.sqrt(pct_of_adv)
            else:
                impact_bps = self.config.futures_impact_bps_per_pct_adv * np.sqrt(pct_of_adv)
            impact_cost = abs(notional) * impact_bps / 10000

        # Commission
        commission = self.config.commission_per_trade

        return spread_cost + impact_cost + commission

    def compute_daily_carry(
        self,
        short_equity_notional: float,
        futures_notional: float,
    ) -> float:
        """Compute daily carry costs."""
        daily_factor = 1 / 252

        # Short equity costs
        borrow = abs(short_equity_notional) * self.config.short_borrow_bps / 10000 * daily_factor
        dividend = abs(short_equity_notional) * self.config.short_dividend_bps / 10000 * daily_factor

        # Futures margin financing
        margin = abs(futures_notional) * 0.10  # ~10% margin
        financing = margin * self.config.futures_margin_rate * daily_factor

        return borrow + dividend + financing


class FuturesRollSimulator:
    """
    Simulate realistic futures roll costs and gap risk.

    Key insight: VIX/V2X futures are typically in steep contango.
    Rolling monthly costs ~1-2% in normal markets, can be 3-4% in stress.
    """

    def __init__(self, config: FuturesRollConfig):
        self.config = config
        self._last_roll_date: Dict[str, date] = {}

    def compute_roll_cost(
        self,
        futures_type: str,  # "equity_index", "bond", "vol"
        notional: float,
        vix: float,
        days_in_month: int = 21,
    ) -> float:
        """
        Compute roll cost for futures position.

        Args:
            futures_type: Type of futures
            notional: Position notional
            vix: Current VIX for stress adjustment
            days_in_month: Trading days in current month

        Returns:
            Roll cost in USD (positive = cost, negative = benefit)
        """
        # Base monthly basis by futures type
        basis_map = {
            "equity_index": self.config.avg_equity_index_basis_bps_monthly,
            "bond": self.config.avg_bond_futures_basis_bps_monthly,
            "vol": self.config.avg_vol_futures_basis_bps_monthly,
        }
        base_basis = basis_map.get(futures_type, self.config.avg_equity_index_basis_bps_monthly)

        # Stress adjustment (basis can widen or flip in stress)
        is_stress = vix > 30
        if is_stress:
            if futures_type == "vol":
                # Vol futures basis often inverts in crisis (backwardation)
                base_basis = -base_basis * 0.5
            else:
                # Equity index basis can widen
                base_basis = base_basis * self.config.basis_vol_multiplier_stress

        # Daily pro-rated cost
        daily_basis = base_basis / days_in_month
        basis_cost = abs(notional) * daily_basis / 10000

        return basis_cost

    def compute_roll_slippage(
        self,
        notional: float,
        vix: float,
    ) -> float:
        """Compute slippage during roll period."""
        base_slip = self.config.roll_slippage_bps
        if vix > 30:
            base_slip *= self.config.roll_slippage_stress_multiplier

        return abs(notional) * base_slip / 10000


@dataclass
class WalkForwardResult:
    """Result from a single walk-forward window."""
    train_start: date
    train_end: date
    test_start: date
    test_end: date

    # In-sample metrics
    is_sharpe: float
    is_max_dd: float
    is_total_return: float

    # Out-of-sample metrics
    oos_sharpe: float
    oos_max_dd: float
    oos_total_return: float
    oos_insurance_score: float

    # Stability metrics
    parameter_drift: float  # How much optimal params changed
    regime_accuracy: float  # Did regime detection work OOS?


@dataclass
class AblationResult:
    """Result from ablating (removing) a sleeve."""
    sleeve_removed: str

    # Full portfolio metrics
    full_sharpe: float
    full_max_dd: float
    full_insurance_score: float

    # Without this sleeve
    ablated_sharpe: float
    ablated_max_dd: float
    ablated_insurance_score: float

    # Marginal contribution
    sharpe_contribution: float  # full - ablated
    dd_contribution: float      # ablated - full (positive = this sleeve helps)
    insurance_contribution: float

    # Verdict
    is_valuable: bool  # Does removing it make things worse?


@dataclass
class InstitutionalBacktestResult:
    """Complete institutional-grade backtest results."""
    # Walk-forward results
    walk_forward_results: List[WalkForwardResult]
    avg_oos_sharpe: float
    avg_oos_max_dd: float
    oos_sharpe_std: float  # Stability across windows

    # Ablation results
    ablation_results: List[AblationResult]
    valuable_sleeves: List[str]
    redundant_sleeves: List[str]

    # Overall assessment
    is_robust: bool  # Passes walk-forward
    passed_ablation: bool  # All sleeves add value

    # Detailed metrics
    total_transaction_costs: float
    total_roll_costs: float
    stress_period_analysis: Dict[str, Any]


class InstitutionalBacktest:
    """
    Institutional-grade backtesting harness.

    Provides:
    1. Stress-aware transaction costs
    2. Futures roll simulation
    3. Walk-forward validation
    4. Ablation testing
    """

    STRESS_PERIODS = {
        "GFC_2008": (date(2008, 9, 1), date(2009, 3, 31)),
        "Euro_Crisis_2011": (date(2011, 7, 1), date(2011, 12, 31)),
        "COVID_2020": (date(2020, 2, 15), date(2020, 4, 15)),
        "Rate_Shock_2022": (date(2022, 1, 1), date(2022, 10, 31)),
    }

    def __init__(
        self,
        cost_config: Optional[StressAwareCostConfig] = None,
        roll_config: Optional[FuturesRollConfig] = None,
        walk_forward_config: Optional[WalkForwardConfig] = None,
        ablation_config: Optional[AblationConfig] = None,
    ):
        self.cost_config = cost_config or StressAwareCostConfig()
        self.roll_config = roll_config or FuturesRollConfig()
        self.wf_config = walk_forward_config or WalkForwardConfig()
        self.ablation_config = ablation_config or AblationConfig()

        self.cost_model = StressAwareCostModel(self.cost_config)
        self.roll_simulator = FuturesRollSimulator(self.roll_config)

    def run_single_backtest(
        self,
        returns_df: pd.DataFrame,
        vix_series: pd.Series,
        sleeve_weights: Dict[str, float],
        start_date: date,
        end_date: date,
        initial_capital: float = 1_000_000,
    ) -> Dict[str, Any]:
        """
        Run a single backtest with institutional-grade costs.

        Args:
            returns_df: DataFrame with sleeve return columns
            vix_series: VIX levels for stress adjustment
            sleeve_weights: Weight for each sleeve
            start_date: Backtest start
            end_date: Backtest end
            initial_capital: Starting capital

        Returns:
            Dict with metrics
        """
        # Convert dates to pandas timestamps for comparison
        start_ts = pd.Timestamp(start_date)
        end_ts = pd.Timestamp(end_date)

        # Filter to date range
        mask = (returns_df.index >= start_ts) & (returns_df.index <= end_ts)
        returns = returns_df.loc[mask].copy()
        vix = vix_series.reindex(returns.index, method='ffill')

        if len(returns) < 20:
            return {"error": "Insufficient data"}

        # Initialize
        nav = initial_capital
        navs = []
        total_tx_costs = 0.0
        total_roll_costs = 0.0
        prev_positions = {s: 0.0 for s in sleeve_weights}

        for dt, row in returns.iterrows():
            current_vix = vix.get(dt, 20.0)

            # Compute target positions
            positions = {s: nav * w for s, w in sleeve_weights.items()}

            # Transaction costs (on position changes)
            for sleeve, target in positions.items():
                change = abs(target - prev_positions.get(sleeve, 0))
                if change > 100:  # Min threshold
                    asset_class = "futures" if "vol" in sleeve else "etf"
                    tx_cost = self.cost_model.compute_transaction_cost(
                        notional=change,
                        asset_class=asset_class,
                        vix=current_vix,
                    )
                    total_tx_costs += tx_cost

            # Roll costs (for futures-based sleeves)
            for sleeve in ["europe_vol_convex", "crisis_alpha"]:
                if sleeve in positions:
                    roll_cost = self.roll_simulator.compute_roll_cost(
                        futures_type="vol",
                        notional=positions[sleeve],
                        vix=current_vix,
                    )
                    total_roll_costs += roll_cost

            # Compute portfolio return
            portfolio_return = 0.0
            for sleeve, weight in sleeve_weights.items():
                if sleeve in row.index:
                    portfolio_return += row[sleeve] * weight

            # Deduct costs
            daily_costs = (total_tx_costs + total_roll_costs) / nav if nav > 0 else 0
            portfolio_return -= daily_costs * 0.1  # Amortize over ~10 days

            # Update NAV
            nav *= (1 + portfolio_return)
            navs.append(nav)
            prev_positions = positions

        # Compute metrics
        navs = pd.Series(navs, index=returns.index)
        daily_returns = navs.pct_change().dropna()

        total_return = (navs.iloc[-1] / initial_capital) - 1
        sharpe = daily_returns.mean() / daily_returns.std() * np.sqrt(252) if daily_returns.std() > 0 else 0

        # Max drawdown
        rolling_max = navs.cummax()
        drawdowns = (navs - rolling_max) / rolling_max
        max_dd = drawdowns.min()

        # Insurance score (performance on stress days)
        stress_days = vix > 25
        stress_returns = daily_returns[stress_days]
        normal_returns = daily_returns[~stress_days]
        insurance_score = (stress_returns.mean() - normal_returns.mean()) * 252 if len(stress_returns) > 0 else 0

        return {
            "sharpe": sharpe,
            "max_dd": max_dd,
            "total_return": total_return,
            "insurance_score": insurance_score,
            "total_tx_costs": total_tx_costs,
            "total_roll_costs": total_roll_costs,
            "navs": navs,
        }

    def run_walk_forward(
        self,
        returns_df: pd.DataFrame,
        vix_series: pd.Series,
        sleeve_weights: Dict[str, float],
        full_start: date = date(2008, 1, 1),
        full_end: Optional[date] = None,
    ) -> List[WalkForwardResult]:
        """
        Run walk-forward validation.

        Rolling window: 3 years training, 1 year testing, step every 6 months.
        """
        if full_end is None:
            full_end = date.today()

        results = []

        current_start = full_start
        train_days = self.wf_config.train_years * 252
        test_days = self.wf_config.test_years * 252
        step_days = self.wf_config.step_months * 21

        while True:
            train_end = current_start + timedelta(days=int(train_days * 365/252))
            test_start = train_end + timedelta(days=1)
            test_end = test_start + timedelta(days=int(test_days * 365/252))

            if test_end > full_end:
                break

            # In-sample backtest
            is_result = self.run_single_backtest(
                returns_df, vix_series, sleeve_weights,
                current_start, train_end
            )

            # Out-of-sample backtest
            oos_result = self.run_single_backtest(
                returns_df, vix_series, sleeve_weights,
                test_start, test_end
            )

            if "error" not in is_result and "error" not in oos_result:
                results.append(WalkForwardResult(
                    train_start=current_start,
                    train_end=train_end,
                    test_start=test_start,
                    test_end=test_end,
                    is_sharpe=is_result["sharpe"],
                    is_max_dd=is_result["max_dd"],
                    is_total_return=is_result["total_return"],
                    oos_sharpe=oos_result["sharpe"],
                    oos_max_dd=oos_result["max_dd"],
                    oos_total_return=oos_result["total_return"],
                    oos_insurance_score=oos_result["insurance_score"],
                    parameter_drift=abs(is_result["sharpe"] - oos_result["sharpe"]),
                    regime_accuracy=0.0,  # TODO: implement
                ))

            current_start += timedelta(days=step_days)

        return results

    def run_ablation_suite(
        self,
        returns_df: pd.DataFrame,
        vix_series: pd.Series,
        sleeve_weights: Dict[str, float],
        start_date: date = date(2008, 1, 1),
        end_date: Optional[date] = None,
    ) -> List[AblationResult]:
        """
        Run ablation tests: remove each sleeve and measure impact.

        A sleeve is valuable if removing it makes the portfolio worse.
        """
        if end_date is None:
            end_date = date.today()

        results = []

        # Full portfolio baseline
        full_result = self.run_single_backtest(
            returns_df, vix_series, sleeve_weights,
            start_date, end_date
        )

        if "error" in full_result:
            return results

        # Test removing each sleeve
        for sleeve in self.ablation_config.sleeves_to_ablate:
            if sleeve not in sleeve_weights:
                continue

            # Create weights without this sleeve (redistribute to others)
            ablated_weights = sleeve_weights.copy()
            removed_weight = ablated_weights.pop(sleeve, 0)

            # Redistribute removed weight proportionally
            total_remaining = sum(ablated_weights.values())
            if total_remaining > 0:
                for s in ablated_weights:
                    ablated_weights[s] *= (1 + removed_weight / total_remaining)

            # Run ablated backtest
            ablated_result = self.run_single_backtest(
                returns_df, vix_series, ablated_weights,
                start_date, end_date
            )

            if "error" in ablated_result:
                continue

            # Compute contributions
            sharpe_contrib = full_result["sharpe"] - ablated_result["sharpe"]
            dd_contrib = ablated_result["max_dd"] - full_result["max_dd"]  # Positive if sleeve helps
            insurance_contrib = full_result["insurance_score"] - ablated_result["insurance_score"]

            # Is it valuable?
            is_valuable = (
                sharpe_contrib > self.ablation_config.sharpe_improvement_threshold or
                dd_contrib > self.ablation_config.drawdown_improvement_threshold or
                insurance_contrib > 0.02  # 2% better insurance
            )

            results.append(AblationResult(
                sleeve_removed=sleeve,
                full_sharpe=full_result["sharpe"],
                full_max_dd=full_result["max_dd"],
                full_insurance_score=full_result["insurance_score"],
                ablated_sharpe=ablated_result["sharpe"],
                ablated_max_dd=ablated_result["max_dd"],
                ablated_insurance_score=ablated_result["insurance_score"],
                sharpe_contribution=sharpe_contrib,
                dd_contribution=dd_contrib,
                insurance_contribution=insurance_contrib,
                is_valuable=is_valuable,
            ))

        return results

    def run_full_analysis(
        self,
        returns_df: pd.DataFrame,
        vix_series: pd.Series,
        sleeve_weights: Dict[str, float],
    ) -> InstitutionalBacktestResult:
        """
        Run complete institutional analysis.

        Returns:
            InstitutionalBacktestResult with all metrics
        """
        logger.info("Running walk-forward validation...")
        wf_results = self.run_walk_forward(returns_df, vix_series, sleeve_weights)

        logger.info("Running ablation suite...")
        ablation_results = self.run_ablation_suite(returns_df, vix_series, sleeve_weights)

        # Compute summary metrics
        if wf_results:
            avg_oos_sharpe = np.mean([r.oos_sharpe for r in wf_results])
            avg_oos_max_dd = np.mean([r.oos_max_dd for r in wf_results])
            oos_sharpe_std = np.std([r.oos_sharpe for r in wf_results])
        else:
            avg_oos_sharpe = 0
            avg_oos_max_dd = 0
            oos_sharpe_std = 0

        valuable = [r.sleeve_removed for r in ablation_results if r.is_valuable]
        redundant = [r.sleeve_removed for r in ablation_results if not r.is_valuable]

        # Robustness checks
        is_robust = (
            len(wf_results) >= 3 and
            avg_oos_sharpe > 0.3 and
            oos_sharpe_std < 0.5  # Stable across windows
        )

        passed_ablation = len(redundant) == 0

        # Full backtest for cost totals
        full_result = self.run_single_backtest(
            returns_df, vix_series, sleeve_weights,
            date(2008, 1, 1), date.today()
        )

        return InstitutionalBacktestResult(
            walk_forward_results=wf_results,
            avg_oos_sharpe=avg_oos_sharpe,
            avg_oos_max_dd=avg_oos_max_dd,
            oos_sharpe_std=oos_sharpe_std,
            ablation_results=ablation_results,
            valuable_sleeves=valuable,
            redundant_sleeves=redundant,
            is_robust=is_robust,
            passed_ablation=passed_ablation,
            total_transaction_costs=full_result.get("total_tx_costs", 0),
            total_roll_costs=full_result.get("total_roll_costs", 0),
            stress_period_analysis={},
        )


def print_ablation_report(results: List[AblationResult]) -> None:
    """Print formatted ablation report."""
    print("\n" + "="*70)
    print("ABLATION ANALYSIS - Sleeve Marginal Value")
    print("="*70)

    for r in results:
        status = "✅ VALUABLE" if r.is_valuable else "❌ REDUNDANT"
        print(f"\n{r.sleeve_removed}: {status}")
        print(f"  Sharpe contribution:    {r.sharpe_contribution:+.3f}")
        print(f"  Drawdown contribution:  {r.dd_contribution:+.1%}")
        print(f"  Insurance contribution: {r.insurance_contribution:+.1%}")


def print_walk_forward_report(results: List[WalkForwardResult]) -> None:
    """Print formatted walk-forward report."""
    print("\n" + "="*70)
    print("WALK-FORWARD VALIDATION")
    print("="*70)

    print(f"\n{'Window':<20} {'IS Sharpe':<12} {'OOS Sharpe':<12} {'OOS MaxDD':<12} {'Insurance':<12}")
    print("-"*70)

    for i, r in enumerate(results):
        window = f"{r.train_start} → {r.test_end}"
        print(f"{window:<20} {r.is_sharpe:>10.2f}   {r.oos_sharpe:>10.2f}   {r.oos_max_dd:>10.1%}   {r.oos_insurance_score:>10.1%}")

    if results:
        avg_oos = np.mean([r.oos_sharpe for r in results])
        std_oos = np.std([r.oos_sharpe for r in results])
        print("-"*70)
        print(f"{'Average OOS Sharpe:':<20} {avg_oos:.2f} (std: {std_oos:.2f})")
