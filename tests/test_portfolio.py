"""
Tests for portfolio management module.
"""

import pytest
import pandas as pd
import numpy as np
from datetime import date, datetime
from pathlib import Path
import json
import tempfile

from src.portfolio import (
    PortfolioState, Position, Sleeve, SleeveAllocation,
    save_portfolio_state, load_portfolio_state,
    save_returns_history, load_returns_history
)


class TestPosition:
    """Tests for Position class."""

    def test_position_creation(self):
        """Test basic position creation."""
        pos = Position(
            instrument_id="SPY",
            quantity=100,
            avg_cost=450.0,
            market_price=455.0,
            multiplier=1.0
        )
        assert pos.instrument_id == "SPY"
        assert pos.quantity == 100
        assert pos.avg_cost == 450.0

    def test_market_value(self):
        """Test market value calculation."""
        pos = Position(
            instrument_id="SPY",
            quantity=100,
            avg_cost=450.0,
            market_price=455.0,
            multiplier=1.0
        )
        assert pos.market_value == 45500.0  # 100 * 455

    def test_market_value_with_multiplier(self):
        """Test market value with futures multiplier."""
        pos = Position(
            instrument_id="ES",
            quantity=2,
            avg_cost=4500.0,
            market_price=4550.0,
            multiplier=50.0  # ES multiplier
        )
        assert pos.market_value == 455000.0  # 2 * 4550 * 50

    def test_unrealized_pnl(self):
        """Test unrealized P&L calculation."""
        pos = Position(
            instrument_id="SPY",
            quantity=100,
            avg_cost=450.0,
            market_price=455.0,
            multiplier=1.0
        )
        assert pos.unrealized_pnl == 500.0  # (455-450) * 100

    def test_unrealized_pnl_short(self):
        """Test unrealized P&L for short position."""
        pos = Position(
            instrument_id="FEZ",
            quantity=-100,
            avg_cost=50.0,
            market_price=48.0,  # Price dropped = profit
            multiplier=1.0
        )
        # Short profit: sold at 50, worth 48, profit = 2 * 100 = 200
        assert pos.unrealized_pnl == 200.0

    def test_unrealized_pnl_pct(self):
        """Test unrealized P&L percentage."""
        pos = Position(
            instrument_id="SPY",
            quantity=100,
            avg_cost=450.0,
            market_price=495.0,  # 10% gain
            multiplier=1.0
        )
        assert abs(pos.unrealized_pnl_pct - 0.10) < 0.001


class TestPortfolioState:
    """Tests for PortfolioState class."""

    def test_portfolio_creation(self):
        """Test basic portfolio creation."""
        portfolio = PortfolioState(
            nav=1000000,
            cash=100000,
            initial_capital=1000000
        )
        assert portfolio.nav == 1000000
        assert portfolio.cash == 100000

    def test_sleeve_allocations_initialized(self):
        """Test that sleeve allocations are initialized."""
        portfolio = PortfolioState(nav=1000000, cash=100000, initial_capital=1000000)
        assert len(portfolio.sleeve_allocations) == len(Sleeve)
        for sleeve in Sleeve:
            assert sleeve in portfolio.sleeve_allocations

    def test_add_position(self):
        """Test adding a position to portfolio."""
        portfolio = PortfolioState(nav=1000000, cash=100000, initial_capital=1000000)
        pos = Position(
            instrument_id="SPY",
            quantity=100,
            avg_cost=450.0,
            market_price=450.0
        )
        portfolio.positions["SPY"] = pos
        assert "SPY" in portfolio.positions
        assert portfolio.positions["SPY"].quantity == 100

    def test_compute_exposures(self):
        """Test exposure computation."""
        portfolio = PortfolioState(nav=100000, cash=10000, initial_capital=100000)

        # Add long position
        portfolio.positions["SPY"] = Position(
            instrument_id="SPY",
            quantity=100,
            avg_cost=450.0,
            market_price=450.0
        )
        # Add short position
        portfolio.positions["FEZ"] = Position(
            instrument_id="FEZ",
            quantity=-500,
            avg_cost=50.0,
            market_price=50.0
        )

        portfolio.compute_exposures()

        assert portfolio.long_exposure == 45000  # 100 * 450
        assert portfolio.short_exposure == 25000  # abs(-500 * 50)
        assert portfolio.gross_exposure == 70000
        assert portfolio.net_exposure == 20000  # 45000 - 25000

    def test_record_daily_pnl(self):
        """Test recording daily P&L."""
        portfolio = PortfolioState(nav=100000, cash=10000, initial_capital=100000)
        portfolio.record_daily_pnl(0.02, date.today())

        assert portfolio.daily_return == 0.02
        assert len(portfolio.pnl_history) == 1
        assert len(portfolio.nav_history) == 1

    def test_drawdown_computation(self):
        """Test drawdown computation."""
        portfolio = PortfolioState(nav=100000, cash=10000, initial_capital=100000)

        # Simulate a series of returns
        nav_values = [100000, 102000, 105000, 100000, 95000, 98000]
        dates = pd.date_range(start='2024-01-01', periods=len(nav_values), freq='D')

        for dt, nav in zip(dates, nav_values):
            portfolio.nav_history[dt] = nav

        portfolio._update_drawdown()

        # Max drawdown should be from 105000 to 95000 = -9.52%
        assert portfolio.max_drawdown < 0
        assert abs(portfolio.max_drawdown - (-0.0952)) < 0.01

    def test_get_sleeve_weights(self):
        """Test getting sleeve weights."""
        portfolio = PortfolioState(nav=100000, cash=10000, initial_capital=100000)
        weights = portfolio.get_sleeve_weights()

        assert isinstance(weights, dict)
        assert all(s.value in weights for s in Sleeve)

    def test_serialization_roundtrip(self):
        """Test to_dict and from_dict."""
        portfolio = PortfolioState(
            nav=1000000,
            cash=100000,
            initial_capital=1000000,
            gross_exposure=500000,
            net_exposure=200000
        )
        portfolio.positions["SPY"] = Position(
            instrument_id="SPY",
            quantity=100,
            avg_cost=450.0,
            market_price=455.0,
            sleeve=Sleeve.CORE_INDEX_RV
        )

        # Serialize
        data = portfolio.to_dict()

        # Deserialize
        loaded = PortfolioState.from_dict(data)

        assert loaded.nav == portfolio.nav
        assert loaded.cash == portfolio.cash
        assert "SPY" in loaded.positions
        assert loaded.positions["SPY"].quantity == 100


class TestPortfolioPersistence:
    """Tests for portfolio persistence functions."""

    def test_save_and_load_portfolio_state(self):
        """Test saving and loading portfolio state."""
        portfolio = PortfolioState(
            nav=1000000,
            cash=100000,
            initial_capital=1000000
        )
        portfolio.positions["SPY"] = Position(
            instrument_id="SPY",
            quantity=100,
            avg_cost=450.0,
            market_price=455.0
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = f"{tmpdir}/portfolio_state.json"

            save_portfolio_state(portfolio, filepath)
            loaded = load_portfolio_state(filepath)

            assert loaded is not None
            assert loaded.nav == portfolio.nav
            assert "SPY" in loaded.positions

    def test_load_nonexistent_file(self):
        """Test loading from nonexistent file."""
        loaded = load_portfolio_state("/nonexistent/path/state.json")
        assert loaded is None

    def test_save_and_load_returns_history(self):
        """Test saving and loading returns history."""
        returns = pd.Series(
            [0.01, -0.005, 0.02, 0.003],
            index=pd.date_range(start='2024-01-01', periods=4, freq='D')
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = f"{tmpdir}/returns_history.csv"

            save_returns_history(returns, filepath)
            loaded = load_returns_history(filepath)

            assert len(loaded) == len(returns)
            assert abs(loaded.iloc[0] - returns.iloc[0]) < 0.0001


class TestSleeveInference:
    """Tests for sleeve inference logic."""

    def test_infer_core_sleeve(self):
        """Test inferring core index RV sleeve."""
        portfolio = PortfolioState(nav=100000, cash=10000, initial_capital=100000)

        assert portfolio._infer_sleeve("us_index_etf") == Sleeve.CORE_INDEX_RV
        assert portfolio._infer_sleeve("eu_index_etf") == Sleeve.CORE_INDEX_RV
        assert portfolio._infer_sleeve("SPY") == Sleeve.CORE_INDEX_RV

    def test_infer_sector_sleeve(self):
        """Test inferring sector RV sleeve."""
        portfolio = PortfolioState(nav=100000, cash=10000, initial_capital=100000)

        assert portfolio._infer_sleeve("tech_xlk") == Sleeve.SECTOR_RV
        assert portfolio._infer_sleeve("QQQ") == Sleeve.SECTOR_RV

    def test_infer_credit_sleeve(self):
        """Test inferring credit carry sleeve."""
        portfolio = PortfolioState(nav=100000, cash=10000, initial_capital=100000)

        assert portfolio._infer_sleeve("ig_lqd") == Sleeve.CREDIT_CARRY
        assert portfolio._infer_sleeve("HYG") == Sleeve.CREDIT_CARRY
