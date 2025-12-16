"""
Unit tests for the execution package.

Tests cover:
- ExecutionPolicy: marketable limit pricing, collars
- OrderManager: state machine, TTL, cancel/replace
- BasketExecutor: trade netting, priority ordering
- PairExecutor: legging detection and protection
- Slippage: calculation and tracking
"""

import sys
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta
from typing import Dict, Any, Optional

import pytest

# Add src to path
sys.path.insert(0, str(__file__).rsplit("/", 2)[0] + "/src")

from execution.types import (
    MarketDataSnapshot,
    OrderIntent,
    OrderPlan,
    OrderTicket,
    OrderStatus,
    OrderType,
    TimeInForce,
    Urgency,
    PairGroup,
)
from execution.policy import ExecutionPolicy, ExecutionConfig, PolicyMode
from execution.order_manager import OrderManager, BrokerTransport, OrderUpdate
from execution.basket import BasketExecutor, InstrumentSpec, calculate_netting_benefit
from execution.slippage import (
    compute_slippage_bps,
    estimate_fixed_slippage,
    estimate_spread_slippage,
    CollarEnforcer,
    SlippageTracker,
)
from execution.calendars import (
    MarketCalendar,
    SessionPhase,
    is_market_open,
    get_session_phase,
    should_avoid_trading,
)


# =============================================================================
# Test Fixtures
# =============================================================================

@pytest.fixture
def market_data():
    """Sample market data snapshot."""
    return MarketDataSnapshot(
        symbol="CSPX",
        ts=datetime.now(),
        last=500.0,
        bid=499.90,
        ask=500.10,
        close=499.50,
    )


@pytest.fixture
def execution_config():
    """Standard execution config."""
    return ExecutionConfig(
        default_policy=PolicyMode.MARKETABLE_LIMIT,
        allow_market_orders=False,
        order_ttl_seconds=120,
        replace_interval_seconds=15,
        max_replace_attempts=6,
        default_max_slippage_bps=10.0,
    )


@pytest.fixture
def order_intent():
    """Sample order intent."""
    return OrderIntent(
        instrument_id="CSPX",
        side="BUY",
        quantity=100,
        reason="rebalance",
        sleeve="core_index_rv",
        urgency=Urgency.NORMAL,
        notional_usd=50000.0,
    )


class MockBrokerTransport(BrokerTransport):
    """Mock broker for testing."""

    def __init__(self):
        self.orders: Dict[int, Dict[str, Any]] = {}
        self.next_order_id = 1
        self.market_data: Dict[str, MarketDataSnapshot] = {}

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
        order_id = self.next_order_id
        self.next_order_id += 1
        self.orders[order_id] = {
            "instrument_id": instrument_id,
            "side": side,
            "quantity": quantity,
            "order_type": order_type,
            "limit_price": limit_price,
            "status": "SUBMITTED",
            "filled_qty": 0,
            "remaining_qty": quantity,
            "avg_fill_price": None,
        }
        return order_id

    def cancel_order(self, broker_order_id: int) -> bool:
        if broker_order_id in self.orders:
            self.orders[broker_order_id]["status"] = "CANCELLED"
            return True
        return False

    def modify_order(self, broker_order_id: int, new_limit_price: float) -> bool:
        if broker_order_id in self.orders:
            self.orders[broker_order_id]["limit_price"] = new_limit_price
            return True
        return False

    def get_order_status(self, broker_order_id: int) -> Optional[OrderUpdate]:
        if broker_order_id not in self.orders:
            return None
        order = self.orders[broker_order_id]
        return OrderUpdate(
            broker_order_id=broker_order_id,
            status=order["status"],
            filled_qty=order["filled_qty"],
            remaining_qty=order["remaining_qty"],
            avg_fill_price=order["avg_fill_price"],
            last_fill_price=None,
            last_fill_qty=None,
            commission=0.0,
        )

    def get_market_data(self, instrument_id: str) -> Optional[MarketDataSnapshot]:
        return self.market_data.get(instrument_id)

    def simulate_fill(self, broker_order_id: int, fill_qty: int, fill_price: float):
        """Simulate a fill for testing."""
        if broker_order_id in self.orders:
            order = self.orders[broker_order_id]
            order["filled_qty"] += fill_qty
            order["remaining_qty"] -= fill_qty
            order["avg_fill_price"] = fill_price
            if order["remaining_qty"] == 0:
                order["status"] = "FILLED"
            else:
                order["status"] = "PARTIAL"


# =============================================================================
# ExecutionPolicy Tests
# =============================================================================

class TestMarketableLimit:
    """Tests for marketable limit pricing."""

    def test_marketable_limit_buy_with_quotes(self, execution_config, market_data):
        """BUY should be priced to cross the spread within collar."""
        policy = ExecutionPolicy(execution_config)
        intent = OrderIntent(
            instrument_id="CSPX",
            side="BUY",
            quantity=100,
            reason="rebalance",
            sleeve="core",
            notional_usd=50000.0,
        )

        plan, warning = policy.create_plan(intent, market_data)

        # Should have a limit price
        assert plan.limit_price is not None
        # Should be above ask (crossing the spread)
        assert plan.limit_price >= market_data.ask
        # Should respect max slippage
        max_price = market_data.mid * (1 + execution_config.default_max_slippage_bps / 10000)
        assert plan.limit_price <= max_price

    def test_marketable_limit_sell_with_quotes(self, execution_config, market_data):
        """SELL should be priced below bid within collar."""
        policy = ExecutionPolicy(execution_config)
        intent = OrderIntent(
            instrument_id="CSPX",
            side="SELL",
            quantity=100,
            reason="rebalance",
            sleeve="core",
        )

        plan, warning = policy.create_plan(intent, market_data)

        # Should have a limit price
        assert plan.limit_price is not None
        # Should be below bid (crossing the spread)
        assert plan.limit_price <= market_data.bid
        # Should respect min collar
        min_price = market_data.mid * (1 - execution_config.default_max_slippage_bps / 10000)
        assert plan.limit_price >= min_price

    def test_marketable_limit_no_quotes(self, execution_config):
        """Without quotes, should use reference price with collar."""
        policy = ExecutionPolicy(execution_config)
        md = MarketDataSnapshot(
            symbol="CSPX",
            ts=datetime.now(),
            last=500.0,
            bid=None,
            ask=None,
            close=499.50,
        )
        intent = OrderIntent(
            instrument_id="CSPX",
            side="BUY",
            quantity=100,
            reason="rebalance",
            sleeve="core",
        )

        plan, warning = policy.create_plan(intent, md)

        # Should use last price with max slippage
        expected = 500.0 * (1 + execution_config.default_max_slippage_bps / 10000)
        assert abs(plan.limit_price - expected) < 0.01

    def test_collar_bounds_set(self, execution_config, market_data):
        """Should set collar bounds on plan."""
        policy = ExecutionPolicy(execution_config)
        intent = OrderIntent(
            instrument_id="CSPX",
            side="BUY",
            quantity=100,
            reason="rebalance",
            sleeve="core",
        )

        plan, _ = policy.create_plan(intent, market_data)

        # BUY should have ceiling, no floor
        assert plan.price_ceiling is not None
        assert plan.price_floor is None

    def test_no_market_orders(self, execution_config, market_data):
        """Should never produce market orders when disabled."""
        policy = ExecutionPolicy(execution_config)
        intent = OrderIntent(
            instrument_id="CSPX",
            side="BUY",
            quantity=100,
            reason="rebalance",
            sleeve="core",
        )

        plan, _ = policy.create_plan(intent, market_data)

        assert plan.order_type != OrderType.MKT

    def test_stale_data_rejected(self, execution_config):
        """Should reject orders with stale market data."""
        policy = ExecutionPolicy(execution_config)
        old_md = MarketDataSnapshot(
            symbol="CSPX",
            ts=datetime.now() - timedelta(minutes=5),  # 5 minutes old
            last=500.0,
            bid=499.90,
            ask=500.10,
        )
        intent = OrderIntent(
            instrument_id="CSPX",
            side="BUY",
            quantity=100,
            reason="rebalance",
            sleeve="core",
        )

        with pytest.raises(ValueError, match="stale"):
            policy.create_plan(intent, old_md)


class TestOrderManagerStateMachine:
    """Tests for OrderManager state transitions."""

    def test_order_submission(self, execution_config, market_data, order_intent):
        """Order should transition to SUBMITTED on success."""
        transport = MockBrokerTransport()
        transport.market_data["CSPX"] = market_data
        policy = ExecutionPolicy(execution_config)
        manager = OrderManager(transport, policy)

        plan = OrderPlan(
            order_type=OrderType.LMT,
            limit_price=500.50,
            tif=TimeInForce.DAY,
            max_slippage_bps=10.0,
            ttl_seconds=120,
        )

        ticket = manager.submit(order_intent, plan, market_data)

        assert ticket.status == OrderStatus.SUBMITTED
        assert ticket.broker_order_id is not None
        assert ticket.arrival_price == market_data.reference_price

    def test_order_fill_updates_status(self, execution_config, market_data, order_intent):
        """Full fill should transition to FILLED."""
        transport = MockBrokerTransport()
        transport.market_data["CSPX"] = market_data
        policy = ExecutionPolicy(execution_config)
        manager = OrderManager(transport, policy)

        plan = OrderPlan(
            order_type=OrderType.LMT,
            limit_price=500.50,
            tif=TimeInForce.DAY,
        )

        ticket = manager.submit(order_intent, plan, market_data)

        # Simulate fill
        transport.simulate_fill(ticket.broker_order_id, 100, 500.25)

        # Update status
        ticket = manager.update(ticket)

        assert ticket.status == OrderStatus.FILLED
        assert ticket.filled_qty == 100
        assert ticket.avg_fill_price == 500.25

    def test_partial_fill_status(self, execution_config, market_data, order_intent):
        """Partial fill should transition to PARTIAL."""
        transport = MockBrokerTransport()
        transport.market_data["CSPX"] = market_data
        policy = ExecutionPolicy(execution_config)
        manager = OrderManager(transport, policy)

        plan = OrderPlan(
            order_type=OrderType.LMT,
            limit_price=500.50,
            tif=TimeInForce.DAY,
        )

        ticket = manager.submit(order_intent, plan, market_data)

        # Simulate partial fill
        transport.simulate_fill(ticket.broker_order_id, 50, 500.25)

        ticket = manager.update(ticket)

        assert ticket.status == OrderStatus.PARTIAL
        assert ticket.filled_qty == 50
        assert ticket.remaining_qty == 50

    def test_ttl_expiry_cancels_order(self, execution_config, market_data, order_intent):
        """Order should be cancelled after TTL expires."""
        transport = MockBrokerTransport()
        transport.market_data["CSPX"] = market_data
        policy = ExecutionPolicy(execution_config)
        manager = OrderManager(transport, policy)

        plan = OrderPlan(
            order_type=OrderType.LMT,
            limit_price=500.50,
            tif=TimeInForce.DAY,
            ttl_seconds=1,  # Very short TTL for testing
        )

        ticket = manager.submit(order_intent, plan, market_data)

        # Wait for TTL
        import time
        time.sleep(1.1)

        # Process should trigger cancel
        manager.process_all()

        # Check order was cancelled
        assert transport.orders[ticket.broker_order_id]["status"] == "CANCELLED"


# =============================================================================
# BasketExecutor Tests
# =============================================================================

class TestBasketNetting:
    """Tests for trade netting across sleeves."""

    def test_opposite_trades_net_out(self):
        """Opposite trades on same instrument should net out."""
        config = ExecutionConfig()
        instruments = {
            "CSPX": InstrumentSpec("CSPX", "ETF", "EUR", 1.0),
        }
        executor = BasketExecutor(config, instruments)

        intents = [
            OrderIntent("CSPX", "BUY", 100, "rebalance", "core"),
            OrderIntent("CSPX", "SELL", 100, "rebalance", "sector"),
        ]
        prices = {"CSPX": 500.0}

        net_positions = executor.net_trades(intents, prices)

        # Should be empty - fully netted
        assert len(net_positions) == 0

    def test_partial_netting(self):
        """Partial netting should reduce to net quantity."""
        config = ExecutionConfig()
        instruments = {
            "CSPX": InstrumentSpec("CSPX", "ETF", "EUR", 1.0),
        }
        executor = BasketExecutor(config, instruments)

        intents = [
            OrderIntent("CSPX", "BUY", 150, "rebalance", "core"),
            OrderIntent("CSPX", "SELL", 50, "rebalance", "sector"),
        ]
        prices = {"CSPX": 500.0}

        net_positions = executor.net_trades(intents, prices)

        assert len(net_positions) == 1
        pos = net_positions[0]
        assert pos.net_qty == 100  # 150 - 50
        assert pos.side == "BUY"
        assert pos.gross_qty == 200  # 150 + 50
        assert pos.netting_savings == 100  # Saved 100 shares of turnover

    def test_netting_benefit_calculation(self):
        """Calculate netting benefit statistics."""
        intents = [
            OrderIntent("CSPX", "BUY", 100, "rebalance", "core"),
            OrderIntent("CSPX", "SELL", 40, "rebalance", "sector"),
            OrderIntent("CS51", "SELL", 200, "rebalance", "core"),
            OrderIntent("CS51", "SELL", 50, "rebalance", "sector"),
        ]

        benefit = calculate_netting_benefit(intents)

        assert benefit["instruments_with_trades"] == 2
        assert benefit["gross_quantity"] == 390  # 100+40+200+50
        assert benefit["net_quantity"] == 310    # 60 (CSPX net buy) + 250 (CS51 net sell)
        assert benefit["quantity_saved"] == 80   # 390 - 310
        assert benefit["savings_pct"] == pytest.approx(80 / 390, rel=0.01)

    def test_priority_ordering(self):
        """Orders should be sorted by priority (futures first, then liquid)."""
        config = ExecutionConfig()
        instruments = {
            "CSPX": InstrumentSpec("CSPX", "ETF", "EUR", 1.0, liquidity_tier=2),
            "ES": InstrumentSpec("ES", "FUT", "USD", 50.0, liquidity_tier=1),
            "IUKD": InstrumentSpec("IUKD", "ETF", "GBP", 1.0, liquidity_tier=3),
        }
        executor = BasketExecutor(config, instruments)

        intents = [
            OrderIntent("IUKD", "BUY", 100, "rebalance", "sector"),
            OrderIntent("CSPX", "BUY", 100, "rebalance", "core"),
            OrderIntent("ES", "SELL", 10, "hedge", "hedge"),
        ]
        prices = {"CSPX": 500.0, "ES": 5000.0, "IUKD": 10.0}

        plan = executor.create_basket_plan(intents, prices)

        # ES (futures) should be first
        assert plan.intents[0].instrument_id == "ES"

    def test_min_notional_filter(self):
        """Orders below minimum notional should be filtered."""
        config = ExecutionConfig(min_trade_notional_usd=5000.0)
        instruments = {
            "CSPX": InstrumentSpec("CSPX", "ETF", "EUR", 1.0),
        }
        executor = BasketExecutor(config, instruments)

        intents = [
            OrderIntent("CSPX", "BUY", 5, "rebalance", "core"),  # $2500 notional
        ]
        prices = {"CSPX": 500.0}

        plan = executor.create_basket_plan(intents, prices)

        assert len(plan.intents) == 0
        assert plan.filtered_count == 1


# =============================================================================
# Slippage Tests
# =============================================================================

class TestSlippageCalculation:
    """Tests for slippage computation."""

    def test_buy_slippage_positive_when_paid_more(self):
        """BUY slippage is positive when fill > arrival."""
        slip = compute_slippage_bps(
            fill_price=100.10,
            arrival_price=100.00,
            side="BUY"
        )
        assert slip == pytest.approx(10.0, rel=0.01)  # 10 bps

    def test_buy_slippage_negative_when_paid_less(self):
        """BUY slippage is negative when fill < arrival (price improvement)."""
        slip = compute_slippage_bps(
            fill_price=99.90,
            arrival_price=100.00,
            side="BUY"
        )
        assert slip == pytest.approx(-10.0, rel=0.01)  # -10 bps

    def test_sell_slippage_positive_when_received_less(self):
        """SELL slippage is positive when fill < arrival."""
        slip = compute_slippage_bps(
            fill_price=99.90,
            arrival_price=100.00,
            side="SELL"
        )
        assert slip == pytest.approx(10.0, rel=0.01)  # 10 bps

    def test_collar_enforcement(self):
        """Collar enforcer should limit prices."""
        enforcer = CollarEnforcer(default_max_bps=10.0)

        collar = enforcer.calculate_collar(
            reference_price=100.0,
            side="BUY",
            max_slippage_bps=10.0,
        )

        assert collar["ceiling"] == pytest.approx(100.10, rel=0.01)
        assert collar["floor"] is None

        # Enforce should cap price at ceiling
        limited = enforcer.enforce_collar(100.50, collar, "BUY")
        assert limited == pytest.approx(100.10, rel=0.01)


class TestSlippageTracker:
    """Tests for slippage tracking."""

    def test_record_and_summarize(self):
        """Should record fills and compute summary."""
        tracker = SlippageTracker()

        # BUY CSPX: paid 500.10 vs arrival 500.00 = +2 bps slippage
        tracker.record("CSPX", "BUY", 100, 500.00, 500.10, "LMT")
        # SELL CS51: received 399.80 vs arrival 400.00 = +5 bps slippage
        tracker.record("CS51", "SELL", 50, 400.00, 399.80, "LMT")

        summary = tracker.get_summary()

        assert summary["count"] == 2
        # Average of 2 bps and 5 bps = 3.5 bps
        assert summary["avg_slippage_bps"] == pytest.approx(3.5, rel=0.1)


# =============================================================================
# Calendar Tests
# =============================================================================

class TestMarketCalendar:
    """Tests for market calendar and session detection."""

    def test_us_market_hours(self):
        """US market should be open during regular hours."""
        calendar = MarketCalendar()

        # 3pm UTC on a Monday in March (before DST) is 10am ET - should be open
        # (NYC is UTC-5 before DST switch)
        import pytz
        test_time = datetime(2024, 3, 4, 15, 0, 0, tzinfo=pytz.UTC)  # Monday 3pm UTC = 10am ET

        phase = calendar.get_session_phase("NYSE", test_time)

        # During regular trading hours
        assert phase == SessionPhase.REGULAR

    def test_market_closed_weekend(self):
        """Market should be closed on weekend."""
        calendar = MarketCalendar()

        import pytz
        # Saturday
        test_time = datetime(2024, 3, 2, 14, 0, 0, tzinfo=pytz.UTC)

        phase = calendar.get_session_phase("NYSE", test_time)

        assert phase == SessionPhase.CLOSED

    def test_avoid_near_open(self):
        """Should recommend avoiding trading near open."""
        import pytz
        # 9:35 AM ET (5 minutes after open)
        test_time = datetime(2024, 3, 4, 14, 35, 0, tzinfo=pytz.UTC)

        should_avoid, reason = should_avoid_trading(
            "NYSE",
            avoid_first_minutes=15,
            avoid_last_minutes=10,
            at_time=test_time
        )

        assert should_avoid
        assert "close to open" in reason.lower()


# =============================================================================
# PairExecutor Tests
# =============================================================================

class TestPairLegging:
    """Tests for pair execution legging detection."""

    def test_detect_legging(self):
        """Should detect legging when one leg significantly ahead."""
        pair = PairGroup(
            name="us_eu_spread",
            intents=[
                OrderIntent("CSPX", "BUY", 100, "rebalance", "core"),
                OrderIntent("CS51", "SELL", 100, "rebalance", "core"),
            ],
            trigger_fill_pct=0.30,
        )

        # Create mock tickets
        ticket1 = OrderTicket(
            intent=pair.intents[0],
            plan=OrderPlan(OrderType.LMT, 500.0, TimeInForce.DAY),
            ticket_id="t1",
        )
        ticket1.filled_qty = 50  # 50% filled
        ticket1.status = OrderStatus.PARTIAL

        ticket2 = OrderTicket(
            intent=pair.intents[1],
            plan=OrderPlan(OrderType.LMT, 400.0, TimeInForce.DAY),
            ticket_id="t2",
        )
        ticket2.filled_qty = 0  # 0% filled
        ticket2.status = OrderStatus.SUBMITTED

        pair.tickets = [ticket1, ticket2]
        pair.started_at = datetime.now()

        # Check if legged
        assert pair.is_legged()
        assert pair.needs_hedge() is False  # No hedge intent set

    def test_not_legged_when_balanced(self):
        """Should not be legged when fills are balanced."""
        pair = PairGroup(
            name="us_eu_spread",
            intents=[
                OrderIntent("CSPX", "BUY", 100, "rebalance", "core"),
                OrderIntent("CS51", "SELL", 100, "rebalance", "core"),
            ],
            trigger_fill_pct=0.30,
        )

        # Create balanced tickets
        ticket1 = OrderTicket(
            intent=pair.intents[0],
            plan=OrderPlan(OrderType.LMT, 500.0, TimeInForce.DAY),
            ticket_id="t1",
        )
        ticket1.filled_qty = 40  # 40% filled

        ticket2 = OrderTicket(
            intent=pair.intents[1],
            plan=OrderPlan(OrderType.LMT, 400.0, TimeInForce.DAY),
            ticket_id="t2",
        )
        ticket2.filled_qty = 35  # 35% filled

        pair.tickets = [ticket1, ticket2]

        # Should not be legged - both above threshold
        assert not pair.is_legged()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
