"""
Execution Policy - Order type selection and parameterization.

Converts high-level OrderIntents into concrete OrderPlans based on:
- Market conditions (liquidity, spread, session timing)
- Instrument characteristics (asset class, ADV)
- Order urgency and size
- Configuration constraints (collars, no market orders, etc.)
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Any, Optional, Tuple
from enum import Enum

from .types import (
    MarketDataSnapshot,
    OrderIntent,
    OrderPlan,
    OrderType,
    TimeInForce,
    Urgency,
)


class PolicyMode(Enum):
    """Execution policy modes."""
    MARKETABLE_LIMIT = "marketable_limit"
    AUCTION_OPEN = "auction_open"
    AUCTION_CLOSE = "auction_close"
    ADAPTIVE = "adaptive"
    VWAP = "vwap"
    TWAP = "twap"


@dataclass
class ExecutionConfig:
    """Configuration for execution policy."""
    # Policy selection
    default_policy: PolicyMode = PolicyMode.MARKETABLE_LIMIT
    allow_market_orders: bool = False

    # Timeouts
    order_ttl_seconds: int = 120
    replace_interval_seconds: int = 15
    max_replace_attempts: int = 6

    # Slippage / collars (bps)
    # 25bps default ensures fills while still providing flash-crash protection
    default_max_slippage_bps: float = 25.0
    max_slippage_bps_by_asset_class: Dict[str, float] = None

    # Turnover control
    min_trade_notional_usd: float = 2500.0
    rebalance_drift_threshold_pct: float = 0.02

    # Pair execution
    pair_max_legging_seconds: int = 60
    pair_hedge_enabled: bool = True
    pair_min_hedge_trigger_fill_pct: float = 0.30

    # Slicing thresholds
    adv_fraction_threshold: float = 0.01
    max_participation_rate: float = 0.10
    slice_interval_seconds: int = 20

    # Session controls
    avoid_first_minutes_after_open: int = 15
    avoid_last_minutes_before_close: int = 10

    # Data freshness
    max_data_age_seconds: int = 30

    def __post_init__(self):
        if self.max_slippage_bps_by_asset_class is None:
            # 25bps for liquid ETFs/stocks ensures fills with flash-crash protection
            # Tighter for futures (more liquid, lower spreads)
            self.max_slippage_bps_by_asset_class = {
                "ETF": 25.0,
                "STK": 30.0,
                "FUT": 5.0,
                "FX_FUT": 3.0,
            }
        if isinstance(self.default_policy, str):
            self.default_policy = PolicyMode(self.default_policy)


class ExecutionPolicy:
    """
    Generates OrderPlans from OrderIntents.

    Key responsibilities:
    1. Choose appropriate order type based on conditions
    2. Calculate limit prices with slippage collars
    3. Determine whether to slice large orders
    4. Enforce safety constraints (no market orders, freshness, etc.)
    """

    def __init__(self, config: ExecutionConfig):
        self.config = config

    def create_plan(
        self,
        intent: OrderIntent,
        md: MarketDataSnapshot,
        asset_class: str = "ETF",
        session_phase: str = "regular",  # "pre_open", "open_auction", "regular", "close_auction", "post_close"
        adv: Optional[int] = None,
    ) -> Tuple[OrderPlan, Optional[str]]:
        """
        Generate an execution plan for an order intent.

        Args:
            intent: What we want to trade
            md: Current market data
            asset_class: Asset class for slippage limits
            session_phase: Current market session phase
            adv: Average daily volume (for slicing decisions)

        Returns:
            Tuple of (OrderPlan, warning_message or None)

        Raises:
            ValueError: If execution is not safe (missing data, bad conditions)
        """
        warnings = []

        # Validate market data freshness
        if not md.is_fresh(self.config.max_data_age_seconds):
            raise ValueError(f"Market data too stale for {intent.instrument_id}")

        # Get reference price
        ref_price = md.reference_price
        if ref_price is None:
            raise ValueError(f"No reference price available for {intent.instrument_id}")

        # Get max slippage for asset class
        max_slip_bps = self.config.max_slippage_bps_by_asset_class.get(
            asset_class, self.config.default_max_slippage_bps
        )

        # Determine policy mode based on conditions
        policy_mode = self._select_policy_mode(
            intent, md, session_phase, adv, asset_class
        )

        # Check if we need to slice this order
        should_slice = self._should_slice(intent, md, adv)
        if should_slice:
            warnings.append(f"Order exceeds ADV threshold, will use slicing")
            policy_mode = PolicyMode.ADAPTIVE  # Use algo for large orders

        # Generate plan based on policy mode
        if policy_mode == PolicyMode.AUCTION_CLOSE:
            plan = self._create_auction_close_plan(intent, md, ref_price, max_slip_bps)
        elif policy_mode == PolicyMode.AUCTION_OPEN:
            plan = self._create_auction_open_plan(intent, md, ref_price, max_slip_bps)
        elif policy_mode in (PolicyMode.VWAP, PolicyMode.TWAP, PolicyMode.ADAPTIVE):
            plan = self._create_algo_plan(intent, md, ref_price, max_slip_bps, policy_mode)
        else:
            plan = self._create_marketable_limit_plan(intent, md, ref_price, max_slip_bps)

        # Add session-based warnings
        if session_phase == "regular":
            minutes_since_open = self._estimate_minutes_since_open(md.ts)
            if minutes_since_open is not None and minutes_since_open < self.config.avoid_first_minutes_after_open:
                warnings.append(f"Near market open ({minutes_since_open}m) - wider spreads likely")

        warning_msg = "; ".join(warnings) if warnings else None
        return plan, warning_msg

    def _select_policy_mode(
        self,
        intent: OrderIntent,
        md: MarketDataSnapshot,
        session_phase: str,
        adv: Optional[int],
        asset_class: str,
    ) -> PolicyMode:
        """Select the best policy mode for current conditions."""
        # Crisis orders always use aggressive execution
        if intent.urgency == Urgency.CRISIS:
            return PolicyMode.MARKETABLE_LIMIT

        # Use auctions when appropriate
        if session_phase == "close_auction" and self.config.default_policy == PolicyMode.AUCTION_CLOSE:
            return PolicyMode.AUCTION_CLOSE
        if session_phase == "open_auction" and self.config.default_policy == PolicyMode.AUCTION_OPEN:
            return PolicyMode.AUCTION_OPEN

        # Large orders use algos
        if adv and intent.notional_usd:
            estimated_shares = intent.quantity
            if md.reference_price:
                order_value = estimated_shares * md.reference_price
                if order_value > adv * md.reference_price * self.config.adv_fraction_threshold:
                    return PolicyMode.ADAPTIVE

        return self.config.default_policy

    def _should_slice(
        self,
        intent: OrderIntent,
        md: MarketDataSnapshot,
        adv: Optional[int],
    ) -> bool:
        """Determine if order should be sliced."""
        if adv is None or adv == 0:
            return False

        # Compare order size to ADV
        if intent.quantity > adv * self.config.adv_fraction_threshold:
            return True

        return False

    def _create_marketable_limit_plan(
        self,
        intent: OrderIntent,
        md: MarketDataSnapshot,
        ref_price: float,
        max_slip_bps: float,
    ) -> OrderPlan:
        """Create a marketable limit order plan."""
        limit_price = self._marketable_limit_price(md, intent.side, max_slip_bps)
        ceiling, floor = self._calculate_collar(ref_price, intent.side, max_slip_bps)

        # Adjust TIF based on urgency
        if intent.urgency == Urgency.CRISIS:
            tif = TimeInForce.IOC
            ttl = 30
        elif intent.urgency == Urgency.HIGH:
            tif = TimeInForce.DAY
            ttl = 60
        else:
            tif = TimeInForce.DAY
            ttl = self.config.order_ttl_seconds

        return OrderPlan(
            order_type=OrderType.LMT,
            limit_price=limit_price,
            tif=tif,
            max_slippage_bps=max_slip_bps,
            ttl_seconds=ttl,
            replace_interval_seconds=self.config.replace_interval_seconds,
            max_replace_attempts=self.config.max_replace_attempts,
            price_ceiling=ceiling,
            price_floor=floor,
        )

    def _create_auction_close_plan(
        self,
        intent: OrderIntent,
        md: MarketDataSnapshot,
        ref_price: float,
        max_slip_bps: float,
    ) -> OrderPlan:
        """Create a limit-on-close order plan."""
        # LOC with collar protection
        limit_price = self._marketable_limit_price(md, intent.side, max_slip_bps)
        ceiling, floor = self._calculate_collar(ref_price, intent.side, max_slip_bps)

        return OrderPlan(
            order_type=OrderType.LOC if not self.config.allow_market_orders else OrderType.MOC,
            limit_price=limit_price if not self.config.allow_market_orders else None,
            tif=TimeInForce.CLS,
            max_slippage_bps=max_slip_bps,
            ttl_seconds=0,  # Auction orders expire at close
            replace_interval_seconds=0,
            max_replace_attempts=0,
            price_ceiling=ceiling,
            price_floor=floor,
        )

    def _create_auction_open_plan(
        self,
        intent: OrderIntent,
        md: MarketDataSnapshot,
        ref_price: float,
        max_slip_bps: float,
    ) -> OrderPlan:
        """Create a limit-on-open order plan."""
        limit_price = self._marketable_limit_price(md, intent.side, max_slip_bps)
        ceiling, floor = self._calculate_collar(ref_price, intent.side, max_slip_bps)

        return OrderPlan(
            order_type=OrderType.LOO if not self.config.allow_market_orders else OrderType.MOO,
            limit_price=limit_price if not self.config.allow_market_orders else None,
            tif=TimeInForce.OPG,
            max_slippage_bps=max_slip_bps,
            ttl_seconds=0,
            replace_interval_seconds=0,
            max_replace_attempts=0,
            price_ceiling=ceiling,
            price_floor=floor,
        )

    def _create_algo_plan(
        self,
        intent: OrderIntent,
        md: MarketDataSnapshot,
        ref_price: float,
        max_slip_bps: float,
        policy_mode: PolicyMode,
    ) -> OrderPlan:
        """Create an algorithmic order plan (VWAP, TWAP, Adaptive)."""
        ceiling, floor = self._calculate_collar(ref_price, intent.side, max_slip_bps)

        # Map policy mode to IBKR algo name
        algo_name = {
            PolicyMode.VWAP: "Vwap",
            PolicyMode.TWAP: "Twap",
            PolicyMode.ADAPTIVE: "Adaptive",
        }.get(policy_mode, "Adaptive")

        # Algo parameters
        algo_params = {
            "maxPctVol": self.config.max_participation_rate,
        }

        if algo_name == "Adaptive":
            # Adaptive algo parameters
            if intent.urgency == Urgency.HIGH:
                algo_params["adaptivePriority"] = "Urgent"
            elif intent.urgency == Urgency.LOW:
                algo_params["adaptivePriority"] = "Patient"
            else:
                algo_params["adaptivePriority"] = "Normal"

        return OrderPlan(
            order_type=OrderType.ALGO,
            limit_price=ceiling if intent.side == "BUY" else floor,  # Collar as limit
            tif=TimeInForce.DAY,
            algo=algo_name,
            algo_params=algo_params,
            max_slippage_bps=max_slip_bps,
            ttl_seconds=self.config.order_ttl_seconds * 2,  # Algos need more time
            replace_interval_seconds=0,  # Algos self-manage
            max_replace_attempts=0,
            price_ceiling=ceiling,
            price_floor=floor,
        )

    def _round_to_tick(self, price: float, tick_size: float = 0.01) -> float:
        """
        Round price to valid tick size.

        IBKR requires prices to conform to minimum price variation.
        Most stocks/ETFs use $0.01 tick size.
        """
        if tick_size <= 0:
            tick_size = 0.01
        return round(price / tick_size) * tick_size

    def _marketable_limit_price(
        self,
        md: MarketDataSnapshot,
        side: str,
        max_slip_bps: float,
    ) -> float:
        """
        Calculate marketable limit price.

        Goal: Cross the spread but cap worst-case fill.
        Returns price rounded to proper tick size.
        """
        ref = md.reference_price
        if ref is None:
            raise ValueError("No reference price for limit calculation")

        max_slip = max_slip_bps / 10000.0

        if md.has_quotes():
            # When bid/ask present, bias toward crossing
            spread = md.spread or 0
            micro_buffer = spread * 0.25  # 25% of spread buffer

            if side == "BUY":
                # Pay up to ask + buffer, but cap at ref*(1+max_slip)
                aggressive_price = md.ask + micro_buffer
                collar_price = ref * (1.0 + max_slip)
                return self._round_to_tick(min(aggressive_price, collar_price))
            else:
                # Accept down to bid - buffer, but floor at ref*(1-max_slip)
                aggressive_price = md.bid - micro_buffer
                collar_price = ref * (1.0 - max_slip)
                return self._round_to_tick(max(aggressive_price, collar_price))
        else:
            # No quotes available - be MORE aggressive to ensure fills
            # Without quotes, we don't know the actual spread, so assume it could be wide
            # Use 2x the normal slippage to account for unknown spread
            # This is safer than not filling at all
            aggressive_slip = max_slip * 2.0

            if side == "BUY":
                # For buys, pay up to ref + 2x slippage to cross unknown spread
                return self._round_to_tick(ref * (1.0 + aggressive_slip))
            else:
                # For sells, accept down to ref - 2x slippage
                return self._round_to_tick(ref * (1.0 - aggressive_slip))

    def _calculate_collar(
        self,
        ref_price: float,
        side: str,
        max_slip_bps: float,
    ) -> Tuple[Optional[float], Optional[float]]:
        """Calculate price collar (ceiling for buys, floor for sells)."""
        max_slip = max_slip_bps / 10000.0

        if side == "BUY":
            ceiling = self._round_to_tick(ref_price * (1.0 + max_slip))
            return (ceiling, None)
        else:
            floor = self._round_to_tick(ref_price * (1.0 - max_slip))
            return (None, floor)

    def _estimate_minutes_since_open(self, ts: datetime) -> Optional[int]:
        """Rough estimate of minutes since market open."""
        # This is a simplified check - use calendars.py for proper logic
        if ts is None:
            return None
        hour = ts.hour
        minute = ts.minute
        # Assume US market opens at 9:30 ET (14:30 UTC)
        if hour < 14:
            return None
        if hour == 14 and minute < 30:
            return None
        return (hour - 14) * 60 + (minute - 30)

    def validate_order_safe(
        self,
        intent: OrderIntent,
        md: MarketDataSnapshot,
        session_phase: str,
    ) -> Tuple[bool, str]:
        """
        Validate that order execution is safe.

        Returns:
            Tuple of (is_safe, reason)
        """
        # Check data freshness
        if not md.is_fresh(self.config.max_data_age_seconds):
            return False, f"Market data stale (>{self.config.max_data_age_seconds}s old)"

        # Check for reference price
        if md.reference_price is None:
            return False, "No reference price available"

        # Check session phase
        if session_phase in ("pre_open", "post_close"):
            if intent.urgency != Urgency.CRISIS:
                return False, f"Market not open (phase: {session_phase})"

        # Check minimum notional
        if intent.notional_usd and intent.notional_usd < self.config.min_trade_notional_usd:
            return False, f"Order below minimum notional (${intent.notional_usd:.0f} < ${self.config.min_trade_notional_usd:.0f})"

        return True, "OK"

    def update_limit_for_replace(
        self,
        current_plan: OrderPlan,
        md: MarketDataSnapshot,
        side: str,
        replace_count: int,
    ) -> Optional[float]:
        """
        Calculate new limit price for order replacement.

        Progressively becomes more aggressive while staying within collar.
        Returns price rounded to proper tick size.

        Returns:
            New limit price, or None if should not replace
        """
        if replace_count >= self.config.max_replace_attempts:
            return None

        ref = md.reference_price
        if ref is None:
            return None

        # Calculate aggression factor (increases with replace count)
        # Start at 50% of collar, increase by 10% each replace
        aggression = 0.5 + (replace_count * 0.1)
        aggression = min(aggression, 1.0)  # Cap at 100%

        max_slip = current_plan.max_slippage_bps / 10000.0

        if side == "BUY":
            # Move limit up toward ceiling
            base_price = md.ask if md.has_quotes() else ref
            collar_ceiling = current_plan.price_ceiling or ref * (1.0 + max_slip)
            new_price = base_price + (collar_ceiling - base_price) * aggression
            return self._round_to_tick(min(new_price, collar_ceiling))
        else:
            # Move limit down toward floor
            base_price = md.bid if md.has_quotes() else ref
            collar_floor = current_plan.price_floor or ref * (1.0 - max_slip)
            new_price = base_price - (base_price - collar_floor) * aggression
            return self._round_to_tick(max(new_price, collar_floor))


def load_execution_config(settings: Dict[str, Any]) -> ExecutionConfig:
    """Load ExecutionConfig from settings dict."""
    exec_settings = settings.get("execution", {})

    return ExecutionConfig(
        default_policy=PolicyMode(exec_settings.get("default_policy", "marketable_limit")),
        allow_market_orders=exec_settings.get("allow_market_orders", False),
        order_ttl_seconds=exec_settings.get("order_ttl_seconds", 120),
        replace_interval_seconds=exec_settings.get("replace_interval_seconds", 15),
        max_replace_attempts=exec_settings.get("max_replace_attempts", 6),
        default_max_slippage_bps=exec_settings.get("default_max_slippage_bps", 10.0),
        max_slippage_bps_by_asset_class=exec_settings.get("max_slippage_bps_by_asset_class"),
        min_trade_notional_usd=exec_settings.get("min_trade_notional_usd", 2500.0),
        rebalance_drift_threshold_pct=exec_settings.get("rebalance_drift_threshold_pct", 0.02),
        pair_max_legging_seconds=exec_settings.get("pair_max_legging_seconds", 60),
        pair_hedge_enabled=exec_settings.get("pair_hedge_enabled", True),
        pair_min_hedge_trigger_fill_pct=exec_settings.get("pair_min_hedge_trigger_fill_pct", 0.30),
        adv_fraction_threshold=exec_settings.get("adv_fraction_threshold", 0.01),
        max_participation_rate=exec_settings.get("max_participation_rate", 0.10),
        slice_interval_seconds=exec_settings.get("slice_interval_seconds", 20),
        avoid_first_minutes_after_open=exec_settings.get("avoid_first_minutes_after_open", 15),
        avoid_last_minutes_before_close=exec_settings.get("avoid_last_minutes_before_close", 10),
        max_data_age_seconds=exec_settings.get("max_data_age_seconds", 30),
    )
