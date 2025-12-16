"""
Liquidity Analysis - ADV estimates and size thresholds.

Provides:
- Average daily volume estimates
- Liquidity tier classification
- Size limit calculations
- Participation rate recommendations
"""

from dataclasses import dataclass
from datetime import datetime, date, timedelta
from typing import Dict, Optional, List, Any
from enum import Enum


class LiquidityTier(Enum):
    """Liquidity classification tiers."""
    ULTRA_LIQUID = 1    # Major index ETFs, mega-cap
    LIQUID = 2          # Large-cap, sector ETFs
    MODERATE = 3        # Mid-cap, specialty ETFs
    ILLIQUID = 4        # Small-cap, frontier markets


@dataclass
class LiquidityProfile:
    """Liquidity profile for an instrument."""
    instrument_id: str
    tier: LiquidityTier
    avg_daily_volume: int           # ADV in shares
    avg_daily_notional: float       # ADV in USD
    avg_spread_bps: float           # Typical bid-ask spread
    typical_trade_size: int         # Institutional trade size
    max_single_order_pct: float     # Max % of ADV per order
    recommended_participation: float  # Recommended POV rate


# Default liquidity profiles by asset class / type
DEFAULT_PROFILES: Dict[str, LiquidityProfile] = {
    # Ultra-liquid US ETFs
    "SPY": LiquidityProfile("SPY", LiquidityTier.ULTRA_LIQUID, 80_000_000, 40_000_000_000, 1, 10000, 0.10, 0.20),
    "QQQ": LiquidityProfile("QQQ", LiquidityTier.ULTRA_LIQUID, 50_000_000, 20_000_000_000, 1, 10000, 0.10, 0.20),
    "IWM": LiquidityProfile("IWM", LiquidityTier.ULTRA_LIQUID, 30_000_000, 6_000_000_000, 2, 5000, 0.08, 0.15),

    # EU ETFs (less liquid)
    "CSPX": LiquidityProfile("CSPX", LiquidityTier.LIQUID, 500_000, 250_000_000, 5, 1000, 0.05, 0.10),
    "CS51": LiquidityProfile("CS51", LiquidityTier.LIQUID, 200_000, 80_000_000, 8, 500, 0.03, 0.08),
    "IUKD": LiquidityProfile("IUKD", LiquidityTier.MODERATE, 100_000, 10_000_000, 15, 200, 0.02, 0.05),

    # Futures (very liquid)
    "ES": LiquidityProfile("ES", LiquidityTier.ULTRA_LIQUID, 1_500_000, 350_000_000_000, 1, 100, 0.10, 0.20),
    "FESX": LiquidityProfile("FESX", LiquidityTier.LIQUID, 500_000, 25_000_000_000, 2, 50, 0.05, 0.10),

    # FX Futures
    "M6E": LiquidityProfile("M6E", LiquidityTier.LIQUID, 100_000, 1_500_000_000, 2, 50, 0.05, 0.10),
    "M6B": LiquidityProfile("M6B", LiquidityTier.LIQUID, 50_000, 500_000_000, 3, 25, 0.04, 0.08),
}


class LiquidityEstimator:
    """
    Estimates liquidity metrics for instruments.

    Uses a combination of:
    - Static profiles (for known instruments)
    - Default estimates by asset class
    - Historical volume data (if available)
    """

    def __init__(self):
        self.profiles: Dict[str, LiquidityProfile] = DEFAULT_PROFILES.copy()
        self.volume_history: Dict[str, List[int]] = {}

    def get_profile(self, instrument_id: str) -> LiquidityProfile:
        """Get liquidity profile for an instrument."""
        if instrument_id in self.profiles:
            return self.profiles[instrument_id]

        # Generate default profile based on instrument type
        return self._generate_default_profile(instrument_id)

    def update_volume(
        self,
        instrument_id: str,
        volume: int,
        for_date: Optional[date] = None,
    ) -> None:
        """Update volume history for an instrument."""
        if instrument_id not in self.volume_history:
            self.volume_history[instrument_id] = []

        self.volume_history[instrument_id].append(volume)

        # Keep last 20 days
        if len(self.volume_history[instrument_id]) > 20:
            self.volume_history[instrument_id] = self.volume_history[instrument_id][-20:]

        # Update profile ADV if we have enough history
        if len(self.volume_history[instrument_id]) >= 5:
            avg_vol = sum(self.volume_history[instrument_id]) / len(self.volume_history[instrument_id])
            if instrument_id in self.profiles:
                self.profiles[instrument_id].avg_daily_volume = int(avg_vol)

    def get_adv(self, instrument_id: str) -> int:
        """Get average daily volume estimate."""
        return self.get_profile(instrument_id).avg_daily_volume

    def get_max_order_size(
        self,
        instrument_id: str,
        price: float,
    ) -> Dict[str, Any]:
        """
        Calculate maximum recommended order size.

        Returns dict with shares and notional limits.
        """
        profile = self.get_profile(instrument_id)

        max_shares = int(profile.avg_daily_volume * profile.max_single_order_pct)
        max_notional = max_shares * price

        return {
            "max_shares": max_shares,
            "max_notional": max_notional,
            "adv": profile.avg_daily_volume,
            "max_pct_adv": profile.max_single_order_pct,
            "tier": profile.tier.name,
        }

    def classify_order_size(
        self,
        instrument_id: str,
        quantity: int,
    ) -> str:
        """
        Classify order size relative to ADV.

        Returns:
            Size classification: "small", "medium", "large", "very_large"
        """
        profile = self.get_profile(instrument_id)
        pct_adv = quantity / profile.avg_daily_volume if profile.avg_daily_volume > 0 else 1.0

        if pct_adv < 0.01:
            return "small"
        elif pct_adv < 0.05:
            return "medium"
        elif pct_adv < 0.10:
            return "large"
        else:
            return "very_large"

    def should_slice(
        self,
        instrument_id: str,
        quantity: int,
        threshold_pct: float = 0.01,
    ) -> bool:
        """Check if order should be sliced."""
        profile = self.get_profile(instrument_id)
        pct_adv = quantity / profile.avg_daily_volume if profile.avg_daily_volume > 0 else 1.0
        return pct_adv > threshold_pct

    def get_slice_params(
        self,
        instrument_id: str,
        total_quantity: int,
        interval_seconds: int = 20,
    ) -> Dict[str, Any]:
        """
        Get slicing parameters for a large order.

        Returns recommended slice size and timing.
        """
        profile = self.get_profile(instrument_id)

        # Target participation rate
        target_pov = profile.recommended_participation

        # Estimate volume per interval (assume 6.5 hour trading day)
        total_intervals = 6.5 * 60 * 60 / interval_seconds
        avg_volume_per_interval = profile.avg_daily_volume / total_intervals

        # Max shares per interval at target POV
        max_per_slice = int(avg_volume_per_interval * target_pov)
        max_per_slice = max(max_per_slice, 1)  # At least 1 share

        # Number of slices needed
        num_slices = (total_quantity + max_per_slice - 1) // max_per_slice

        # Expected execution time
        expected_time_seconds = num_slices * interval_seconds

        return {
            "slice_size": max_per_slice,
            "num_slices": num_slices,
            "interval_seconds": interval_seconds,
            "target_pov": target_pov,
            "expected_time_seconds": expected_time_seconds,
            "expected_time_minutes": expected_time_seconds / 60,
        }

    def _generate_default_profile(self, instrument_id: str) -> LiquidityProfile:
        """Generate default profile for unknown instrument."""
        # Guess based on instrument ID patterns
        if instrument_id.startswith("ES") or instrument_id.startswith("M6"):
            tier = LiquidityTier.LIQUID
            adv = 100_000
            spread = 3
        elif any(instrument_id.endswith(x) for x in ["ETF", "SPY", "QQQ"]):
            tier = LiquidityTier.LIQUID
            adv = 1_000_000
            spread = 5
        else:
            # Default to moderate
            tier = LiquidityTier.MODERATE
            adv = 500_000
            spread = 10

        return LiquidityProfile(
            instrument_id=instrument_id,
            tier=tier,
            avg_daily_volume=adv,
            avg_daily_notional=0,  # Unknown
            avg_spread_bps=spread,
            typical_trade_size=int(adv * 0.001),
            max_single_order_pct=0.05,
            recommended_participation=0.10,
        )

    def set_profile(self, profile: LiquidityProfile) -> None:
        """Set or update a liquidity profile."""
        self.profiles[profile.instrument_id] = profile


# Singleton instance
_estimator_instance: Optional[LiquidityEstimator] = None


def get_liquidity_estimator() -> LiquidityEstimator:
    """Get singleton LiquidityEstimator instance."""
    global _estimator_instance
    if _estimator_instance is None:
        _estimator_instance = LiquidityEstimator()
    return _estimator_instance
