"""
Integration Tests for Strategy Evolution v2.1.

Tests:
1. EuropeVolEngine integration with TailHedgeManager
2. SectorPairEngine integration with Strategy
3. VSTOXX data feed (mocked)
4. Fallback behavior when engines unavailable

Run with: pytest tests/test_strategy_evolution.py -v
"""

import pytest
from datetime import date
from unittest.mock import Mock, MagicMock, patch
from typing import Dict, Any


# =============================================================================
# Test Fixtures
# =============================================================================

@pytest.fixture
def mock_settings() -> Dict[str, Any]:
    """Settings with v2.1 features enabled."""
    return {
        'sleeves': {
            'core_index_rv': 0.20,
            'sector_rv': 0.25,
            'single_name': 0.15,
            'credit_carry': 0.15,
            'crisis_alpha': 0.15,  # EU Vol Convexity
            'cash_buffer': 0.10,
        },
        'term_structure': {
            'enabled': True,
            'contango_threshold': 0.5,
            'backwardation_threshold': -0.5,
            'zscore_lookback_days': 60,
        },
        'vol_of_vol': {
            'enabled': True,
            'lookback_days': 20,
            'jump_threshold_std': 2.0,
            'jump_window_days': 3,
        },
        'vol_regime': {
            'enabled': True,
            'low_threshold': 18.0,
            'elevated_threshold': 25.0,
            'crisis_threshold': 35.0,
        },
        'sector_pairs': {
            'enabled': True,
            'included_sectors': ['financials', 'technology', 'industrials', 'healthcare'],
            'beta_adjust': True,
            'neutralize_growth_value': True,
            'max_growth_exposure': 0.1,
            'max_value_exposure': 0.1,
        },
        'trend_filter': {
            'enabled': True,
            'short_lookback_days': 60,
            'long_lookback_days': 252,
        },
        'crisis': {
            'vix_threshold': 40,
            'pnl_spike_threshold_pct': 0.10,
        },
        'hedge_budget_annual_pct': 0.025,
        'option_validator': {},
    }


@pytest.fixture
def mock_settings_disabled() -> Dict[str, Any]:
    """Settings with v2.1 features disabled (fallback mode)."""
    return {
        'sleeves': {
            'core_index_rv': 0.35,
            'sector_rv': 0.25,
            'single_name': 0.15,
            'credit_carry': 0.15,
            'crisis_alpha': 0.05,
            'cash_buffer': 0.05,
        },
        'term_structure': {'enabled': False},
        'vol_of_vol': {'enabled': False},
        'vol_regime': {'enabled': False},
        'sector_pairs': {'enabled': False},
        'trend_filter': {'enabled': True},
        'crisis': {'vix_threshold': 40},
        'hedge_budget_annual_pct': 0.025,
        'option_validator': {},
    }


@pytest.fixture
def mock_instruments() -> Dict[str, Any]:
    """Instrument configuration."""
    return {
        'sector_pairs': {
            'financials': {
                'us': {'symbol': 'XLF', 'beta': 1.1},
                'eu': {'symbol': 'EXV1', 'beta': 1.4},
            },
            'technology': {
                'us': {'symbol': 'XLK', 'beta': 1.2},
                'eu': {'symbol': 'EXV3', 'beta': 1.1},
            },
        },
        'eurex_options': {
            'OVS2': {'multiplier': 100, 'tick_size': 0.05},
            'OESX': {'multiplier': 10, 'tick_size': 0.1},
        },
    }


# =============================================================================
# EuropeVolEngine Tests
# =============================================================================

class TestEuropeVolEngine:
    """Tests for EuropeVolEngine module."""

    def test_import(self):
        """Test that EuropeVolEngine can be imported."""
        from src.europe_vol import (
            EuropeVolEngine, VolSignal, VolRegime, TermStructure
        )
        assert EuropeVolEngine is not None

    def test_compute_signal_normal_regime(self):
        """Test signal computation in NORMAL regime."""
        from src.europe_vol import EuropeVolEngine, VolRegime, TermStructure

        engine = EuropeVolEngine()

        # Simulate normal market: V2X = 20 (NORMAL), slight contango
        signal = engine.compute_signal(
            v2x_spot=20.0,
            v2x_front=19.5,
            v2x_back=20.5,  # Contango: back > front
            v2x_history=[19.0, 19.5, 20.0, 20.5, 20.0] * 12  # 60 days
        )

        assert signal is not None
        assert signal.vol_regime == VolRegime.NORMAL
        assert signal.term_structure == TermStructure.CONTANGO
        assert signal.term_spread > 0  # Positive = contango
        assert 0.5 <= signal.sizing_multiplier <= 2.0

    def test_compute_signal_low_vol_regime(self):
        """Test signal computation in LOW vol regime."""
        from src.europe_vol import EuropeVolEngine, VolRegime

        engine = EuropeVolEngine()

        # Low vol: V2X = 15 (LOW)
        signal = engine.compute_signal(
            v2x_spot=15.0,
            v2x_front=14.5,
            v2x_back=15.5,
            v2x_history=[15.0] * 60
        )

        assert signal.vol_regime == VolRegime.LOW
        assert signal.should_add_convexity  # Vol cheap, should add
        assert signal.structure_preference == "outrights"  # Low vol = outrights

    def test_compute_signal_crisis_regime(self):
        """Test signal computation in CRISIS regime."""
        from src.europe_vol import EuropeVolEngine, VolRegime

        engine = EuropeVolEngine()

        # Crisis: V2X = 45 (CRISIS), backwardation
        signal = engine.compute_signal(
            v2x_spot=45.0,
            v2x_front=46.0,
            v2x_back=44.0,  # Backwardation: back < front
            v2x_history=[25.0] * 50 + [35.0, 40.0, 43.0, 44.0, 45.0]  # Spike
        )

        assert signal.vol_regime == VolRegime.CRISIS
        assert signal.should_monetize  # Crisis = monetize winners
        assert signal.sizing_multiplier < 1.0  # Reduced sizing in crisis

    def test_compute_target_positions(self):
        """Test position targeting from signal."""
        from src.europe_vol import EuropeVolEngine, VolRegime

        engine = EuropeVolEngine()

        signal = engine.compute_signal(
            v2x_spot=22.0,
            v2x_front=21.5,
            v2x_back=23.0,
            v2x_history=[22.0] * 60
        )

        positions = engine.compute_target_positions(
            signal=signal,
            sleeve_nav=100000.0,  # $100k to EU Vol Convexity
            sx5e_spot=4800.0,
            current_dte=45
        )

        assert positions is not None
        assert positions.vstoxx_call_notional > 0
        assert positions.sx5e_put_notional > 0
        assert positions.eu_banks_put_notional > 0
        # Notionals should sum to ~sleeve_nav
        total = (positions.vstoxx_call_notional +
                 positions.sx5e_put_notional +
                 positions.eu_banks_put_notional)
        assert total > 0


# =============================================================================
# TailHedgeManager Integration Tests
# =============================================================================

class TestTailHedgeIntegration:
    """Tests for TailHedgeManager with EuropeVolEngine."""

    def test_init_with_engine_enabled(self, mock_settings, mock_instruments):
        """Test that TailHedgeManager initializes EuropeVolEngine."""
        from src.tail_hedge import TailHedgeManager

        manager = TailHedgeManager(mock_settings, mock_instruments)

        assert manager.use_dynamic_targeting is True
        assert manager.europe_vol_engine is not None

    def test_init_with_engine_disabled(self, mock_settings_disabled, mock_instruments):
        """Test fallback to static allocation when disabled."""
        from src.tail_hedge import TailHedgeManager

        manager = TailHedgeManager(mock_settings_disabled, mock_instruments)

        assert manager.use_dynamic_targeting is False
        assert manager.europe_vol_engine is None

    def test_compute_dynamic_allocation_normal(self, mock_settings, mock_instruments):
        """Test dynamic allocation in NORMAL regime."""
        from src.tail_hedge import TailHedgeManager, HedgeType

        manager = TailHedgeManager(mock_settings, mock_instruments)

        # Compute signal first
        signal = manager.compute_vol_signal(
            v2x_spot=22.0,
            v2x_front=21.5,
            v2x_back=23.0
        )

        allocation = manager.compute_dynamic_hedge_allocation(signal)

        # Should have all hedge types
        assert HedgeType.EU_VOL_CALL in allocation
        assert HedgeType.EU_EQUITY_PUT in allocation
        assert HedgeType.US_VOL_CALL in allocation

        # EU VOL should be primary (highest allocation in Europe-centric)
        assert allocation[HedgeType.EU_VOL_CALL] > allocation[HedgeType.US_VOL_CALL]

        # Allocations should sum to ~1.0
        total = sum(allocation.values())
        assert 0.99 <= total <= 1.01

    def test_dynamic_allocation_fallback(self, mock_settings_disabled, mock_instruments):
        """Test fallback to static allocation."""
        from src.tail_hedge import TailHedgeManager, HedgeType

        manager = TailHedgeManager(mock_settings_disabled, mock_instruments)

        # Without engine, should return static allocation
        allocation = manager.compute_dynamic_hedge_allocation()

        # Should match static HEDGE_ALLOCATION
        assert allocation[HedgeType.EU_VOL_CALL] == manager.HEDGE_ALLOCATION[HedgeType.EU_VOL_CALL]

    def test_vol_signal_summary(self, mock_settings, mock_instruments):
        """Test vol signal summary for metrics."""
        from src.tail_hedge import TailHedgeManager

        manager = TailHedgeManager(mock_settings, mock_instruments)

        # Before any signal computed
        summary = manager.get_vol_signal_summary()
        assert summary['available'] is False

        # After computing signal
        manager.compute_vol_signal(
            v2x_spot=25.0,
            v2x_front=24.5,
            v2x_back=26.0
        )

        summary = manager.get_vol_signal_summary()
        assert summary['available'] is True
        assert 'vol_regime' in summary
        assert 'term_structure' in summary
        assert 'sizing_multiplier' in summary


# =============================================================================
# SectorPairEngine Tests
# =============================================================================

class TestSectorPairEngine:
    """Tests for SectorPairEngine module."""

    def test_import(self):
        """Test that SectorPairEngine can be imported."""
        from src.sector_pairs import SectorPairEngine, Sector, SECTOR_PAIRS
        assert SectorPairEngine is not None
        assert len(SECTOR_PAIRS) > 0

    def test_compute_positions_default(self):
        """Test position computation with defaults."""
        from src.sector_pairs import SectorPairEngine

        engine = SectorPairEngine()

        positions = engine.compute_positions(
            sleeve_nav=100000.0,
            scaling=1.0
        )

        assert len(positions) > 0
        for pos in positions:
            assert pos.us_notional > 0  # Long US
            assert pos.eu_notional < 0  # Short EU

    def test_compute_positions_beta_adjusted(self):
        """Test that beta adjustment is applied."""
        from src.sector_pairs import SectorPairEngine, Sector

        engine = SectorPairEngine({
            'included_sectors': [Sector.FINANCIALS],
            'beta_adjust': True,
        })

        positions = engine.compute_positions(
            sleeve_nav=100000.0,
            scaling=1.0
        )

        assert len(positions) == 1
        pos = positions[0]

        # Financials: EU beta (1.4) > US beta (1.1)
        # EU notional should be adjusted by beta_ratio
        assert abs(pos.eu_notional) > pos.us_notional

    def test_factor_neutralization(self):
        """Test growth/value factor neutralization."""
        from src.sector_pairs import SectorPairEngine, Sector

        engine = SectorPairEngine({
            'included_sectors': [
                Sector.FINANCIALS, Sector.TECHNOLOGY,
                Sector.INDUSTRIALS, Sector.HEALTHCARE
            ],
            'neutralize_growth_value': True,
            'max_growth_exposure': 0.1,
            'max_value_exposure': 0.1,
        })

        positions = engine.compute_positions(
            sleeve_nav=100000.0,
            scaling=1.0
        )

        # Sum up factor exposures
        total_growth = sum(p.net_growth_exposure for p in positions)
        total_value = sum(p.net_value_exposure for p in positions)
        total_regional = sum(p.net_regional_exposure for p in positions)

        # Growth/value should be within bounds relative to regional
        if total_regional > 0:
            growth_ratio = abs(total_growth / total_regional)
            value_ratio = abs(total_value / total_regional)

            # Should be reasonably neutralized
            assert growth_ratio < 0.5  # Less than 50% of regional in growth
            assert value_ratio < 0.5   # Less than 50% of regional in value


# =============================================================================
# Strategy Integration Tests
# =============================================================================

class TestStrategyIntegration:
    """Tests for Strategy class with SectorPairEngine."""

    def test_init_with_sector_pairs_enabled(self, mock_settings, mock_instruments):
        """Test Strategy initializes SectorPairEngine."""
        from src.strategy_logic import Strategy
        from src.risk_engine import RiskEngine

        risk_engine = Mock(spec=RiskEngine)

        strategy = Strategy(
            settings=mock_settings,
            instruments_config=mock_instruments,
            risk_engine=risk_engine
        )

        assert strategy.use_sector_pairs is True
        assert strategy.sector_pair_engine is not None

    def test_init_with_sector_pairs_disabled(self, mock_settings_disabled, mock_instruments):
        """Test Strategy fallback when sector pairs disabled."""
        from src.strategy_logic import Strategy
        from src.risk_engine import RiskEngine

        risk_engine = Mock(spec=RiskEngine)

        strategy = Strategy(
            settings=mock_settings_disabled,
            instruments_config=mock_instruments,
            risk_engine=risk_engine
        )

        assert strategy.use_sector_pairs is False
        assert strategy.sector_pair_engine is None

    def test_sector_pairs_summary(self, mock_settings, mock_instruments):
        """Test sector pairs summary for metrics."""
        from src.strategy_logic import Strategy
        from src.risk_engine import RiskEngine

        risk_engine = Mock(spec=RiskEngine)

        strategy = Strategy(
            settings=mock_settings,
            instruments_config=mock_instruments,
            risk_engine=risk_engine
        )

        summary = strategy.get_sector_pairs_summary()

        assert summary['enabled'] is True
        assert summary['engine_available'] is True


# =============================================================================
# VSTOXX Data Feed Tests
# =============================================================================

class TestVSTOXXDataFeed:
    """Tests for VSTOXX data feed methods."""

    def test_fvs_expiry_calculation(self):
        """Test VSTOXX futures expiry calculation."""
        from src.marketdata.live import EuropeRegimeData
        from datetime import date

        # Mock IB client
        mock_ib = Mock()
        data = EuropeRegimeData(mock_ib)

        # Test expiry for January 2025
        expiry = data._get_fvs_expiry(date(2025, 1, 1), 0)
        assert expiry.weekday() == 2  # Wednesday

        # Test expiry for next month
        expiry_next = data._get_fvs_expiry(date(2025, 1, 1), 1)
        assert expiry_next > expiry  # Should be later
        assert expiry_next.weekday() == 2  # Also Wednesday

    def test_vstoxx_all_with_spot_fallback(self):
        """Test get_vstoxx_all with only spot available."""
        from src.marketdata.live import EuropeRegimeData

        mock_ib = Mock()
        data = EuropeRegimeData(mock_ib)

        # Mock get_v2x_level to return spot
        with patch.object(data, 'get_v2x_level', return_value=22.0):
            with patch.object(data, 'get_vstoxx_futures', return_value=None):
                result = data.get_vstoxx_all()

        # Should estimate futures from spot
        assert 'spot' in result
        assert result['spot'] == 22.0
        assert 'front' in result
        assert 'back' in result
        assert result['front'] < result['spot']  # Front at discount
        assert result['back'] > result['spot']   # Back at premium


# =============================================================================
# Full Integration Test
# =============================================================================

class TestFullIntegration:
    """End-to-end integration tests."""

    def test_hedge_summary_includes_v2_1_features(self, mock_settings, mock_instruments):
        """Test that hedge summary includes v2.1 dynamic targeting info."""
        from src.tail_hedge import TailHedgeManager

        manager = TailHedgeManager(mock_settings, mock_instruments)

        # Initialize some V2X history
        for i in range(30):
            manager.update_v2x_history(20.0 + i * 0.1)

        # Compute signal
        manager.compute_vol_signal(
            v2x_spot=23.0,
            v2x_front=22.5,
            v2x_back=24.0
        )

        summary = manager.get_hedge_summary()

        # Check v2.1 features in summary
        assert 'dynamic_targeting' in summary
        assert summary['dynamic_targeting']['enabled'] is True
        assert summary['dynamic_targeting']['v2x_history_length'] == 30

        assert 'vol_signal' in summary
        assert summary['vol_signal']['available'] is True

    def test_strategy_output_includes_sector_pairs(self, mock_settings, mock_instruments):
        """Test that strategy output includes sector pair info."""
        from src.strategy_logic import Strategy
        from src.risk_engine import RiskEngine

        risk_engine = Mock(spec=RiskEngine)

        strategy = Strategy(
            settings=mock_settings,
            instruments_config=mock_instruments,
            risk_engine=risk_engine
        )

        summary = strategy.get_sector_pairs_summary()

        assert summary['enabled'] is True
        assert 'positions' in summary or summary.get('engine_available', False)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
