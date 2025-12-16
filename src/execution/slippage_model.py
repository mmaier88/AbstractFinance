"""
Self-Calibrating Slippage Model - Learns from execution history.

Phase 2 Enhancement: Updates slippage parameters from ExecutionAnalytics
to provide accurate cost estimates for trade gating and limit pricing.

The model tracks:
- Implementation shortfall (IS) by instrument and asset class
- Fill rates and time-to-fill
- Adverse selection indicators
"""

import json
import logging
import statistics
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Optional, Any
from threading import Lock

from .analytics import ExecutionAnalytics, OrderMetrics


logger = logging.getLogger(__name__)


@dataclass
class SlippageModelConfig:
    """Configuration for slippage model."""
    enabled: bool = True
    lookback_trades: int = 200
    min_trades_per_instrument: int = 15
    percentile_for_limits: float = 0.70      # Use p70 as baseline
    safety_buffer_bps: float = 1.0
    clamp_bps: tuple = (0.5, 25.0)           # (min, max)
    persist_path: str = "state/slippage_model.json"
    update_frequency: str = "daily"          # "daily" or "after_each_trade"


@dataclass
class InstrumentSlippageStats:
    """Slippage statistics for a single instrument."""
    instrument_id: str
    sample_count: int = 0
    median_is_bps: float = 0.0
    p70_is_bps: float = 0.0
    p90_is_bps: float = 0.0
    mean_is_bps: float = 0.0
    std_is_bps: float = 0.0
    fill_rate: float = 1.0
    avg_time_to_fill_s: float = 0.0
    avg_replace_count: float = 0.0
    adverse_selection_bps: float = 0.0       # Signed IS (positive = adverse)
    last_updated: Optional[str] = None


@dataclass
class AssetClassSlippageStats:
    """Slippage statistics for an asset class."""
    asset_class: str
    sample_count: int = 0
    median_is_bps: float = 0.0
    p70_is_bps: float = 0.0
    p90_is_bps: float = 0.0
    mean_is_bps: float = 0.0
    fill_rate: float = 1.0
    last_updated: Optional[str] = None


# Default slippage by asset class (used when no history)
DEFAULT_SLIPPAGE_BY_ASSET_CLASS = {
    "ETF": 3.0,
    "STK": 5.0,
    "FUT": 1.5,
    "FX_FUT": 1.0,
    "OPT": 10.0,
}


class SlippageModel:
    """
    Self-calibrating slippage model.

    Learns from execution history to provide accurate slippage
    estimates for trade gating and limit price calculation.

    Usage:
        model = SlippageModel(config)
        model.update_from_analytics(analytics)
        est_slip = model.get_estimated_slippage_bps("AAPL", "BUY")
    """

    def __init__(
        self,
        config: Optional[SlippageModelConfig] = None,
    ):
        """
        Initialize slippage model.

        Args:
            config: Model configuration
        """
        self.config = config or SlippageModelConfig()
        self._lock = Lock()

        # Statistics storage
        self.instrument_stats: Dict[str, InstrumentSlippageStats] = {}
        self.asset_class_stats: Dict[str, AssetClassSlippageStats] = {}

        # Raw history for recalculation
        self._recent_trades: List[Dict[str, Any]] = []

        # Persistence
        self.persist_path = Path(self.config.persist_path)
        self.persist_path.parent.mkdir(parents=True, exist_ok=True)

        # Load existing model
        self._load()

    def _load(self) -> None:
        """Load model from disk."""
        if not self.persist_path.exists():
            return

        try:
            with open(self.persist_path, "r") as f:
                data = json.load(f)

            # Load instrument stats
            for inst_id, stats_dict in data.get("instrument_stats", {}).items():
                self.instrument_stats[inst_id] = InstrumentSlippageStats(**stats_dict)

            # Load asset class stats
            for ac, stats_dict in data.get("asset_class_stats", {}).items():
                self.asset_class_stats[ac] = AssetClassSlippageStats(**stats_dict)

            # Load recent trades
            self._recent_trades = data.get("recent_trades", [])

            logger.info(f"Loaded slippage model: {len(self.instrument_stats)} instruments")

        except Exception as e:
            logger.error(f"Failed to load slippage model: {e}")

    def _save(self) -> None:
        """Save model to disk."""
        try:
            data = {
                "updated_at": datetime.utcnow().isoformat(),
                "instrument_stats": {
                    inst_id: asdict(stats)
                    for inst_id, stats in self.instrument_stats.items()
                },
                "asset_class_stats": {
                    ac: asdict(stats)
                    for ac, stats in self.asset_class_stats.items()
                },
                "recent_trades": self._recent_trades[-self.config.lookback_trades:],
            }
            with open(self.persist_path, "w") as f:
                json.dump(data, f, indent=2)

        except Exception as e:
            logger.error(f"Failed to save slippage model: {e}")

    def update_from_analytics(
        self,
        analytics: ExecutionAnalytics,
        for_date: Optional[date] = None,
    ) -> None:
        """
        Update model from execution analytics.

        Called at end of day to incorporate recent execution data.

        Args:
            analytics: ExecutionAnalytics instance with order history
            for_date: Date to process (default: today)
        """
        if not self.config.enabled:
            return

        target_date = for_date or date.today()

        # Get orders for the day
        day_orders = [
            m for m in analytics.order_metrics
            if m.timestamp.date() == target_date
            and m.status == "FILLED"
            and m.slippage_bps is not None
        ]

        if not day_orders:
            logger.info("No filled orders to update slippage model")
            return

        with self._lock:
            # Add to recent trades
            for order in day_orders:
                self._recent_trades.append({
                    "instrument_id": order.instrument_id,
                    "side": order.side,
                    "slippage_bps": order.slippage_bps,
                    "notional_usd": order.notional_usd,
                    "elapsed_seconds": order.elapsed_seconds,
                    "replace_count": order.replace_count,
                    "timestamp": order.timestamp.isoformat(),
                })

            # Trim to lookback
            self._recent_trades = self._recent_trades[-self.config.lookback_trades:]

            # Recalculate statistics
            self._recalculate_stats()

            # Save
            self._save()

        logger.info(f"Updated slippage model with {len(day_orders)} orders")

    def _recalculate_stats(self) -> None:
        """Recalculate all statistics from recent trades."""
        # Group by instrument
        by_instrument: Dict[str, List[Dict]] = {}
        by_asset_class: Dict[str, List[Dict]] = {}

        for trade in self._recent_trades:
            inst_id = trade["instrument_id"]
            if inst_id not in by_instrument:
                by_instrument[inst_id] = []
            by_instrument[inst_id].append(trade)

            # Guess asset class
            ac = self._guess_asset_class(inst_id)
            if ac not in by_asset_class:
                by_asset_class[ac] = []
            by_asset_class[ac].append(trade)

        # Calculate instrument stats
        for inst_id, trades in by_instrument.items():
            self.instrument_stats[inst_id] = self._calculate_stats_for_trades(
                inst_id, trades, is_instrument=True
            )

        # Calculate asset class stats
        for ac, trades in by_asset_class.items():
            stats = self._calculate_stats_for_trades(ac, trades, is_instrument=False)
            self.asset_class_stats[ac] = AssetClassSlippageStats(
                asset_class=ac,
                sample_count=stats.sample_count,
                median_is_bps=stats.median_is_bps,
                p70_is_bps=stats.p70_is_bps,
                p90_is_bps=stats.p90_is_bps,
                mean_is_bps=stats.mean_is_bps,
                fill_rate=stats.fill_rate,
                last_updated=datetime.utcnow().isoformat(),
            )

    def _calculate_stats_for_trades(
        self,
        identifier: str,
        trades: List[Dict],
        is_instrument: bool,
    ) -> InstrumentSlippageStats:
        """Calculate statistics for a set of trades."""
        if not trades:
            return InstrumentSlippageStats(instrument_id=identifier)

        # Extract slippage values (absolute)
        slippages = [abs(t["slippage_bps"]) for t in trades]
        signed_slippages = [t["slippage_bps"] for t in trades]  # For adverse selection

        # Basic statistics
        n = len(slippages)
        sorted_slip = sorted(slippages)

        median_is = statistics.median(slippages) if slippages else 0.0
        mean_is = statistics.mean(slippages) if slippages else 0.0
        std_is = statistics.stdev(slippages) if len(slippages) > 1 else 0.0

        # Percentiles
        p70_idx = int(n * 0.70)
        p90_idx = int(n * 0.90)
        p70_is = sorted_slip[min(p70_idx, n - 1)] if slippages else 0.0
        p90_is = sorted_slip[min(p90_idx, n - 1)] if slippages else 0.0

        # Apply clamps
        min_bps, max_bps = self.config.clamp_bps
        p70_is = max(min_bps, min(max_bps, p70_is))

        # Time and replace stats
        times = [t.get("elapsed_seconds", 0) for t in trades]
        replaces = [t.get("replace_count", 0) for t in trades]

        avg_time = statistics.mean(times) if times else 0.0
        avg_replace = statistics.mean(replaces) if replaces else 0.0

        # Adverse selection (mean of signed slippage)
        adverse = statistics.mean(signed_slippages) if signed_slippages else 0.0

        return InstrumentSlippageStats(
            instrument_id=identifier,
            sample_count=n,
            median_is_bps=median_is,
            p70_is_bps=p70_is,
            p90_is_bps=p90_is,
            mean_is_bps=mean_is,
            std_is_bps=std_is,
            fill_rate=1.0,  # All these trades filled
            avg_time_to_fill_s=avg_time,
            avg_replace_count=avg_replace,
            adverse_selection_bps=adverse,
            last_updated=datetime.utcnow().isoformat(),
        )

    def get_estimated_slippage_bps(
        self,
        instrument_id: str,
        side: str = "BUY",
        asset_class: Optional[str] = None,
    ) -> float:
        """
        Get estimated slippage for an instrument.

        Priority:
        1. Instrument-specific if enough samples
        2. Asset class if available
        3. Default by asset class

        Args:
            instrument_id: Instrument identifier
            side: Trade side (BUY or SELL)
            asset_class: Optional asset class override

        Returns:
            Estimated slippage in basis points
        """
        if not self.config.enabled:
            return DEFAULT_SLIPPAGE_BY_ASSET_CLASS.get("ETF", 5.0)

        with self._lock:
            # Try instrument-specific
            inst_stats = self.instrument_stats.get(instrument_id)
            if inst_stats and inst_stats.sample_count >= self.config.min_trades_per_instrument:
                base = inst_stats.p70_is_bps
            else:
                # Fall back to asset class
                ac = asset_class or self._guess_asset_class(instrument_id)
                ac_stats = self.asset_class_stats.get(ac)

                if ac_stats and ac_stats.sample_count > 0:
                    base = ac_stats.p70_is_bps
                else:
                    base = DEFAULT_SLIPPAGE_BY_ASSET_CLASS.get(ac, 5.0)

            # Add safety buffer
            estimate = base + self.config.safety_buffer_bps

            # Apply clamps
            min_bps, max_bps = self.config.clamp_bps
            return max(min_bps, min(max_bps, estimate))

    def get_limit_offset_bps(
        self,
        instrument_id: str,
        side: str,
        asset_class: Optional[str] = None,
    ) -> float:
        """
        Get limit price offset for marketable limits.

        Uses slippage estimate to determine how aggressive
        the initial limit should be.

        Args:
            instrument_id: Instrument identifier
            side: Trade side
            asset_class: Optional asset class

        Returns:
            Offset in basis points to add to mid for limit
        """
        base_slip = self.get_estimated_slippage_bps(instrument_id, side, asset_class)

        # For BUY: add offset to mid (willing to pay more)
        # For SELL: subtract offset from mid (willing to receive less)
        # ExecutionPolicy should use this as the initial aggressiveness
        return base_slip * self.config.percentile_for_limits

    def get_instrument_stats(
        self,
        instrument_id: str,
    ) -> Optional[InstrumentSlippageStats]:
        """Get detailed stats for an instrument."""
        with self._lock:
            return self.instrument_stats.get(instrument_id)

    def get_asset_class_stats(
        self,
        asset_class: str,
    ) -> Optional[AssetClassSlippageStats]:
        """Get detailed stats for an asset class."""
        with self._lock:
            return self.asset_class_stats.get(asset_class)

    def get_summary(self) -> Dict[str, Any]:
        """Get summary of model state."""
        with self._lock:
            return {
                "total_instruments": len(self.instrument_stats),
                "total_asset_classes": len(self.asset_class_stats),
                "total_recent_trades": len(self._recent_trades),
                "instruments_with_enough_samples": sum(
                    1 for s in self.instrument_stats.values()
                    if s.sample_count >= self.config.min_trades_per_instrument
                ),
                "asset_class_summary": {
                    ac: {
                        "samples": stats.sample_count,
                        "p70_bps": stats.p70_is_bps,
                    }
                    for ac, stats in self.asset_class_stats.items()
                },
            }

    def _guess_asset_class(self, instrument_id: str) -> str:
        """Guess asset class from instrument ID."""
        symbol = instrument_id.upper()

        # Futures patterns
        if any(symbol.startswith(f) for f in ["ES", "NQ", "RTY", "YM", "FESX", "FDAX", "FGBL", "ZN", "ZB"]):
            return "FUT"
        if any(symbol.startswith(f) for f in ["M6E", "M6B", "M6A", "6E", "6B", "6A"]):
            return "FX_FUT"

        # ETF patterns
        if symbol in ["SPY", "QQQ", "IWM", "DIA", "EEM", "VTI", "VEA", "EFA", "FEZ", "VGK"]:
            return "ETF"

        # Options
        if len(symbol) > 6 and (symbol[-9:-6].isdigit() or "C" in symbol[-3:] or "P" in symbol[-3:]):
            return "OPT"

        # Default to stock
        return "STK"


# Singleton instance
_slippage_model: Optional[SlippageModel] = None


def get_slippage_model() -> SlippageModel:
    """Get singleton SlippageModel instance."""
    global _slippage_model
    if _slippage_model is None:
        _slippage_model = SlippageModel()
    return _slippage_model


def init_slippage_model(
    config: Optional[SlippageModelConfig] = None,
) -> SlippageModel:
    """Initialize the slippage model singleton."""
    global _slippage_model
    _slippage_model = SlippageModel(config=config)
    return _slippage_model
