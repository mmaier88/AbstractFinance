"""
Risk engine for AbstractFinance.
Handles volatility targeting, drawdown control, exposure limits, and risk decisions.

ENGINE_FIX_PLAN Updates:
- Phase 6: Volatility targeting with EWMA, floor, and clip
- Phase 7: Regime system with hysteresis (no single-day flip-flopping)
- Phase 8: Emergency de-risk as state machine (not multiplicative stacking)
"""

import pandas as pd
import numpy as np
from datetime import datetime, date
from typing import Dict, List, Optional, Tuple, Any, TYPE_CHECKING
from dataclasses import dataclass, field
from enum import Enum

from .portfolio import PortfolioState, Sleeve


class RiskRegime(Enum):
    """Market risk regime classifications."""
    NORMAL = "normal"
    ELEVATED = "elevated"
    CRISIS = "crisis"
    RECOVERY = "recovery"


class RiskState(Enum):
    """
    Risk state machine for Phase 8 (emergency de-risk).
    Replaces multiplicative stacking with explicit states.
    """
    NORMAL = 1.0
    ELEVATED = 0.7
    CRISIS = 0.3


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
    scaling_diagnostics: Dict[str, Any] = None  # Vol burn-in + scaling clamp diagnostics

    def __post_init__(self):
        self.close_positions = self.close_positions or []
        self.warnings = self.warnings or []
        self.scaling_diagnostics = self.scaling_diagnostics or {}


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

    ENGINE_FIX_PLAN Updates:
    - Phase 6: EWMA volatility with floor and clip
    - Phase 7: Regime hysteresis (N days to switch)
    - Phase 8: Risk state machine (not multiplicative)

    ROADMAP Phase B: Europe-First Regime Detection
    - Uses V2X (VSTOXX) + VIX + EURUSD trend + drawdown
    - Configurable weights for stress score
    - Graceful degradation when V2X unavailable
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

        # Phase 6: Volatility floor and EWMA settings
        vol_settings = settings.get('volatility', {})
        self.vol_floor = vol_settings.get('floor', 0.08)  # 8% minimum vol assumption
        self.ewma_span = vol_settings.get('ewma_span', 20)  # EWMA span for vol calc
        self.vol_blend_weight = vol_settings.get('blend_weight', 0.7)  # 70% EWMA, 30% rolling

        # Vol Burn-In Prior settings (prevents day-0 deleveraging)
        burn_in_settings = settings.get('vol_burn_in', {})
        self.burn_in_days = burn_in_settings.get('burn_in_days', 60)
        self.initial_vol_annual = burn_in_settings.get('initial_vol_annual', 0.10)
        self.min_vol_annual = burn_in_settings.get('min_vol_annual', 0.06)

        # Scaling Factor Clamps (prevents extreme scaling)
        clamp_settings = settings.get('scaling_clamps', {})
        self.min_scaling_factor = clamp_settings.get('min_scaling_factor', 0.80)
        self.max_scaling_factor = clamp_settings.get('max_scaling_factor', 1.25)

        # Momentum settings
        momentum_settings = settings.get('momentum', {})
        self.short_window = momentum_settings.get('short_window_days', 50)
        self.long_window = momentum_settings.get('long_window_days', 200)
        self.regime_reduce_factor = momentum_settings.get('regime_reduce_factor', 0.5)

        # Crisis settings
        crisis_settings = settings.get('crisis', {})
        self.vix_threshold = crisis_settings.get('vix_threshold', 40)
        self.pnl_spike_threshold = crisis_settings.get('pnl_spike_threshold_pct', 0.10)

        # Phase 7: Hysteresis settings
        hysteresis_settings = settings.get('hysteresis', {})
        self.regime_persistence_days = hysteresis_settings.get('persistence_days', 3)
        self.vix_enter_elevated = hysteresis_settings.get('vix_enter_elevated', 25)
        self.vix_exit_elevated = hysteresis_settings.get('vix_exit_elevated', 20)
        self.vix_enter_crisis = hysteresis_settings.get('vix_enter_crisis', 40)
        self.vix_exit_crisis = hysteresis_settings.get('vix_exit_crisis', 35)

        # Phase B: Europe-First Regime Detection weights
        europe_regime_settings = settings.get('europe_regime', {})
        self.v2x_weight = europe_regime_settings.get('v2x_weight', 0.4)
        self.vix_weight = europe_regime_settings.get('vix_weight', 0.3)
        self.eurusd_trend_weight = europe_regime_settings.get('eurusd_trend_weight', 0.2)
        self.drawdown_weight = europe_regime_settings.get('drawdown_weight', 0.1)
        self.eurusd_trend_lookback = europe_regime_settings.get('eurusd_trend_lookback_days', 60)
        self.stress_score_elevated_threshold = europe_regime_settings.get('stress_elevated_threshold', 0.3)
        self.stress_score_crisis_threshold = europe_regime_settings.get('stress_crisis_threshold', 0.6)

        # Phase 7: Regime state tracking
        self._current_regime = RiskRegime.NORMAL
        self._regime_days_count = 0
        self._pending_regime: Optional[RiskRegime] = None
        self._pending_regime_days = 0

        # Phase 8: Risk state machine
        self._risk_state = RiskState.NORMAL

        # Phase B: Cache for regime inputs
        self._last_v2x: Optional[float] = None
        self._last_eurusd_trend: Optional[float] = None
        self._regime_inputs_missing: bool = False

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
            Annualized volatility (returns target_vol if insufficient data)
        """
        # Handle empty or insufficient data
        if returns is None or len(returns) < 5:
            # Return target vol as default when no history
            return self.vol_target_annual

        if len(returns) < window:
            window = max(len(returns), 5)

        daily_vol = returns.tail(window).std()

        # Handle NaN or zero volatility
        if pd.isna(daily_vol) or daily_vol <= 0:
            return self.vol_target_annual

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
        gross_leverage_max: Optional[float] = None,
        history_days: Optional[int] = None,
    ) -> Tuple[float, Dict[str, Any]]:
        """
        Compute position scaling factor for volatility targeting.

        ENGINE_FIX_PLAN Phase 6:
        - Apply volatility floor before scaling
        - Allow scaling > 1.0 (levering up in low vol)
        - Clip to max leverage

        Vol Burn-In + Scaling Clamps:
        - Uses effective_realized_vol() with burn-in prior
        - Clamps raw scaling to [min_scaling_factor, max_scaling_factor]
        - Returns diagnostics for decision logging

        Args:
            realized_vol_annual: Current annualized volatility
            target_vol_annual: Target annualized volatility
            gross_leverage_max: Maximum gross leverage
            history_days: Number of days of return history (for burn-in)

        Returns:
            Tuple of (scaling_factor, diagnostics_dict)
        """
        target_vol = target_vol_annual or self.vol_target_annual
        max_leverage = gross_leverage_max or self.gross_leverage_max
        hist_days = history_days if history_days is not None else self.burn_in_days

        diagnostics = {
            'target_vol': target_vol,
            'max_leverage': max_leverage,
            'raw_realized_vol': realized_vol_annual,
            'history_days': hist_days,
        }

        # Use burn-in prior for effective volatility
        eff_vol, burn_in_active, vol_diagnostics = self.effective_realized_vol(
            realized_vol_annual, hist_days
        )
        diagnostics.update(vol_diagnostics)

        # Handle invalid volatility values (after burn-in still zero)
        if eff_vol is None or pd.isna(eff_vol) or eff_vol <= 0:
            diagnostics['fallback'] = 'invalid_vol'
            return 1.0, diagnostics

        # Phase 6: Apply volatility floor
        floored_vol = max(self.vol_floor, eff_vol)
        diagnostics['floored_vol'] = floored_vol

        # Compute raw scaling
        raw_scaling = target_vol / floored_vol
        diagnostics['raw_scaling'] = raw_scaling

        # Ensure we never return NaN or infinity
        if pd.isna(raw_scaling) or np.isinf(raw_scaling):
            diagnostics['fallback'] = 'nan_or_inf'
            return 1.0, diagnostics

        # Apply scaling clamps (prevents extreme scaling)
        clamped_scaling = np.clip(raw_scaling, self.min_scaling_factor, self.max_scaling_factor)
        diagnostics['clamped_scaling'] = clamped_scaling
        diagnostics['clamp_applied'] = clamped_scaling != raw_scaling
        diagnostics['min_clamp'] = self.min_scaling_factor
        diagnostics['max_clamp'] = self.max_scaling_factor

        # Phase 6: Also clip to [0.0, max_leverage] for leverage constraint
        final_scaling = np.clip(clamped_scaling, 0.0, max_leverage)
        diagnostics['final_scaling'] = final_scaling
        diagnostics['leverage_clamp_applied'] = final_scaling != clamped_scaling

        return final_scaling, diagnostics

    def compute_ewma_vol(
        self,
        returns: pd.Series,
        span: Optional[int] = None
    ) -> float:
        """
        Compute EWMA (exponentially weighted) volatility.

        Phase 6: More responsive to recent volatility changes.

        Args:
            returns: Daily returns series
            span: EWMA span (default: self.ewma_span)

        Returns:
            Annualized EWMA volatility
        """
        span = span or self.ewma_span

        if returns is None or len(returns) < 5:
            return self.vol_target_annual

        ewma_vol = returns.ewm(span=span).std().iloc[-1]

        if pd.isna(ewma_vol) or ewma_vol <= 0:
            return self.vol_target_annual

        return ewma_vol * np.sqrt(252)

    def compute_blended_vol(
        self,
        returns: pd.Series,
        short_window: int = 20,
        long_window: int = 60
    ) -> float:
        """
        Compute blended volatility using EWMA and rolling.

        Phase 6: Blend short-term responsiveness with long-term stability.

        Formula: vol = blend_weight * ewma_vol + (1 - blend_weight) * rolling_vol

        Args:
            returns: Daily returns series
            short_window: Short-term rolling window
            long_window: Long-term rolling window

        Returns:
            Annualized blended volatility
        """
        if returns is None or len(returns) < short_window:
            return self.vol_target_annual

        # EWMA volatility (more responsive)
        ewma_vol = self.compute_ewma_vol(returns)

        # Rolling volatility (more stable)
        window = min(len(returns), long_window)
        rolling_vol = returns.tail(window).std() * np.sqrt(252)

        if pd.isna(rolling_vol) or rolling_vol <= 0:
            rolling_vol = ewma_vol

        # Blend
        blended = self.vol_blend_weight * ewma_vol + (1 - self.vol_blend_weight) * rolling_vol

        # Apply floor
        return max(self.vol_floor, blended)

    def effective_realized_vol(
        self,
        realized_vol_annual: Optional[float],
        history_days: int
    ) -> Tuple[float, bool, Dict[str, Any]]:
        """
        Compute effective realized volatility with burn-in prior.

        During burn-in period (history_days < burn_in_days), uses a prior
        volatility estimate to prevent extreme scaling on day 0.

        Args:
            realized_vol_annual: Computed realized vol (or None if unavailable)
            history_days: Number of valid daily returns in history

        Returns:
            Tuple of (effective_vol, burn_in_active, diagnostics_dict)
        """
        diagnostics = {
            'raw_realized_vol': realized_vol_annual,
            'history_days': history_days,
            'burn_in_days_config': self.burn_in_days,
            'initial_vol_prior': self.initial_vol_annual,
            'min_vol_floor': self.min_vol_annual,
        }

        rv = realized_vol_annual if realized_vol_annual is not None else 0.0
        burn_in_active = history_days < self.burn_in_days

        # During burn-in, use prior if realized vol is lower
        if burn_in_active:
            rv = max(rv, self.initial_vol_annual)
            diagnostics['burn_in_applied'] = True
        else:
            diagnostics['burn_in_applied'] = False

        # Apply hard minimum floor
        if self.min_vol_annual:
            rv = max(rv, self.min_vol_annual)

        # Final safety: if still zero/invalid, use prior
        if rv <= 0:
            rv = self.initial_vol_annual

        diagnostics['effective_vol'] = rv
        diagnostics['burn_in_active'] = burn_in_active

        return rv, burn_in_active, diagnostics

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
        Detect current market risk regime WITH HYSTERESIS.

        ENGINE_FIX_PLAN Phase 7:
        - Separate enter vs exit thresholds
        - Regime must persist N days to switch
        - No single-day flip-flopping

        Args:
            vix_level: Current VIX level
            spread_momentum: US/EU spread momentum signal
            current_drawdown: Current portfolio drawdown

        Returns:
            RiskRegime classification (with hysteresis applied)
        """
        # Determine what regime TODAY'S conditions suggest
        raw_regime = self._detect_raw_regime(vix_level, spread_momentum, current_drawdown)

        # Apply hysteresis: only switch if new regime persists
        if raw_regime != self._current_regime:
            if raw_regime == self._pending_regime:
                # Same pending regime - increment counter
                self._pending_regime_days += 1
            else:
                # New pending regime - reset counter
                self._pending_regime = raw_regime
                self._pending_regime_days = 1

            # Check if persistence threshold met
            # Exception: Always switch to CRISIS immediately (no delay)
            if raw_regime == RiskRegime.CRISIS:
                self._current_regime = RiskRegime.CRISIS
                self._pending_regime = None
                self._pending_regime_days = 0
            elif self._pending_regime_days >= self.regime_persistence_days:
                # Persistence threshold met - switch regime
                self._current_regime = self._pending_regime
                self._pending_regime = None
                self._pending_regime_days = 0
        else:
            # Conditions match current regime - reset pending
            self._pending_regime = None
            self._pending_regime_days = 0

        return self._current_regime

    def detect_regime_europe_first(
        self,
        vix_level: float,
        v2x_level: Optional[float],
        eurusd_trend: Optional[float],
        current_drawdown: float
    ) -> Tuple[RiskRegime, float, Dict[str, Any]]:
        """
        Europe-first regime detection using V2X + VIX + EURUSD trend + drawdown.

        ROADMAP Phase B: Multi-factor regime model optimized for
        "insurance for Europeans" strategy positioning.

        Args:
            vix_level: Current VIX level
            v2x_level: Current V2X (VSTOXX) level, or None if unavailable
            eurusd_trend: EURUSD trend (negative = EUR weakening), or None
            current_drawdown: Current portfolio drawdown (negative)

        Returns:
            Tuple of (regime, stress_score, inputs_dict)
        """
        inputs = {
            'vix': vix_level,
            'v2x': v2x_level,
            'eurusd_trend': eurusd_trend,
            'drawdown': current_drawdown,
            'v2x_available': v2x_level is not None,
            'eurusd_trend_available': eurusd_trend is not None,
        }

        # Cache values for metrics
        self._last_v2x = v2x_level
        self._last_eurusd_trend = eurusd_trend

        # Handle missing V2X: fallback to VIX-based estimate
        if v2x_level is None:
            v2x_level = vix_level * 1.2  # Historical V2X/VIX ratio ~1.2
            self._regime_inputs_missing = True
        else:
            self._regime_inputs_missing = False

        # Handle missing EURUSD trend
        if eurusd_trend is None:
            eurusd_trend = 0.0
            self._regime_inputs_missing = True

        # Compute stress score components (each normalized to 0-1)
        # V2X component: elevated when V2X > 20, max score at V2X = 40
        v2x_score = max(0, min(1, (v2x_level - 20) / 20))

        # VIX component: elevated when VIX > 20, max score at VIX = 45
        vix_score = max(0, min(1, (vix_level - 20) / 25))

        # EURUSD trend component: negative trend (EUR weakening) is stress
        # Normalize: -10% annual trend = max score
        eurusd_score = max(0, min(1, -eurusd_trend / 0.10))

        # Drawdown component: normalized to max_drawdown threshold
        drawdown_score = max(0, min(1, -current_drawdown / self.max_drawdown_pct))

        # Weighted stress score
        stress_score = (
            self.v2x_weight * v2x_score +
            self.vix_weight * vix_score +
            self.eurusd_trend_weight * eurusd_score +
            self.drawdown_weight * drawdown_score
        )

        inputs['stress_score'] = stress_score
        inputs['v2x_score'] = v2x_score
        inputs['vix_score'] = vix_score
        inputs['eurusd_score'] = eurusd_score
        inputs['drawdown_score'] = drawdown_score

        # Determine raw regime from stress score
        if stress_score >= self.stress_score_crisis_threshold:
            raw_regime = RiskRegime.CRISIS
        elif stress_score >= self.stress_score_elevated_threshold:
            raw_regime = RiskRegime.ELEVATED
        else:
            raw_regime = RiskRegime.NORMAL

        # Apply hysteresis (same as original detect_regime)
        if raw_regime != self._current_regime:
            if raw_regime == self._pending_regime:
                self._pending_regime_days += 1
            else:
                self._pending_regime = raw_regime
                self._pending_regime_days = 1

            # CRISIS is immediate, others need persistence
            if raw_regime == RiskRegime.CRISIS:
                self._current_regime = RiskRegime.CRISIS
                self._pending_regime = None
                self._pending_regime_days = 0
            elif self._pending_regime_days >= self.regime_persistence_days:
                self._current_regime = self._pending_regime
                self._pending_regime = None
                self._pending_regime_days = 0
        else:
            self._pending_regime = None
            self._pending_regime_days = 0

        inputs['regime'] = self._current_regime.value
        inputs['pending_regime'] = self._pending_regime.value if self._pending_regime else None
        inputs['pending_days'] = self._pending_regime_days

        return self._current_regime, stress_score, inputs

    def compute_eurusd_trend(self, eurusd_series: pd.Series) -> float:
        """
        Compute EURUSD trend (slope) over lookback period.

        Args:
            eurusd_series: Series of EURUSD prices

        Returns:
            Annualized trend (negative = EUR weakening)
        """
        if eurusd_series is None or len(eurusd_series) < self.eurusd_trend_lookback:
            return 0.0

        # Use returns over lookback period
        lookback = min(len(eurusd_series), self.eurusd_trend_lookback)
        returns = eurusd_series.pct_change().tail(lookback)

        # Annualize the mean return
        return returns.mean() * 252

    def get_regime_inputs(self) -> Dict[str, Any]:
        """Get cached regime inputs for metrics."""
        return {
            'v2x': self._last_v2x,
            'eurusd_trend': self._last_eurusd_trend,
            'inputs_missing': self._regime_inputs_missing,
            'current_regime': self._current_regime.value,
            'pending_regime': self._pending_regime.value if self._pending_regime else None,
            'pending_days': self._pending_regime_days,
        }

    def _detect_raw_regime(
        self,
        vix_level: float,
        spread_momentum: float,
        current_drawdown: float
    ) -> RiskRegime:
        """
        Detect raw regime without hysteresis (internal helper).
        Uses separate enter/exit thresholds for stability.
        """
        current = self._current_regime

        # CRISIS: Immediate entry, delayed exit
        if current != RiskRegime.CRISIS:
            # Enter crisis if above enter threshold
            if vix_level >= self.vix_enter_crisis or current_drawdown <= -self.max_drawdown_pct:
                return RiskRegime.CRISIS
        else:
            # In crisis - only exit if below exit threshold
            if vix_level < self.vix_exit_crisis and current_drawdown > -self.max_drawdown_pct * 0.5:
                pass  # Will check for elevated below
            else:
                return RiskRegime.CRISIS

        # ELEVATED: Separate enter/exit thresholds
        if current != RiskRegime.ELEVATED:
            # Enter elevated if above enter threshold
            if vix_level >= self.vix_enter_elevated or current_drawdown <= -0.05:
                return RiskRegime.ELEVATED
        else:
            # In elevated - only exit if below exit threshold
            if vix_level >= self.vix_exit_elevated or current_drawdown <= -0.03:
                return RiskRegime.ELEVATED

        # RECOVERY: Coming out of drawdown
        if current_drawdown >= -0.05 and current_drawdown <= -0.02 and vix_level < 20 and spread_momentum > 0:
            return RiskRegime.RECOVERY

        # NORMAL: Default
        return RiskRegime.NORMAL

    def get_risk_state_scaling(self) -> float:
        """
        Get scaling factor from risk state machine.

        Phase 8: Returns fixed scaling based on current risk state.
        This replaces multiplicative stacking.

        Returns:
            Scaling factor (1.0 for NORMAL, 0.7 for ELEVATED, 0.3 for CRISIS)
        """
        return self._risk_state.value

    def update_risk_state(self, regime: RiskRegime, current_drawdown: float) -> RiskState:
        """
        Update risk state machine based on regime and drawdown.

        Phase 8: State machine replaces multiplicative reduction.

        Args:
            regime: Current risk regime
            current_drawdown: Current portfolio drawdown

        Returns:
            New RiskState
        """
        # Drawdown floor (overrides regime)
        if current_drawdown <= -self.max_drawdown_pct:
            self._risk_state = RiskState.CRISIS
            return self._risk_state

        # Map regime to risk state
        if regime == RiskRegime.CRISIS:
            self._risk_state = RiskState.CRISIS
        elif regime == RiskRegime.ELEVATED:
            self._risk_state = RiskState.ELEVATED
        else:
            self._risk_state = RiskState.NORMAL

        return self._risk_state

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

        ENGINE_FIX_PLAN Updates:
        - Phase 6: Uses blended EWMA/rolling vol with floor
        - Phase 7: Regime detection with hysteresis
        - Phase 8: Risk state machine for scaling (not multiplicative)

        Args:
            portfolio_state: Current portfolio state
            returns_series: Historical returns
            vix_level: Current VIX level
            ratio_series: SPX/SX5E ratio series for momentum

        Returns:
            RiskDecision with scaling factors and flags
        """
        warnings = []

        # Phase 6: Compute blended volatility (EWMA + rolling)
        realized_vol = self.compute_blended_vol(returns_series)

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

        # Phase 7: Detect regime with hysteresis
        regime = self.detect_regime(vix_level, spread_momentum, current_dd)

        # Phase 8: Update risk state machine
        risk_state = self.update_risk_state(regime, current_dd)

        # Phase 6: Compute base scaling from volatility targeting (with burn-in + clamps)
        # Determine history days from returns series
        history_days = len(returns_series) if returns_series is not None else 0
        vol_scaling, scaling_diagnostics = self.compute_scaling_factor(
            realized_vol, history_days=history_days
        )

        # Phase 8: Get state machine scaling (replaces multiplicative)
        state_scaling = self.get_risk_state_scaling()

        # Final scaling is min of vol-targeting and state machine
        # This ensures we respect BOTH volatility constraints AND regime constraints
        scaling_factor = min(vol_scaling, state_scaling)

        # Emergency de-risk check (Phase 8: sets floor, not multiplier)
        emergency_derisk = current_dd <= -self.max_drawdown_pct
        if emergency_derisk:
            warnings.append(f"EMERGENCY: Drawdown {current_dd:.2%} exceeds max {self.max_drawdown_pct:.2%}")
            # State machine already set to CRISIS, so scaling_factor is 0.3

        # Spread momentum scaling for Core RV (Phase 7)
        should_reduce = spread_momentum <= 0
        reduce_factor = max(0.0, spread_momentum) if should_reduce else 1.0

        if should_reduce:
            warnings.append(f"Spread momentum <= 0: Core RV scaler = {reduce_factor:.2f}")

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
            warnings=warnings,
            scaling_diagnostics=scaling_diagnostics
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

        # Scaling (with burn-in + clamps)
        history_days = len(returns_series) if returns_series is not None else 0
        scaling, _ = self.compute_scaling_factor(vol_20d, history_days=history_days)

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
