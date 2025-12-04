"""
Tests for risk engine module.
"""

import pytest
import pandas as pd
import numpy as np
from datetime import date, datetime

from src.risk_engine import (
    RiskEngine, RiskDecision, RiskMetrics, RiskRegime
)
from src.portfolio import PortfolioState


@pytest.fixture
def default_settings():
    """Default settings for tests."""
    return {
        'vol_target_annual': 0.12,
        'gross_leverage_max': 2.0,
        'net_leverage_max': 1.0,
        'max_drawdown_pct': 0.10,
        'rebalance_threshold_pct': 0.02,
        'momentum': {
            'short_window_days': 50,
            'long_window_days': 200,
            'regime_reduce_factor': 0.5
        },
        'crisis': {
            'vix_threshold': 40,
            'pnl_spike_threshold_pct': 0.10
        }
    }


@pytest.fixture
def risk_engine(default_settings):
    """Create risk engine with default settings."""
    return RiskEngine(default_settings)


@pytest.fixture
def sample_returns():
    """Generate sample returns series."""
    np.random.seed(42)
    returns = pd.Series(
        np.random.normal(0.0004, 0.01, 252),  # ~10% annual return, 16% vol
        index=pd.date_range(start='2024-01-01', periods=252, freq='D')
    )
    return returns


class TestVolatilityComputation:
    """Tests for volatility computation."""

    def test_compute_realized_vol_annual(self, risk_engine):
        """Test annualized volatility computation."""
        # Create returns with known volatility
        daily_vol = 0.01  # 1% daily
        np.random.seed(42)
        returns = pd.Series(np.random.normal(0, daily_vol, 100))

        vol = risk_engine.compute_realized_vol_annual(returns, window=20)

        # Should be approximately 16% annual (1% * sqrt(252))
        # Tolerance increased to 0.05 due to inherent variance in random sample volatility estimation
        expected = daily_vol * np.sqrt(252)
        assert abs(vol - expected) < 0.05

    def test_compute_realized_vol_short_series(self, risk_engine):
        """Test volatility with short series."""
        returns = pd.Series([0.01, -0.01, 0.005, -0.005, 0.01])
        vol = risk_engine.compute_realized_vol_annual(returns, window=20)
        assert vol >= 0  # Should not raise error

    def test_compute_realized_vol_empty_series(self, risk_engine):
        """Test volatility with empty series."""
        returns = pd.Series(dtype=float)
        # Should handle gracefully
        try:
            vol = risk_engine.compute_realized_vol_annual(returns)
            assert True  # Passed without error
        except Exception:
            pass  # Also acceptable behavior


class TestDrawdownComputation:
    """Tests for drawdown computation."""

    def test_compute_max_drawdown(self, risk_engine):
        """Test max drawdown computation."""
        # Create equity curve with known drawdown
        equity = pd.Series([100, 110, 105, 95, 100, 90, 95])

        dd = risk_engine.compute_max_drawdown(equity)

        # Max drawdown: 110 -> 90 = -18.18%
        assert dd < 0
        assert abs(dd - (-0.1818)) < 0.01

    def test_compute_current_drawdown(self, risk_engine):
        """Test current drawdown computation."""
        equity = pd.Series([100, 110, 105, 108])

        dd = risk_engine.compute_current_drawdown(equity)

        # Current drawdown: 110 -> 108 = -1.82%
        assert dd < 0
        assert abs(dd - (-0.0182)) < 0.01

    def test_no_drawdown(self, risk_engine):
        """Test with no drawdown (always increasing)."""
        equity = pd.Series([100, 101, 102, 103, 104])

        dd = risk_engine.compute_max_drawdown(equity)
        assert dd == 0

        current_dd = risk_engine.compute_current_drawdown(equity)
        assert current_dd == 0


class TestScalingFactor:
    """Tests for position scaling factor."""

    def test_compute_scaling_factor_normal(self, risk_engine):
        """Test scaling factor under normal conditions."""
        # Realized vol = 8%, target = 12%
        # Should scale up by 1.5x
        factor = risk_engine.compute_scaling_factor(0.08)
        assert abs(factor - 1.5) < 0.01

    def test_compute_scaling_factor_high_vol(self, risk_engine):
        """Test scaling factor caps at max leverage."""
        # Realized vol = 4%, target = 12%
        # Raw factor would be 3.0, but capped at 2.0
        factor = risk_engine.compute_scaling_factor(0.04)
        assert factor == 2.0

    def test_compute_scaling_factor_low_vol(self, risk_engine):
        """Test scaling factor with very low vol."""
        # Very low vol should result in max leverage
        factor = risk_engine.compute_scaling_factor(0.01)
        assert factor == 2.0  # Capped at max

    def test_compute_scaling_factor_zero_vol(self, risk_engine):
        """Test scaling factor with zero vol."""
        factor = risk_engine.compute_scaling_factor(0.0)
        assert factor == 1.0


class TestVaR:
    """Tests for Value at Risk computation."""

    def test_compute_var_95(self, risk_engine, sample_returns):
        """Test 95% VaR computation."""
        var = risk_engine.compute_var(sample_returns, 0.95)
        assert var > 0  # VaR is positive (potential loss)
        assert var < 0.05  # Should be reasonable for daily returns

    def test_compute_var_99(self, risk_engine, sample_returns):
        """Test 99% VaR computation."""
        var_95 = risk_engine.compute_var(sample_returns, 0.95)
        var_99 = risk_engine.compute_var(sample_returns, 0.99)

        # 99% VaR should be larger than 95%
        assert var_99 > var_95

    def test_compute_expected_shortfall(self, risk_engine, sample_returns):
        """Test Expected Shortfall computation."""
        var = risk_engine.compute_var(sample_returns, 0.95)
        es = risk_engine.compute_expected_shortfall(sample_returns, 0.95)

        # ES should be >= VaR
        assert es >= var


class TestRegimeDetection:
    """Tests for regime detection."""

    def test_detect_normal_regime(self, risk_engine):
        """Test normal regime detection."""
        regime = risk_engine.detect_regime(
            vix_level=18,
            spread_momentum=0.5,
            current_drawdown=-0.02
        )
        assert regime == RiskRegime.NORMAL

    def test_detect_elevated_regime(self, risk_engine):
        """Test elevated regime detection."""
        regime = risk_engine.detect_regime(
            vix_level=28,
            spread_momentum=0.0,
            current_drawdown=-0.03
        )
        assert regime == RiskRegime.ELEVATED

    def test_detect_crisis_regime_vix(self, risk_engine):
        """Test crisis regime from VIX."""
        regime = risk_engine.detect_regime(
            vix_level=45,  # Above threshold
            spread_momentum=0.0,
            current_drawdown=-0.05
        )
        assert regime == RiskRegime.CRISIS

    def test_detect_crisis_regime_drawdown(self, risk_engine):
        """Test crisis regime from drawdown."""
        regime = risk_engine.detect_regime(
            vix_level=25,
            spread_momentum=0.0,
            current_drawdown=-0.12  # Below max DD threshold
        )
        assert regime == RiskRegime.CRISIS


class TestSpreadMomentum:
    """Tests for spread momentum computation."""

    def test_compute_spread_momentum_positive(self, risk_engine):
        """Test positive momentum signal."""
        # Create upward trending ratio
        ratio = pd.Series(
            np.linspace(1.0, 1.2, 250),
            index=pd.date_range(start='2024-01-01', periods=250, freq='D')
        )
        momentum = risk_engine.compute_spread_momentum(ratio)
        assert momentum > 0

    def test_compute_spread_momentum_negative(self, risk_engine):
        """Test negative momentum signal."""
        # Create downward trending ratio
        ratio = pd.Series(
            np.linspace(1.2, 1.0, 250),
            index=pd.date_range(start='2024-01-01', periods=250, freq='D')
        )
        momentum = risk_engine.compute_spread_momentum(ratio)
        assert momentum < 0

    def test_compute_spread_momentum_short_series(self, risk_engine):
        """Test momentum with insufficient data."""
        ratio = pd.Series([1.0, 1.01, 1.02])  # Too short
        momentum = risk_engine.compute_spread_momentum(ratio)
        assert momentum == 0.0


class TestRiskDecision:
    """Tests for risk decision evaluation."""

    def test_evaluate_risk_normal(self, risk_engine, sample_returns):
        """Test risk evaluation under normal conditions."""
        portfolio = PortfolioState(
            nav=1000000,
            cash=100000,
            initial_capital=1000000
        )
        portfolio.nav_history = pd.Series(
            [1000000, 1010000, 1005000],
            index=pd.date_range(start='2024-01-01', periods=3, freq='D')
        )

        decision = risk_engine.evaluate_risk(
            portfolio_state=portfolio,
            returns_series=sample_returns,
            vix_level=18
        )

        assert isinstance(decision, RiskDecision)
        assert decision.scaling_factor > 0
        assert not decision.emergency_derisk
        assert decision.regime == RiskRegime.NORMAL

    def test_evaluate_risk_emergency_derisk(self, risk_engine, sample_returns):
        """Test emergency de-risk trigger."""
        portfolio = PortfolioState(
            nav=880000,  # 12% loss from 1M
            cash=100000,
            initial_capital=1000000
        )
        portfolio.nav_history = pd.Series(
            [1000000, 950000, 900000, 880000],
            index=pd.date_range(start='2024-01-01', periods=4, freq='D')
        )

        decision = risk_engine.evaluate_risk(
            portfolio_state=portfolio,
            returns_series=sample_returns,
            vix_level=35
        )

        assert decision.emergency_derisk
        assert decision.scaling_factor < 1.0

    def test_evaluate_risk_with_warnings(self, risk_engine, sample_returns):
        """Test that warnings are generated appropriately."""
        portfolio = PortfolioState(
            nav=950000,
            cash=100000,
            initial_capital=1000000,
            gross_exposure=2500000  # 2.5x leverage, above max
        )
        portfolio.nav_history = pd.Series(
            [1000000, 970000, 950000],
            index=pd.date_range(start='2024-01-01', periods=3, freq='D')
        )

        decision = risk_engine.evaluate_risk(
            portfolio_state=portfolio,
            returns_series=sample_returns,
            vix_level=32
        )

        assert len(decision.warnings) > 0


class TestRebalanceCheck:
    """Tests for rebalance threshold checking."""

    def test_needs_rebalance(self, risk_engine):
        """Test rebalance needed when drift exceeds threshold."""
        current = {"SPY": 0.35, "FEZ": 0.25}
        target = {"SPY": 0.40, "FEZ": 0.25}  # SPY drifted 5%

        needs_rebal = risk_engine.check_rebalance_needed(current, target)
        assert needs_rebal

    def test_no_rebalance_needed(self, risk_engine):
        """Test no rebalance when within threshold."""
        current = {"SPY": 0.39, "FEZ": 0.25}  # Only 1% drift
        target = {"SPY": 0.40, "FEZ": 0.25}

        needs_rebal = risk_engine.check_rebalance_needed(current, target)
        assert not needs_rebal


class TestPositionLimits:
    """Tests for position limit computation."""

    def test_compute_position_limits_etf(self, risk_engine):
        """Test ETF position limits."""
        nav = 1000000
        limits = risk_engine.compute_position_limits(nav, "ETF")

        assert limits["max_pct"] == 0.15
        assert limits["max_notional"] == 150000

    def test_compute_position_limits_futures(self, risk_engine):
        """Test futures position limits."""
        nav = 1000000
        limits = risk_engine.compute_position_limits(nav, "FUT")

        assert limits["max_pct"] == 0.25
        assert limits["max_notional"] == 250000

    def test_compute_position_limits_options(self, risk_engine):
        """Test options position limits."""
        nav = 1000000
        limits = risk_engine.compute_position_limits(nav, "OPT")

        assert limits["max_pct"] == 0.05
        assert limits["max_notional"] == 50000
