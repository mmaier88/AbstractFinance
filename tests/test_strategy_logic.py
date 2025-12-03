"""
Tests for strategy logic module.
"""

import pytest
import pandas as pd
import numpy as np
from datetime import date
from unittest.mock import Mock, MagicMock

from src.strategy_logic import (
    Strategy, OrderSpec, SleeveTargets, StrategyOutput,
    generate_rebalance_orders
)
from src.portfolio import PortfolioState, Position, Sleeve
from src.risk_engine import RiskEngine, RiskDecision, RiskRegime


@pytest.fixture
def default_settings():
    """Default settings for tests."""
    return {
        'vol_target_annual': 0.12,
        'gross_leverage_max': 2.0,
        'max_drawdown_pct': 0.10,
        'sleeves': {
            'core_index_rv': 0.35,
            'sector_rv': 0.25,
            'single_name': 0.15,
            'credit_carry': 0.15,
            'crisis_alpha': 0.05,
            'cash_buffer': 0.05
        },
        'momentum': {
            'short_window_days': 50,
            'long_window_days': 200,
            'regime_reduce_factor': 0.5
        },
        'crisis': {
            'vix_threshold': 40,
            'pnl_spike_threshold_pct': 0.10,
            'crisis_redeploy_fraction': 0.6
        }
    }


@pytest.fixture
def instruments_config():
    """Sample instruments configuration."""
    return {
        'equity_indices': {
            'us_index_etf': {
                'symbol': 'SPY',
                'exchange': 'ARCA',
                'sec_type': 'STK',
                'currency': 'USD',
                'multiplier': 1.0
            },
            'eu_index_etf': {
                'symbol': 'FEZ',
                'exchange': 'ARCA',
                'sec_type': 'STK',
                'currency': 'USD',
                'multiplier': 1.0
            }
        },
        'sectors_us': {
            'tech_xlk': {
                'symbol': 'XLK',
                'exchange': 'ARCA',
                'sec_type': 'STK',
                'currency': 'USD'
            }
        }
    }


@pytest.fixture
def mock_data_feed():
    """Create mock data feed."""
    feed = Mock()
    feed.get_last_price.side_effect = lambda x: {
        'SPY': 450.0,
        'FEZ': 48.0,
        'XLK': 180.0,
        'QQQ': 380.0,
        'SMH': 200.0,
        'XLV': 140.0,
        'QUAL': 150.0,
        'EUFN': 20.0,
        'EWG': 30.0,
        'EWU': 35.0,
        'LQD': 110.0,
        'HYG': 75.0,
        'BKLN': 21.0,
        'ARCC': 20.0
    }.get(x, 100.0)
    return feed


@pytest.fixture
def strategy(default_settings, instruments_config):
    """Create strategy instance."""
    risk_engine = RiskEngine(default_settings)
    return Strategy(default_settings, instruments_config, risk_engine)


class TestOrderSpec:
    """Tests for OrderSpec class."""

    def test_order_spec_creation(self):
        """Test basic order spec creation."""
        order = OrderSpec(
            instrument_id="SPY",
            side="BUY",
            quantity=100,
            order_type="MKT",
            sleeve=Sleeve.CORE_INDEX_RV
        )
        assert order.instrument_id == "SPY"
        assert order.side == "BUY"
        assert order.quantity == 100

    def test_order_spec_with_limit(self):
        """Test order spec with limit price."""
        order = OrderSpec(
            instrument_id="SPY",
            side="SELL",
            quantity=50,
            order_type="LMT",
            limit_price=455.0
        )
        assert order.order_type == "LMT"
        assert order.limit_price == 455.0


class TestSleeveTargets:
    """Tests for SleeveTargets class."""

    def test_sleeve_targets_creation(self):
        """Test sleeve targets creation."""
        targets = SleeveTargets(
            sleeve=Sleeve.CORE_INDEX_RV,
            target_positions={"SPY": 100, "FEZ": -50},
            target_notional=100000,
            target_weight=0.35
        )
        assert targets.sleeve == Sleeve.CORE_INDEX_RV
        assert "SPY" in targets.target_positions
        assert targets.target_weight == 0.35


class TestStrategy:
    """Tests for Strategy class."""

    def test_strategy_initialization(self, strategy, default_settings):
        """Test strategy initialization."""
        assert strategy.sleeve_weights[Sleeve.CORE_INDEX_RV] == 0.35
        assert strategy.sleeve_weights[Sleeve.SECTOR_RV] == 0.25

    def test_compute_all_sleeve_targets(self, strategy, mock_data_feed):
        """Test computing targets for all sleeves."""
        portfolio = PortfolioState(
            nav=1000000,
            cash=100000,
            initial_capital=1000000
        )

        risk_decision = RiskDecision(
            scaling_factor=1.0,
            emergency_derisk=False,
            regime=RiskRegime.NORMAL
        )

        output = strategy.compute_all_sleeve_targets(
            portfolio=portfolio,
            data_feed=mock_data_feed,
            risk_decision=risk_decision
        )

        assert isinstance(output, StrategyOutput)
        assert Sleeve.CORE_INDEX_RV in output.sleeve_targets
        assert Sleeve.SECTOR_RV in output.sleeve_targets
        assert output.scaling_factor == 1.0

    def test_compute_targets_with_regime_reduction(self, strategy, mock_data_feed):
        """Test targets are reduced in adverse regime."""
        portfolio = PortfolioState(
            nav=1000000,
            cash=100000,
            initial_capital=1000000
        )

        risk_decision = RiskDecision(
            scaling_factor=0.8,
            emergency_derisk=False,
            regime=RiskRegime.ELEVATED,
            reduce_core_exposure=True,
            reduce_factor=0.5
        )

        output = strategy.compute_all_sleeve_targets(
            portfolio=portfolio,
            data_feed=mock_data_feed,
            risk_decision=risk_decision
        )

        # Effective scaling should be reduced
        # 0.8 * 0.5 = 0.4
        assert output.scaling_factor < 0.8

    def test_generate_orders(self, strategy, mock_data_feed):
        """Test order generation."""
        portfolio = PortfolioState(
            nav=1000000,
            cash=100000,
            initial_capital=1000000
        )

        risk_decision = RiskDecision(
            scaling_factor=1.0,
            emergency_derisk=False,
            regime=RiskRegime.NORMAL
        )

        output = strategy.compute_all_sleeve_targets(
            portfolio=portfolio,
            data_feed=mock_data_feed,
            risk_decision=risk_decision
        )

        # Should generate orders since no existing positions
        assert len(output.orders) > 0


class TestCoreIndexTargets:
    """Tests for core index RV sleeve."""

    def test_build_core_index_targets(self, strategy, mock_data_feed):
        """Test core index target building."""
        risk_decision = RiskDecision(
            scaling_factor=1.0,
            emergency_derisk=False,
            regime=RiskRegime.NORMAL
        )

        targets = strategy._build_core_index_targets(
            nav=1000000,
            scaling=1.0,
            data_feed=mock_data_feed,
            risk_decision=risk_decision
        )

        assert targets.sleeve == Sleeve.CORE_INDEX_RV
        # Should have long US position
        assert 'us_index_etf' in targets.target_positions
        # Should have short EU position
        assert 'eu_index_etf' in targets.target_positions
        # US should be positive (long)
        assert targets.target_positions['us_index_etf'] > 0
        # EU should be negative (short)
        assert targets.target_positions['eu_index_etf'] < 0


class TestRebalanceOrders:
    """Tests for rebalance order generation."""

    def test_generate_rebalance_orders_buy(self):
        """Test generating buy orders."""
        current = {"SPY": 100}
        target = {"SPY": 150}

        orders = generate_rebalance_orders(current, target, {})

        assert len(orders) == 1
        assert orders[0].side == "BUY"
        assert orders[0].quantity == 50

    def test_generate_rebalance_orders_sell(self):
        """Test generating sell orders."""
        current = {"SPY": 150}
        target = {"SPY": 100}

        orders = generate_rebalance_orders(current, target, {})

        assert len(orders) == 1
        assert orders[0].side == "SELL"
        assert orders[0].quantity == 50

    def test_generate_rebalance_orders_new_position(self):
        """Test generating orders for new position."""
        current = {}
        target = {"SPY": 100}

        orders = generate_rebalance_orders(current, target, {})

        assert len(orders) == 1
        assert orders[0].side == "BUY"
        assert orders[0].quantity == 100

    def test_generate_rebalance_orders_close_position(self):
        """Test generating orders to close position."""
        current = {"SPY": 100}
        target = {}

        orders = generate_rebalance_orders(current, target, {})

        assert len(orders) == 1
        assert orders[0].side == "SELL"
        assert orders[0].quantity == 100

    def test_generate_rebalance_orders_no_change(self):
        """Test no orders when positions match."""
        current = {"SPY": 100}
        target = {"SPY": 100}

        orders = generate_rebalance_orders(current, target, {})

        assert len(orders) == 0

    def test_generate_rebalance_orders_multiple(self):
        """Test generating multiple orders."""
        current = {"SPY": 100, "FEZ": -50}
        target = {"SPY": 150, "FEZ": -80, "XLK": 30}

        orders = generate_rebalance_orders(current, target, {})

        assert len(orders) == 3

        spy_order = next(o for o in orders if o.instrument_id == "SPY")
        assert spy_order.side == "BUY"
        assert spy_order.quantity == 50

        fez_order = next(o for o in orders if o.instrument_id == "FEZ")
        assert fez_order.side == "SELL"
        assert fez_order.quantity == 30

        xlk_order = next(o for o in orders if o.instrument_id == "XLK")
        assert xlk_order.side == "BUY"
        assert xlk_order.quantity == 30


class TestETFMapping:
    """Tests for ETF to instrument ID mapping."""

    def test_etf_to_instrument_id(self, strategy):
        """Test ETF symbol to instrument ID mapping."""
        assert strategy._etf_to_instrument_id("SPY") == "us_index_etf"
        assert strategy._etf_to_instrument_id("FEZ") == "eu_index_etf"
        assert strategy._etf_to_instrument_id("XLK") == "tech_xlk"
        assert strategy._etf_to_instrument_id("LQD") == "ig_lqd"

    def test_unknown_etf_mapping(self, strategy):
        """Test unknown ETF returns None."""
        result = strategy._etf_to_instrument_id("UNKNOWN")
        assert result is None
