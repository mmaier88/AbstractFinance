"""
Order Manager - State machine for order lifecycle management.

Handles:
- Order submission and tracking
- Poll-based status updates
- Cancel/replace logic with TTL
- Partial fill management
- Execution metrics collection
"""

import uuid
from datetime import datetime
from typing import Dict, List, Optional, Callable, Any, Tuple
from dataclasses import dataclass, field
import logging

from .types import (
    MarketDataSnapshot,
    OrderIntent,
    OrderPlan,
    OrderTicket,
    OrderStatus,
    ExecutionResult,
)
from .policy import ExecutionPolicy


logger = logging.getLogger(__name__)


@dataclass
class OrderUpdate:
    """Update received from broker."""
    broker_order_id: int
    status: str
    filled_qty: int
    remaining_qty: int
    avg_fill_price: Optional[float]
    last_fill_price: Optional[float]
    last_fill_qty: Optional[int]
    commission: float
    error_message: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.now)


class BrokerTransport:
    """
    Abstract interface for broker communication.

    Implement this for your specific broker (IBKR, etc.)
    """

    def submit_order(
        self,
        instrument_id: str,
        side: str,
        quantity: int,
        order_type: str,
        limit_price: Optional[float],
        tif: str,
        algo: Optional[str] = None,
        algo_params: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Submit order to broker. Returns broker_order_id."""
        raise NotImplementedError

    def cancel_order(self, broker_order_id: int) -> bool:
        """Cancel an order. Returns success."""
        raise NotImplementedError

    def modify_order(
        self,
        broker_order_id: int,
        new_limit_price: float,
    ) -> Tuple[bool, Optional[int]]:
        """
        Modify order limit price (cancel/replace).

        Returns:
            Tuple of (success, new_broker_order_id)
            new_broker_order_id is the ID of the replacement order, or None if failed
        """
        raise NotImplementedError

    def get_order_status(self, broker_order_id: int) -> Optional[OrderUpdate]:
        """Get current order status from broker."""
        raise NotImplementedError

    def get_market_data(self, instrument_id: str) -> Optional[MarketDataSnapshot]:
        """Get current market data for instrument."""
        raise NotImplementedError


class OrderManager:
    """
    Manages order lifecycle with state machine transitions.

    Key behaviors:
    1. Submit orders via broker transport
    2. Poll for status updates
    3. Cancel/replace unfilled orders at intervals
    4. Expire orders after TTL
    5. Track fill metrics for analytics
    """

    def __init__(
        self,
        transport: BrokerTransport,
        policy: ExecutionPolicy,
        on_fill: Optional[Callable[[OrderTicket], None]] = None,
        on_complete: Optional[Callable[[ExecutionResult], None]] = None,
    ):
        """
        Initialize OrderManager.

        Args:
            transport: Broker communication interface
            policy: Execution policy for order parameterization
            on_fill: Callback when order receives a fill
            on_complete: Callback when order reaches terminal state
        """
        self.transport = transport
        self.policy = policy
        self.on_fill = on_fill
        self.on_complete = on_complete

        # Active tickets by internal ticket_id
        self.active_tickets: Dict[str, OrderTicket] = {}

        # Mapping from broker_order_id to ticket_id
        self.broker_to_ticket: Dict[int, str] = {}

        # Completed tickets (for analytics)
        self.completed_tickets: List[OrderTicket] = []

    def submit(
        self,
        intent: OrderIntent,
        plan: OrderPlan,
        market_data: Optional[MarketDataSnapshot] = None,
    ) -> OrderTicket:
        """
        Submit a new order.

        Args:
            intent: What we want to trade
            plan: How to execute it
            market_data: Current market data for arrival price

        Returns:
            OrderTicket tracking the order
        """
        ticket_id = str(uuid.uuid4())[:8]

        # Create ticket
        ticket = OrderTicket(
            intent=intent,
            plan=plan,
            ticket_id=ticket_id,
            created_at=datetime.now(),
            remaining_qty=intent.quantity,
        )

        # Record arrival price for slippage calculation
        if market_data:
            ticket.arrival_price = market_data.reference_price
            ticket.arrival_mid = market_data.mid

        # Submit to broker
        try:
            broker_order_id = self.transport.submit_order(
                instrument_id=intent.instrument_id,
                side=intent.side,
                quantity=intent.quantity,
                order_type=plan.order_type.value,
                limit_price=plan.limit_price,
                tif=plan.tif.value,
                algo=plan.algo,
                algo_params=plan.algo_params,
            )

            ticket.broker_order_id = broker_order_id
            ticket.submitted_at = datetime.now()
            ticket.status = OrderStatus.SUBMITTED

            # Track the ticket
            self.active_tickets[ticket_id] = ticket
            self.broker_to_ticket[broker_order_id] = ticket_id

            logger.info(
                f"Order submitted: {ticket_id} -> broker:{broker_order_id} "
                f"{intent.side} {intent.quantity} {intent.instrument_id} "
                f"@ {plan.limit_price}"
            )

        except Exception as e:
            ticket.status = OrderStatus.REJECTED
            ticket.last_error = str(e)
            ticket.completed_at = datetime.now()
            logger.error(f"Order submission failed: {ticket_id} - {e}")

            if self.on_complete:
                self.on_complete(ExecutionResult.from_ticket(ticket))

        return ticket

    def update(self, ticket: OrderTicket) -> OrderTicket:
        """
        Update ticket status from broker.

        Args:
            ticket: Ticket to update

        Returns:
            Updated ticket
        """
        if ticket.broker_order_id is None:
            return ticket

        update = self.transport.get_order_status(ticket.broker_order_id)
        if update is None:
            return ticket

        old_status = ticket.status
        old_filled = ticket.filled_qty

        # Update fill info
        ticket.filled_qty = update.filled_qty
        ticket.remaining_qty = update.remaining_qty
        ticket.avg_fill_price = update.avg_fill_price
        ticket.total_commission = update.commission
        ticket.broker_status = update.status

        if update.error_message:
            ticket.last_error = update.error_message

        # Map broker status to our status
        ticket.status = self._map_broker_status(update.status, ticket)

        # Trigger fill callback if new fill
        if ticket.filled_qty > old_filled:
            logger.info(
                f"Fill: {ticket.ticket_id} filled {ticket.filled_qty}/{ticket.intent.quantity} "
                f"@ {ticket.avg_fill_price}"
            )
            if self.on_fill:
                self.on_fill(ticket)

        # Check for terminal state
        if ticket.is_terminal and old_status != ticket.status:
            ticket.completed_at = datetime.now()
            self._handle_completion(ticket)

        return ticket

    def process_all(self) -> List[OrderTicket]:
        """
        Process all active orders.

        Polls status, handles timeouts, triggers replaces.

        Returns:
            List of updated tickets
        """
        updated = []

        for ticket_id, ticket in list(self.active_tickets.items()):
            # Update from broker
            ticket = self.update(ticket)

            # Check if we need to take action
            if ticket.is_active:
                action_taken = self._check_order_actions(ticket)
                if action_taken:
                    # Re-update after action
                    ticket = self.update(ticket)

            updated.append(ticket)

            # Remove from active if terminal
            if ticket.is_terminal:
                del self.active_tickets[ticket_id]
                if ticket.broker_order_id in self.broker_to_ticket:
                    del self.broker_to_ticket[ticket.broker_order_id]

        return updated

    def cancel(self, ticket: OrderTicket, reason: str = "manual") -> bool:
        """
        Cancel an order.

        Args:
            ticket: Ticket to cancel
            reason: Reason for cancellation

        Returns:
            True if cancel request sent successfully
        """
        if ticket.broker_order_id is None:
            return False

        if ticket.is_terminal:
            return False

        logger.info(f"Cancelling order {ticket.ticket_id}: {reason}")

        ticket.status = OrderStatus.PENDING_CANCEL
        ticket.cancel_attempts += 1

        success = self.transport.cancel_order(ticket.broker_order_id)
        if not success:
            logger.warning(f"Cancel request failed for {ticket.ticket_id}")

        return success

    def cancel_all(self, reason: str = "cancel_all") -> int:
        """Cancel all active orders. Returns count cancelled."""
        count = 0
        for ticket in list(self.active_tickets.values()):
            if self.cancel(ticket, reason):
                count += 1
        return count

    def _check_order_actions(self, ticket: OrderTicket) -> bool:
        """
        Check if order needs action (replace or cancel).

        Returns:
            True if action was taken
        """
        elapsed = ticket.elapsed_seconds()
        plan = ticket.plan

        # Check TTL expiry
        if plan.ttl_seconds > 0 and elapsed >= plan.ttl_seconds:
            if ticket.filled_qty > 0:
                # Partial fill - cancel remaining
                self.cancel(ticket, "ttl_expired_partial")
            else:
                # No fills - cancel
                self.cancel(ticket, "ttl_expired")
            return True

        # Check if we should replace
        if plan.replace_interval_seconds > 0 and plan.max_replace_attempts > 0:
            time_since_last = elapsed
            if ticket.last_replace_at:
                time_since_last = (datetime.now() - ticket.last_replace_at).total_seconds()

            if time_since_last >= plan.replace_interval_seconds:
                if ticket.replace_count < plan.max_replace_attempts:
                    return self._try_replace(ticket)

        return False

    def _try_replace(self, ticket: OrderTicket) -> bool:
        """
        Attempt to replace order with updated limit.

        Returns:
            True if replace was attempted
        """
        # Get current market data
        md = self.transport.get_market_data(ticket.intent.instrument_id)
        if md is None:
            logger.warning(f"No market data for replace: {ticket.ticket_id}")
            return False

        # Calculate new limit price
        new_limit = self.policy.update_limit_for_replace(
            ticket.plan,
            md,
            ticket.intent.side,
            ticket.replace_count,
        )

        if new_limit is None:
            # Max replaces reached or can't calculate new price
            self.cancel(ticket, "max_replaces_reached")
            return True

        # Check collar bounds
        if ticket.intent.side == "BUY":
            if ticket.plan.price_ceiling and new_limit > ticket.plan.price_ceiling:
                new_limit = ticket.plan.price_ceiling
        else:
            if ticket.plan.price_floor and new_limit < ticket.plan.price_floor:
                new_limit = ticket.plan.price_floor

        logger.info(
            f"Replacing order {ticket.ticket_id}: "
            f"{ticket.plan.limit_price} -> {new_limit}"
        )

        old_broker_id = ticket.broker_order_id
        ticket.status = OrderStatus.PENDING_REPLACE
        ticket.replace_count += 1
        ticket.last_replace_at = datetime.now()

        success, new_broker_id = self.transport.modify_order(
            ticket.broker_order_id,
            new_limit,
        )

        if success and new_broker_id is not None:
            # Update ticket with new broker order ID
            ticket.broker_order_id = new_broker_id
            ticket.plan.limit_price = new_limit
            ticket.status = OrderStatus.SUBMITTED

            # Update our mappings
            if old_broker_id in self.broker_to_ticket:
                del self.broker_to_ticket[old_broker_id]
            self.broker_to_ticket[new_broker_id] = ticket.ticket_id

            logger.info(
                f"Order {ticket.ticket_id} replaced: "
                f"broker_id {old_broker_id} -> {new_broker_id}"
            )
        else:
            logger.warning(f"Replace failed for {ticket.ticket_id}")
            ticket.status = OrderStatus.SUBMITTED  # Revert status

        return True

    def _map_broker_status(self, broker_status: str, ticket: OrderTicket) -> OrderStatus:
        """Map broker status string to OrderStatus enum."""
        status_upper = broker_status.upper()

        if status_upper in ("FILLED", "COMPLETED"):
            return OrderStatus.FILLED
        elif status_upper in ("CANCELLED", "CANCELED"):
            return OrderStatus.CANCELLED
        elif status_upper in ("REJECTED", "ERROR"):
            return OrderStatus.REJECTED
        elif status_upper in ("EXPIRED",):
            return OrderStatus.EXPIRED
        elif status_upper in ("PARTIALLY_FILLED", "PARTIAL"):
            return OrderStatus.PARTIAL
        elif status_upper in ("PENDING_CANCEL",):
            return OrderStatus.PENDING_CANCEL
        elif status_upper in ("SUBMITTED", "ACTIVE", "WORKING"):
            if ticket.filled_qty > 0:
                return OrderStatus.PARTIAL
            return OrderStatus.SUBMITTED
        else:
            # Unknown - keep current
            return ticket.status

    def _handle_completion(self, ticket: OrderTicket) -> None:
        """Handle order completion."""
        self.completed_tickets.append(ticket)

        result = ExecutionResult.from_ticket(ticket)

        logger.info(
            f"Order complete: {ticket.ticket_id} "
            f"status={ticket.status.value} "
            f"filled={ticket.filled_qty}/{ticket.intent.quantity} "
            f"slippage={result.slippage_bps:.1f}bps" if result.slippage_bps else ""
        )

        if self.on_complete:
            self.on_complete(result)

    def get_active_tickets(self) -> List[OrderTicket]:
        """Get list of active tickets."""
        return list(self.active_tickets.values())

    def get_ticket(self, ticket_id: str) -> Optional[OrderTicket]:
        """Get ticket by ID."""
        return self.active_tickets.get(ticket_id)

    def get_ticket_by_broker_id(self, broker_order_id: int) -> Optional[OrderTicket]:
        """Get ticket by broker order ID."""
        ticket_id = self.broker_to_ticket.get(broker_order_id)
        if ticket_id:
            return self.active_tickets.get(ticket_id)
        return None

    def get_execution_summary(self) -> Dict[str, Any]:
        """Get summary of completed executions."""
        if not self.completed_tickets:
            return {
                "total_orders": 0,
                "filled": 0,
                "cancelled": 0,
                "rejected": 0,
                "avg_slippage_bps": 0.0,
                "total_commission": 0.0,
            }

        filled = [t for t in self.completed_tickets if t.status == OrderStatus.FILLED]
        cancelled = [t for t in self.completed_tickets if t.status == OrderStatus.CANCELLED]
        rejected = [t for t in self.completed_tickets if t.status == OrderStatus.REJECTED]

        slippages = [t.slippage_bps for t in filled if t.slippage_bps is not None]
        avg_slippage = sum(slippages) / len(slippages) if slippages else 0.0

        total_commission = sum(t.total_commission for t in self.completed_tickets)

        return {
            "total_orders": len(self.completed_tickets),
            "filled": len(filled),
            "cancelled": len(cancelled),
            "rejected": len(rejected),
            "avg_slippage_bps": avg_slippage,
            "total_commission": total_commission,
            "partial_fills": len([t for t in cancelled if t.filled_qty > 0]),
        }

    def clear_completed(self) -> None:
        """Clear completed tickets (for daily reset)."""
        self.completed_tickets.clear()
