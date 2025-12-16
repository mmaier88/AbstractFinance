"""
Unit tests for ROADMAP features.

Tests cover:
- Phase A: Run Ledger (exactly-once execution)
- Phase B: Europe-first regime detection
- Phase C: FX hedge policy modes
- Phase D: Option validator
- Phase E: Backtest harness
- Phase F: Enhanced metrics
"""

import pytest
import tempfile
import os
from datetime import date, datetime
from pathlib import Path

# Phase A imports
from src.state.run_ledger import (
    RunLedger, RunStatus, TradingRun, OrderRecord,
    compute_inputs_hash, compute_intents_hash
)

# Phase B imports - will test risk_engine
from src.risk_engine import RiskEngine, RiskRegime

# Phase C imports
from src.strategy_logic import FXHedgeMode, FXHedgePolicy

# Phase D imports
from src.options.validator import (
    OptionValidator, OptionValidationConfig, OptionQuote,
    ValidationFailure
)

# Phase E imports
from src.research.backtest import (
    BacktestConfig, CostModelConfig, CostModel,
    BacktestRunner, ResearchMarketData
)


# =============================================================================
# Phase A: Run Ledger Tests
# =============================================================================

class TestRunLedger:
    """Tests for exactly-once execution tracking."""

    @pytest.fixture
    def temp_db(self):
        """Create a temporary database for testing."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            yield f.name
        os.unlink(f.name)

    @pytest.fixture
    def ledger(self, temp_db):
        """Create a run ledger instance."""
        return RunLedger(temp_db)

    def test_begin_run_creates_record(self, ledger):
        """Test that begin_run creates a new run record."""
        run = ledger.begin_run(
            run_date=date.today(),
            strategy_version="v1.0",
            inputs_hash="abc123"
        )
        assert run.run_id is not None
        assert run.status == RunStatus.PLANNED
        assert run.run_date == date.today()

    def test_deterministic_run_id(self, ledger):
        """Test that same inputs produce same run_id."""
        id1 = TradingRun.generate_run_id(date(2025, 1, 1), "hash123")
        id2 = TradingRun.generate_run_id(date(2025, 1, 1), "hash123")
        assert id1 == id2

    def test_different_inputs_different_id(self, ledger):
        """Test that different inputs produce different run_id."""
        id1 = TradingRun.generate_run_id(date(2025, 1, 1), "hash123")
        id2 = TradingRun.generate_run_id(date(2025, 1, 1), "hash456")
        assert id1 != id2

    def test_get_run_for_date(self, ledger):
        """Test retrieving run by date."""
        run = ledger.begin_run(
            run_date=date(2025, 1, 15),
            strategy_version="v1.0",
            inputs_hash="test123"
        )
        retrieved = ledger.get_run_for_date(date(2025, 1, 15))
        assert retrieved is not None
        assert retrieved.run_id == run.run_id

    def test_record_order(self, ledger):
        """Test recording orders for a run."""
        run = ledger.begin_run(
            run_date=date.today(),
            strategy_version="v1.0",
            inputs_hash="test123"
        )
        order = OrderRecord(
            client_order_id="order_001",
            instrument_id="SPY",
            side="BUY",
            quantity=100,
            sleeve="core_index_rv"
        )
        ledger.record_order(run.run_id, order)

        retrieved = ledger.get_run(run.run_id)
        assert len(retrieved.orders) == 1
        assert retrieved.orders[0].instrument_id == "SPY"

    def test_status_transitions(self, ledger):
        """Test run status transitions."""
        run = ledger.begin_run(
            run_date=date.today(),
            strategy_version="v1.0",
            inputs_hash="test123"
        )
        assert run.status == RunStatus.PLANNED

        ledger.mark_submitted(run.run_id)
        run = ledger.get_run(run.run_id)
        assert run.status == RunStatus.SUBMITTED

        ledger.mark_done(run.run_id)
        run = ledger.get_run(run.run_id)
        assert run.status == RunStatus.DONE

    def test_duplicate_detection(self, ledger):
        """Test that duplicate client_order_id is detected."""
        run = ledger.begin_run(
            run_date=date.today(),
            strategy_version="v1.0",
            inputs_hash="test123"
        )
        order = OrderRecord(
            client_order_id="order_unique_001",
            instrument_id="SPY",
            side="BUY",
            quantity=100,
            sleeve="core_index_rv"
        )
        ledger.record_order(run.run_id, order)

        assert ledger.has_duplicate_client_order_id("order_unique_001")
        assert not ledger.has_duplicate_client_order_id("order_unique_002")

    def test_inputs_hash_computation(self):
        """Test inputs hash is deterministic."""
        positions = {"SPY": 100, "EFA": -50}
        fx_rates = {"EUR/USD": 1.05}
        market_data = {"VIX": 15.5}

        hash1 = compute_inputs_hash(positions, fx_rates, market_data)
        hash2 = compute_inputs_hash(positions, fx_rates, market_data)
        assert hash1 == hash2

    def test_client_order_id_generation(self):
        """Test client order ID is deterministic."""
        id1 = TradingRun.generate_client_order_id(
            "run123", "SPY", "BUY", 100.0, "core"
        )
        id2 = TradingRun.generate_client_order_id(
            "run123", "SPY", "BUY", 100.0, "core"
        )
        assert id1 == id2

        # Different quantity should produce different ID
        id3 = TradingRun.generate_client_order_id(
            "run123", "SPY", "BUY", 101.0, "core"
        )
        assert id1 != id3


# =============================================================================
# Phase B: Europe-First Regime Detection Tests
# =============================================================================

class TestEuropeFirstRegime:
    """Tests for multi-factor European regime detection."""

    @pytest.fixture
    def risk_engine(self):
        """Create a risk engine with Europe-first settings."""
        settings = {
            'vol_target_annual': 0.12,
            'gross_leverage_max': 2.0,
            'max_drawdown_pct': 0.10,
            'europe_regime': {
                'v2x_weight': 0.4,
                'vix_weight': 0.3,
                'eurusd_trend_weight': 0.2,
                'drawdown_weight': 0.1,
                'stress_elevated_threshold': 0.3,
                'stress_crisis_threshold': 0.6,
            },
            'hysteresis': {
                'persistence_days': 3,
            }
        }
        return RiskEngine(settings)

    def test_normal_regime_low_stress(self, risk_engine):
        """Test NORMAL regime with low stress conditions."""
        regime, score, inputs = risk_engine.detect_regime_europe_first(
            vix_level=15,
            v2x_level=18,
            eurusd_trend=0.02,  # EUR strengthening
            current_drawdown=-0.01
        )
        assert regime == RiskRegime.NORMAL
        assert score < 0.3

    def test_elevated_regime_high_v2x(self, risk_engine):
        """Test ELEVATED regime when V2X is high."""
        # Reset regime state
        risk_engine._current_regime = RiskRegime.NORMAL
        risk_engine._pending_regime = None
        risk_engine._pending_regime_days = 0

        # High V2X should trigger elevated
        for _ in range(4):  # Need persistence
            regime, score, inputs = risk_engine.detect_regime_europe_first(
                vix_level=22,
                v2x_level=32,  # High V2X
                eurusd_trend=0.0,
                current_drawdown=-0.02
            )

        assert regime == RiskRegime.ELEVATED
        assert score >= 0.3

    def test_crisis_regime_immediate(self, risk_engine):
        """Test CRISIS regime triggers immediately (no hysteresis)."""
        risk_engine._current_regime = RiskRegime.NORMAL

        # CRISIS should trigger immediately
        regime, score, inputs = risk_engine.detect_regime_europe_first(
            vix_level=45,
            v2x_level=50,
            eurusd_trend=-0.15,  # EUR weakening sharply
            current_drawdown=-0.08
        )
        assert regime == RiskRegime.CRISIS
        assert score >= 0.6

    def test_eurusd_trend_impact(self, risk_engine):
        """Test that EUR weakening increases stress score."""
        risk_engine._current_regime = RiskRegime.NORMAL

        _, score_stable, _ = risk_engine.detect_regime_europe_first(
            vix_level=20,
            v2x_level=22,
            eurusd_trend=0.0,
            current_drawdown=0.0
        )

        _, score_weak_eur, _ = risk_engine.detect_regime_europe_first(
            vix_level=20,
            v2x_level=22,
            eurusd_trend=-0.10,  # EUR weakening 10% annualized
            current_drawdown=0.0
        )

        assert score_weak_eur > score_stable

    def test_v2x_fallback_when_missing(self, risk_engine):
        """Test graceful fallback when V2X is unavailable."""
        regime, score, inputs = risk_engine.detect_regime_europe_first(
            vix_level=20,
            v2x_level=None,  # V2X unavailable
            eurusd_trend=0.0,
            current_drawdown=0.0
        )
        assert inputs['v2x_available'] is False
        assert regime in [RiskRegime.NORMAL, RiskRegime.ELEVATED, RiskRegime.CRISIS]

    def test_hysteresis_prevents_flip_flop(self, risk_engine):
        """Test that hysteresis prevents rapid regime changes."""
        risk_engine._current_regime = RiskRegime.NORMAL
        risk_engine._pending_regime = None

        # First day elevated conditions
        regime1, _, _ = risk_engine.detect_regime_europe_first(
            vix_level=28, v2x_level=30, eurusd_trend=-0.05, current_drawdown=-0.03
        )
        assert regime1 == RiskRegime.NORMAL  # Not changed yet

        # Second day
        regime2, _, _ = risk_engine.detect_regime_europe_first(
            vix_level=28, v2x_level=30, eurusd_trend=-0.05, current_drawdown=-0.03
        )
        assert regime2 == RiskRegime.NORMAL  # Still waiting

        # Third day
        regime3, _, _ = risk_engine.detect_regime_europe_first(
            vix_level=28, v2x_level=30, eurusd_trend=-0.05, current_drawdown=-0.03
        )
        # May now be ELEVATED depending on exact score


# =============================================================================
# Phase C: FX Hedge Policy Tests
# =============================================================================

class TestFXHedgePolicy:
    """Tests for FX hedge policy modes."""

    @pytest.fixture
    def policy(self):
        """Create default FX hedge policy."""
        return FXHedgePolicy()

    def test_default_mode_is_partial(self, policy):
        """Test default mode is PARTIAL."""
        assert policy.mode == FXHedgeMode.PARTIAL

    def test_full_mode_hedge_ratio(self, policy):
        """Test FULL mode gives high hedge ratio."""
        policy.mode = FXHedgeMode.FULL
        ratio = policy.get_hedge_ratio("NORMAL")
        assert ratio >= 0.95  # Should hedge at least 95%

    def test_partial_mode_hedge_ratio(self, policy):
        """Test PARTIAL mode gives medium hedge ratio."""
        ratio = policy.get_hedge_ratio("NORMAL")
        assert 0.70 <= ratio <= 0.80  # Should be ~75%

    def test_none_mode_hedge_ratio(self, policy):
        """Test NONE mode gives zero hedge ratio."""
        policy.mode = FXHedgeMode.NONE
        ratio = policy.get_hedge_ratio("NORMAL")
        assert ratio == 0.0

    def test_crisis_override(self, policy):
        """Test CRISIS regime overrides to NONE (no hedge)."""
        # Default regime_overrides has CRISIS -> NONE
        mode = policy.get_effective_mode("CRISIS")
        assert mode == FXHedgeMode.NONE
        ratio = policy.get_hedge_ratio("CRISIS")
        assert ratio == 0.0

    def test_from_settings(self):
        """Test creating policy from settings dict."""
        settings = {
            'fx_hedge': {
                'mode': 'FULL',
                'target_residual_pct_nav': {
                    'FULL': 0.01,
                    'PARTIAL': 0.20,
                    'NONE': 1.0
                },
                'regime_overrides': {
                    'NORMAL': 'FULL',
                    'CRISIS': 'NONE'
                }
            }
        }
        policy = FXHedgePolicy.from_settings(settings)
        assert policy.mode == FXHedgeMode.FULL
        assert policy.get_effective_mode("CRISIS") == FXHedgeMode.NONE


# =============================================================================
# Phase D: Option Validator Tests
# =============================================================================

class TestOptionValidator:
    """Tests for option lifecycle validator."""

    @pytest.fixture
    def validator(self):
        """Create option validator with default config."""
        return OptionValidator()

    @pytest.fixture
    def valid_quote(self):
        """Create a valid option quote."""
        return OptionQuote(
            symbol="SPY250117P450",
            underlying="SPY",
            strike=450,
            expiry=date(2025, 1, 17),
            option_type="put",
            bid=5.0,
            ask=5.40,  # 8% spread
            volume=500,
            open_interest=2000
        )

    def test_valid_option_passes(self, validator, valid_quote):
        """Test that valid options pass validation."""
        result = validator.validate(
            valid_quote,
            hedge_type="equity_put",
            budget_remaining=100000,
            quantity=10
        )
        assert result.is_valid

    def test_spread_too_wide_fails(self, validator):
        """Test that wide spreads fail validation."""
        quote = OptionQuote(
            symbol="SPY250117P450",
            underlying="SPY",
            strike=450,
            expiry=date(2025, 1, 17),
            option_type="put",
            bid=5.0,
            ask=6.0,  # 20% spread - too wide
            volume=500,
            open_interest=2000
        )
        result = validator.validate(quote, "equity_put", 100000)
        assert not result.is_valid
        assert ValidationFailure.SPREAD_TOO_WIDE in result.failures

    def test_low_volume_fails(self, validator):
        """Test that low volume fails validation."""
        quote = OptionQuote(
            symbol="SPY250117P450",
            underlying="SPY",
            strike=450,
            expiry=date(2025, 1, 17),
            option_type="put",
            bid=5.0,
            ask=5.30,
            volume=10,  # Too low
            open_interest=2000
        )
        result = validator.validate(quote, "equity_put", 100000)
        assert not result.is_valid
        assert ValidationFailure.LOW_VOLUME in result.failures

    def test_low_open_interest_fails(self, validator):
        """Test that low open interest fails validation."""
        quote = OptionQuote(
            symbol="SPY250117P450",
            underlying="SPY",
            strike=450,
            expiry=date(2025, 1, 17),
            option_type="put",
            bid=5.0,
            ask=5.30,
            volume=500,
            open_interest=50  # Too low
        )
        result = validator.validate(quote, "equity_put", 100000)
        assert not result.is_valid
        assert ValidationFailure.LOW_OPEN_INTEREST in result.failures

    def test_premium_exceeds_budget_fails(self, validator):
        """Test that premium exceeding budget fails."""
        quote = OptionQuote(
            symbol="SPY250117P450",
            underlying="SPY",
            strike=450,
            expiry=date(2025, 1, 17),
            option_type="put",
            bid=50.0,
            ask=51.0,
            volume=500,
            open_interest=2000
        )
        # Budget is 10k, but 10 contracts @ $51 * 100 = $51,000
        result = validator.validate(quote, "equity_put", 10000, quantity=10)
        assert not result.is_valid
        assert ValidationFailure.PREMIUM_EXCEEDS_BUDGET in result.failures

    def test_near_expiry_fails(self, validator):
        """Test that near-expiry options fail validation."""
        quote = OptionQuote(
            symbol="SPY250117P450",
            underlying="SPY",
            strike=450,
            expiry=date.today(),  # Expires today
            option_type="put",
            bid=5.0,
            ask=5.30,
            volume=500,
            open_interest=2000
        )
        result = validator.validate(quote, "equity_put", 100000)
        assert not result.is_valid
        assert ValidationFailure.EXPIRY_TOO_CLOSE in result.failures

    def test_no_quotes_fails(self, validator):
        """Test that missing quotes fail validation."""
        quote = OptionQuote(
            symbol="SPY250117P450",
            underlying="SPY",
            strike=450,
            expiry=date(2025, 6, 17),
            option_type="put",
            bid=None,
            ask=None,
            volume=500,
            open_interest=2000
        )
        result = validator.validate(quote, "equity_put", 100000)
        assert not result.is_valid
        assert ValidationFailure.NO_QUOTES in result.failures

    def test_alternative_strikes_suggested(self, validator):
        """Test that alternatives are suggested on failure."""
        quote = OptionQuote(
            symbol="SPY250117P450",
            underlying="SPY",
            strike=450,
            expiry=date(2025, 6, 17),
            option_type="put",
            bid=5.0,
            ask=6.0,  # Too wide
            volume=500,
            open_interest=2000
        )
        result = validator.validate(quote, "equity_put", 100000)
        assert len(result.alternative_strikes) > 0

    def test_vix_calls_wider_spread_allowed(self, validator):
        """Test that VIX calls allow wider spreads."""
        quote = OptionQuote(
            symbol="VIX250117C30",
            underlying="VIX",
            strike=30,
            expiry=date(2025, 6, 17),
            option_type="call",
            bid=3.0,
            ask=3.33,  # 11% spread - ok for VIX
            volume=100,
            open_interest=500
        )
        result = validator.validate(quote, "vix_call", 100000)
        # May pass or fail depending on other thresholds
        # but spread alone should not fail for VIX
        spread_failures = [f for f in result.failures if f == ValidationFailure.SPREAD_TOO_WIDE]
        assert len(spread_failures) == 0


# =============================================================================
# Phase E: Backtest Tests
# =============================================================================

class TestCostModel:
    """Tests for transaction cost model."""

    @pytest.fixture
    def cost_model(self):
        """Create cost model with default config."""
        return CostModel(CostModelConfig())

    def test_transaction_cost_includes_slippage(self, cost_model):
        """Test that transaction cost includes slippage."""
        cost = cost_model.compute_transaction_cost(
            notional=100000,
            asset_class="etf",
            is_buy=True
        )
        # 100k * 4bps = $40 slippage + $1 commission = $41
        assert cost > 40
        assert cost < 50

    def test_futures_lower_slippage(self, cost_model):
        """Test that futures have lower slippage."""
        etf_cost = cost_model.compute_transaction_cost(100000, "etf", True)
        fut_cost = cost_model.compute_transaction_cost(100000, "futures", True)
        assert fut_cost < etf_cost

    def test_carry_cost_computation(self, cost_model):
        """Test daily carry cost computation."""
        cost = cost_model.compute_daily_carry_cost(
            short_notional=100000,
            futures_notional=50000
        )
        assert cost > 0
        # Should be roughly (100k * (200 + 50)bps / 252) = ~$99/day
        assert cost < 200  # Sanity check


class TestBacktestConfig:
    """Tests for backtest configuration."""

    def test_default_config(self):
        """Test default backtest config."""
        config = BacktestConfig()
        assert config.start_date == date(2008, 1, 1)
        assert config.initial_capital == 1_000_000.0
        assert config.vol_target_annual == 0.12

    def test_custom_config(self):
        """Test custom backtest config."""
        config = BacktestConfig(
            start_date=date(2015, 1, 1),
            initial_capital=500000,
            fx_hedge_mode="FULL"
        )
        assert config.start_date == date(2015, 1, 1)
        assert config.initial_capital == 500000
        assert config.fx_hedge_mode == "FULL"


# =============================================================================
# Integration Tests
# =============================================================================

class TestIntegration:
    """Integration tests across multiple phases."""

    def test_regime_affects_fx_hedge(self):
        """Test that regime change affects FX hedge ratio."""
        settings = {
            'vol_target_annual': 0.12,
            'max_drawdown_pct': 0.10,
            'europe_regime': {
                'v2x_weight': 0.4,
                'vix_weight': 0.3,
                'eurusd_trend_weight': 0.2,
                'drawdown_weight': 0.1,
                'stress_crisis_threshold': 0.6,
            },
            'fx_hedge': {
                'mode': 'PARTIAL',
                'regime_overrides': {
                    'NORMAL': 'PARTIAL',
                    'CRISIS': 'NONE'
                }
            }
        }

        risk_engine = RiskEngine(settings)
        fx_policy = FXHedgePolicy.from_settings(settings)

        # Normal conditions
        regime_normal, _, _ = risk_engine.detect_regime_europe_first(
            vix_level=15, v2x_level=18, eurusd_trend=0.0, current_drawdown=0.0
        )
        ratio_normal = fx_policy.get_hedge_ratio(regime_normal.value)

        # Force crisis
        risk_engine._current_regime = RiskRegime.CRISIS
        ratio_crisis = fx_policy.get_hedge_ratio("CRISIS")

        # In crisis, should have lower hedge ratio
        assert ratio_crisis < ratio_normal

    def test_run_ledger_with_validation(self):
        """Test run ledger workflow with option validation."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name

        try:
            ledger = RunLedger(db_path)
            validator = OptionValidator()

            # Begin run
            run = ledger.begin_run(
                run_date=date.today(),
                strategy_version="v1.0",
                inputs_hash="test_integration"
            )

            # Simulate option order validation
            quote = OptionQuote(
                symbol="SPY250117P450",
                underlying="SPY",
                strike=450,
                expiry=date(2025, 6, 17),
                option_type="put",
                bid=5.0,
                ask=5.30,
                volume=500,
                open_interest=2000
            )
            validation = validator.validate(quote, "equity_put", 100000)

            if validation.is_valid:
                # Generate client order ID
                client_id = TradingRun.generate_client_order_id(
                    run.run_id, "SPY_PUT", "BUY", 10.0, "crisis_alpha"
                )

                # Record order
                order = OrderRecord(
                    client_order_id=client_id,
                    instrument_id="SPY_PUT",
                    side="BUY",
                    quantity=10,
                    sleeve="crisis_alpha"
                )
                ledger.record_order(run.run_id, order)

            ledger.mark_done(run.run_id)

            # Verify
            final_run = ledger.get_run(run.run_id)
            assert final_run.status == RunStatus.DONE
        finally:
            os.unlink(db_path)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
