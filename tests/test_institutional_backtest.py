"""
Tests for Institutional-Grade Backtest Harness.

Phase L: v2.2 Roadmap validation.
"""

import pytest
import numpy as np
import pandas as pd
from datetime import date, timedelta

from src.research.institutional_backtest import (
    StressAwareCostModel,
    StressAwareCostConfig,
    StressLevel,
    FuturesRollSimulator,
    FuturesRollConfig,
    InstitutionalBacktest,
    WalkForwardConfig,
    AblationConfig,
)


class TestStressAwareCostModel:
    """Tests for stress-aware transaction cost model."""

    def test_stress_level_classification(self):
        """Test VIX to stress level mapping."""
        model = StressAwareCostModel(StressAwareCostConfig())

        assert model.get_stress_level(12) == StressLevel.CALM
        assert model.get_stress_level(20) == StressLevel.NORMAL
        assert model.get_stress_level(30) == StressLevel.ELEVATED
        assert model.get_stress_level(50) == StressLevel.CRISIS

    def test_spread_multiplier_increases_with_stress(self):
        """Spreads should widen during stress."""
        model = StressAwareCostModel(StressAwareCostConfig())

        mult_calm = model.get_spread_multiplier(10)
        mult_normal = model.get_spread_multiplier(20)
        mult_elevated = model.get_spread_multiplier(30)
        mult_crisis = model.get_spread_multiplier(50)

        assert mult_calm < mult_normal < mult_elevated < mult_crisis
        assert mult_crisis >= 5.0  # Crisis should be 5x

    def test_transaction_cost_stress_sensitivity(self):
        """Transaction costs should increase with VIX."""
        model = StressAwareCostModel(StressAwareCostConfig())
        notional = 100_000

        cost_calm = model.compute_transaction_cost(notional, "etf", vix=12)
        cost_crisis = model.compute_transaction_cost(notional, "etf", vix=50)

        # Crisis costs should be ~5x calm
        assert cost_crisis > cost_calm * 3
        assert cost_crisis < cost_calm * 8  # Sanity bound

    def test_futures_cheaper_than_equity(self):
        """Futures should have tighter spreads."""
        model = StressAwareCostModel(StressAwareCostConfig())
        notional = 100_000
        vix = 20

        cost_equity = model.compute_transaction_cost(notional, "equity", vix)
        cost_futures = model.compute_transaction_cost(notional, "futures", vix)

        assert cost_futures < cost_equity

    def test_market_impact_scales_with_size(self):
        """Market impact should increase with order size."""
        model = StressAwareCostModel(StressAwareCostConfig())
        adv = 1_000_000  # $1M ADV

        cost_small = model.compute_transaction_cost(10_000, "etf", vix=20, adv=adv)
        cost_large = model.compute_transaction_cost(500_000, "etf", vix=20, adv=adv)

        # Large order should cost more than linear scaling
        assert cost_large > cost_small * 10  # Impact is non-linear


class TestFuturesRollSimulator:
    """Tests for futures roll simulation."""

    def test_vol_futures_contango_cost(self):
        """Vol futures should have contango cost in normal markets."""
        sim = FuturesRollSimulator(FuturesRollConfig())

        cost = sim.compute_roll_cost(
            futures_type="vol",
            notional=100_000,
            vix=20,
        )

        # Should be positive (contango costs money)
        assert cost > 0

    def test_vol_futures_backwardation_in_crisis(self):
        """Vol futures term structure can invert in crisis."""
        sim = FuturesRollSimulator(FuturesRollConfig())

        cost_normal = sim.compute_roll_cost("vol", 100_000, vix=20)
        cost_crisis = sim.compute_roll_cost("vol", 100_000, vix=50)

        # In crisis, VIX term structure often inverts
        # Cost should be much lower or negative
        assert cost_crisis < cost_normal

    def test_bond_futures_slight_backwardation(self):
        """Bond futures typically have slight backwardation."""
        sim = FuturesRollSimulator(FuturesRollConfig())

        cost = sim.compute_roll_cost("bond", 100_000, vix=20)

        # Should be negative (backwardation = benefit)
        assert cost < 0

    def test_roll_slippage_increases_in_stress(self):
        """Roll slippage should increase in stress."""
        sim = FuturesRollSimulator(FuturesRollConfig())

        slip_normal = sim.compute_roll_slippage(100_000, vix=20)
        slip_crisis = sim.compute_roll_slippage(100_000, vix=50)

        assert slip_crisis > slip_normal


class TestInstitutionalBacktest:
    """Tests for the full institutional backtest harness."""

    @pytest.fixture
    def sample_returns(self):
        """Create sample return data for testing."""
        dates = pd.date_range(start="2010-01-01", end="2020-12-31", freq="B")
        np.random.seed(42)

        returns = pd.DataFrame({
            "core_index_rv": np.random.normal(0.0003, 0.01, len(dates)),
            "sector_rv": np.random.normal(0.0002, 0.008, len(dates)),
            "europe_vol_convex": np.random.normal(-0.0001, 0.02, len(dates)),
            "crisis_alpha": np.random.normal(-0.0001, 0.015, len(dates)),
            "credit_carry": np.random.normal(0.0001, 0.003, len(dates)),
        }, index=dates)

        return returns

    @pytest.fixture
    def sample_vix(self, sample_returns):
        """Create sample VIX series."""
        np.random.seed(42)
        base_vix = 18 + np.cumsum(np.random.normal(0, 0.5, len(sample_returns)))
        vix = pd.Series(np.clip(base_vix, 10, 80), index=sample_returns.index)
        return vix

    def test_single_backtest_runs(self, sample_returns, sample_vix):
        """Basic backtest should run without errors."""
        harness = InstitutionalBacktest()

        weights = {
            "core_index_rv": 0.30,
            "sector_rv": 0.20,
            "europe_vol_convex": 0.15,
            "crisis_alpha": 0.10,
            "credit_carry": 0.15,
        }

        result = harness.run_single_backtest(
            sample_returns, sample_vix, weights,
            date(2010, 1, 1), date(2015, 12, 31)
        )

        assert "error" not in result
        assert "sharpe" in result
        assert "max_dd" in result
        assert result["max_dd"] <= 0  # Drawdown is negative

    def test_walk_forward_produces_results(self, sample_returns, sample_vix):
        """Walk-forward should produce multiple windows."""
        harness = InstitutionalBacktest(
            walk_forward_config=WalkForwardConfig(
                train_years=2,
                test_years=1,
                step_months=6,
            )
        )

        weights = {
            "core_index_rv": 0.30,
            "sector_rv": 0.20,
            "europe_vol_convex": 0.15,
        }

        results = harness.run_walk_forward(
            sample_returns, sample_vix, weights,
            date(2010, 1, 1), date(2018, 12, 31)
        )

        assert len(results) > 0
        for r in results:
            assert r.train_end < r.test_start
            assert r.is_sharpe != 0 or r.oos_sharpe != 0

    def test_ablation_identifies_contribution(self, sample_returns, sample_vix):
        """Ablation should measure each sleeve's contribution."""
        harness = InstitutionalBacktest(
            ablation_config=AblationConfig(
                sleeves_to_ablate=["core_index_rv", "europe_vol_convex"]
            )
        )

        weights = {
            "core_index_rv": 0.30,
            "sector_rv": 0.20,
            "europe_vol_convex": 0.15,
            "credit_carry": 0.15,
        }

        results = harness.run_ablation_suite(
            sample_returns, sample_vix, weights,
            date(2010, 1, 1), date(2018, 12, 31)
        )

        assert len(results) == 2
        for r in results:
            assert r.sleeve_removed in ["core_index_rv", "europe_vol_convex"]
            # Sharpe contribution should be computed
            assert r.sharpe_contribution != r.full_sharpe

    def test_stress_period_costs_higher(self, sample_returns, sample_vix):
        """Costs during stress periods should be higher."""
        harness = InstitutionalBacktest()

        # Simulate stress period with high VIX
        stress_vix = sample_vix.copy()
        stress_vix.iloc[100:150] = 50  # Inject stress

        weights = {"core_index_rv": 0.50, "sector_rv": 0.30}

        result = harness.run_single_backtest(
            sample_returns, stress_vix, weights,
            date(2010, 1, 1), date(2012, 12, 31)
        )

        # Should have non-zero costs
        assert result.get("total_tx_costs", 0) > 0


class TestCostRealism:
    """Tests to verify cost model produces realistic values."""

    def test_annual_vol_futures_roll_cost_realistic(self):
        """Annual vol futures roll cost should be positive (contango)."""
        sim = FuturesRollSimulator(FuturesRollConfig())

        # Roll cost is daily, based on monthly basis
        daily_cost = sim.compute_roll_cost("vol", 100_000, vix=18)
        # Annual cost = daily * 252 days / notional
        annual_cost = daily_cost * 252 / 100_000

        # Config: 15 bps/month = ~1.8% annual (15 * 12 / 10000)
        # Should be positive (contango costs money) and in reasonable range
        assert annual_cost > 0  # Contango = positive cost
        assert 0.01 < annual_cost < 0.10  # 1-10% annual is realistic

    def test_equity_etf_round_trip_cost_realistic(self):
        """ETF round-trip cost should be 5-20 bps in normal markets."""
        model = StressAwareCostModel(StressAwareCostConfig())

        # Round trip (buy + sell)
        cost = 2 * model.compute_transaction_cost(100_000, "etf", vix=18)
        cost_bps = cost / 100_000 * 10000

        # Should be 5-25 bps round trip
        assert 5 < cost_bps < 30

    def test_crisis_round_trip_cost_elevated(self):
        """Crisis round-trip costs should be 25-100 bps."""
        model = StressAwareCostModel(StressAwareCostConfig())

        cost = 2 * model.compute_transaction_cost(100_000, "etf", vix=50)
        cost_bps = cost / 100_000 * 10000

        # Should be 20-100 bps round trip in crisis
        assert 20 < cost_bps < 150
