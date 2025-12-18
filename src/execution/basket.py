"""
Basket Executor - Trade netting and coordinated basket execution.

Provides:
- Trade netting across sleeves
- Priority ordering (liquid first)
- Minimum notional filtering
- Exposure constraint checks
- Coordinated execution of multiple orders
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any
import logging

from .types import (
    OrderIntent,
    OrderTicket,
    ExecutionResult,
    Urgency,
)
from .policy import ExecutionPolicy, ExecutionConfig


logger = logging.getLogger(__name__)


@dataclass
class InstrumentSpec:
    """Instrument specification for execution."""
    instrument_id: str
    asset_class: str         # ETF, STK, FUT, FX_FUT
    currency: str
    multiplier: float = 1.0
    exchange: str = "US"
    liquidity_tier: int = 1  # 1=most liquid, 3=least liquid
    avg_daily_volume: Optional[int] = None


@dataclass
class NetPosition:
    """Net position delta after aggregation."""
    instrument_id: str
    net_qty: int
    buy_qty: int              # Total buys before netting
    sell_qty: int             # Total sells before netting
    gross_qty: int            # Gross turnover before netting
    notional_usd: float
    contributing_sleeves: List[str]
    urgency: Urgency = Urgency.NORMAL

    @property
    def side(self) -> str:
        """Net trade side."""
        return "BUY" if self.net_qty > 0 else "SELL"

    @property
    def abs_qty(self) -> int:
        """Absolute quantity to trade."""
        return abs(self.net_qty)

    @property
    def netting_savings(self) -> int:
        """Quantity saved by netting."""
        return self.gross_qty - self.abs_qty


@dataclass
class BasketPlan:
    """Planned basket of trades."""
    intents: List[OrderIntent]
    total_notional_usd: float
    instruments_count: int
    netting_savings_qty: int
    netting_savings_pct: float
    filtered_count: int       # Orders filtered out (below threshold)
    created_at: datetime = field(default_factory=datetime.now)


class BasketExecutor:
    """
    Executes baskets of trades with netting and priority ordering.

    Key features:
    1. Nets trades across sleeves (reduces turnover)
    2. Filters sub-threshold trades
    3. Orders execution by liquidity (liquid first)
    4. Validates exposure constraints
    5. Coordinates multi-order execution
    """

    def __init__(
        self,
        config: ExecutionConfig,
        instruments: Dict[str, InstrumentSpec],
    ):
        """
        Initialize BasketExecutor.

        Args:
            config: Execution configuration
            instruments: Instrument specifications by ID
        """
        self.config = config
        self.instruments = instruments

    def net_trades(
        self,
        intents: List[OrderIntent],
        prices: Dict[str, float],
    ) -> List[NetPosition]:
        """
        Net trades across sleeves per instrument.

        Args:
            intents: Raw order intents from all sleeves
            prices: Current prices by instrument_id

        Returns:
            List of NetPosition objects
        """
        # Aggregate by instrument
        aggregated: Dict[str, Dict[str, Any]] = {}

        for intent in intents:
            inst_id = intent.instrument_id
            if inst_id not in aggregated:
                aggregated[inst_id] = {
                    "buy_qty": 0,
                    "sell_qty": 0,
                    "sleeves": [],
                    "urgency": Urgency.NORMAL,
                }

            if intent.side == "BUY":
                aggregated[inst_id]["buy_qty"] += intent.quantity
            else:
                aggregated[inst_id]["sell_qty"] += intent.quantity

            aggregated[inst_id]["sleeves"].append(intent.sleeve)

            # Urgency is max of all contributing intents
            if intent.urgency.value > aggregated[inst_id]["urgency"].value:
                aggregated[inst_id]["urgency"] = intent.urgency

        # Calculate net positions
        net_positions = []
        for inst_id, data in aggregated.items():
            buy_qty = data["buy_qty"]
            sell_qty = data["sell_qty"]
            net_qty = buy_qty - sell_qty
            gross_qty = buy_qty + sell_qty

            if net_qty == 0:
                continue  # Fully netted out

            price = prices.get(inst_id, 0)
            spec = self.instruments.get(inst_id)
            multiplier = spec.multiplier if spec else 1.0

            notional = abs(net_qty) * price * multiplier

            net_positions.append(NetPosition(
                instrument_id=inst_id,
                net_qty=net_qty,
                buy_qty=buy_qty,
                sell_qty=sell_qty,
                gross_qty=gross_qty,
                notional_usd=notional,
                contributing_sleeves=list(set(data["sleeves"])),
                urgency=data["urgency"],
            ))

        return net_positions

    def filter_by_threshold(
        self,
        net_positions: List[NetPosition],
    ) -> Tuple[List[NetPosition], List[NetPosition]]:
        """
        Filter out positions below minimum notional threshold.

        Args:
            net_positions: Net positions to filter

        Returns:
            Tuple of (positions_to_trade, filtered_out)
        """
        to_trade = []
        filtered = []

        for pos in net_positions:
            if pos.notional_usd >= self.config.min_trade_notional_usd:
                to_trade.append(pos)
            else:
                filtered.append(pos)
                logger.debug(
                    f"Filtered {pos.instrument_id}: "
                    f"${pos.notional_usd:.0f} < ${self.config.min_trade_notional_usd:.0f}"
                )

        return to_trade, filtered

    def order_by_priority(
        self,
        net_positions: List[NetPosition],
    ) -> List[NetPosition]:
        """
        Order positions by execution priority.

        Priority rules:
        1. Crisis urgency first
        2. Futures first (fast hedging)
        3. SELLS before BUYS (frees margin for subsequent buys)
        4. Then by liquidity tier
        5. Then by notional (largest first for efficient execution)

        Args:
            net_positions: Positions to order

        Returns:
            Ordered list of positions
        """
        def priority_key(pos: NetPosition) -> Tuple:
            spec = self.instruments.get(pos.instrument_id)

            # Urgency (lower = higher priority)
            urgency_score = {
                Urgency.CRISIS: 0,
                Urgency.HIGH: 1,
                Urgency.NORMAL: 2,
                Urgency.LOW: 3,
            }.get(pos.urgency, 2)

            # Asset class (futures first for hedging)
            asset_class = spec.asset_class if spec else "STK"
            asset_score = {
                "FUT": 0,
                "FX_FUT": 1,
                "ETF": 2,
                "STK": 3,
            }.get(asset_class, 3)

            # Side: SELL before BUY (frees up margin for subsequent buys)
            side_score = 0 if pos.side == "SELL" else 1

            # Liquidity tier
            liquidity_tier = spec.liquidity_tier if spec else 2

            # Notional (negative for descending order)
            notional_score = -pos.notional_usd

            return (urgency_score, asset_score, side_score, liquidity_tier, notional_score)

        return sorted(net_positions, key=priority_key)

    def create_basket_plan(
        self,
        intents: List[OrderIntent],
        prices: Dict[str, float],
    ) -> BasketPlan:
        """
        Create an execution plan for a basket of trades.

        Args:
            intents: Raw order intents from strategy
            prices: Current prices by instrument_id

        Returns:
            BasketPlan with netted and ordered intents
        """
        # Step 1: Net trades across sleeves
        net_positions = self.net_trades(intents, prices)

        # Calculate gross turnover before netting
        gross_notional = sum(p.gross_qty * prices.get(p.instrument_id, 0)
                           for p in net_positions)
        gross_qty = sum(p.gross_qty for p in net_positions)

        # Step 2: Filter by threshold
        to_trade, filtered = self.filter_by_threshold(net_positions)

        # Step 3: Order by priority
        ordered_positions = self.order_by_priority(to_trade)

        # Step 4: Convert back to OrderIntents
        final_intents = []
        for pos in ordered_positions:
            intent = OrderIntent(
                instrument_id=pos.instrument_id,
                side=pos.side,
                quantity=pos.abs_qty,
                reason="rebalance",
                sleeve=",".join(pos.contributing_sleeves),  # Combined sleeves
                urgency=pos.urgency,
                notional_usd=pos.notional_usd,
            )
            final_intents.append(intent)

        # Calculate netting savings
        net_qty = sum(p.abs_qty for p in ordered_positions)
        netting_savings_qty = gross_qty - net_qty
        netting_savings_pct = netting_savings_qty / gross_qty if gross_qty > 0 else 0

        total_notional = sum(p.notional_usd for p in ordered_positions)

        return BasketPlan(
            intents=final_intents,
            total_notional_usd=total_notional,
            instruments_count=len(final_intents),
            netting_savings_qty=netting_savings_qty,
            netting_savings_pct=netting_savings_pct,
            filtered_count=len(filtered),
        )

    def validate_basket(
        self,
        plan: BasketPlan,
        current_nav: float,
        current_gross_exposure: float,
        current_net_exposure: float,
        max_gross_exposure: float,
        max_net_exposure: float,
    ) -> Tuple[bool, List[str]]:
        """
        Validate basket against exposure and turnover constraints.

        Args:
            plan: Basket plan to validate
            current_nav: Current NAV
            current_gross_exposure: Current gross exposure
            current_net_exposure: Current net exposure
            max_gross_exposure: Maximum allowed gross exposure
            max_net_exposure: Maximum allowed net exposure

        Returns:
            Tuple of (is_valid, list_of_warnings)
        """
        warnings = []
        is_valid = True

        # Check daily turnover limit
        max_turnover = current_nav * self.config.rebalance_drift_threshold_pct * 10  # ~20% max
        if plan.total_notional_usd > max_turnover:
            warnings.append(
                f"High turnover: ${plan.total_notional_usd:,.0f} "
                f"(limit ${max_turnover:,.0f})"
            )

        # Estimate post-trade exposures (simplified)
        # In reality, need to compute properly with long/short breakdown
        estimated_gross = current_gross_exposure + plan.total_notional_usd * 0.5
        estimated_net = current_net_exposure  # Net effect depends on directions

        if estimated_gross > max_gross_exposure:
            warnings.append(
                f"Would exceed gross exposure: {estimated_gross/current_nav:.1%} "
                f"> {max_gross_exposure/current_nav:.1%}"
            )
            is_valid = False

        # Check max single order
        max_single_order = current_nav * self.config.rebalance_drift_threshold_pct * 5  # ~10% NAV
        for intent in plan.intents:
            if intent.notional_usd and intent.notional_usd > max_single_order:
                warnings.append(
                    f"Large order: {intent.instrument_id} "
                    f"${intent.notional_usd:,.0f} > ${max_single_order:,.0f}"
                )

        return is_valid, warnings

    def split_into_phases(
        self,
        intents: List[OrderIntent],
    ) -> Dict[str, List[OrderIntent]]:
        """
        Split intents into execution phases.

        Phases:
        - hedge: Futures and FX hedges (execute first)
        - liquid: High liquidity ETFs
        - illiquid: Single stocks and less liquid instruments

        Args:
            intents: Ordered intents from basket plan

        Returns:
            Dict mapping phase name to intents
        """
        phases = {
            "hedge": [],
            "liquid": [],
            "illiquid": [],
        }

        for intent in intents:
            spec = self.instruments.get(intent.instrument_id)
            if spec is None:
                phases["illiquid"].append(intent)
                continue

            if spec.asset_class in ("FUT", "FX_FUT"):
                phases["hedge"].append(intent)
            elif spec.liquidity_tier == 1:
                phases["liquid"].append(intent)
            else:
                phases["illiquid"].append(intent)

        return phases


def calculate_netting_benefit(
    intents: List[OrderIntent],
) -> Dict[str, Any]:
    """
    Calculate the benefit of trade netting (for reporting).

    Args:
        intents: Raw intents before netting

    Returns:
        Dict with netting statistics
    """
    # Group by instrument
    by_instrument: Dict[str, Dict[str, int]] = {}
    for intent in intents:
        inst = intent.instrument_id
        if inst not in by_instrument:
            by_instrument[inst] = {"buy": 0, "sell": 0}

        if intent.side == "BUY":
            by_instrument[inst]["buy"] += intent.quantity
        else:
            by_instrument[inst]["sell"] += intent.quantity

    # Calculate stats
    gross_trades = 0
    net_trades = 0
    fully_netted = 0

    for inst, data in by_instrument.items():
        gross = data["buy"] + data["sell"]
        net = abs(data["buy"] - data["sell"])

        gross_trades += gross
        net_trades += net

        if net == 0:
            fully_netted += 1

    return {
        "instruments_with_trades": len(by_instrument),
        "gross_quantity": gross_trades,
        "net_quantity": net_trades,
        "quantity_saved": gross_trades - net_trades,
        "savings_pct": (gross_trades - net_trades) / gross_trades if gross_trades > 0 else 0,
        "fully_netted_instruments": fully_netted,
    }
