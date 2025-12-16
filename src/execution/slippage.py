"""
Slippage Models and Calculations.

Provides:
- Slippage calculation vs arrival price
- Cost estimation models
- Collar enforcement
- Spread analysis
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Dict, Any, List
from enum import Enum


class SlippageModel(Enum):
    """Slippage estimation models."""
    FIXED_BPS = "fixed_bps"
    SPREAD_BASED = "spread_based"
    VOLUME_IMPACT = "volume_impact"
    MARKET_IMPACT = "market_impact"


@dataclass
class SlippageEstimate:
    """Estimated slippage for an order."""
    model: SlippageModel
    estimated_bps: float
    estimated_cost_usd: float
    confidence: float          # 0-1 confidence in estimate
    breakdown: Dict[str, float]  # Component breakdown


def compute_slippage_bps(
    fill_price: float,
    arrival_price: float,
    side: str,
) -> float:
    """
    Compute realized slippage in basis points.

    Positive slippage = worse execution than arrival.

    Args:
        fill_price: Average fill price
        arrival_price: Price at order arrival (mid or reference)
        side: "BUY" or "SELL"

    Returns:
        Slippage in basis points
    """
    if arrival_price == 0:
        return 0.0

    if side == "BUY":
        # Paid more than arrival = positive slippage
        slip = (fill_price - arrival_price) / arrival_price
    else:
        # Received less than arrival = positive slippage
        slip = (arrival_price - fill_price) / arrival_price

    return slip * 10000.0


def estimate_fixed_slippage(
    notional_usd: float,
    fixed_bps: float,
) -> SlippageEstimate:
    """
    Simple fixed basis point slippage estimate.

    Args:
        notional_usd: Order notional value
        fixed_bps: Fixed slippage assumption

    Returns:
        SlippageEstimate
    """
    cost = notional_usd * fixed_bps / 10000.0

    return SlippageEstimate(
        model=SlippageModel.FIXED_BPS,
        estimated_bps=fixed_bps,
        estimated_cost_usd=cost,
        confidence=0.5,
        breakdown={"fixed": fixed_bps},
    )


def estimate_spread_slippage(
    notional_usd: float,
    spread_bps: float,
    crossing_fraction: float = 0.5,
) -> SlippageEstimate:
    """
    Spread-based slippage estimate.

    Assumes you pay some fraction of the spread.

    Args:
        notional_usd: Order notional value
        spread_bps: Current bid-ask spread in bps
        crossing_fraction: Expected fraction of spread paid (0.5 = half spread)

    Returns:
        SlippageEstimate
    """
    expected_bps = spread_bps * crossing_fraction
    cost = notional_usd * expected_bps / 10000.0

    return SlippageEstimate(
        model=SlippageModel.SPREAD_BASED,
        estimated_bps=expected_bps,
        estimated_cost_usd=cost,
        confidence=0.7,
        breakdown={
            "spread": spread_bps,
            "crossing_fraction": crossing_fraction,
            "expected": expected_bps,
        },
    )


def estimate_volume_impact(
    notional_usd: float,
    order_shares: int,
    avg_daily_volume: int,
    avg_price: float,
    participation_rate: float = 0.10,
) -> SlippageEstimate:
    """
    Volume-based market impact estimate.

    Uses simplified square-root law:
    Impact = k * sqrt(participation_rate) * volatility

    Args:
        notional_usd: Order notional
        order_shares: Number of shares
        avg_daily_volume: ADV in shares
        avg_price: Average price per share
        participation_rate: Target participation rate

    Returns:
        SlippageEstimate
    """
    if avg_daily_volume == 0:
        return estimate_fixed_slippage(notional_usd, 10.0)

    # Order as fraction of daily volume
    order_pct = order_shares / avg_daily_volume

    # Simplified impact model
    # Base impact coefficient (calibrated empirically)
    k = 0.1  # 10 bps base

    # Impact scales with square root of participation
    actual_participation = min(order_pct / participation_rate, 1.0)
    impact_bps = k * (actual_participation ** 0.5) * 10000.0

    # Cap at reasonable maximum
    impact_bps = min(impact_bps, 50.0)

    cost = notional_usd * impact_bps / 10000.0

    return SlippageEstimate(
        model=SlippageModel.VOLUME_IMPACT,
        estimated_bps=impact_bps,
        estimated_cost_usd=cost,
        confidence=0.6,
        breakdown={
            "order_pct_adv": order_pct * 100,
            "participation_rate": participation_rate * 100,
            "impact_coefficient": k,
            "raw_impact_bps": impact_bps,
        },
    )


def estimate_total_cost(
    notional_usd: float,
    spread_bps: float,
    order_shares: int,
    avg_daily_volume: int,
    avg_price: float,
    commission_per_share: float = 0.005,
) -> Dict[str, float]:
    """
    Estimate total execution cost including all components.

    Args:
        notional_usd: Order notional
        spread_bps: Current spread
        order_shares: Number of shares
        avg_daily_volume: ADV
        avg_price: Price per share
        commission_per_share: Commission rate

    Returns:
        Dict with cost components
    """
    # Spread cost
    spread_cost = estimate_spread_slippage(notional_usd, spread_bps)

    # Market impact
    impact_cost = estimate_volume_impact(
        notional_usd, order_shares, avg_daily_volume, avg_price
    )

    # Commission
    commission = order_shares * commission_per_share

    total_bps = spread_cost.estimated_bps + impact_cost.estimated_bps
    total_usd = spread_cost.estimated_cost_usd + impact_cost.estimated_cost_usd + commission

    return {
        "spread_bps": spread_cost.estimated_bps,
        "impact_bps": impact_cost.estimated_bps,
        "total_bps": total_bps,
        "spread_usd": spread_cost.estimated_cost_usd,
        "impact_usd": impact_cost.estimated_cost_usd,
        "commission_usd": commission,
        "total_usd": total_usd,
    }


class CollarEnforcer:
    """
    Enforces price collars on orders.

    Ensures limit prices stay within acceptable bounds.
    """

    def __init__(self, default_max_bps: float = 10.0):
        self.default_max_bps = default_max_bps

    def calculate_collar(
        self,
        reference_price: float,
        side: str,
        max_slippage_bps: Optional[float] = None,
    ) -> Dict[str, Optional[float]]:
        """
        Calculate collar bounds for an order.

        Args:
            reference_price: Reference price for collar calculation
            side: "BUY" or "SELL"
            max_slippage_bps: Maximum slippage allowed

        Returns:
            Dict with ceiling/floor prices
        """
        if max_slippage_bps is None:
            max_slippage_bps = self.default_max_bps

        slip_mult = max_slippage_bps / 10000.0

        if side == "BUY":
            ceiling = reference_price * (1.0 + slip_mult)
            return {"ceiling": ceiling, "floor": None}
        else:
            floor = reference_price * (1.0 - slip_mult)
            return {"ceiling": None, "floor": floor}

    def enforce_collar(
        self,
        limit_price: float,
        collar: Dict[str, Optional[float]],
        side: str,
    ) -> float:
        """
        Enforce collar bounds on a limit price.

        Args:
            limit_price: Proposed limit price
            collar: Collar bounds from calculate_collar
            side: "BUY" or "SELL"

        Returns:
            Adjusted limit price within collar
        """
        if side == "BUY" and collar.get("ceiling"):
            return min(limit_price, collar["ceiling"])
        elif side == "SELL" and collar.get("floor"):
            return max(limit_price, collar["floor"])
        return limit_price

    def check_violation(
        self,
        fill_price: float,
        collar: Dict[str, Optional[float]],
        side: str,
    ) -> Optional[str]:
        """
        Check if a fill price violates the collar.

        Args:
            fill_price: Actual fill price
            collar: Collar bounds
            side: Order side

        Returns:
            Violation message or None if OK
        """
        if side == "BUY" and collar.get("ceiling"):
            if fill_price > collar["ceiling"]:
                return f"BUY fill {fill_price:.4f} > ceiling {collar['ceiling']:.4f}"
        elif side == "SELL" and collar.get("floor"):
            if fill_price < collar["floor"]:
                return f"SELL fill {fill_price:.4f} < floor {collar['floor']:.4f}"
        return None


@dataclass
class SlippageRecord:
    """Record of realized slippage for a single fill."""
    instrument_id: str
    side: str
    quantity: int
    arrival_price: float
    fill_price: float
    slippage_bps: float
    notional_usd: float
    cost_usd: float
    timestamp: datetime
    order_type: str
    replace_count: int


class SlippageTracker:
    """
    Tracks realized slippage over time.

    Used for performance analysis and policy tuning.
    """

    def __init__(self):
        self.records: List[SlippageRecord] = []

    def record(
        self,
        instrument_id: str,
        side: str,
        quantity: int,
        arrival_price: float,
        fill_price: float,
        order_type: str,
        replace_count: int = 0,
    ) -> SlippageRecord:
        """Record a fill's slippage."""
        slippage_bps = compute_slippage_bps(fill_price, arrival_price, side)
        notional = quantity * fill_price
        cost = abs(slippage_bps / 10000.0 * notional)

        record = SlippageRecord(
            instrument_id=instrument_id,
            side=side,
            quantity=quantity,
            arrival_price=arrival_price,
            fill_price=fill_price,
            slippage_bps=slippage_bps,
            notional_usd=notional,
            cost_usd=cost,
            timestamp=datetime.now(),
            order_type=order_type,
            replace_count=replace_count,
        )

        self.records.append(record)
        return record

    def get_summary(self, since: Optional[datetime] = None) -> Dict[str, Any]:
        """Get summary statistics."""
        records = self.records
        if since:
            records = [r for r in records if r.timestamp >= since]

        if not records:
            return {
                "count": 0,
                "total_notional": 0,
                "total_cost": 0,
                "avg_slippage_bps": 0,
                "max_slippage_bps": 0,
                "worst_fill": None,
            }

        slippages = [r.slippage_bps for r in records]
        worst = max(records, key=lambda r: r.slippage_bps)

        return {
            "count": len(records),
            "total_notional": sum(r.notional_usd for r in records),
            "total_cost": sum(r.cost_usd for r in records),
            "avg_slippage_bps": sum(slippages) / len(slippages),
            "max_slippage_bps": max(slippages),
            "min_slippage_bps": min(slippages),
            "worst_fill": {
                "instrument": worst.instrument_id,
                "slippage_bps": worst.slippage_bps,
                "quantity": worst.quantity,
            },
        }

    def get_by_instrument(self) -> Dict[str, Dict[str, Any]]:
        """Get summary by instrument."""
        by_inst: Dict[str, List[SlippageRecord]] = {}
        for r in self.records:
            if r.instrument_id not in by_inst:
                by_inst[r.instrument_id] = []
            by_inst[r.instrument_id].append(r)

        result = {}
        for inst, records in by_inst.items():
            slippages = [r.slippage_bps for r in records]
            result[inst] = {
                "count": len(records),
                "avg_slippage_bps": sum(slippages) / len(slippages),
                "total_cost": sum(r.cost_usd for r in records),
            }

        return result

    def clear(self) -> None:
        """Clear all records."""
        self.records.clear()
