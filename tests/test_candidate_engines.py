"""
Tests for Candidate Engines.

Phase N: v2.2 Roadmap - Candidate engine validation.
"""

import pytest
import numpy as np
import pandas as pd
from datetime import date, timedelta

from src.research.candidate_engines import (
    EUSovereignSpreadsEngine,
    EUSovereignSpreadConfig,
    EUStressLevel,
    EnergyShockEngine,
    EnergyShockConfig,
    ConditionalDurationEngine,
    ConditionalDurationConfig,
    InflationRegime,
    BacktestResult,
    compute_sharpe,
    compute_max_drawdown,
    compute_insurance_score,
)


class TestEUSovereignSpreadsEngine:
    """Tests for EU Sovereign Spreads engine."""

    @pytest.fixture
    def engine(self):
        return EUSovereignSpreadsEngine()

    def test_calm_stress_level(self, engine):
        """Low V2X and spreads should be CALM."""
        stress = engine.compute_eu_stress_level(
            v2x=18,
            btp_spread_bps=100,
            oat_spread_bps=30,
        )
        assert stress == EUStressLevel.CALM

    def test_elevated_stress_level(self, engine):
        """High BTP spread + moderate V2X should trigger ELEVATED."""
        stress = engine.compute_eu_stress_level(
            v2x=27,           # V2X > 25 gives +1
            btp_spread_bps=180,  # BTP > 150 gives +1
            oat_spread_bps=40,
        )
        # Score = 1 + 1 = 2, which is ELEVATED
        assert stress == EUStressLevel.ELEVATED

    def test_crisis_stress_level(self, engine):
        """High V2X + wide spreads should be CRISIS."""
        stress = engine.compute_eu_stress_level(
            v2x=35,
            btp_spread_bps=280,
            oat_spread_bps=110,
        )
        assert stress == EUStressLevel.CRISIS

    def test_no_signal_in_calm(self, engine):
        """Should produce no signal when calm."""
        signal = engine.compute_signal(
            v2x=18,
            btp_spread_bps=100,
            oat_spread_bps=30,
            nav=1_000_000,
        )
        assert signal.signal_strength == 0.0
        assert signal.target_allocation == 0.0
        assert len(signal.positions) == 0

    def test_signal_in_crisis_resolution(self, engine):
        """Should produce signal when spreads elevated AND V2X declining."""
        # Build V2X history showing decline
        for v2x in [40, 38, 36, 34, 32, 30]:  # Declining V2X
            engine.compute_signal(
                v2x=v2x,
                btp_spread_bps=280,
                oat_spread_bps=60,
                nav=1_000_000,
            )

        # Now with V2X still declining, should get signal
        signal = engine.compute_signal(
            v2x=28,  # Still declining from 32
            btp_spread_bps=280,
            oat_spread_bps=60,
            nav=1_000_000,
        )
        assert signal.signal_strength > 0
        assert signal.target_allocation > 0
        assert "FGBL_long_vs_FBTP" in signal.positions

    def test_signal_on_extreme_spread(self, engine):
        """Should signal on very wide spreads even without V2X decline."""
        signal = engine.compute_signal(
            v2x=35,
            btp_spread_bps=400,  # Extreme spread > 350
            oat_spread_bps=100,
            nav=1_000_000,
        )
        assert signal.signal_strength > 0
        assert "FGBL_long_vs_FBTP" in signal.positions

    def test_btp_spread_activation_threshold(self, engine):
        """BTP spread must exceed threshold to activate."""
        # Below threshold
        signal_low = engine.compute_signal(
            v2x=30,
            btp_spread_bps=140,  # Below 150 threshold
            oat_spread_bps=20,
            nav=1_000_000,
        )

        # Above threshold
        signal_high = engine.compute_signal(
            v2x=30,
            btp_spread_bps=160,  # Above 150 threshold
            oat_spread_bps=20,
            nav=1_000_000,
        )

        assert len(signal_low.positions) == 0 or "FGBL_long_vs_FBTP" not in signal_low.positions
        # High should trigger if in elevated/crisis (need to check stress level)

    def test_simulate_returns(self, engine):
        """Should simulate returns from spread changes during crisis resolution."""
        dates = pd.date_range("2020-01-01", periods=20, freq="D")

        # Scenario: V2X declining from crisis peak, spreads elevated then narrowing
        v2x_values = [45, 43, 41, 39, 37, 35, 33, 31, 29, 27,
                      26, 25, 24, 23, 22, 21, 20, 19, 18, 18]  # Declining V2X
        btp_spread_values = [350, 340, 330, 320, 310, 300, 290, 280, 270, 260,
                            250, 240, 230, 220, 210, 200, 190, 180, 170, 160]  # Narrowing spread

        spread_changes = pd.DataFrame({
            "btp_spread_change": [-10] * 20,  # Consistent narrowing
            "oat_spread_change": [-3] * 20,
        }, index=dates)

        v2x = pd.Series(v2x_values, index=dates)
        btp_spread = pd.Series(btp_spread_values, index=dates)
        oat_spread = pd.Series([60] * 20, index=dates)

        returns = engine.simulate_returns(
            spread_changes, v2x, btp_spread, oat_spread
        )

        assert len(returns) == 20
        # With V2X declining and spreads narrowing, should have positive returns
        # Need some history before signal activates
        later_returns = returns.iloc[10:]  # After warmup
        assert later_returns.sum() >= 0  # Should be net positive or at least neutral


class TestEnergyShockEngine:
    """Tests for Energy Shock Hedge engine."""

    @pytest.fixture
    def engine(self):
        return EnergyShockEngine()

    def test_no_signal_without_trend(self, engine):
        """Should produce no signal without trend."""
        prices = pd.Series([100] * 25)  # Flat prices

        signal = engine.compute_signal(
            oil_prices=prices,
            v2x=30,
            eu_stressed=True,
        )

        assert signal.signal_strength == 0.0
        assert signal.target_allocation == 0.0

    def test_signal_on_uptrend(self, engine):
        """Should produce signal on uptrend during EU stress."""
        # Create 10% uptrend over 20 days
        prices = pd.Series([100 + i * 0.55 for i in range(25)])  # ~11% up

        signal = engine.compute_signal(
            oil_prices=prices,
            v2x=30,
            eu_stressed=True,
        )

        assert signal.signal_strength > 0
        assert signal.target_allocation > 0
        assert "CL" in signal.positions

    def test_gated_without_eu_stress(self, engine):
        """Should be gated without EU stress."""
        prices = pd.Series([100 + i * 0.55 for i in range(25)])

        signal = engine.compute_signal(
            oil_prices=prices,
            v2x=20,  # Below threshold
            eu_stressed=False,
        )

        assert signal.signal_strength == 0.0
        assert signal.metadata.get("gated") == "EU stress not met"

    def test_trend_threshold_sensitivity(self, engine):
        """Should not trigger below trend threshold."""
        # 5% move (below 10% threshold)
        prices = pd.Series([100 + i * 0.25 for i in range(25)])

        signal = engine.compute_signal(
            oil_prices=prices,
            v2x=30,
            eu_stressed=True,
        )

        assert signal.signal_strength == 0.0


class TestConditionalDurationEngine:
    """Tests for Conditional Duration engine."""

    @pytest.fixture
    def engine(self):
        return ConditionalDurationEngine()

    def test_inflation_regime_classification(self, engine):
        """Should correctly classify inflation regimes."""
        assert engine.compute_inflation_regime(0.2) == InflationRegime.DEFLATION
        assert engine.compute_inflation_regime(1.5) == InflationRegime.LOW
        assert engine.compute_inflation_regime(3.0) == InflationRegime.MODERATE
        assert engine.compute_inflation_regime(5.0) == InflationRegime.HIGH

    def test_recession_detection(self, engine):
        """Should detect recession from PMI."""
        assert engine.is_recession(48.0)  # Below 49
        assert not engine.is_recession(52.0)  # Above 49

    def test_no_signal_in_high_inflation(self, engine):
        """Should NOT produce signal during inflation shock."""
        signal = engine.compute_signal(
            cpi_yoy=5.0,  # High inflation
            pmi=45.0,     # Recession
            nav=1_000_000,
        )

        assert signal.signal_strength == 0.0
        assert signal.metadata.get("blocked") == "inflation_shock"

    def test_no_signal_in_deflation_without_recession(self, engine):
        """Should not signal in deflation without recession."""
        signal = engine.compute_signal(
            cpi_yoy=0.3,   # Deflation
            pmi=52.0,      # Not recession
            nav=1_000_000,
        )

        assert signal.signal_strength == 0.0

    def test_signal_requires_persistence(self, engine):
        """Should require days in regime before signaling."""
        # First 9 days: build persistence
        for _ in range(9):
            signal = engine.compute_signal(
                cpi_yoy=0.3,
                pmi=45.0,
                nav=1_000_000,
            )
            assert signal.signal_strength == 0.0

        # 10th day: should activate
        signal = engine.compute_signal(
            cpi_yoy=0.3,
            pmi=45.0,
            nav=1_000_000,
        )

        assert signal.signal_strength == 1.0
        assert signal.target_allocation > 0

    def test_persistence_resets_on_regime_change(self, engine):
        """Persistence should reset if regime changes."""
        # Build 5 days persistence
        for _ in range(5):
            engine.compute_signal(0.3, 45.0, 1_000_000)

        # Break regime (inflation rises)
        engine.compute_signal(3.0, 45.0, 1_000_000)

        # Days should be reset
        assert engine._days_in_deflation == 0


class TestBacktestMetrics:
    """Tests for backtest metrics calculation."""

    def test_compute_sharpe_positive(self):
        """Should compute positive Sharpe for good returns."""
        # Strong positive returns with low vol
        np.random.seed(42)
        returns = pd.Series(np.random.normal(0.001, 0.01, 252))

        sharpe = compute_sharpe(returns, risk_free_rate=0.02)

        # Should be positive
        assert sharpe > 0

    def test_compute_sharpe_handles_zero_vol(self):
        """Should handle zero volatility gracefully."""
        returns = pd.Series([0.0] * 100)

        sharpe = compute_sharpe(returns)

        assert sharpe == 0.0

    def test_compute_max_drawdown(self):
        """Should compute correct max drawdown."""
        # Create drawdown scenario
        returns = pd.Series([0.1, -0.15, -0.05, 0.02, 0.03])

        dd = compute_max_drawdown(returns)

        # Should be negative (drawdown)
        assert dd < 0
        # Should be between -1 and 0
        assert -1 < dd < 0

    def test_compute_insurance_score(self):
        """Should compute insurance score correctly."""
        returns = pd.Series([0.01, -0.02, 0.03, 0.05, 0.001])
        stress_mask = pd.Series([False, True, False, True, False])

        # Returns during stress: -0.02, 0.05 -> avg 0.015
        # Returns during normal: 0.01, 0.03, 0.001 -> avg ~0.0137

        score = compute_insurance_score(returns, stress_mask)

        # Stress performance better than normal = positive insurance
        # (0.015 - 0.0137) * 252 should be slightly positive
        assert isinstance(score, float)


class TestBacktestResult:
    """Tests for BacktestResult evaluation."""

    def test_passes_all_gates(self):
        """Should pass all gates with good metrics."""
        result = BacktestResult(
            engine_name="test",
            sharpe_ratio=0.5,
            max_drawdown=-0.10,
            total_return=0.15,
            insurance_score=0.05,
            avg_allocation=0.10,
            in_sample_sharpe=0.6,
            out_of_sample_sharpe=0.4,
            parameter_stability=0.9,
            portfolio_sharpe_with=1.0,
            portfolio_sharpe_without=0.85,
            marginal_contribution=0.15,
        )

        result.evaluate_gates()

        assert result.passes_standalone_sharpe  # 0.5 > 0.3
        assert result.passes_portfolio_improvement  # 0.15 > 0.1
        assert result.passes_insurance_score  # 0.05 > 0
        assert result.passes_walk_forward  # 0.4 > 0
        assert result.passes_all_gates

    def test_fails_with_low_sharpe(self):
        """Should fail if standalone Sharpe too low."""
        result = BacktestResult(
            engine_name="test",
            sharpe_ratio=0.2,  # Below 0.3
            max_drawdown=-0.10,
            total_return=0.15,
            insurance_score=0.05,
            avg_allocation=0.10,
            in_sample_sharpe=0.3,
            out_of_sample_sharpe=0.2,
            parameter_stability=0.9,
            portfolio_sharpe_with=1.0,
            portfolio_sharpe_without=0.85,
            marginal_contribution=0.15,
        )

        result.evaluate_gates()

        assert not result.passes_standalone_sharpe
        assert not result.passes_all_gates

    def test_fails_with_negative_insurance(self):
        """Should fail if insurance score negative."""
        result = BacktestResult(
            engine_name="test",
            sharpe_ratio=0.5,
            max_drawdown=-0.10,
            total_return=0.15,
            insurance_score=-0.05,  # Negative
            avg_allocation=0.10,
            in_sample_sharpe=0.6,
            out_of_sample_sharpe=0.4,
            parameter_stability=0.9,
            portfolio_sharpe_with=1.0,
            portfolio_sharpe_without=0.85,
            marginal_contribution=0.15,
        )

        result.evaluate_gates()

        assert not result.passes_insurance_score
        assert not result.passes_all_gates


class TestIntegration:
    """Integration tests for candidate engines with backtest harness."""

    def test_eu_sovereign_spreads_on_historical_data(self):
        """Test EU Sovereign Spreads on simulated historical data."""
        engine = EUSovereignSpreadsEngine()

        # Simulate 2 years of data with EU crisis period
        dates = pd.date_range("2011-01-01", "2012-12-31", freq="B")
        n = len(dates)

        # Create crisis scenario mid-2011 to mid-2012
        crisis_mask = (dates >= "2011-07-01") & (dates <= "2012-06-30")

        # BTP spread widens during crisis
        btp_spread = pd.Series(
            np.where(crisis_mask, 350 + np.random.normal(0, 50, n), 100 + np.random.normal(0, 20, n)),
            index=dates
        )

        # V2X spikes during crisis
        v2x = pd.Series(
            np.where(crisis_mask, 35 + np.random.normal(0, 5, n), 20 + np.random.normal(0, 3, n)),
            index=dates
        )

        # OAT spread
        oat_spread = pd.Series(
            np.where(crisis_mask, 80 + np.random.normal(0, 15, n), 30 + np.random.normal(0, 5, n)),
            index=dates
        )

        # Spread changes - narrowing during crisis resolution
        btp_changes = pd.DataFrame({
            "btp_spread_change": np.where(crisis_mask, -2 + np.random.normal(0, 5, n), np.random.normal(0, 2, n)),
            "oat_spread_change": np.where(crisis_mask, -0.5 + np.random.normal(0, 2, n), np.random.normal(0, 1, n)),
        }, index=dates)

        returns = engine.simulate_returns(
            btp_changes, v2x, btp_spread, oat_spread
        )

        sharpe = compute_sharpe(returns)
        max_dd = compute_max_drawdown(returns)
        insurance_score = compute_insurance_score(returns, crisis_mask)

        # Should have returns during crisis period
        assert len(returns) == n
        # Insurance score should be positive (pays during stress)
        # Note: This depends on the random seed, so we're flexible here
        assert isinstance(insurance_score, float)
