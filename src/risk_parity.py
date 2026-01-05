"""
Risk Parity Allocator for AbstractFinance.

Phase 1: Inverse-volatility weighting across strategy sleeves.
Implements risk parity allocation with volatility targeting and
dynamic rebalancing.

Key Features:
- Inverse-vol weighting: Allocate more to lower-vol assets
- Target vol budget: 12% annual portfolio volatility
- Monthly recalibration with quarterly full rebalancing
- Integration with existing sleeve architecture
"""

import logging
import numpy as np
import pandas as pd
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from enum import Enum

from .portfolio import PortfolioState, Sleeve

logger = logging.getLogger(__name__)


class RebalanceFrequency(Enum):
    """Rebalance frequency options."""
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"


@dataclass
class SleeveVolatility:
    """Volatility metrics for a single sleeve."""
    sleeve: Sleeve
    realized_vol_20d: float
    realized_vol_60d: float
    ewma_vol: float
    correlation_to_portfolio: float = 1.0
    contribution_to_risk: float = 0.0

    @property
    def blended_vol(self) -> float:
        """Blended volatility (EWMA + rolling)."""
        return 0.7 * self.ewma_vol + 0.3 * self.realized_vol_60d


@dataclass
class RiskParityWeights:
    """Risk parity weight allocation output."""
    weights: Dict[Sleeve, float]
    inverse_vol_weights: Dict[Sleeve, float]
    vol_contributions: Dict[Sleeve, float]
    target_vol: float
    expected_portfolio_vol: float
    scaling_factor: float
    timestamp: datetime = field(default_factory=datetime.now)
    rebalance_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging/metrics."""
        return {
            "weights": {k.value: round(v, 4) for k, v in self.weights.items()},
            "inverse_vol_weights": {k.value: round(v, 4) for k, v in self.inverse_vol_weights.items()},
            "vol_contributions": {k.value: round(v, 4) for k, v in self.vol_contributions.items()},
            "target_vol": self.target_vol,
            "expected_portfolio_vol": round(self.expected_portfolio_vol, 4),
            "scaling_factor": round(self.scaling_factor, 4),
            "timestamp": self.timestamp.isoformat(),
            "rebalance_reason": self.rebalance_reason,
        }


@dataclass
class RiskParityConfig:
    """Configuration for risk parity allocator."""
    target_vol_annual: float = 0.12  # 12% annual target
    vol_floor: float = 0.06  # Minimum vol assumption
    vol_ceiling: float = 0.30  # Maximum vol assumption

    # Rebalancing settings
    rebalance_frequency: RebalanceFrequency = RebalanceFrequency.MONTHLY
    drift_threshold: float = 0.05  # 5% drift triggers rebalance

    # Weight constraints
    min_sleeve_weight: float = 0.05  # Minimum 5% per sleeve
    max_sleeve_weight: float = 0.40  # Maximum 40% per sleeve

    # Lookback windows
    vol_lookback_short: int = 20
    vol_lookback_long: int = 60
    ewma_span: int = 20

    # Sleeve-specific settings
    sleeve_vol_priors: Dict[Sleeve, float] = field(default_factory=lambda: {
        Sleeve.CORE_INDEX_RV: 0.12,  # Long/short equity RV ~12%
        Sleeve.SECTOR_RV: 0.15,  # Sector pairs ~15%
        Sleeve.CREDIT_CARRY: 0.08,  # Credit ~8%
        Sleeve.EUROPE_VOL_CONVEX: 0.25,  # Options sleeve ~25%
        Sleeve.MONEY_MARKET: 0.02,  # Money market ~2%
    })

    @classmethod
    def from_settings(cls, settings: Dict[str, Any]) -> "RiskParityConfig":
        """Create config from settings dict."""
        rp_settings = settings.get('risk_parity', {})

        # Parse rebalance frequency
        freq_str = rp_settings.get('rebalance_frequency', 'monthly')
        try:
            frequency = RebalanceFrequency(freq_str.lower())
        except ValueError:
            frequency = RebalanceFrequency.MONTHLY

        return cls(
            target_vol_annual=rp_settings.get('target_vol_annual', 0.12),
            vol_floor=rp_settings.get('vol_floor', 0.06),
            vol_ceiling=rp_settings.get('vol_ceiling', 0.30),
            rebalance_frequency=frequency,
            drift_threshold=rp_settings.get('drift_threshold', 0.05),
            min_sleeve_weight=rp_settings.get('min_sleeve_weight', 0.05),
            max_sleeve_weight=rp_settings.get('max_sleeve_weight', 0.40),
            vol_lookback_short=rp_settings.get('vol_lookback_short', 20),
            vol_lookback_long=rp_settings.get('vol_lookback_long', 60),
            ewma_span=rp_settings.get('ewma_span', 20),
        )


class RiskParityAllocator:
    """
    Risk Parity Allocator for multi-sleeve portfolio.

    Implements inverse-volatility weighting to achieve target
    portfolio volatility while equalizing risk contribution
    across sleeves.

    Key Principles:
    1. Lower-vol sleeves get higher weight
    2. Total portfolio targets 12% annual vol
    3. Risk contribution roughly equal across sleeves
    4. Constraints prevent extreme allocations
    """

    def __init__(self, config: Optional[RiskParityConfig] = None):
        """
        Initialize risk parity allocator.

        Args:
            config: Risk parity configuration
        """
        self.config = config or RiskParityConfig()

        # State tracking
        self._last_rebalance: Optional[datetime] = None
        self._current_weights: Dict[Sleeve, float] = {}
        self._sleeve_returns: Dict[Sleeve, pd.Series] = {}
        self._sleeve_vols: Dict[Sleeve, SleeveVolatility] = {}

        # Correlation matrix (updated periodically)
        self._correlation_matrix: Optional[pd.DataFrame] = None
        self._last_correlation_update: Optional[datetime] = None

    def update_sleeve_returns(
        self,
        sleeve: Sleeve,
        returns: pd.Series
    ) -> None:
        """
        Update return history for a sleeve.

        Args:
            sleeve: Sleeve to update
            returns: Daily returns series
        """
        self._sleeve_returns[sleeve] = returns

    def compute_sleeve_volatility(
        self,
        sleeve: Sleeve,
        returns: Optional[pd.Series] = None
    ) -> SleeveVolatility:
        """
        Compute volatility metrics for a sleeve.

        Args:
            sleeve: Sleeve to compute vol for
            returns: Returns series (uses cached if not provided)

        Returns:
            SleeveVolatility with all metrics
        """
        returns = returns or self._sleeve_returns.get(sleeve)

        # Use prior if no returns available
        if returns is None or len(returns) < 5:
            prior_vol = self.config.sleeve_vol_priors.get(sleeve, 0.10)
            return SleeveVolatility(
                sleeve=sleeve,
                realized_vol_20d=prior_vol,
                realized_vol_60d=prior_vol,
                ewma_vol=prior_vol,
            )

        # Compute realized vol
        vol_20d = self._compute_realized_vol(returns, self.config.vol_lookback_short)
        vol_60d = self._compute_realized_vol(returns, self.config.vol_lookback_long)

        # Compute EWMA vol
        ewma_vol = self._compute_ewma_vol(returns, self.config.ewma_span)

        # Apply floor and ceiling
        vol_20d = np.clip(vol_20d, self.config.vol_floor, self.config.vol_ceiling)
        vol_60d = np.clip(vol_60d, self.config.vol_floor, self.config.vol_ceiling)
        ewma_vol = np.clip(ewma_vol, self.config.vol_floor, self.config.vol_ceiling)

        sleeve_vol = SleeveVolatility(
            sleeve=sleeve,
            realized_vol_20d=vol_20d,
            realized_vol_60d=vol_60d,
            ewma_vol=ewma_vol,
        )

        self._sleeve_vols[sleeve] = sleeve_vol
        return sleeve_vol

    def _compute_realized_vol(
        self,
        returns: pd.Series,
        window: int
    ) -> float:
        """Compute annualized realized volatility."""
        if len(returns) < window:
            window = max(len(returns), 5)

        daily_vol = returns.tail(window).std()
        if pd.isna(daily_vol) or daily_vol <= 0:
            return self.config.vol_floor

        return daily_vol * np.sqrt(252)

    def _compute_ewma_vol(
        self,
        returns: pd.Series,
        span: int
    ) -> float:
        """Compute annualized EWMA volatility."""
        if len(returns) < 5:
            return self.config.vol_floor

        ewma_vol = returns.ewm(span=span).std().iloc[-1]
        if pd.isna(ewma_vol) or ewma_vol <= 0:
            return self.config.vol_floor

        return ewma_vol * np.sqrt(252)

    def compute_inverse_vol_weights(
        self,
        sleeve_vols: Optional[Dict[Sleeve, float]] = None
    ) -> Dict[Sleeve, float]:
        """
        Compute inverse-volatility weights.

        Higher weight to lower-vol sleeves.

        Args:
            sleeve_vols: Dict of sleeve -> annualized vol
                        Uses cached if not provided

        Returns:
            Dict of sleeve -> inverse-vol weight (sums to 1.0)
        """
        # Get volatilities
        if sleeve_vols is None:
            sleeve_vols = {}
            for sleeve in Sleeve:
                if sleeve in self._sleeve_vols:
                    sleeve_vols[sleeve] = self._sleeve_vols[sleeve].blended_vol
                else:
                    sleeve_vols[sleeve] = self.config.sleeve_vol_priors.get(sleeve, 0.10)

        # Compute inverse volatilities
        inverse_vols = {}
        for sleeve, vol in sleeve_vols.items():
            # Floor the vol to avoid extreme weights
            floored_vol = max(vol, self.config.vol_floor)
            inverse_vols[sleeve] = 1.0 / floored_vol

        # Normalize to sum to 1.0
        total_inverse = sum(inverse_vols.values())
        if total_inverse <= 0:
            # Fallback to equal weight
            n = len(inverse_vols)
            return {sleeve: 1.0 / n for sleeve in inverse_vols}

        weights = {
            sleeve: inv / total_inverse
            for sleeve, inv in inverse_vols.items()
        }

        return weights

    def apply_weight_constraints(
        self,
        weights: Dict[Sleeve, float]
    ) -> Dict[Sleeve, float]:
        """
        Apply min/max weight constraints.

        Args:
            weights: Unconstrained weights

        Returns:
            Constrained weights (still sum to 1.0)
        """
        constrained = {}

        # First pass: apply min/max constraints
        overflow = 0.0
        underflow = 0.0

        for sleeve, weight in weights.items():
            if weight < self.config.min_sleeve_weight:
                underflow += self.config.min_sleeve_weight - weight
                constrained[sleeve] = self.config.min_sleeve_weight
            elif weight > self.config.max_sleeve_weight:
                overflow += weight - self.config.max_sleeve_weight
                constrained[sleeve] = self.config.max_sleeve_weight
            else:
                constrained[sleeve] = weight

        # Second pass: redistribute overflow/underflow
        if overflow > 0 or underflow > 0:
            net_adjustment = overflow - underflow

            # Find sleeves that can absorb adjustment
            adjustable_sleeves = [
                s for s, w in constrained.items()
                if self.config.min_sleeve_weight < w < self.config.max_sleeve_weight
            ]

            if adjustable_sleeves:
                adj_per_sleeve = net_adjustment / len(adjustable_sleeves)
                for sleeve in adjustable_sleeves:
                    constrained[sleeve] += adj_per_sleeve

        # Normalize to ensure sum = 1.0
        total = sum(constrained.values())
        if total > 0:
            constrained = {k: v / total for k, v in constrained.items()}

        return constrained

    def compute_vol_contributions(
        self,
        weights: Dict[Sleeve, float],
        sleeve_vols: Dict[Sleeve, float]
    ) -> Dict[Sleeve, float]:
        """
        Compute each sleeve's contribution to portfolio volatility.

        Uses simplified formula: contribution = weight * vol * correlation
        For uncorrelated sleeves, correlation assumed to be 0.5.

        Args:
            weights: Sleeve weights
            sleeve_vols: Sleeve volatilities

        Returns:
            Dict of sleeve -> volatility contribution
        """
        contributions = {}

        # Simplified: assume average correlation of 0.5 between sleeves
        # In production, would use actual correlation matrix
        avg_correlation = 0.5

        for sleeve, weight in weights.items():
            vol = sleeve_vols.get(sleeve, self.config.sleeve_vol_priors.get(sleeve, 0.10))
            # Marginal contribution to volatility
            contributions[sleeve] = weight * vol * avg_correlation

        return contributions

    def compute_portfolio_volatility(
        self,
        weights: Dict[Sleeve, float],
        sleeve_vols: Dict[Sleeve, float],
        correlation: float = 0.5
    ) -> float:
        """
        Estimate portfolio volatility given weights and sleeve vols.

        Uses simplified formula with average correlation.

        Args:
            weights: Sleeve weights
            sleeve_vols: Sleeve volatilities
            correlation: Average correlation between sleeves

        Returns:
            Expected annualized portfolio volatility
        """
        # Portfolio variance with correlation
        # Var(P) = sum(w_i^2 * var_i) + sum_i!=j(w_i * w_j * cov_ij)
        # Simplified: cov_ij = rho * vol_i * vol_j

        variance = 0.0

        sleeves = list(weights.keys())
        for i, sleeve_i in enumerate(sleeves):
            w_i = weights.get(sleeve_i, 0)
            vol_i = sleeve_vols.get(sleeve_i, self.config.vol_floor)

            # Own variance contribution
            variance += w_i ** 2 * vol_i ** 2

            # Cross-variance contributions
            for sleeve_j in sleeves[i+1:]:
                w_j = weights.get(sleeve_j, 0)
                vol_j = sleeve_vols.get(sleeve_j, self.config.vol_floor)

                # Covariance = rho * vol_i * vol_j
                covariance = correlation * vol_i * vol_j
                variance += 2 * w_i * w_j * covariance

        return np.sqrt(variance)

    def compute_scaling_factor(
        self,
        expected_vol: float
    ) -> float:
        """
        Compute scaling factor to achieve target volatility.

        Args:
            expected_vol: Expected portfolio volatility

        Returns:
            Scaling factor (< 1.0 to delever, > 1.0 to lever up)
        """
        if expected_vol <= 0:
            return 1.0

        scaling = self.config.target_vol_annual / expected_vol

        # Clip to reasonable range [0.5, 2.0]
        return np.clip(scaling, 0.5, 2.0)

    def should_rebalance(
        self,
        current_weights: Dict[Sleeve, float],
        target_weights: Dict[Sleeve, float],
        today: Optional[date] = None
    ) -> Tuple[bool, str]:
        """
        Check if rebalancing is needed.

        Args:
            current_weights: Current sleeve weights
            target_weights: Target sleeve weights
            today: Current date

        Returns:
            Tuple of (should_rebalance, reason)
        """
        today = today or date.today()

        # Check drift
        max_drift = 0.0
        for sleeve in target_weights:
            current = current_weights.get(sleeve, 0)
            target = target_weights.get(sleeve, 0)
            drift = abs(current - target)
            max_drift = max(max_drift, drift)

        if max_drift >= self.config.drift_threshold:
            return True, f"Drift {max_drift:.1%} exceeds threshold {self.config.drift_threshold:.1%}"

        # Check time since last rebalance
        if self._last_rebalance is None:
            return True, "Initial rebalance"

        days_since = (today - self._last_rebalance.date()).days

        if self.config.rebalance_frequency == RebalanceFrequency.DAILY:
            if days_since >= 1:
                return True, "Daily rebalance"
        elif self.config.rebalance_frequency == RebalanceFrequency.WEEKLY:
            if days_since >= 7:
                return True, "Weekly rebalance"
        elif self.config.rebalance_frequency == RebalanceFrequency.MONTHLY:
            if days_since >= 21:  # ~1 month trading days
                return True, "Monthly rebalance"
        elif self.config.rebalance_frequency == RebalanceFrequency.QUARTERLY:
            if days_since >= 63:  # ~3 months trading days
                return True, "Quarterly rebalance"

        return False, "No rebalance needed"

    def compute_risk_parity_weights(
        self,
        portfolio_state: Optional[PortfolioState] = None,
        today: Optional[date] = None,
        force_rebalance: bool = False
    ) -> RiskParityWeights:
        """
        Compute risk parity weights for all sleeves.

        Main entry point for risk parity allocation.

        Args:
            portfolio_state: Current portfolio state (for current weights)
            today: Current date
            force_rebalance: Force recalculation even if not needed

        Returns:
            RiskParityWeights with allocations and metadata
        """
        today = today or date.today()

        # Get current weights from portfolio state
        current_weights = {}
        if portfolio_state:
            total_exposure = sum(
                abs(pos.market_value)
                for pos in portfolio_state.positions.values()
            )
            if total_exposure > 0:
                for sleeve in Sleeve:
                    sleeve_exposure = sum(
                        abs(pos.market_value)
                        for pos in portfolio_state.positions.values()
                        if pos.sleeve == sleeve
                    )
                    current_weights[sleeve] = sleeve_exposure / total_exposure

        # Compute sleeve volatilities
        sleeve_vols = {}
        for sleeve in Sleeve:
            vol_metrics = self.compute_sleeve_volatility(sleeve)
            sleeve_vols[sleeve] = vol_metrics.blended_vol

        # Compute inverse-vol weights
        raw_weights = self.compute_inverse_vol_weights(sleeve_vols)

        # Apply constraints
        constrained_weights = self.apply_weight_constraints(raw_weights)

        # Check if rebalance needed
        should_rebal, reason = self.should_rebalance(
            current_weights or {},
            constrained_weights,
            today
        )

        if not should_rebal and not force_rebalance and self._current_weights:
            # Return existing weights
            return RiskParityWeights(
                weights=self._current_weights,
                inverse_vol_weights=raw_weights,
                vol_contributions=self.compute_vol_contributions(
                    self._current_weights, sleeve_vols
                ),
                target_vol=self.config.target_vol_annual,
                expected_portfolio_vol=self.compute_portfolio_volatility(
                    self._current_weights, sleeve_vols
                ),
                scaling_factor=1.0,
                rebalance_reason="No change needed"
            )

        # Compute portfolio vol with new weights
        expected_vol = self.compute_portfolio_volatility(constrained_weights, sleeve_vols)

        # Compute scaling factor
        scaling = self.compute_scaling_factor(expected_vol)

        # Compute vol contributions
        vol_contributions = self.compute_vol_contributions(constrained_weights, sleeve_vols)

        # Update state
        self._current_weights = constrained_weights
        self._last_rebalance = datetime.now()

        logger.info(
            f"Risk parity weights computed: expected_vol={expected_vol:.2%}, "
            f"scaling={scaling:.2f}, reason={reason}"
        )

        return RiskParityWeights(
            weights=constrained_weights,
            inverse_vol_weights=raw_weights,
            vol_contributions=vol_contributions,
            target_vol=self.config.target_vol_annual,
            expected_portfolio_vol=expected_vol,
            scaling_factor=scaling,
            rebalance_reason=reason
        )

    def get_summary(self) -> Dict[str, Any]:
        """Get summary of current risk parity state."""
        return {
            "current_weights": {
                k.value: round(v, 4) for k, v in self._current_weights.items()
            } if self._current_weights else {},
            "last_rebalance": self._last_rebalance.isoformat() if self._last_rebalance else None,
            "sleeve_vols": {
                k.value: {
                    "blended": round(v.blended_vol, 4),
                    "ewma": round(v.ewma_vol, 4),
                    "realized_20d": round(v.realized_vol_20d, 4),
                    "realized_60d": round(v.realized_vol_60d, 4),
                }
                for k, v in self._sleeve_vols.items()
            } if self._sleeve_vols else {},
            "config": {
                "target_vol": self.config.target_vol_annual,
                "rebalance_frequency": self.config.rebalance_frequency.value,
                "drift_threshold": self.config.drift_threshold,
                "min_weight": self.config.min_sleeve_weight,
                "max_weight": self.config.max_sleeve_weight,
            }
        }
