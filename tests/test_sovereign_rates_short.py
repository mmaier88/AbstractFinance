"""
Unit tests for EU Sovereign Fragility Short sleeve (v3.0).

Tests cover:
- Fragmentation signal calculation
- Target weight sizing
- DV01-neutral position construction
- Kill-switches (hard and soft)
- Take-profit rules
- Deflation guard logic
"""

import pytest
import pandas as pd
import numpy as np
from datetime import date, datetime, timedelta
from unittest.mock import Mock, MagicMock

from src.sovereign_rates_short import (
    SovereignRatesShortEngine,
    SovereignRatesShortConfig,
    FragmentationSignal,
    SizingResult,
    DV01Position,
    KillSwitchType,
    SleeveState,
    create_sovereign_rates_short_engine,
)
from src.risk_engine import RiskRegime
from src.portfolio import PortfolioState, Sleeve


class TestFragmentationSignal:
    """Tests for fragmentation signal calculation."""

    def test_spread_calculation(self):
        """Test BTP-Bund spread calculation."""
        engine = SovereignRatesShortEngine()

        # BTP yield = 4.5%, Bund yield = 2.5% -> spread = 200bps
        signal = engine.compute_fragmentation_signal(
            btp_yield=4.5,
            bund_yield=2.5,
            vix_level=20.0,
            stress_score=0.3,
        )

        assert signal.spread_bps == 200.0
        assert signal.vix_level == 20.0
        assert signal.stress_score == 0.3

    def test_spread_z_score_with_history(self):
        """Test spread z-score calculation with price history."""
        engine = SovereignRatesShortEngine()

        # Build up history
        for i in range(30):
            day = date.today() - timedelta(days=30-i)
            # Simulate stable spread around 150bps
            btp = 4.0 + np.random.normal(0, 0.05)
            bund = 2.5 + np.random.normal(0, 0.02)
            engine.update_yield_history(btp, bund, day)

        # Now test with a wider spread (should have positive z)
        signal = engine.compute_fragmentation_signal(
            btp_yield=4.5,  # Higher than historical
            bund_yield=2.3,  # Lower than historical
            vix_level=20.0,
            stress_score=0.3,
        )

        # Spread is wider than historical average, so z should be positive
        assert signal.spread_z > 0

    def test_deflation_guard_triggers(self):
        """Test deflation guard conditions."""
        engine = SovereignRatesShortEngine()

        # Scenario: High VIX + rates falling = deflation guard
        signal = FragmentationSignal(
            spread_bps=200.0,
            spread_z=0.5,
            spread_mom_20d=10.0,
            bund_yield_mom_60d=-50.0,  # Rates falling
            bund_yield_change_5d=-35.0,  # Sharp drop
            bund_yield_mom_20d=-45.0,  # Momentum down
            vix_level=35.0,  # High VIX
            stress_score=0.8,  # High stress
        )

        assert signal.risk_off is True
        assert signal.rates_down_shock is True
        assert signal.deflation_guard is True

    def test_no_deflation_guard_normal_conditions(self):
        """Test no deflation guard in normal conditions."""
        signal = FragmentationSignal(
            spread_bps=200.0,
            spread_z=0.5,
            spread_mom_20d=10.0,
            bund_yield_mom_60d=20.0,  # Rates rising
            bund_yield_change_5d=5.0,
            bund_yield_mom_20d=15.0,
            vix_level=18.0,  # Low VIX
            stress_score=0.2,  # Low stress
        )

        assert signal.risk_off is False
        assert signal.rates_down_shock is False
        assert signal.deflation_guard is False


class TestSizingRules:
    """Tests for deterministic sizing rules."""

    def test_base_weights_by_regime(self):
        """Test base weights for each regime."""
        engine = SovereignRatesShortEngine()

        # Create signal for normal conditions
        signal = FragmentationSignal(
            spread_bps=200.0,
            spread_z=0.5,  # Moderate
            spread_mom_20d=10.0,
            bund_yield_mom_60d=20.0,  # Rates stable/up
            bund_yield_change_5d=5.0,
            bund_yield_mom_20d=10.0,
            vix_level=18.0,
            stress_score=0.2,
        )

        # Test NORMAL regime
        result_normal = engine.compute_target_weight(signal, RiskRegime.NORMAL, 1000000)
        assert result_normal.base_weight == 0.06
        assert result_normal.target_weight <= 0.10  # Max for normal

        # Test ELEVATED regime
        result_elevated = engine.compute_target_weight(signal, RiskRegime.ELEVATED, 1000000)
        assert result_elevated.base_weight == 0.12
        assert result_elevated.target_weight <= 0.16  # Max for elevated

        # Test CRISIS regime
        result_crisis = engine.compute_target_weight(signal, RiskRegime.CRISIS, 1000000)
        assert result_crisis.base_weight == 0.16
        assert result_crisis.target_weight <= 0.20  # Max for crisis

    def test_fragmentation_multiplier(self):
        """Test fragmentation multiplier based on spread z-score."""
        engine = SovereignRatesShortEngine()

        # Create base signal
        base_signal_params = {
            'spread_bps': 200.0,
            'spread_mom_20d': 10.0,
            'bund_yield_mom_60d': 20.0,
            'bund_yield_change_5d': 5.0,
            'bund_yield_mom_20d': 10.0,
            'vix_level': 18.0,
            'stress_score': 0.2,
        }

        # Test z < 0 -> 0.5x multiplier
        signal_low_z = FragmentationSignal(spread_z=-0.5, **base_signal_params)
        result_low = engine.compute_target_weight(signal_low_z, RiskRegime.NORMAL, 1000000)
        assert result_low.frag_multiplier == 0.5

        # Test 0 <= z < 1 -> 1.0x multiplier
        signal_mid_z = FragmentationSignal(spread_z=0.5, **base_signal_params)
        result_mid = engine.compute_target_weight(signal_mid_z, RiskRegime.NORMAL, 1000000)
        assert result_mid.frag_multiplier == 1.0

        # Test 1 <= z < 2 -> 1.3x multiplier
        signal_high_z = FragmentationSignal(spread_z=1.5, **base_signal_params)
        result_high = engine.compute_target_weight(signal_high_z, RiskRegime.NORMAL, 1000000)
        assert result_high.frag_multiplier == 1.3

        # Test z >= 2 -> 1.6x multiplier
        signal_very_high_z = FragmentationSignal(spread_z=2.5, **base_signal_params)
        result_very_high = engine.compute_target_weight(signal_very_high_z, RiskRegime.NORMAL, 1000000)
        assert result_very_high.frag_multiplier == 1.6

    def test_deflation_guard_zeroes_weight(self):
        """Test that deflation guard sets target weight to zero."""
        engine = SovereignRatesShortEngine()

        # High VIX + rates falling = deflation guard
        signal = FragmentationSignal(
            spread_bps=200.0,
            spread_z=1.5,  # Even with high spread z
            spread_mom_20d=10.0,
            bund_yield_mom_60d=-50.0,
            bund_yield_change_5d=-35.0,
            bund_yield_mom_20d=-45.0,
            vix_level=35.0,
            stress_score=0.8,
        )

        result = engine.compute_target_weight(signal, RiskRegime.CRISIS, 1000000)

        assert result.target_weight == 0.0
        assert result.deflation_guard is True


class TestDV01Position:
    """Tests for DV01-neutral position construction."""

    def test_dv01_neutral_construction(self):
        """Test that positions are DV01-neutral."""
        engine = SovereignRatesShortEngine()

        # 10% weight, $1M NAV
        position = engine.compute_dv01_position(
            target_weight=0.10,
            nav=1000000,
            use_etf_fallback=False
        )

        # Check that position is neutral (within 5%)
        assert position.is_neutral

        # BTP should be short (negative)
        assert position.btp_contracts < 0

        # Bund should be long (positive)
        assert position.bund_contracts > 0

    def test_zero_weight_gives_zero_contracts(self):
        """Test that zero weight gives zero contracts."""
        engine = SovereignRatesShortEngine()

        position = engine.compute_dv01_position(
            target_weight=0.0,
            nav=1000000,
            use_etf_fallback=False
        )

        assert position.btp_contracts == 0
        assert position.bund_contracts == 0
        assert position.target_dv01 == 0.0

    def test_etf_fallback_mode(self):
        """Test ETF fallback mode."""
        engine = SovereignRatesShortEngine()

        position = engine.compute_dv01_position(
            target_weight=0.10,
            nav=1000000,
            use_etf_fallback=True
        )

        # Should still produce contracts
        assert position.btp_contracts != 0 or position.bund_contracts != 0


class TestKillSwitches:
    """Tests for kill-switch logic."""

    def test_soft_kill_on_spread_compression(self):
        """Test soft kill when spreads compress strongly."""
        engine = SovereignRatesShortEngine()

        signal = FragmentationSignal(
            spread_bps=100.0,
            spread_z=-0.7,  # Below soft kill threshold (-0.5)
            spread_mom_20d=-30.0,
            bund_yield_mom_60d=10.0,
            bund_yield_change_5d=5.0,
            bund_yield_mom_20d=10.0,
            vix_level=18.0,
            stress_score=0.2,
        )

        result = engine.compute_target_weight(signal, RiskRegime.NORMAL, 1000000)

        # Soft kill should reduce by 50%
        assert result.soft_kill is True
        # Target weight should be reduced
        base_without_soft_kill = result.base_weight * result.frag_multiplier * result.rates_multiplier
        assert result.target_weight < base_without_soft_kill


class TestTakeProfit:
    """Tests for take-profit rules."""

    def test_take_profit_on_high_spread_z(self):
        """Test take-profit triggers on high spread z-score."""
        engine = SovereignRatesShortEngine()

        # Set up entry state
        engine._tracker.entry_date = date.today() - timedelta(days=10)
        engine._tracker.entry_spread_avg_bps = 150.0

        signal = FragmentationSignal(
            spread_bps=280.0,
            spread_z=2.6,  # Above take-profit threshold (2.5)
            spread_mom_20d=50.0,
            bund_yield_mom_60d=30.0,
            bund_yield_change_5d=10.0,
            bund_yield_mom_20d=20.0,
            vix_level=22.0,
            stress_score=0.4,
        )

        should_take, take_pct, reason = engine.check_take_profit(signal)

        assert should_take is True
        assert take_pct == 0.50  # 50% take
        assert "z-score" in reason.lower()

    def test_take_profit_on_spread_widening(self):
        """Test take-profit triggers on spread widening from entry."""
        engine = SovereignRatesShortEngine()

        # Set up entry state
        engine._tracker.entry_date = date.today() - timedelta(days=10)
        engine._tracker.entry_spread_avg_bps = 150.0

        signal = FragmentationSignal(
            spread_bps=280.0,  # 130bps widening (> 120bps threshold)
            spread_z=1.8,  # Below z threshold
            spread_mom_20d=50.0,
            bund_yield_mom_60d=30.0,
            bund_yield_change_5d=10.0,
            bund_yield_mom_20d=20.0,
            vix_level=22.0,
            stress_score=0.4,
        )

        should_take, take_pct, reason = engine.check_take_profit(signal)

        assert should_take is True
        assert take_pct == 0.50
        assert "widening" in reason.lower()


class TestConfigFromSettings:
    """Tests for configuration loading."""

    def test_default_config(self):
        """Test default configuration values."""
        config = SovereignRatesShortConfig()

        assert config.enabled is True
        assert config.target_weight_pct == 0.12
        assert config.dv01_budget_per_nav == 0.0007
        assert config.btp_symbol == "FBTP"
        assert config.bund_symbol == "FGBL"

    def test_config_from_settings(self):
        """Test configuration loading from settings dict."""
        settings = {
            'sovereign_rates_short': {
                'enabled': True,
                'target_weight_pct': 0.15,
                'base_weights': {
                    'normal': 0.08,
                    'elevated': 0.14,
                    'crisis': 0.18,
                },
                'dv01_budget_per_nav': 0.0008,
            }
        }

        config = SovereignRatesShortConfig.from_settings(settings)

        assert config.target_weight_pct == 0.15
        assert config.base_weights['normal'] == 0.08
        assert config.dv01_budget_per_nav == 0.0008


class TestEngineFactory:
    """Tests for engine factory function."""

    def test_create_engine(self):
        """Test factory function creates engine."""
        settings = {
            'sovereign_rates_short': {
                'enabled': True,
            }
        }

        engine = create_sovereign_rates_short_engine(settings)

        assert engine is not None
        assert isinstance(engine, SovereignRatesShortEngine)
        assert engine.config.enabled is True


class TestEngineSummary:
    """Tests for engine summary output."""

    def test_summary_structure(self):
        """Test that summary has expected structure."""
        engine = SovereignRatesShortEngine()

        summary = engine.get_summary()

        assert 'enabled' in summary
        assert 'state' in summary
        assert 'config' in summary
        assert summary['enabled'] is True
        assert summary['state'] == 'active'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
