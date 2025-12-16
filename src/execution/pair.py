"""
Pair Executor - Legging protection for paired trades.

Manages coordinated execution of paired/multi-leg trades to minimize
exposure from unbalanced fills. Implements:
- Concurrent submission of paired legs
- Legging detection and monitoring
- Temporary hedging when one leg runs ahead
- Undo capability for severe legging
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any
import logging

from .types import (
    OrderIntent,
    OrderTicket,
    PairGroup,
    Urgency,
    OrderStatus,
)
from .order_manager import OrderManager


logger = logging.getLogger(__name__)


@dataclass
class LeggingState:
    """Current legging state for a pair group."""
    pair_name: str
    is_legged: bool
    max_fill_pct: float
    min_fill_pct: float
    fill_imbalance: float           # Difference between max and min fill pct
    elapsed_seconds: float
    action_required: str = "none"   # none, hedge, undo, wait
    leading_leg: Optional[str] = None
    lagging_leg: Optional[str] = None


@dataclass
class PairExecutionResult:
    """Result of pair execution."""
    pair_name: str
    success: bool
    all_filled: bool
    hedge_deployed: bool
    hedge_filled: bool
    undone: bool
    legs: List[OrderTicket]
    hedge_ticket: Optional[OrderTicket]
    elapsed_seconds: float
    error: Optional[str] = None


class PairExecutor:
    """
    Executes paired trades with legging protection.

    Legging protection options:
    A. Temporary hedge: Place hedge in liquid proxy when one leg leads
    B. Undo: Reduce filled leg toward neutral if other leg can't fill
    C. Aggressive reprice: Widen limit on unfilled leg

    Typically uses A + C together.
    """

    # Hedge instruments for different pair types
    HEDGE_PROXIES = {
        # For US/EU equity pairs, use index futures
        "us_eu_equity": {"long": "ES", "short": "FESX"},
        # For FX exposure from EU shorts
        "eur_fx": {"hedge": "M6E"},
        "gbp_fx": {"hedge": "M6B"},
    }

    def __init__(
        self,
        order_manager: OrderManager,
        max_legging_seconds: int = 60,
        hedge_trigger_fill_pct: float = 0.30,
        enable_hedge: bool = True,
        enable_undo: bool = False,
        enable_aggressive_reprice: bool = True,
    ):
        """
        Initialize PairExecutor.

        Args:
            order_manager: Order manager for execution
            max_legging_seconds: Max time before legging action
            hedge_trigger_fill_pct: Fill % that triggers hedge
            enable_hedge: Enable temporary hedging
            enable_undo: Enable undo of filled leg
            enable_aggressive_reprice: Enable aggressive repricing of lagging leg
        """
        self.order_manager = order_manager
        self.max_legging_seconds = max_legging_seconds
        self.hedge_trigger_fill_pct = hedge_trigger_fill_pct
        self.enable_hedge = enable_hedge
        self.enable_undo = enable_undo
        self.enable_aggressive_reprice = enable_aggressive_reprice

        # Active pair groups
        self.active_pairs: Dict[str, PairGroup] = {}

    def create_pair_group(
        self,
        name: str,
        intents: List[OrderIntent],
        hedge_intent: Optional[OrderIntent] = None,
        pair_type: str = "us_eu_equity",
    ) -> PairGroup:
        """
        Create a pair group for coordinated execution.

        Args:
            name: Unique name for this pair group
            intents: Order intents for the legs (usually 2)
            hedge_intent: Optional hedge instrument intent
            pair_type: Type of pair (for hedge proxy selection)

        Returns:
            PairGroup object
        """
        # Auto-create hedge intent if not provided and hedging enabled
        if hedge_intent is None and self.enable_hedge:
            hedge_intent = self._create_hedge_intent(intents, pair_type)

        pair_group = PairGroup(
            name=name,
            intents=intents,
            hedge_intent=hedge_intent,
            max_legging_seconds=self.max_legging_seconds,
            trigger_fill_pct=self.hedge_trigger_fill_pct,
        )

        return pair_group

    def execute_pair(
        self,
        pair_group: PairGroup,
    ) -> None:
        """
        Start executing a pair group.

        Submits all legs concurrently.

        Args:
            pair_group: Pair group to execute
        """
        pair_group.started_at = datetime.now()
        pair_group.tickets = []

        # Submit all legs concurrently
        for intent in pair_group.intents:
            # Get market data
            md = self.order_manager.transport.get_market_data(intent.instrument_id)
            if md is None:
                logger.error(f"No market data for {intent.instrument_id}")
                continue

            # Create plan
            plan, warning = self.order_manager.policy.create_plan(
                intent, md,
                asset_class=self._get_asset_class(intent.instrument_id),
            )

            if warning:
                logger.warning(f"Pair leg warning: {warning}")

            # Submit
            ticket = self.order_manager.submit(intent, plan, md)
            pair_group.tickets.append(ticket)

        # Register for monitoring
        self.active_pairs[pair_group.name] = pair_group

        logger.info(
            f"Pair execution started: {pair_group.name} "
            f"with {len(pair_group.tickets)} legs"
        )

    def check_legging(self, pair_group: PairGroup) -> LeggingState:
        """
        Check current legging state of a pair group.

        Args:
            pair_group: Pair group to check

        Returns:
            LeggingState with current status and required action
        """
        if len(pair_group.tickets) < 2:
            return LeggingState(
                pair_name=pair_group.name,
                is_legged=False,
                max_fill_pct=0,
                min_fill_pct=0,
                fill_imbalance=0,
                elapsed_seconds=pair_group.elapsed_seconds(),
            )

        # Get fill percentages
        fill_pcts = [(t.intent.instrument_id, t.fill_pct) for t in pair_group.tickets]
        max_fill = max(pct for _, pct in fill_pcts)
        min_fill = min(pct for _, pct in fill_pcts)
        imbalance = max_fill - min_fill

        # Identify leading and lagging legs
        leading_leg = None
        lagging_leg = None
        for inst, pct in fill_pcts:
            if pct == max_fill:
                leading_leg = inst
            if pct == min_fill:
                lagging_leg = inst

        # Determine if legged
        is_legged = (
            max_fill >= pair_group.trigger_fill_pct and
            min_fill < 0.1  # Significantly behind
        )

        # Determine action
        elapsed = pair_group.elapsed_seconds()
        action = "none"

        if is_legged:
            if elapsed >= pair_group.max_legging_seconds:
                if self.enable_hedge and pair_group.hedge_ticket is None:
                    action = "hedge"
                elif self.enable_undo:
                    action = "undo"
            elif self.enable_aggressive_reprice:
                action = "reprice"
            else:
                action = "wait"

        return LeggingState(
            pair_name=pair_group.name,
            is_legged=is_legged,
            max_fill_pct=max_fill,
            min_fill_pct=min_fill,
            fill_imbalance=imbalance,
            elapsed_seconds=elapsed,
            action_required=action,
            leading_leg=leading_leg,
            lagging_leg=lagging_leg,
        )

    def process_pairs(self) -> List[LeggingState]:
        """
        Process all active pair groups.

        Checks for legging and takes corrective action.

        Returns:
            List of legging states
        """
        states = []

        for pair_name, pair_group in list(self.active_pairs.items()):
            # Update ticket statuses
            for ticket in pair_group.tickets:
                self.order_manager.update(ticket)

            # Check completion
            if self._is_pair_complete(pair_group):
                logger.info(f"Pair complete: {pair_name}")
                del self.active_pairs[pair_name]
                continue

            # Check legging
            state = self.check_legging(pair_group)
            states.append(state)

            # Take action if needed
            if state.action_required == "hedge":
                self._deploy_hedge(pair_group, state)
            elif state.action_required == "reprice":
                self._aggressive_reprice(pair_group, state)
            elif state.action_required == "undo":
                self._undo_leading_leg(pair_group, state)

        return states

    def _deploy_hedge(
        self,
        pair_group: PairGroup,
        state: LeggingState,
    ) -> None:
        """Deploy temporary hedge for legged pair."""
        if pair_group.hedge_intent is None:
            logger.warning(f"No hedge intent for {pair_group.name}")
            return

        if pair_group.hedge_ticket is not None:
            return  # Already hedged

        logger.info(
            f"Deploying hedge for {pair_group.name}: "
            f"leading {state.leading_leg} @ {state.max_fill_pct:.1%}, "
            f"lagging {state.lagging_leg} @ {state.min_fill_pct:.1%}"
        )

        # Calculate hedge size based on imbalance
        # Find the leading ticket
        leading_ticket = None
        for t in pair_group.tickets:
            if t.intent.instrument_id == state.leading_leg:
                leading_ticket = t
                break

        if leading_ticket is None:
            return

        # Hedge should offset the excess exposure from leading leg
        hedge_qty = int(leading_ticket.filled_qty * 0.5)  # Simplified

        if hedge_qty <= 0:
            return

        # Create hedge intent
        hedge_intent = OrderIntent(
            instrument_id=pair_group.hedge_intent.instrument_id,
            side=pair_group.hedge_intent.side,
            quantity=hedge_qty,
            reason="pair_hedge",
            sleeve="hedge",
            urgency=Urgency.HIGH,
        )

        # Get market data
        md = self.order_manager.transport.get_market_data(hedge_intent.instrument_id)
        if md is None:
            logger.error(f"No market data for hedge {hedge_intent.instrument_id}")
            return

        # Create and submit hedge order
        plan, _ = self.order_manager.policy.create_plan(
            hedge_intent, md,
            asset_class="FUT",
        )

        pair_group.hedge_ticket = self.order_manager.submit(hedge_intent, plan, md)

    def _aggressive_reprice(
        self,
        pair_group: PairGroup,
        state: LeggingState,
    ) -> None:
        """Aggressively reprice lagging leg."""
        # Find lagging ticket
        lagging_ticket = None
        for t in pair_group.tickets:
            if t.intent.instrument_id == state.lagging_leg:
                lagging_ticket = t
                break

        if lagging_ticket is None:
            return

        if lagging_ticket.is_terminal:
            return

        # Force a reprice by resetting replace timer
        # The order manager will handle the actual reprice on next process
        if lagging_ticket.replace_count < lagging_ticket.plan.max_replace_attempts:
            logger.info(
                f"Triggering aggressive reprice for {state.lagging_leg} "
                f"in pair {pair_group.name}"
            )
            # Reset last replace time to trigger immediate reprice
            lagging_ticket.last_replace_at = None

    def _undo_leading_leg(
        self,
        pair_group: PairGroup,
        state: LeggingState,
    ) -> None:
        """Undo (reverse) the leading leg's fills."""
        # Find leading ticket
        leading_ticket = None
        for t in pair_group.tickets:
            if t.intent.instrument_id == state.leading_leg:
                leading_ticket = t
                break

        if leading_ticket is None:
            return

        filled_qty = leading_ticket.filled_qty
        if filled_qty == 0:
            return

        logger.warning(
            f"Undoing leading leg {state.leading_leg} "
            f"in pair {pair_group.name}: qty={filled_qty}"
        )

        # Create reverse order
        reverse_side = "SELL" if leading_ticket.intent.side == "BUY" else "BUY"
        undo_intent = OrderIntent(
            instrument_id=leading_ticket.intent.instrument_id,
            side=reverse_side,
            quantity=filled_qty,
            reason="pair_undo",
            sleeve=leading_ticket.intent.sleeve,
            urgency=Urgency.HIGH,
        )

        # Get market data
        md = self.order_manager.transport.get_market_data(undo_intent.instrument_id)
        if md is None:
            return

        # Submit undo order
        plan, _ = self.order_manager.policy.create_plan(undo_intent, md)
        self.order_manager.submit(undo_intent, plan, md)

        # Cancel original orders
        for ticket in pair_group.tickets:
            if not ticket.is_terminal:
                self.order_manager.cancel(ticket, "pair_undo")

    def _is_pair_complete(self, pair_group: PairGroup) -> bool:
        """Check if all legs of pair are complete."""
        if not pair_group.tickets:
            return False

        for ticket in pair_group.tickets:
            if not ticket.is_terminal:
                return False

        # If hedge was deployed, check if it's complete too
        if pair_group.hedge_ticket is not None:
            if not pair_group.hedge_ticket.is_terminal:
                return False

        return True

    def _create_hedge_intent(
        self,
        intents: List[OrderIntent],
        pair_type: str,
    ) -> Optional[OrderIntent]:
        """Auto-create hedge intent based on pair type."""
        proxy = self.HEDGE_PROXIES.get(pair_type)
        if proxy is None:
            return None

        # Determine hedge direction based on pair composition
        # This is simplified - real logic would consider exposures
        hedge_instrument = proxy.get("hedge") or proxy.get("short")
        if hedge_instrument is None:
            return None

        # Placeholder hedge - actual size determined at execution
        return OrderIntent(
            instrument_id=hedge_instrument,
            side="SELL",  # Usually shorting the hedge
            quantity=1,   # Placeholder
            reason="legging_hedge",
            sleeve="hedge",
            urgency=Urgency.HIGH,
        )

    def _get_asset_class(self, instrument_id: str) -> str:
        """Get asset class for instrument (simplified)."""
        if instrument_id in ("ES", "FESX", "M6E", "M6B"):
            return "FUT"
        elif instrument_id.startswith("M6"):
            return "FX_FUT"
        else:
            return "ETF"

    def get_pair_result(self, pair_name: str) -> Optional[PairExecutionResult]:
        """Get result for a completed pair."""
        pair_group = self.active_pairs.get(pair_name)
        if pair_group is None:
            return None

        all_filled = all(
            t.status == OrderStatus.FILLED
            for t in pair_group.tickets
        )

        hedge_deployed = pair_group.hedge_ticket is not None
        hedge_filled = (
            hedge_deployed and
            pair_group.hedge_ticket.status == OrderStatus.FILLED
        )

        return PairExecutionResult(
            pair_name=pair_name,
            success=all_filled,
            all_filled=all_filled,
            hedge_deployed=hedge_deployed,
            hedge_filled=hedge_filled,
            undone=False,  # Would track this if undo was used
            legs=pair_group.tickets,
            hedge_ticket=pair_group.hedge_ticket,
            elapsed_seconds=pair_group.elapsed_seconds(),
        )
