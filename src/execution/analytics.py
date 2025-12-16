"""
Execution Analytics - Metrics recording and reporting.

Provides:
- Per-order execution metrics
- Daily aggregation
- Prometheus metric hooks
- Telegram summary generation
"""

from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Dict, List, Optional, Any
import logging

from .types import OrderTicket, ExecutionResult, OrderStatus
from .slippage import SlippageTracker, compute_slippage_bps


logger = logging.getLogger(__name__)


@dataclass
class OrderMetrics:
    """Metrics for a single order."""
    ticket_id: str
    instrument_id: str
    side: str
    quantity: int
    filled_qty: int
    arrival_price: Optional[float]
    avg_fill_price: Optional[float]
    slippage_bps: Optional[float]
    notional_usd: float
    commission: float
    elapsed_seconds: float
    replace_count: int
    status: str
    timestamp: datetime


@dataclass
class DailyMetrics:
    """Aggregated daily execution metrics."""
    date: date
    total_orders: int = 0
    filled_orders: int = 0
    partial_fills: int = 0
    cancelled_orders: int = 0
    rejected_orders: int = 0

    total_traded_notional: float = 0.0
    total_commission: float = 0.0
    total_slippage_cost: float = 0.0

    avg_slippage_bps: float = 0.0
    max_slippage_bps: float = 0.0
    min_slippage_bps: float = 0.0

    avg_fill_time_seconds: float = 0.0
    avg_replace_count: float = 0.0

    netting_savings_qty: int = 0
    netting_savings_pct: float = 0.0

    # Worst execution
    worst_slippage_instrument: Optional[str] = None
    worst_slippage_bps: Optional[float] = None
    worst_slippage_qty: Optional[int] = None

    # By asset class
    by_asset_class: Dict[str, Dict[str, float]] = field(default_factory=dict)


class ExecutionAnalytics:
    """
    Records and analyzes execution metrics.

    Integrates with:
    - Prometheus for real-time metrics
    - Daily reporting for Telegram/email
    - Historical analysis
    """

    def __init__(self):
        self.order_metrics: List[OrderMetrics] = []
        self.slippage_tracker = SlippageTracker()
        self.daily_metrics: Dict[date, DailyMetrics] = {}

        # Running totals for current day
        self._current_date: Optional[date] = None
        self._running_metrics: Optional[DailyMetrics] = None

    def record_order_complete(
        self,
        result: ExecutionResult,
        asset_class: str = "ETF",
    ) -> OrderMetrics:
        """
        Record metrics for a completed order.

        Args:
            result: Execution result
            asset_class: Asset class of instrument

        Returns:
            OrderMetrics record
        """
        ticket = result.ticket

        # Calculate notional
        notional = 0.0
        if ticket.avg_fill_price and ticket.filled_qty:
            notional = ticket.avg_fill_price * ticket.filled_qty

        metrics = OrderMetrics(
            ticket_id=ticket.ticket_id,
            instrument_id=ticket.intent.instrument_id,
            side=ticket.intent.side,
            quantity=ticket.intent.quantity,
            filled_qty=ticket.filled_qty,
            arrival_price=ticket.arrival_price,
            avg_fill_price=ticket.avg_fill_price,
            slippage_bps=result.slippage_bps,
            notional_usd=notional,
            commission=result.commission,
            elapsed_seconds=result.elapsed_seconds,
            replace_count=result.replace_count,
            status=ticket.status.value,
            timestamp=datetime.now(),
        )

        self.order_metrics.append(metrics)

        # Update slippage tracker
        if ticket.arrival_price and ticket.avg_fill_price:
            self.slippage_tracker.record(
                instrument_id=ticket.intent.instrument_id,
                side=ticket.intent.side,
                quantity=ticket.filled_qty,
                arrival_price=ticket.arrival_price,
                fill_price=ticket.avg_fill_price,
                order_type=ticket.plan.order_type.value,
                replace_count=ticket.replace_count,
            )

        # Update running daily metrics
        self._update_running_metrics(metrics, asset_class)

        # Emit Prometheus metrics
        self._emit_prometheus_metrics(metrics, asset_class)

        return metrics

    def get_daily_metrics(self, for_date: Optional[date] = None) -> DailyMetrics:
        """Get daily metrics for a specific date."""
        target_date = for_date or date.today()

        if target_date in self.daily_metrics:
            return self.daily_metrics[target_date]

        if self._current_date == target_date and self._running_metrics:
            return self._running_metrics

        # No metrics for this date
        return DailyMetrics(date=target_date)

    def finalize_day(self, for_date: Optional[date] = None) -> DailyMetrics:
        """
        Finalize metrics for a day.

        Called at end of trading day to compute final aggregates.

        Returns:
            Finalized DailyMetrics
        """
        target_date = for_date or date.today()

        if self._running_metrics and self._current_date == target_date:
            # Calculate final averages
            metrics = self._running_metrics

            # Find worst slippage
            filled_orders = [
                m for m in self.order_metrics
                if m.timestamp.date() == target_date
                and m.status == "FILLED"
                and m.slippage_bps is not None
            ]

            if filled_orders:
                worst = max(filled_orders, key=lambda m: m.slippage_bps or 0)
                metrics.worst_slippage_instrument = worst.instrument_id
                metrics.worst_slippage_bps = worst.slippage_bps
                metrics.worst_slippage_qty = worst.filled_qty

            # Store finalized metrics
            self.daily_metrics[target_date] = metrics

            # Reset for next day
            self._running_metrics = None
            self._current_date = None

            return metrics

        return self.get_daily_metrics(target_date)

    def get_telegram_summary(self, for_date: Optional[date] = None) -> str:
        """
        Generate Telegram-formatted daily summary.

        Returns:
            Formatted summary string
        """
        metrics = self.get_daily_metrics(for_date)

        lines = [
            f"📊 *Execution Summary* - {metrics.date}",
            "",
            f"Orders: {metrics.total_orders} total, {metrics.filled_orders} filled",
            f"Traded: ${metrics.total_traded_notional:,.0f}",
            "",
            f"*Costs:*",
            f"  Slippage: {metrics.avg_slippage_bps:.1f} bps avg (${metrics.total_slippage_cost:,.0f})",
            f"  Commission: ${metrics.total_commission:,.0f}",
            "",
        ]

        if metrics.worst_slippage_instrument:
            lines.extend([
                f"*Worst execution:*",
                f"  {metrics.worst_slippage_instrument}: "
                f"{metrics.worst_slippage_bps:.1f} bps on {metrics.worst_slippage_qty} shares",
                "",
            ])

        if metrics.netting_savings_pct > 0:
            lines.append(
                f"Netting saved: {metrics.netting_savings_pct:.1%} of turnover"
            )

        if metrics.partial_fills > 0:
            lines.append(f"⚠️ Partial fills: {metrics.partial_fills}")

        if metrics.rejected_orders > 0:
            lines.append(f"❌ Rejected: {metrics.rejected_orders}")

        return "\n".join(lines)

    def get_prometheus_metrics(self) -> Dict[str, Any]:
        """Get metrics in Prometheus-compatible format."""
        metrics = self.get_daily_metrics()

        return {
            "execution_orders_total": metrics.total_orders,
            "execution_orders_filled": metrics.filled_orders,
            "execution_orders_rejected": metrics.rejected_orders,
            "execution_notional_total": metrics.total_traded_notional,
            "execution_slippage_bps_avg": metrics.avg_slippage_bps,
            "execution_slippage_bps_max": metrics.max_slippage_bps,
            "execution_commission_total": metrics.total_commission,
            "execution_fill_time_avg": metrics.avg_fill_time_seconds,
            "execution_replace_count_avg": metrics.avg_replace_count,
        }

    def _update_running_metrics(
        self,
        order_metrics: OrderMetrics,
        asset_class: str,
    ) -> None:
        """Update running daily metrics with new order."""
        today = date.today()

        # Initialize if needed
        if self._current_date != today or self._running_metrics is None:
            self._current_date = today
            self._running_metrics = DailyMetrics(date=today)

        m = self._running_metrics

        # Update counts
        m.total_orders += 1
        if order_metrics.status == "FILLED":
            m.filled_orders += 1
        elif order_metrics.status == "CANCELLED" and order_metrics.filled_qty > 0:
            m.partial_fills += 1
        elif order_metrics.status == "CANCELLED":
            m.cancelled_orders += 1
        elif order_metrics.status == "REJECTED":
            m.rejected_orders += 1

        # Update totals
        m.total_traded_notional += order_metrics.notional_usd
        m.total_commission += order_metrics.commission

        # Update slippage
        if order_metrics.slippage_bps is not None:
            slip = order_metrics.slippage_bps
            slip_cost = abs(slip / 10000.0 * order_metrics.notional_usd)
            m.total_slippage_cost += slip_cost

            # Update max/min
            if slip > m.max_slippage_bps:
                m.max_slippage_bps = slip
            if m.min_slippage_bps == 0 or slip < m.min_slippage_bps:
                m.min_slippage_bps = slip

        # Update averages (incremental)
        n = m.filled_orders
        if n > 0:
            # Running average for slippage
            filled_with_slip = [
                om for om in self.order_metrics
                if om.status == "FILLED"
                and om.slippage_bps is not None
                and om.timestamp.date() == today
            ]
            if filled_with_slip:
                m.avg_slippage_bps = sum(om.slippage_bps for om in filled_with_slip) / len(filled_with_slip)

            # Running average for fill time
            m.avg_fill_time_seconds = (
                (m.avg_fill_time_seconds * (n - 1) + order_metrics.elapsed_seconds) / n
            )

            # Running average for replace count
            m.avg_replace_count = (
                (m.avg_replace_count * (n - 1) + order_metrics.replace_count) / n
            )

        # Update by asset class
        if asset_class not in m.by_asset_class:
            m.by_asset_class[asset_class] = {
                "count": 0,
                "notional": 0.0,
                "avg_slippage_bps": 0.0,
            }

        ac = m.by_asset_class[asset_class]
        ac["count"] += 1
        ac["notional"] += order_metrics.notional_usd

    def _emit_prometheus_metrics(
        self,
        order_metrics: OrderMetrics,
        asset_class: str,
    ) -> None:
        """Emit Prometheus metrics (if available)."""
        try:
            # Import here to avoid circular deps and handle if not installed
            from ..metrics import (
                record_execution_fill,
                record_execution_commission,
                record_execution_rejected,
            )

            if order_metrics.status == "FILLED":
                record_execution_fill(
                    instrument=order_metrics.instrument_id,
                    side=order_metrics.side,
                    quantity=order_metrics.filled_qty,
                    price=order_metrics.avg_fill_price or 0,
                    slippage_bps=order_metrics.slippage_bps or 0,
                    asset_class=asset_class,
                    replace_count=order_metrics.replace_count,
                )
                if order_metrics.commission > 0:
                    record_execution_commission(order_metrics.commission)

            elif order_metrics.status == "REJECTED":
                record_execution_rejected(
                    instrument=order_metrics.instrument_id,
                    reason="rejected",
                )

        except ImportError:
            pass  # Prometheus not available
        except Exception as e:
            logger.warning(f"Failed to emit Prometheus metrics: {e}")

    def clear_day(self, for_date: Optional[date] = None) -> None:
        """Clear metrics for a specific day."""
        target_date = for_date or date.today()

        self.order_metrics = [
            m for m in self.order_metrics
            if m.timestamp.date() != target_date
        ]

        if target_date in self.daily_metrics:
            del self.daily_metrics[target_date]

        if self._current_date == target_date:
            self._running_metrics = None
            self._current_date = None

    def get_historical_summary(
        self,
        days: int = 30,
    ) -> Dict[str, Any]:
        """Get summary over historical period."""
        end_date = date.today()
        start_date = date(end_date.year, end_date.month, end_date.day)

        relevant_metrics = [
            self.daily_metrics[d]
            for d in self.daily_metrics
            if d >= start_date
        ]

        if not relevant_metrics:
            return {
                "days": 0,
                "avg_daily_orders": 0,
                "avg_daily_notional": 0,
                "avg_slippage_bps": 0,
                "total_cost": 0,
            }

        return {
            "days": len(relevant_metrics),
            "avg_daily_orders": sum(m.total_orders for m in relevant_metrics) / len(relevant_metrics),
            "avg_daily_notional": sum(m.total_traded_notional for m in relevant_metrics) / len(relevant_metrics),
            "avg_slippage_bps": sum(m.avg_slippage_bps for m in relevant_metrics) / len(relevant_metrics),
            "total_cost": sum(m.total_slippage_cost + m.total_commission for m in relevant_metrics),
        }


# Singleton instance for global access
_analytics_instance: Optional[ExecutionAnalytics] = None


def get_execution_analytics() -> ExecutionAnalytics:
    """Get singleton ExecutionAnalytics instance."""
    global _analytics_instance
    if _analytics_instance is None:
        _analytics_instance = ExecutionAnalytics()
    return _analytics_instance
