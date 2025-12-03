"""
Tests for tail hedge module.
"""

import pytest
from datetime import date, timedelta
from unittest.mock import Mock

from src.tail_hedge import (
    TailHedgeManager, HedgePosition, HedgeBudget, HedgeType, CrisisAction
)
from src.portfolio import PortfolioState, Sleeve
from src.strategy_logic import OrderSpec


@pytest.fixture
def default_settings():
    """Default settings for tests."""
    return {
        'hedge_budget_annual_pct': 0.025,  # 2.5% of NAV
        'crisis': {
            'vix_threshold': 40,
            'pnl_spike_threshold_pct': 0.10,
            'crisis_redeploy_fraction': 0.6
        }
    }


@pytest.fixture
def instruments_config():
    """Sample instruments config."""
    return {
        'options': {
            'spy_etf': {
                'underlying': 'SPY',
                'exchange': 'ARCA',
                'sec_type': 'OPT'
            },
            'vix_index': {
                'underlying': 'VIX',
                'exchange': 'CBOE',
                'sec_type': 'OPT'
            }
        }
    }


@pytest.fixture
def hedge_manager(default_settings, instruments_config):
    """Create hedge manager."""
    return TailHedgeManager(default_settings, instruments_config)


@pytest.fixture
def mock_data_feed():
    """Create mock data feed."""
    feed = Mock()
    feed.get_last_price.side_effect = lambda x: {
        'SPY': 450.0,
        'FEZ': 48.0,
        'HYG': 75.0,
        'EUFN': 20.0
    }.get(x, 100.0)
    feed.get_vix_level.return_value = 18.0
    return feed


class TestHedgeBudget:
    """Tests for HedgeBudget class."""

    def test_budget_creation(self):
        """Test hedge budget creation."""
        budget = HedgeBudget(
            annual_budget_pct=0.025,
            nav_at_year_start=1000000
        )
        assert budget.total_budget == 25000  # 2.5% of 1M

    def test_budget_remaining(self):
        """Test remaining budget calculation."""
        budget = HedgeBudget(
            annual_budget_pct=0.025,
            nav_at_year_start=1000000,
            used_ytd=10000
        )
        assert budget.remaining == 15000  # 25000 - 10000

    def test_budget_with_realized_gains(self):
        """Test budget recovers from realized gains."""
        budget = HedgeBudget(
            annual_budget_pct=0.025,
            nav_at_year_start=1000000,
            used_ytd=20000,
            realized_gains_ytd=10000
        )
        # 25000 - 20000 + (10000 * 0.5) = 10000
        assert budget.remaining == 10000

    def test_budget_usage_pct(self):
        """Test budget usage percentage."""
        budget = HedgeBudget(
            annual_budget_pct=0.025,
            nav_at_year_start=1000000,
            used_ytd=12500
        )
        assert budget.usage_pct == 0.5  # 50% used


class TestHedgePosition:
    """Tests for HedgePosition class."""

    def test_hedge_position_creation(self):
        """Test hedge position creation."""
        pos = HedgePosition(
            hedge_id="spy_put_1",
            hedge_type=HedgeType.EQUITY_PUT,
            instrument_id="spy_put",
            underlying="SPY",
            quantity=10,
            strike=400.0,
            expiry=date.today() + timedelta(days=60),
            premium_paid=5000
        )
        assert pos.hedge_id == "spy_put_1"
        assert pos.hedge_type == HedgeType.EQUITY_PUT
        assert pos.quantity == 10

    def test_hedge_pnl(self):
        """Test hedge P&L calculation."""
        pos = HedgePosition(
            hedge_id="spy_put_1",
            hedge_type=HedgeType.EQUITY_PUT,
            instrument_id="spy_put",
            underlying="SPY",
            quantity=10,
            premium_paid=5000,
            current_value=8000
        )
        assert pos.pnl == 3000  # 8000 - 5000

    def test_days_to_expiry(self):
        """Test days to expiry calculation."""
        expiry = date.today() + timedelta(days=30)
        pos = HedgePosition(
            hedge_id="spy_put_1",
            hedge_type=HedgeType.EQUITY_PUT,
            instrument_id="spy_put",
            underlying="SPY",
            quantity=10,
            expiry=expiry,
            premium_paid=5000
        )
        assert pos.days_to_expiry == 30


class TestTailHedgeManager:
    """Tests for TailHedgeManager class."""

    def test_initialize_budget(self, hedge_manager):
        """Test budget initialization."""
        hedge_manager.initialize_budget(1000000)
        assert hedge_manager.budget is not None
        assert hedge_manager.budget.total_budget == 25000

    def test_ensure_tail_hedges_no_budget(self, hedge_manager, mock_data_feed):
        """Test no hedges without budget."""
        portfolio = PortfolioState(nav=1000000, cash=100000, initial_capital=1000000)

        # Without initializing budget
        orders = hedge_manager.ensure_tail_hedges(
            portfolio_state=portfolio,
            data_feed=mock_data_feed
        )

        # Should initialize budget automatically
        assert hedge_manager.budget is not None

    def test_ensure_tail_hedges_creates_orders(self, hedge_manager, mock_data_feed):
        """Test that hedges are created when needed."""
        portfolio = PortfolioState(nav=1000000, cash=100000, initial_capital=1000000)
        hedge_manager.initialize_budget(1000000)

        orders = hedge_manager.ensure_tail_hedges(
            portfolio_state=portfolio,
            data_feed=mock_data_feed
        )

        # Should create some hedge orders
        assert isinstance(orders, list)
        # All orders should be for crisis alpha sleeve
        for order in orders:
            assert order.sleeve == Sleeve.CRISIS_ALPHA

    def test_handle_crisis_no_crisis(self, hedge_manager, mock_data_feed):
        """Test no crisis action when conditions normal."""
        portfolio = PortfolioState(nav=1000000, cash=100000, initial_capital=1000000)
        hedge_manager.initialize_budget(1000000)

        orders, action = hedge_manager.handle_crisis_if_any(
            portfolio_state=portfolio,
            data_feed=mock_data_feed,
            vix_level=18,  # Below threshold
            daily_pnl=0.01  # Normal day
        )

        assert len(orders) == 0
        assert action.action_type == "none"

    def test_handle_crisis_vix_spike(self, hedge_manager, mock_data_feed):
        """Test crisis handling on VIX spike."""
        portfolio = PortfolioState(nav=1000000, cash=100000, initial_capital=1000000)
        hedge_manager.initialize_budget(1000000)

        orders, action = hedge_manager.handle_crisis_if_any(
            portfolio_state=portfolio,
            data_feed=mock_data_feed,
            vix_level=45,  # Above threshold
            daily_pnl=0.01
        )

        assert action.action_type == "increase_hedges"
        assert action.rebalance_instruction == "reduce_exposure"
        assert action.urgency == "immediate"

    def test_handle_crisis_hedge_payoff(self, hedge_manager, mock_data_feed):
        """Test crisis handling when hedges pay off."""
        portfolio = PortfolioState(nav=1000000, cash=100000, initial_capital=1000000)
        hedge_manager.initialize_budget(1000000)

        # Add profitable hedge
        hedge = HedgePosition(
            hedge_id="spy_put_1",
            hedge_type=HedgeType.EQUITY_PUT,
            instrument_id="spy_put",
            underlying="SPY",
            quantity=10,
            premium_paid=5000,
            current_value=15000,  # Profitable
            is_active=True
        )
        hedge_manager.active_hedges["spy_put_1"] = hedge

        orders, action = hedge_manager.handle_crisis_if_any(
            portfolio_state=portfolio,
            data_feed=mock_data_feed,
            vix_level=25,
            daily_pnl=0.15  # Large positive = hedges paying off
        )

        assert action.action_type == "realize_hedges"
        assert action.rebalance_instruction == "increase_core_exposure"


class TestHedgeAllocation:
    """Tests for hedge allocation."""

    def test_hedge_allocation_totals(self):
        """Test hedge allocations sum to 1."""
        total = sum(TailHedgeManager.HEDGE_ALLOCATION.values())
        assert abs(total - 1.0) < 0.001

    def test_hedge_allocation_types(self):
        """Test all hedge types have allocation."""
        for hedge_type in [HedgeType.EQUITY_PUT, HedgeType.VOL_CALL,
                          HedgeType.CREDIT_PUT, HedgeType.SOVEREIGN_SPREAD,
                          HedgeType.BANK_PUT]:
            assert hedge_type in TailHedgeManager.HEDGE_ALLOCATION


class TestHedgeSummary:
    """Tests for hedge summary generation."""

    def test_get_hedge_summary_empty(self, hedge_manager):
        """Test summary with no hedges."""
        hedge_manager.initialize_budget(1000000)
        summary = hedge_manager.get_hedge_summary()

        assert summary["total_hedges"] == 0
        assert summary["total_premium_paid"] == 0
        assert summary["budget"] is not None

    def test_get_hedge_summary_with_hedges(self, hedge_manager):
        """Test summary with active hedges."""
        hedge_manager.initialize_budget(1000000)

        # Add hedge
        hedge = HedgePosition(
            hedge_id="spy_put_1",
            hedge_type=HedgeType.EQUITY_PUT,
            instrument_id="spy_put",
            underlying="SPY",
            quantity=10,
            premium_paid=5000,
            current_value=6000,
            is_active=True
        )
        hedge_manager.active_hedges["spy_put_1"] = hedge

        summary = hedge_manager.get_hedge_summary()

        assert summary["total_hedges"] == 1
        assert summary["total_premium_paid"] == 5000
        assert summary["total_current_value"] == 6000
        assert summary["total_pnl"] == 1000
        assert summary["by_type"]["equity_put"]["count"] == 1


class TestHedgeRolling:
    """Tests for hedge rolling logic."""

    def test_check_and_roll_expiring(self, hedge_manager, mock_data_feed):
        """Test rolling of expiring hedges."""
        hedge_manager.initialize_budget(1000000)

        # Add hedge expiring soon
        hedge = HedgePosition(
            hedge_id="spy_put_1",
            hedge_type=HedgeType.EQUITY_PUT,
            instrument_id="spy_put",
            underlying="SPY",
            quantity=10,
            expiry=date.today() + timedelta(days=15),  # Within roll window
            premium_paid=5000,
            is_active=True
        )
        hedge_manager.active_hedges["spy_put_1"] = hedge

        orders = hedge_manager._check_and_roll_hedges(mock_data_feed, date.today())

        # Should generate close order for expiring hedge and new hedge
        assert len(orders) >= 1
        # Old hedge should be marked inactive
        assert not hedge_manager.active_hedges["spy_put_1"].is_active

    def test_no_roll_if_not_expiring(self, hedge_manager, mock_data_feed):
        """Test no rolling if hedge not expiring."""
        hedge_manager.initialize_budget(1000000)

        # Add hedge with plenty of time
        hedge = HedgePosition(
            hedge_id="spy_put_1",
            hedge_type=HedgeType.EQUITY_PUT,
            instrument_id="spy_put",
            underlying="SPY",
            quantity=10,
            expiry=date.today() + timedelta(days=60),  # Plenty of time
            premium_paid=5000,
            is_active=True
        )
        hedge_manager.active_hedges["spy_put_1"] = hedge

        orders = hedge_manager._check_and_roll_hedges(mock_data_feed, date.today())

        assert len(orders) == 0
        assert hedge_manager.active_hedges["spy_put_1"].is_active


class TestOTMTargets:
    """Tests for OTM strike targeting."""

    def test_otm_targets_defined(self):
        """Test OTM targets are defined for relevant types."""
        assert HedgeType.EQUITY_PUT in TailHedgeManager.OTM_TARGETS
        assert HedgeType.CREDIT_PUT in TailHedgeManager.OTM_TARGETS
        assert HedgeType.BANK_PUT in TailHedgeManager.OTM_TARGETS

    def test_otm_targets_reasonable(self):
        """Test OTM targets are reasonable values."""
        for hedge_type, otm in TailHedgeManager.OTM_TARGETS.items():
            assert 0.05 <= otm <= 0.30  # Between 5% and 30% OTM
