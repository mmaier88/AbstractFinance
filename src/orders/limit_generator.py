"""
Limit Order Generator for AbstractFinance.

Generates safe limit prices based on reference price confidence levels.
Higher confidence = tighter spreads, lower confidence = wider margins.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class OrderSide(Enum):
    """Order direction."""
    BUY = "BUY"
    SELL = "SELL"


class PriceAdjustment(Enum):
    """Price adjustment strategy."""
    AGGRESSIVE = "aggressive"  # Cross the spread (market-like)
    PASSIVE = "passive"        # Stay on our side of spread
    MIDPOINT = "midpoint"      # Target mid price
    GUARDRAIL = "guardrail"    # Maximum safety margin


@dataclass
class LimitOrderSpec:
    """Specification for a limit order."""

    instrument_id: str
    side: OrderSide
    quantity: float
    limit_price: float
    reference_price: float
    confidence: float
    adjustment: PriceAdjustment
    spread_bps: float  # Spread from reference in basis points

    # Metadata
    price_tier: str
    price_source: str
    generated_at: datetime

    @property
    def is_buy(self) -> bool:
        return self.side == OrderSide.BUY

    @property
    def is_sell(self) -> bool:
        return self.side == OrderSide.SELL

    @property
    def slippage_tolerance_pct(self) -> float:
        """Maximum acceptable slippage as percentage."""
        return self.spread_bps / 100.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "side": self.side.value,
            "quantity": self.quantity,
            "limit_price": self.limit_price,
            "reference_price": self.reference_price,
            "confidence": self.confidence,
            "adjustment": self.adjustment.value,
            "spread_bps": self.spread_bps,
            "price_tier": self.price_tier,
            "price_source": self.price_source,
            "generated_at": self.generated_at.isoformat(),
        }


# Default spread configurations by confidence level
# Lower confidence = wider spreads for safety
DEFAULT_SPREAD_CONFIG = {
    # Confidence range: (min_spread_bps, max_spread_bps)
    "tier_a": {"min": 5, "max": 20},      # Real-time: tight spreads
    "tier_b": {"min": 10, "max": 30},     # Delayed: slightly wider
    "tier_c": {"min": 20, "max": 50},     # Portfolio: wider
    "tier_d": {"min": 30, "max": 75},     # Cache: conservative
    "tier_e": {"min": 50, "max": 100},    # Guardrail: very wide
}

# Instrument-specific overrides
INSTRUMENT_SPREAD_OVERRIDES = {
    # Highly liquid ETFs - tighter spreads OK
    "us_index_etf": {"multiplier": 0.5},
    "msci_world": {"multiplier": 0.5},

    # European ETFs - slightly wider due to currency/liquidity
    "eu_stoxx50": {"multiplier": 1.2},
    "eu_stoxx600": {"multiplier": 1.2},
    "eu_bank_etf": {"multiplier": 1.5},

    # Options - much wider spreads
    "vix_call": {"multiplier": 3.0, "min_spread": 100},
    "vstoxx_call": {"multiplier": 3.0, "min_spread": 100},
    "sx5e_put": {"multiplier": 2.5, "min_spread": 75},
    "eu_bank_put": {"multiplier": 3.0, "min_spread": 100},
    "hyg_put": {"multiplier": 2.5, "min_spread": 75},

    # Commodities
    "gold_etf": {"multiplier": 0.8},
    "commodity_broad": {"multiplier": 1.0},
}


class LimitOrderGenerator:
    """
    Generates limit order prices based on reference price confidence.

    Strategies:
    - High confidence (0.9+): Tight spreads, aggressive pricing
    - Medium confidence (0.7-0.9): Moderate spreads
    - Low confidence (<0.7): Wide safety margins
    """

    def __init__(
        self,
        spread_config: Optional[Dict[str, Dict[str, int]]] = None,
        instrument_overrides: Optional[Dict[str, Dict[str, float]]] = None,
        max_spread_bps: int = 200,  # Hard cap at 2%
    ):
        """
        Initialize limit order generator.

        Args:
            spread_config: Spread configuration by tier
            instrument_overrides: Per-instrument spread adjustments
            max_spread_bps: Maximum spread in basis points
        """
        self.spread_config = spread_config or DEFAULT_SPREAD_CONFIG
        self.instrument_overrides = instrument_overrides or INSTRUMENT_SPREAD_OVERRIDES
        self.max_spread_bps = max_spread_bps

        # Metrics
        self._orders_generated = 0
        self._total_spread_bps = 0.0

    def generate(
        self,
        instrument_id: str,
        side: OrderSide,
        quantity: float,
        price_result: Any,  # PriceResult from ReferencePriceResolver
        adjustment: PriceAdjustment = PriceAdjustment.MIDPOINT,
    ) -> Optional[LimitOrderSpec]:
        """
        Generate a limit order specification.

        Args:
            instrument_id: Instrument identifier
            side: BUY or SELL
            quantity: Order quantity
            price_result: PriceResult with price, tier, confidence
            adjustment: Pricing strategy

        Returns:
            LimitOrderSpec or None if price_result invalid
        """
        if price_result is None:
            logger.warning(f"Cannot generate limit for {instrument_id}: no price result")
            return None

        # Extract price info
        if hasattr(price_result, 'price'):
            ref_price = price_result.price
            confidence = getattr(price_result, 'confidence', 0.5)
            tier = getattr(price_result, 'tier', 'unknown')
            source = getattr(price_result, 'source', 'unknown')
            bid = getattr(price_result, 'bid', None)
            ask = getattr(price_result, 'ask', None)
        elif isinstance(price_result, dict):
            ref_price = price_result.get('price')
            confidence = price_result.get('confidence', 0.5)
            tier = price_result.get('tier', 'unknown')
            source = price_result.get('source', 'unknown')
            bid = price_result.get('bid')
            ask = price_result.get('ask')
        else:
            logger.warning(f"Invalid price_result type: {type(price_result)}")
            return None

        if ref_price is None or ref_price <= 0:
            logger.warning(f"Invalid reference price for {instrument_id}: {ref_price}")
            return None

        # Convert enums to strings
        if hasattr(tier, 'value'):
            tier = tier.value
        if hasattr(source, 'value'):
            source = source.value

        # Calculate spread based on tier and confidence
        spread_bps = self._calculate_spread(
            instrument_id=instrument_id,
            tier=tier,
            confidence=confidence,
            adjustment=adjustment,
        )

        # Generate limit price
        limit_price = self._calculate_limit_price(
            side=side,
            ref_price=ref_price,
            spread_bps=spread_bps,
            bid=bid,
            ask=ask,
            adjustment=adjustment,
        )

        # Round to appropriate tick size
        limit_price = self._round_price(limit_price, instrument_id)

        # Track metrics
        self._orders_generated += 1
        self._total_spread_bps += spread_bps

        logger.info(
            f"Generated {side.value} limit for {instrument_id}: "
            f"ref={ref_price:.4f} → limit={limit_price:.4f} "
            f"(spread={spread_bps:.0f}bps, conf={confidence:.2f}, tier={tier})"
        )

        return LimitOrderSpec(
            instrument_id=instrument_id,
            side=side,
            quantity=quantity,
            limit_price=limit_price,
            reference_price=ref_price,
            confidence=confidence,
            adjustment=adjustment,
            spread_bps=spread_bps,
            price_tier=tier,
            price_source=source,
            generated_at=datetime.now(),
        )

    def _calculate_spread(
        self,
        instrument_id: str,
        tier: str,
        confidence: float,
        adjustment: PriceAdjustment,
    ) -> float:
        """Calculate spread in basis points."""

        # Get base spread for tier
        tier_key = f"tier_{tier.lower()}" if tier else "tier_c"
        config = self.spread_config.get(tier_key, self.spread_config["tier_c"])

        min_spread = config["min"]
        max_spread = config["max"]

        # Interpolate based on confidence (higher confidence = tighter spread)
        # confidence 1.0 → min_spread, confidence 0.0 → max_spread
        base_spread = max_spread - (confidence * (max_spread - min_spread))

        # Apply adjustment strategy
        if adjustment == PriceAdjustment.AGGRESSIVE:
            base_spread *= 0.5  # Halve the spread
        elif adjustment == PriceAdjustment.PASSIVE:
            base_spread *= 1.5  # 50% wider
        elif adjustment == PriceAdjustment.GUARDRAIL:
            base_spread *= 2.0  # Double for maximum safety

        # Apply instrument-specific multiplier
        if instrument_id in self.instrument_overrides:
            override = self.instrument_overrides[instrument_id]
            base_spread *= override.get("multiplier", 1.0)
            min_instrument_spread = override.get("min_spread", 0)
            base_spread = max(base_spread, min_instrument_spread)

        # Cap at maximum
        return min(base_spread, self.max_spread_bps)

    def _calculate_limit_price(
        self,
        side: OrderSide,
        ref_price: float,
        spread_bps: float,
        bid: Optional[float],
        ask: Optional[float],
        adjustment: PriceAdjustment,
    ) -> float:
        """Calculate limit price from reference and spread."""

        spread_pct = spread_bps / 10000.0  # Convert bps to decimal

        # If we have bid/ask and want aggressive pricing, use them
        if adjustment == PriceAdjustment.AGGRESSIVE:
            if side == OrderSide.BUY and ask and ask > 0:
                # Buy at ask (cross spread to get filled)
                return ask
            elif side == OrderSide.SELL and bid and bid > 0:
                # Sell at bid
                return bid

        # Standard spread-based calculation
        if side == OrderSide.BUY:
            # For buys: limit = ref * (1 + spread) to allow some upside
            return ref_price * (1 + spread_pct)
        else:
            # For sells: limit = ref * (1 - spread) to allow some downside
            return ref_price * (1 - spread_pct)

    def _round_price(self, price: float, instrument_id: str) -> float:
        """Round price to appropriate tick size."""

        # Default tick sizes based on price level
        if price >= 100:
            tick = 0.01  # $0.01 for higher-priced
        elif price >= 10:
            tick = 0.01  # $0.01 standard
        elif price >= 1:
            tick = 0.001  # $0.001 for sub-$10
        else:
            tick = 0.0001  # $0.0001 for penny stocks

        # Options often have specific tick sizes
        if any(x in instrument_id for x in ['_call', '_put', 'vix', 'vstoxx']):
            tick = 0.05  # $0.05 for options

        return round(price / tick) * tick

    def generate_from_side_string(
        self,
        instrument_id: str,
        side: str,  # "BUY" or "SELL"
        quantity: float,
        price_result: Any,
        adjustment: PriceAdjustment = PriceAdjustment.MIDPOINT,
    ) -> Optional[LimitOrderSpec]:
        """Convenience method accepting side as string."""
        order_side = OrderSide.BUY if side.upper() == "BUY" else OrderSide.SELL
        return self.generate(instrument_id, order_side, quantity, price_result, adjustment)

    def get_metrics(self) -> Dict[str, Any]:
        """Get generator metrics."""
        avg_spread = (
            self._total_spread_bps / self._orders_generated
            if self._orders_generated > 0
            else 0.0
        )
        return {
            "orders_generated": self._orders_generated,
            "avg_spread_bps": round(avg_spread, 2),
        }


def create_limit_from_price(
    instrument_id: str,
    side: str,
    quantity: float,
    price: float,
    confidence: float = 0.5,
    tier: str = "c",
) -> Optional[LimitOrderSpec]:
    """
    Convenience function to create a limit order from raw price data.

    Args:
        instrument_id: Instrument identifier
        side: "BUY" or "SELL"
        quantity: Order quantity
        price: Reference price
        confidence: Price confidence (0-1)
        tier: Price tier (a-e)

    Returns:
        LimitOrderSpec or None
    """
    generator = LimitOrderGenerator()
    price_result = {
        "price": price,
        "confidence": confidence,
        "tier": tier,
        "source": "manual",
    }
    order_side = OrderSide.BUY if side.upper() == "BUY" else OrderSide.SELL
    return generator.generate(instrument_id, order_side, quantity, price_result)
