"""
Risk engine for AbstractFinance.
Handles volatility targeting, drawdown control, exposure limits, and risk decisions.
"""

import pandas as pd
import numpy as np
from datetime import datetime, date
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass
from enum import Enum

from .portfolio import PortfolioState, Sleeve


class RiskRegime(Enum):
    """Market risk regime classifications."""
    NORMAL = "normal"
    ELEVATED = "elevated"
    CRISIS = "crisis"
    RECOVERY = "recovery"


@dataclass
class RiskDecision:
    """
    Risk engine decision output.
    Contains scaling factors and flags for portfolio adjustments.
    """
    scaling_factor: float
    emergency_derisk: bool
    regime: RiskRegime
    reduce_core_exposure: bool = False
    reduce_factor: float = 1.0
    increase_hedges: bool = False
    close_positions: List[str] = None
    warnings: List[str] = None

    def __post_init__(self):
        self.close_positions = self.close_positions or []
        self.warnings = self.warnings or []


@dataclass
class RiskMetrics:
    """Collection of risk metrics for monitoring."""
    realized_vol_20d: float
    realized_vol_60d: float
    target_vol: float
    scaling_factor: float
    current_drawdown: float
    max_drawdown: float
    var_95: float
    var_99: float
    expected_shortfall: float
    gross_leverage: float
    net_leverage: float
    beta_exposure: float
    regime: RiskRegime
    vix_level: float
    spread_momentum: float


class RiskEngine:
    """
    Risk management engine for the portfolio.
    Implements volatility targeting, drawdown control, and regime-based adjustments.
    """

    def __init__(self, settings: Dict[str, Any]):
        """
        Initialize risk engine with settings.

        Args:
            settings: Application settings dictionary
        """
        self.settings = settings
        self.vol_target_annual = settings.get('vol_target_annual', 0.12)
        self.gross_leverage_max = settings.get('gross_leverage_max', 2.0)
        self.net_leverage_max = settings.get('net_leverage_max', 1.0)
        self.max_drawdown_pct = settings.get('max_drawdown_pct', 0.10)
        self.rebalance_threshold = settings.get('rebalance_threshold_pct', 0.02)

        # Momentum settings
        momentum_settings = settings.get('momentum', {})
        self.short_window = momentum_settings.get('short_window_days', 50)
        self.long_window = momentum_settings.get('long_window_days', 200)
        self.regime_reduce_factor = momentum_settings.get('regime_reduce_factor', 0.5)

        # Crisis settings
        crisis_settings = settings.get('crisis', {})
        self.vix_threshold = crisis_settings.get('vix_threshold', 40)
        self.pnl_spike_threshold = crisis_settings.get('pnl_spike_threshold_pct', 0.10)

    def compute_realized_vol_annual(
        self,
        returns: pd.Series,
        window: int = 20
    ) -> float:
        """
        Compute annualized realized volatility.

        Args:
            returns: Series of daily returns
            window: Lookback window in days

        Returns:
            Annualized volatility
        """
        if len(returns) < window:
            window = max(len(returns), 5)

        daily_vol = returns.tail(window).std()
        return daily_vol * np.sqrt(252)

    def compute_max_drawdown(self, equity_curve: pd.Series) -> float:
        """
        Compute maximum drawdown from equity curve.

        Args:
            equity_curve: Series of cumulative P&L or NAV

        Returns:
            Maximum drawdown as negative decimal (e.g., -0.15 for 15% DD)
        """
        if equity_curve.empty:
            return 0.0

        rolling_max = equity_curve.cummax()
        drawdown = (equity_curve - rolling_max) / rolling_max
        return drawdown.min()

    def compute_current_drawdown(self, equity_curve: pd.Series) -> float:
        """
        Compute current drawdown from equity curve.

        Args:
            equity_curve: Series of cumulative P&L or NAV

        Returns:
            Current drawdown as negative decimal
        """
        if equity_curve.empty:
            return 0.0

        rolling_max = equity_curve.cummax()
        current_dd = (equity_curve.iloc[-1] - rolling_max.iloc[-1]) / rolling_max.iloc[-1]
        return current_dd

    def estimate_betas(
        self,
        us_returns: pd.Series,
        eu_returns: pd.Series,
        market_returns: Optional[pd.Series] = None
    ) -> Tuple[float, float]:
        """
        Estimate betas for US and EU legs.

        Args:
            us_returns: US index returns
            eu_returns: EU index returns
            market_returns: Global market returns (optional)

        Returns:
            Tuple of (us_beta, eu_beta)
        """
        # If no market returns provided, use average of both
        if market_returns is None:
            market_returns = (us_returns + eu_returns) / 2

        # Align series
        common_idx = us_returns.index.intersection(eu_returns.index).intersection(market_returns.index)

        if len(common_idx) < 20:
            return 1.0, 1.0

        us = us_returns.loc[common_idx]
        eu = eu_returns.loc[common_idx]
        mkt = market_returns.loc[common_idx]

        # Simple regression betas
        us_beta = np.cov(us, mkt)[0, 1] / np.var(mkt) if np.var(mkt) > 0 else 1.0
        eu_beta = np.cov(eu, mkt)[0, 1] / np.var(mkt) if np.var(mkt) > 0 else 1.0

        return us_beta, eu_beta

    def compute_scaling_factor(
        self,
        realized_vol_annual: float,
        target_vol_annual: Optional[float] = None,
        gross_leverage_max: Optional[float] = None
    ) -> float:
        """
        Compute position scaling factor for volatility targeting.

        Args:
            realized_vol_annual: Current annualized volatility
            target_vol_annual: Target annualized volatility
            gross_leverage_max: Maximum gross leverage

        Returns:
            Scaling factor to apply to positions
        """
        target_vol = target_vol_annual or self.vol_target_annual
        max_leverage = gross_leverage_max or self.gross_leverage_max

        if realized_vol_annual <= 0:
            return 1.0

        raw_scaling = target_vol / realized_vol_annual
        return min(raw_scaling, max_leverage)

    def compute_var(
        self,
        returns: pd.Series,
        confidence_level: float = 0.95,
        window: int = 252
    ) -> float:
        """
        Compute Value at Risk (VaR).

        Args:
            returns: Daily returns series
            confidence_level: Confidence level (e.g., 0.95, 0.99)
            window: Lookback window

        Returns:
            VaR as positive number (potential loss)
        """
        if len(returns) < window:
            window = len(returns)

        percentile = (1 - confidence_level) * 100
        var = np.percentile(returns.tail(window), percentile)
        return abs(var)

    def compute_expected_shortfall(
        self,
        returns: pd.Series,
        confidence_level: float = 0.95,
        window: int = 252
    ) -> float:
        """
        Compute Expected Shortfall (CVaR).

        Args:
            returns: Daily returns series
            confidence_level: Confidence level
            window: Lookback window

        Returns:
            Expected shortfall as positive number
        """
        if len(returns) < window:
            window = len(returns)

        var = self.compute_var(returns, confidence_level, window)
        tail_returns = returns.tail(window)[returns.tail(window) <= -var]

        if tail_returns.empty:
            return var

        return abs(tail_returns.mean())

    def detect_regime(
        self,
        vix_level: float,
        spread_momentum: float,
        current_drawdown: float
    ) -> RiskRegime:
        """
        Detect current market risk regime.

        Args:
            vix_level: Current VIX level
            spread_momentum: US/EU spread momentum signal
            current_drawdown: Current portfolio drawdown

        Returns:
            RiskRegime classification
        """
        # Crisis conditions
        if vix_level >= self.vix_threshold or current_drawdown <= -self.max_drawdown_pct:
            return RiskRegime.CRISIS

        # Elevated risk
        if vix_level >= 25 or current_drawdown <= -0.05:
            return RiskRegime.ELEVATED

        # Recovery (coming out of drawdown with improving conditions)
        if current_drawdown < 0 and current_drawdown > -0.03 and vix_level < 20:
            return RiskRegime.RECOVERY

        return RiskRegime.NORMAL

    def compute_spread_momentum(self, ratio_series: pd.Series) -> float:
        """
        Compute momentum signal for US/EU spread.

        Args:
            ratio_series: SPX/SX5E price ratio series

        Returns:
            Momentum score (-1 to 1)
        """
        if len(ratio_series) < self.long_window:
            return 0.0

        # Compute MAs
        ma_short = ratio_series.rolling(self.short_window).mean().iloc[-1]
        ma_long = ratio_series.rolling(self.long_window).mean().iloc[-1]

        # Compute slope of short MA
        ma_short_series = ratio_series.rolling(self.short_window).mean()
        slope = (ma_short_series.iloc[-1] - ma_short_series.iloc[-20]) / ma_short_series.iloc[-20] if len(ma_short_series) > 20 else 0

        # Momentum signal
        if ma_short > ma_long and slope > 0:
            return 1.0  # Strong positive momentum
        elif ma_short > ma_long:
            return 0.5  # Positive but weakening
        elif ma_short < ma_long and slope < 0:
            return -1.0  # Strong negative momentum
        else:
            return -0.5  # Negative but stabilizing

    def should_reduce_exposure(
        self,
        spread_momentum: float,
        regime: RiskRegime
    ) -> Tuple[bool, float]:
        """
        Determine if exposure should be reduced based on regime/momentum.

        Args:
            spread_momentum: Current momentum signal
            regime: Current risk regime

        Returns:
            Tuple of (should_reduce, reduce_factor)
        """
        # Crisis mode - significant reduction
        if regime == RiskRegime.CRISIS:
            return True, 0.3

        # Negative momentum - partial reduction
        if spread_momentum < 0:
            reduce_factor = 1.0 + (spread_momentum * (1 - self.regime_reduce_factor))
            return True, max(reduce_factor, self.regime_reduce_factor)

        # Elevated regime with weak momentum
        if regime == RiskRegime.ELEVATED and spread_momentum < 0.5:
            return True, 0.75

        return False, 1.0

    def evaluate_risk(
        self,
        portfolio_state: PortfolioState,
        returns_series: pd.Series,
        vix_level: float = 20.0,
        ratio_series: Optional[pd.Series] = None
    ) -> RiskDecision:
        """
        Comprehensive risk evaluation for the portfolio.

        Args:
            portfolio_state: Current portfolio state
            returns_series: Historical returns
            vix_level: Current VIX level
            ratio_series: SPX/SX5E ratio series for momentum

        Returns:
            RiskDecision with scaling factors and flags
        """
        warnings = []

        # Compute volatility
        vol_20d = self.compute_realized_vol_annual(returns_series, 20)
        vol_60d = self.compute_realized_vol_annual(returns_series, 60)

        # Use longer-term vol if short-term is abnormally high
        realized_vol = vol_20d if vol_20d < vol_60d * 1.5 else vol_60d

        # Compute drawdown
        if not portfolio_state.nav_history.empty:
            equity_curve = portfolio_state.nav_history
        else:
            equity_curve = (1 + returns_series).cumprod()

        current_dd = self.compute_current_drawdown(equity_curve)
        max_dd = self.compute_max_drawdown(equity_curve)

        # Compute momentum if ratio series provided
        spread_momentum = 0.0
        if ratio_series is not None and len(ratio_series) > 0:
            spread_momentum = self.compute_spread_momentum(ratio_series)

        # Detect regime
        regime = self.detect_regime(vix_level, spread_momentum, current_dd)

        # Base scaling factor
        scaling_factor = self.compute_scaling_factor(realized_vol)

        # Emergency de-risk check
        emergency_derisk = current_dd <= -self.max_drawdown_pct
        if emergency_derisk:
            warnings.append(f"EMERGENCY: Drawdown {current_dd:.2%} exceeds max {self.max_drawdown_pct:.2%}")
            scaling_factor = 0.25  # Reduce to 25% of target

        # Regime-based reduction
        should_reduce, reduce_factor = self.should_reduce_exposure(spread_momentum, regime)

        # Apply regime reduction
        if should_reduce and not emergency_derisk:
            scaling_factor *= reduce_factor
            warnings.append(f"Regime reduction applied: factor={reduce_factor:.2f}")

        # Check leverage limits
        current_gross = portfolio_state.gross_exposure / portfolio_state.nav if portfolio_state.nav > 0 else 0
        if current_gross > self.gross_leverage_max:
            warnings.append(f"Gross leverage {current_gross:.2f} exceeds max {self.gross_leverage_max:.2f}")
            scaling_factor = min(scaling_factor, self.gross_leverage_max / current_gross)

        # VIX warning
        if vix_level > 30:
            warnings.append(f"Elevated VIX: {vix_level:.1f}")

        # Hedge increase signal
        increase_hedges = regime in [RiskRegime.ELEVATED, RiskRegime.CRISIS] or vix_level > 25

        return RiskDecision(
            scaling_factor=scaling_factor,
            emergency_derisk=emergency_derisk,
            regime=regime,
            reduce_core_exposure=should_reduce,
            reduce_factor=reduce_factor,
            increase_hedges=increase_hedges,
            warnings=warnings
        )

    def compute_risk_metrics(
        self,
        portfolio_state: PortfolioState,
        returns_series: pd.Series,
        vix_level: float = 20.0,
        ratio_series: Optional[pd.Series] = None
    ) -> RiskMetrics:
        """
        Compute comprehensive risk metrics.

        Args:
            portfolio_state: Current portfolio state
            returns_series: Historical returns
            vix_level: Current VIX level
            ratio_series: US/EU ratio series

        Returns:
            RiskMetrics dataclass with all metrics
        """
        # Volatility
        vol_20d = self.compute_realized_vol_annual(returns_series, 20)
        vol_60d = self.compute_realized_vol_annual(returns_series, 60)

        # Scaling
        scaling = self.compute_scaling_factor(vol_20d)

        # Drawdown
        if not portfolio_state.nav_history.empty:
            equity_curve = portfolio_state.nav_history
        else:
            equity_curve = (1 + returns_series).cumprod()

        current_dd = self.compute_current_drawdown(equity_curve)
        max_dd = self.compute_max_drawdown(equity_curve)

        # VaR and ES
        var_95 = self.compute_var(returns_series, 0.95)
        var_99 = self.compute_var(returns_series, 0.99)
        es = self.compute_expected_shortfall(returns_series, 0.95)

        # Leverage
        gross_leverage = portfolio_state.gross_exposure / portfolio_state.nav if portfolio_state.nav > 0 else 0
        net_leverage = portfolio_state.net_exposure / portfolio_state.nav if portfolio_state.nav > 0 else 0

        # Momentum
        spread_momentum = 0.0
        if ratio_series is not None and len(ratio_series) > 0:
            spread_momentum = self.compute_spread_momentum(ratio_series)

        # Regime
        regime = self.detect_regime(vix_level, spread_momentum, current_dd)

        return RiskMetrics(
            realized_vol_20d=vol_20d,
            realized_vol_60d=vol_60d,
            target_vol=self.vol_target_annual,
            scaling_factor=scaling,
            current_drawdown=current_dd,
            max_drawdown=max_dd,
            var_95=var_95,
            var_99=var_99,
            expected_shortfall=es,
            gross_leverage=gross_leverage,
            net_leverage=net_leverage,
            beta_exposure=net_leverage,  # Simplified
            regime=regime,
            vix_level=vix_level,
            spread_momentum=spread_momentum
        )

    def check_rebalance_needed(
        self,
        current_weights: Dict[str, float],
        target_weights: Dict[str, float]
    ) -> bool:
        """
        Check if rebalancing is needed based on drift.

        Args:
            current_weights: Current position weights
            target_weights: Target position weights

        Returns:
            True if rebalancing needed
        """
        for key in target_weights:
            current = current_weights.get(key, 0.0)
            target = target_weights[key]

            if abs(current - target) > self.rebalance_threshold:
                return True

        return False

    def compute_position_limits(
        self,
        nav: float,
        instrument_type: str = "ETF"
    ) -> Dict[str, float]:
        """
        Compute position size limits.

        Args:
            nav: Current NAV
            instrument_type: Type of instrument

        Returns:
            Dict with min/max position sizes
        """
        limits = {
            "ETF": {"max_pct": 0.15, "max_notional": nav * 0.15},
            "FUT": {"max_pct": 0.25, "max_notional": nav * 0.25},
            "OPT": {"max_pct": 0.05, "max_notional": nav * 0.05},
            "STK": {"max_pct": 0.05, "max_notional": nav * 0.05}
        }

        return limits.get(instrument_type, limits["ETF"])
