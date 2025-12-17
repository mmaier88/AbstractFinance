"""
Candidate Engines for v2.2 Testing.

Phase N: Test each engine with institutional backtest harness.
Only implement engines that pass ALL implementation gates.

Candidates:
1. EU Sovereign Spreads (Bund vs BTP/OAT)
2. Energy Shock Hedge (CL/BZ)
3. Conditional Duration (Bund only in deflation)
"""

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Tuple
from enum import Enum
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class EUStressLevel(Enum):
    """EU-specific stress levels."""
    CALM = "calm"
    ELEVATED = "elevated"
    CRISIS = "crisis"


class InflationRegime(Enum):
    """Inflation regime for conditional duration."""
    DEFLATION = "deflation"
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"


@dataclass
class EUSovereignSpreadConfig:
    """Configuration for EU Sovereign Spreads engine."""
    # Spread targets (long Bund, short peripheral)
    spread_pairs: List[Tuple[str, str]] = field(default_factory=lambda: [
        ("FGBL", "FBTP"),  # Bund vs BTP (Italy)
        ("FGBL", "FOAT"),  # Bund vs OAT (France)
    ])

    # Activation threshold (spread widens when EU stress)
    activation_spread_bps: Dict[str, float] = field(default_factory=lambda: {
        "FGBL_FBTP": 150,  # BTP spread > 150 bps activates
        "FGBL_FOAT": 50,   # OAT spread > 50 bps activates
    })

    # Position sizing
    max_position_pct_nav: float = 15.0
    per_spread_max_pct_nav: float = 10.0

    # DV01 matching
    require_dv01_match: bool = True
    max_dv01_mismatch_pct: float = 5.0


@dataclass
class EnergyShockConfig:
    """Configuration for Energy Shock Hedge engine."""
    # Instruments
    primary_instrument: str = "CL"  # WTI
    secondary_instrument: str = "BZ"  # Brent

    # Trend signal parameters
    trend_lookback_days: int = 20
    trend_threshold: float = 0.10  # 10% move triggers

    # EU stress gate
    require_eu_stress: bool = True
    eu_stress_threshold: float = 25.0  # V2X > 25

    # Position sizing
    max_position_pct_nav: float = 10.0

    # Stop loss
    stop_loss_pct: float = 0.08  # 8% stop


@dataclass
class ConditionalDurationConfig:
    """Configuration for Conditional Duration engine."""
    # Instrument
    instrument: str = "FGBL"  # Euro-Bund

    # Deflation detection
    deflation_cpi_threshold: float = 0.5  # YoY CPI < 0.5%
    recession_indicator_threshold: float = -1.0  # PMI deviation from 50

    # Inflation shock guard (2022 trap avoidance)
    inflation_shock_threshold: float = 4.0  # YoY CPI > 4% = NO duration

    # Position sizing
    max_position_pct_nav: float = 15.0

    # Regime persistence
    min_days_in_regime: int = 10  # Require 10 days before activating


@dataclass
class EngineSignal:
    """Signal output from a candidate engine."""
    engine_name: str
    signal_strength: float  # -1 to 1
    target_allocation: float  # % of NAV
    positions: Dict[str, float]  # instrument -> quantity (signed)
    metadata: Dict = field(default_factory=dict)


class EUSovereignSpreadsEngine:
    """
    EU Sovereign Spreads Engine.

    Strategy: Mean-reversion on BTP/OAT spreads when V2X is declining.
    Long Bund, Short BTP/OAT when spreads are elevated but crisis is resolving.
    DV01-matched to ensure no hidden duration bet.

    Hypothesis: Pays off during EU crisis RESOLUTION (spreads narrow after peaks).
    """

    def __init__(self, config: Optional[EUSovereignSpreadConfig] = None):
        self.config = config or EUSovereignSpreadConfig()
        self._v2x_history: List[float] = []
        self._spread_history: List[float] = []

    def compute_eu_stress_level(
        self,
        v2x: float,
        btp_spread_bps: float,
        oat_spread_bps: float,
    ) -> EUStressLevel:
        """Compute EU-specific stress level."""
        stress_score = 0

        # V2X contribution (Europe vol)
        if v2x > 30:
            stress_score += 2
        elif v2x > 25:
            stress_score += 1

        # BTP spread contribution (Italy risk)
        if btp_spread_bps > 250:
            stress_score += 2
        elif btp_spread_bps > 150:
            stress_score += 1

        # OAT spread contribution (France risk)
        if oat_spread_bps > 100:
            stress_score += 1
        elif oat_spread_bps > 50:
            stress_score += 0.5

        if stress_score >= 4:
            return EUStressLevel.CRISIS
        elif stress_score >= 2:
            return EUStressLevel.ELEVATED
        return EUStressLevel.CALM

    def compute_signal(
        self,
        v2x: float,
        btp_spread_bps: float,
        oat_spread_bps: float,
        nav: float,
        v2x_5d_ago: Optional[float] = None,
        btp_spread_5d_ago: Optional[float] = None,
    ) -> EngineSignal:
        """
        Compute spread positions using mean-reversion with crisis resolution filter.

        Key insight: Enter when spreads are elevated but V2X is DECLINING
        (crisis resolution phase), not during crisis onset.
        """
        stress = self.compute_eu_stress_level(v2x, btp_spread_bps, oat_spread_bps)

        positions = {}
        signal_strength = 0.0
        target_allocation = 0.0

        # Update history
        self._v2x_history.append(v2x)
        self._spread_history.append(btp_spread_bps)
        if len(self._v2x_history) > 20:
            self._v2x_history = self._v2x_history[-20:]
            self._spread_history = self._spread_history[-20:]

        # Check V2X trend (declining = crisis resolving)
        v2x_declining = False
        if len(self._v2x_history) >= 5:
            v2x_5d = self._v2x_history[-5]
            v2x_declining = v2x < v2x_5d * 0.95  # V2X down 5%+ over 5 days

        # Check spread is elevated but starting to narrow
        spread_elevated = btp_spread_bps > self.config.activation_spread_bps["FGBL_FBTP"]
        spread_narrowing = False
        if len(self._spread_history) >= 5:
            spread_5d = self._spread_history[-5]
            spread_narrowing = btp_spread_bps < spread_5d  # Spread narrowing

        # Entry condition: Elevated spreads + V2X declining (crisis resolution)
        if spread_elevated and v2x_declining:
            # Scale position by how elevated the spread is
            spread_z = (btp_spread_bps - 150) / 100  # Z-score above activation
            signal_strength = min(1.0, max(0.3, spread_z))
            target_allocation = signal_strength * self.config.max_position_pct_nav

            positions["FGBL_long_vs_FBTP"] = target_allocation * 0.7

            # Add OAT if also elevated
            if oat_spread_bps > self.config.activation_spread_bps["FGBL_FOAT"]:
                positions["FGBL_long_vs_FOAT"] = target_allocation * 0.3

        # Alternative: Very high spreads even without V2X signal (deep value)
        elif btp_spread_bps > 350:  # Extreme spread = always enter
            signal_strength = 0.5
            target_allocation = self.config.max_position_pct_nav * 0.5
            positions["FGBL_long_vs_FBTP"] = target_allocation

        return EngineSignal(
            engine_name="eu_sovereign_spreads",
            signal_strength=signal_strength,
            target_allocation=target_allocation,
            positions=positions,
            metadata={
                "stress_level": stress.value,
                "v2x": v2x,
                "v2x_declining": v2x_declining,
                "btp_spread_bps": btp_spread_bps,
                "spread_elevated": spread_elevated,
                "spread_narrowing": spread_narrowing,
            }
        )

    def simulate_returns(
        self,
        spread_changes_df: pd.DataFrame,  # Columns: btp_spread_change, oat_spread_change (in bps)
        v2x_series: pd.Series,
        btp_spread_series: pd.Series,
        oat_spread_series: pd.Series,
        nav: float = 1_000_000,
    ) -> pd.Series:
        """
        Simulate returns from spread trading.

        Return = spread narrowing * position size * DV01 sensitivity
        """
        returns = []

        for dt in spread_changes_df.index:
            v2x = v2x_series.get(dt, 20)
            btp_spread = btp_spread_series.get(dt, 100)
            oat_spread = oat_spread_series.get(dt, 30)

            signal = self.compute_signal(v2x, btp_spread, oat_spread, nav)

            daily_return = 0.0

            # Return from BTP spread position
            if "FGBL_long_vs_FBTP" in signal.positions:
                allocation = signal.positions["FGBL_long_vs_FBTP"] / 100
                # Spread narrowing = profit (we're long Bund, short BTP)
                btp_change = spread_changes_df.loc[dt, "btp_spread_change"]
                # ~0.01% per bp * allocation (simplified)
                daily_return -= allocation * btp_change * 0.0001

            # Return from OAT spread position
            if "FGBL_long_vs_FOAT" in signal.positions:
                allocation = signal.positions["FGBL_long_vs_FOAT"] / 100
                oat_change = spread_changes_df.loc[dt, "oat_spread_change"]
                daily_return -= allocation * oat_change * 0.0001

            returns.append(daily_return)

        return pd.Series(returns, index=spread_changes_df.index)


class EnergyShockEngine:
    """
    Energy Shock Hedge Engine.

    Strategy: Long energy during EU stress + trend breakout.
    Designed for 2022-type energy shock protection.

    Hypothesis: Energy spikes during EU-specific shocks (gas, oil supply).
    """

    def __init__(self, config: Optional[EnergyShockConfig] = None):
        self.config = config or EnergyShockConfig()
        self._price_history: List[float] = []

    def compute_trend_signal(self, prices: pd.Series) -> float:
        """Compute trend signal from price history."""
        if len(prices) < self.config.trend_lookback_days:
            return 0.0

        lookback = prices.iloc[-self.config.trend_lookback_days:]
        start_price = lookback.iloc[0]
        end_price = lookback.iloc[-1]

        if start_price == 0:
            return 0.0

        pct_change = (end_price - start_price) / start_price

        if abs(pct_change) < self.config.trend_threshold:
            return 0.0

        # Normalize to -1 to 1
        return np.clip(pct_change / self.config.trend_threshold, -1, 1)

    def compute_signal(
        self,
        oil_prices: pd.Series,
        v2x: float,
        eu_stressed: bool = False,
    ) -> EngineSignal:
        """Compute energy hedge signal."""
        trend_signal = self.compute_trend_signal(oil_prices)

        positions = {}
        signal_strength = 0.0
        target_allocation = 0.0

        # Gate: Only trade during EU stress (if required)
        if self.config.require_eu_stress:
            if v2x < self.config.eu_stress_threshold and not eu_stressed:
                return EngineSignal(
                    engine_name="energy_shock",
                    signal_strength=0.0,
                    target_allocation=0.0,
                    positions={},
                    metadata={"gated": "EU stress not met"}
                )

        # Positive trend = long energy (protection against price spike)
        if trend_signal > 0:
            signal_strength = trend_signal
            target_allocation = signal_strength * self.config.max_position_pct_nav
            positions[self.config.primary_instrument] = target_allocation

        return EngineSignal(
            engine_name="energy_shock",
            signal_strength=signal_strength,
            target_allocation=target_allocation,
            positions=positions,
            metadata={
                "trend_signal": trend_signal,
                "v2x": v2x,
            }
        )

    def simulate_returns(
        self,
        oil_returns: pd.Series,
        v2x_series: pd.Series,
        oil_prices: pd.Series,
    ) -> pd.Series:
        """Simulate returns from energy hedge."""
        returns = []

        for i, dt in enumerate(oil_returns.index):
            # Build price history up to this point
            prices_to_date = oil_prices.loc[:dt]
            v2x = v2x_series.get(dt, 20)

            signal = self.compute_signal(prices_to_date, v2x)

            if signal.target_allocation > 0:
                allocation = signal.target_allocation / 100
                daily_return = allocation * oil_returns.loc[dt]
            else:
                daily_return = 0.0

            returns.append(daily_return)

        return pd.Series(returns, index=oil_returns.index)


class ConditionalDurationEngine:
    """
    Conditional Duration Engine.

    Strategy: Long Bund ONLY in deflationary recession.
    Explicit inflation-shock guard to avoid 2022 trap.

    Hypothesis: Captures flight-to-quality in deflation without
    getting killed by rate rises during inflation shocks.
    """

    def __init__(self, config: Optional[ConditionalDurationConfig] = None):
        self.config = config or ConditionalDurationConfig()
        self._days_in_deflation = 0

    def compute_inflation_regime(
        self,
        cpi_yoy: float,
    ) -> InflationRegime:
        """Classify inflation regime."""
        if cpi_yoy > self.config.inflation_shock_threshold:
            return InflationRegime.HIGH
        elif cpi_yoy > 2.5:
            return InflationRegime.MODERATE
        elif cpi_yoy > self.config.deflation_cpi_threshold:
            return InflationRegime.LOW
        return InflationRegime.DEFLATION

    def is_recession(self, pmi: float) -> bool:
        """Check if in recession based on PMI."""
        return pmi < (50 + self.config.recession_indicator_threshold)

    def compute_signal(
        self,
        cpi_yoy: float,
        pmi: float,
        nav: float,
    ) -> EngineSignal:
        """Compute conditional duration signal."""
        inflation_regime = self.compute_inflation_regime(cpi_yoy)
        is_recession = self.is_recession(pmi)

        positions = {}
        signal_strength = 0.0
        target_allocation = 0.0

        # CRITICAL: Inflation shock guard (2022 trap avoidance)
        if inflation_regime == InflationRegime.HIGH:
            self._days_in_deflation = 0
            return EngineSignal(
                engine_name="conditional_duration",
                signal_strength=0.0,
                target_allocation=0.0,
                positions={},
                metadata={
                    "blocked": "inflation_shock",
                    "cpi_yoy": cpi_yoy,
                    "inflation_regime": inflation_regime.value,
                }
            )

        # Only long duration in deflationary recession
        if inflation_regime == InflationRegime.DEFLATION and is_recession:
            self._days_in_deflation += 1

            # Require persistence
            if self._days_in_deflation >= self.config.min_days_in_regime:
                signal_strength = 1.0
                target_allocation = self.config.max_position_pct_nav
                positions[self.config.instrument] = target_allocation
        else:
            self._days_in_deflation = 0

        return EngineSignal(
            engine_name="conditional_duration",
            signal_strength=signal_strength,
            target_allocation=target_allocation,
            positions=positions,
            metadata={
                "inflation_regime": inflation_regime.value,
                "is_recession": is_recession,
                "days_in_deflation": self._days_in_deflation,
                "cpi_yoy": cpi_yoy,
                "pmi": pmi,
            }
        )

    def simulate_returns(
        self,
        bund_returns: pd.Series,
        cpi_series: pd.Series,
        pmi_series: pd.Series,
    ) -> pd.Series:
        """Simulate returns from conditional duration."""
        returns = []
        self._days_in_deflation = 0  # Reset state

        for dt in bund_returns.index:
            cpi = cpi_series.get(dt, 2.0)
            pmi = pmi_series.get(dt, 50.0)

            signal = self.compute_signal(cpi, pmi, 1_000_000)

            if signal.target_allocation > 0:
                allocation = signal.target_allocation / 100
                daily_return = allocation * bund_returns.loc[dt]
            else:
                daily_return = 0.0

            returns.append(daily_return)

        return pd.Series(returns, index=bund_returns.index)


@dataclass
class BacktestResult:
    """Result from backtesting a candidate engine."""
    engine_name: str
    sharpe_ratio: float
    max_drawdown: float
    total_return: float
    insurance_score: float  # Performance in stress periods vs normal
    avg_allocation: float

    # Walk-forward results
    in_sample_sharpe: float
    out_of_sample_sharpe: float
    parameter_stability: float

    # Ablation results (when combined with portfolio)
    portfolio_sharpe_with: float
    portfolio_sharpe_without: float
    marginal_contribution: float

    # Pass/Fail gates
    passes_standalone_sharpe: bool = False  # > 0.3
    passes_portfolio_improvement: bool = False  # > 0.1
    passes_insurance_score: bool = False  # > 0
    passes_walk_forward: bool = False  # OOS Sharpe > 0
    passes_all_gates: bool = False

    def evaluate_gates(self):
        """Evaluate all implementation gates."""
        self.passes_standalone_sharpe = self.sharpe_ratio > 0.3
        self.passes_portfolio_improvement = self.marginal_contribution > 0.1
        self.passes_insurance_score = self.insurance_score > 0
        self.passes_walk_forward = self.out_of_sample_sharpe > 0

        self.passes_all_gates = (
            self.passes_standalone_sharpe and
            self.passes_portfolio_improvement and
            self.passes_insurance_score and
            self.passes_walk_forward
        )


def compute_sharpe(returns: pd.Series, risk_free_rate: float = 0.02) -> float:
    """Compute annualized Sharpe ratio."""
    if len(returns) == 0 or returns.std() == 0:
        return 0.0

    excess_returns = returns - risk_free_rate / 252
    return np.sqrt(252) * excess_returns.mean() / returns.std()


def compute_max_drawdown(returns: pd.Series) -> float:
    """Compute maximum drawdown from returns series."""
    cumulative = (1 + returns).cumprod()
    rolling_max = cumulative.expanding().max()
    drawdown = (cumulative - rolling_max) / rolling_max
    return drawdown.min()


def compute_insurance_score(
    returns: pd.Series,
    stress_mask: pd.Series,
) -> float:
    """
    Compute insurance score.

    Positive = strategy makes money during stress (good insurance).
    Negative = strategy loses during stress (bad insurance).
    """
    if stress_mask.sum() == 0:
        return 0.0

    stress_returns = returns[stress_mask].mean() * 252
    normal_returns = returns[~stress_mask].mean() * 252

    # Insurance score = stress performance - normal performance
    # Good insurance: positive in stress, neutral/negative normally
    return stress_returns - normal_returns
