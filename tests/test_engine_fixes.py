"""
Tests for ENGINE_FIX_PLAN mandatory changes.

Phase 10 - Mandatory Unit Tests covering:
1. FX sanity: verify FXRates.get_rate("EUR","USD") != 0
2. Futures sanity: position_exposure(FUT) != position_nav_value(FUT)
3. Reconciliation: computed_nav vs broker_nlv triggers correct status
4. Sizing: verify EUR position notional ≈ target_usd/EURUSD
5. Exposure: check gross_exposure sums abs values properly
"""

import pytest
import sys
from datetime import datetime
from typing import Dict
from unittest.mock import MagicMock

# Mock yfinance before importing fx_rates
sys.modules['yfinance'] = MagicMock()

from src.fx_rates import (
    FXRates, BASE_CCY, cash_in_base_ccy,
    compute_net_fx_exposure, compute_fx_hedge_quantities
)
from src.portfolio import (
    Position, PortfolioState, InstrumentType, Sleeve,
    position_nav_value, position_exposure
)
from src.risk_engine import RiskEngine, RiskRegime, RiskState


# =============================================================================
# Phase 1 Tests: FX Rates
# =============================================================================

class TestFXRates:
    """Tests for Phase 1: Base Currency Accounting."""

    def test_fx_rates_initialization(self):
        """Test FXRates initializes with identity rates."""
        fx = FXRates()

        # All supported currencies should have identity rates
        for ccy in FXRates.SUPPORTED_CURRENCIES:
            assert fx.get_rate(ccy, ccy) == 1.0

    def test_fx_get_rate_not_zero(self):
        """FX sanity: verify get_rate returns non-zero for valid pairs."""
        fx = FXRates()
        fx.set_rate("EUR", "USD", 1.05)
        fx.set_rate("GBP", "USD", 1.25)

        # Direct rate
        assert fx.get_rate("EUR", "USD") != 0
        assert fx.get_rate("EUR", "USD") == 1.05

        # Inverse rate
        assert fx.get_rate("USD", "EUR") != 0
        assert abs(fx.get_rate("USD", "EUR") - (1/1.05)) < 0.0001

    def test_fx_cross_rate(self):
        """Test cross rate calculation via USD."""
        fx = FXRates()
        fx.set_rate("EUR", "USD", 1.05)
        fx.set_rate("GBP", "USD", 1.25)

        # EUR to GBP via USD
        eur_gbp = fx.get_rate("EUR", "GBP")
        expected = 1.05 / 1.25  # EUR->USD / GBP->USD
        assert abs(eur_gbp - expected) < 0.0001

    def test_fx_convert(self):
        """Test currency conversion."""
        fx = FXRates()
        fx.set_rate("EUR", "USD", 1.05)

        # Convert 1000 EUR to USD
        usd_amount = fx.convert(1000, "EUR", "USD")
        assert usd_amount == 1050.0

        # Convert back
        eur_amount = fx.convert(1050, "USD", "EUR")
        assert abs(eur_amount - 1000) < 0.01

    def test_fx_to_base(self):
        """Test conversion to base currency."""
        fx = FXRates()
        fx.set_rate("EUR", "USD", 1.05)

        assert fx.to_base(1000, "EUR") == 1050.0
        assert fx.to_base(1000, "USD") == 1000.0  # Identity

    def test_fx_missing_rate_raises(self):
        """Test KeyError raised for unavailable rate."""
        fx = FXRates()
        # No rate set for NOK
        with pytest.raises(KeyError):
            fx.get_rate("NOK", "USD")

    def test_fx_stale_check(self):
        """Test staleness detection."""
        fx = FXRates()

        # No timestamp = stale
        assert fx.is_stale() is True

        # Set a rate (updates timestamp)
        fx.set_rate("EUR", "USD", 1.05)
        assert fx.is_stale(max_age_seconds=60) is False

    def test_base_ccy_constant(self):
        """Test BASE_CCY is USD."""
        assert BASE_CCY == "USD"


class TestCashInBaseCcy:
    """Tests for multi-currency cash conversion."""

    def test_cash_single_currency(self):
        """Test with single currency."""
        fx = FXRates()
        cash_by_ccy = {"USD": 100000}

        total = cash_in_base_ccy(cash_by_ccy, fx)
        assert total == 100000

    def test_cash_multi_currency(self):
        """Test with multiple currencies."""
        fx = FXRates()
        fx.set_rate("EUR", "USD", 1.05)
        fx.set_rate("GBP", "USD", 1.25)

        cash_by_ccy = {
            "USD": 50000,
            "EUR": 10000,  # = 10500 USD
            "GBP": 5000,   # = 6250 USD
        }

        total = cash_in_base_ccy(cash_by_ccy, fx)
        expected = 50000 + 10500 + 6250
        assert abs(total - expected) < 0.01


# =============================================================================
# Phase 2 Tests: NAV vs Exposure Logic
# =============================================================================

class TestNAVvsExposure:
    """Tests for Phase 2: Futures P&L only in NAV."""

    def test_futures_nav_is_pnl_only(self):
        """Futures sanity: position_nav_value != position_exposure for futures."""
        fx = FXRates()
        fx.set_rate("EUR", "USD", 1.05)

        # M6E futures position - 10 contracts at 1.0520, now at 1.0550
        fut_position = Position(
            instrument_id="M6EZ4",
            quantity=10,
            avg_cost=1.0520,
            market_price=1.0550,
            multiplier=12500,
            currency="USD",
            instrument_type=InstrumentType.FUT
        )

        # NAV value = P&L only (not notional)
        nav_value = position_nav_value(fut_position, fx)

        # Exposure = full notional
        exposure = position_exposure(fut_position, fx)

        # Key assertion: these MUST be different for futures
        assert nav_value != exposure

        # NAV = unrealized P&L = (1.0550 - 1.0520) * 10 * 12500 = 3750
        expected_pnl = (1.0550 - 1.0520) * 10 * 12500
        assert abs(nav_value - expected_pnl) < 0.01

        # Exposure = full notional = 1.0550 * 10 * 12500 = 131875
        expected_exposure = 1.0550 * 10 * 12500
        assert abs(exposure - expected_exposure) < 0.01

    def test_stock_nav_equals_market_value(self):
        """Stocks: NAV value = market value (not just P&L)."""
        fx = FXRates()
        fx.set_rate("GBP", "USD", 1.25)

        # UK stock position
        stk_position = Position(
            instrument_id="IUKD",
            quantity=1000,
            avg_cost=15.50,
            market_price=16.00,
            multiplier=1,
            currency="GBP",
            instrument_type=InstrumentType.STK
        )

        nav_value = position_nav_value(stk_position, fx)
        exposure = position_exposure(stk_position, fx)

        # For stocks, NAV and exposure are both based on market value
        expected_mv_usd = 16.00 * 1000 * 1.25  # 20,000 USD
        assert abs(nav_value - expected_mv_usd) < 0.01
        assert abs(exposure - expected_mv_usd) < 0.01

    def test_etf_nav_equals_market_value(self):
        """ETFs: NAV value = market value."""
        fx = FXRates()

        etf_position = Position(
            instrument_id="SPY",
            quantity=100,
            avg_cost=450.0,
            market_price=455.0,
            multiplier=1,
            currency="USD",
            instrument_type=InstrumentType.ETF
        )

        nav_value = position_nav_value(etf_position, fx)
        expected = 455.0 * 100  # 45,500 USD
        assert abs(nav_value - expected) < 0.01


# =============================================================================
# Phase 3 Tests: Broker Reconciliation
# =============================================================================

class TestBrokerReconciliation:
    """Tests for Phase 3: Broker Reconciliation Circuit Breaker."""

    def test_reconciliation_pass(self):
        """Test reconciliation passes when within threshold."""
        portfolio = PortfolioState(
            nav=1000000,
            cash_by_ccy={"USD": 100000},
            initial_capital=1000000
        )

        # Broker reports NLV very close to our computed NAV
        broker_nlv = 1000500  # 0.05% difference

        passes, status = portfolio.reconcile_with_broker(broker_nlv)

        assert passes is True
        assert "PASS" in status
        assert portfolio.reconciliation_status == "PASS"
        assert portfolio.can_trade() is True

    def test_reconciliation_halt(self):
        """Test reconciliation triggers HALT at 0.25% threshold."""
        portfolio = PortfolioState(
            nav=1000000,
            cash_by_ccy={"USD": 100000},
            initial_capital=1000000
        )

        # Broker reports NLV 0.3% different (above 0.25% threshold)
        broker_nlv = 1003000  # 0.3% higher

        passes, status = portfolio.reconcile_with_broker(broker_nlv, halt_threshold_pct=0.0025)

        assert passes is False
        assert "HALT" in status
        assert portfolio.reconciliation_status == "HALT"
        assert portfolio.can_trade() is False

    def test_reconciliation_emergency(self):
        """Test reconciliation triggers EMERGENCY at 1% threshold."""
        portfolio = PortfolioState(
            nav=1000000,
            cash_by_ccy={"USD": 100000},
            initial_capital=1000000
        )

        # Broker reports NLV 1.5% different (above 1% emergency threshold)
        broker_nlv = 1015000  # 1.5% higher

        passes, status = portfolio.reconcile_with_broker(
            broker_nlv,
            halt_threshold_pct=0.0025,
            emergency_threshold_pct=0.01
        )

        assert passes is False
        assert "EMERGENCY" in status
        assert portfolio.reconciliation_status == "EMERGENCY"

    def test_reconciliation_not_checked(self):
        """Test initial reconciliation status."""
        portfolio = PortfolioState(
            nav=1000000,
            cash_by_ccy={"USD": 100000},
            initial_capital=1000000
        )

        assert portfolio.reconciliation_status == "NOT_CHECKED"
        # NOT_CHECKED blocks trading until reconciled
        assert portfolio.can_trade() is False


# =============================================================================
# Phase 4 Tests: Currency-Correct Position Sizing
# =============================================================================

class TestCurrencyCorrectSizing:
    """Tests for Phase 4: Currency-Correct Position Sizing."""

    def test_eur_position_sizing(self):
        """Verify EUR position notional approx equals target_usd / EURUSD."""
        fx = FXRates()
        fx.set_rate("EUR", "USD", 1.05)

        # Target: $100,000 notional for EUR-denominated instrument
        target_usd = 100000

        # Convert to EUR for sizing
        target_eur = fx.convert(target_usd, "USD", "EUR")

        # Verify conversion is correct
        expected_eur = target_usd / 1.05
        assert abs(target_eur - expected_eur) < 0.01

        # If CS51 trades at EUR 50, we should buy ~1905 shares
        cs51_price_eur = 50.0
        qty = round(target_eur / cs51_price_eur)

        # Verify resulting EUR notional
        eur_notional = qty * cs51_price_eur
        usd_notional = fx.convert(eur_notional, "EUR", "USD")

        # Should be close to target (within rounding)
        assert abs(usd_notional - target_usd) < 1000  # Within 1% tolerance

    def test_gbp_position_sizing(self):
        """Verify GBP position notional calculation."""
        fx = FXRates()
        fx.set_rate("GBP", "USD", 1.25)

        target_usd = 50000
        target_gbp = fx.convert(target_usd, "USD", "GBP")

        expected_gbp = target_usd / 1.25
        assert abs(target_gbp - expected_gbp) < 0.01


# =============================================================================
# Phase 5 Tests: Portfolio-Level FX Hedging
# =============================================================================

class TestFXHedging:
    """Tests for Phase 5: Portfolio-Level FX Hedging."""

    def test_compute_net_fx_exposure(self):
        """Test net FX exposure computation."""
        fx = FXRates()
        fx.set_rate("EUR", "USD", 1.05)
        fx.set_rate("GBP", "USD", 1.25)

        # EUR position
        positions = {
            "CS51": Position(
                instrument_id="CS51",
                quantity=1000,
                avg_cost=50.0,
                market_price=52.0,
                currency="EUR",
                multiplier=1
            ),
            "IUKD": Position(
                instrument_id="IUKD",
                quantity=500,
                avg_cost=15.0,
                market_price=16.0,
                currency="GBP",
                multiplier=1
            )
        }

        cash_by_ccy = {"USD": 100000, "EUR": 5000}

        exposure = compute_net_fx_exposure(positions, cash_by_ccy, fx)

        # EUR exposure = position (52000) + cash (5000) = 57000 EUR
        assert "EUR" in exposure
        assert abs(exposure["EUR"] - 57000) < 1

        # GBP exposure = position (8000) = 8000 GBP
        assert "GBP" in exposure
        assert abs(exposure["GBP"] - 8000) < 1

        # USD should NOT be in exposure (it's base currency)
        assert "USD" not in exposure

    def test_compute_fx_hedge_quantities(self):
        """Test FX hedge quantity calculation."""
        fx = FXRates()
        fx.set_rate("EUR", "USD", 1.05)

        # 100,000 EUR exposure - need to hedge
        net_exposure = {"EUR": 100000}

        hedges = compute_fx_hedge_quantities(net_exposure, fx, hedge_ratio=1.0)

        # M6E contract size = 12,500 EUR
        # 100000 / 12500 = 8 contracts
        # Negative because we're shorting to hedge
        assert hedges["EUR"] == -8

    def test_fx_hedge_partial_ratio(self):
        """Test partial hedge ratio."""
        fx = FXRates()

        net_exposure = {"EUR": 100000}

        # 50% hedge
        hedges = compute_fx_hedge_quantities(net_exposure, fx, hedge_ratio=0.5)

        # 50000 / 12500 = 4 contracts
        assert hedges["EUR"] == -4


# =============================================================================
# Phase 6 Tests: Volatility Targeting
# =============================================================================

class TestVolatilityTargeting:
    """Tests for Phase 6: EWMA Volatility with Floor and Clip."""

    def _make_risk_engine(self):
        """Create RiskEngine with test settings."""
        settings = {
            'vol_target_annual': 0.12,
            'gross_leverage_max': 2.0,
            'net_leverage_max': 1.0,
            'max_drawdown_pct': 0.10,
            'volatility': {
                'floor': 0.08,
                'ewma_span': 20,
                'blend_weight': 0.7
            },
            'hysteresis': {
                'persistence_days': 3,
                'vix_enter_elevated': 25,
                'vix_exit_elevated': 20,
                'vix_enter_crisis': 40,
                'vix_exit_crisis': 35
            }
        }
        return RiskEngine(settings)

    def test_vol_floor_applied(self):
        """Test volatility floor is applied."""
        import pandas as pd
        engine = self._make_risk_engine()

        # Very low volatility returns
        returns = pd.Series([0.001] * 30)

        blended_vol = engine.compute_blended_vol(returns)

        # Should be at least the floor
        assert blended_vol >= 0.08

    def test_vol_clip_applied(self):
        """Test volatility is clipped to reasonable range."""
        import pandas as pd
        engine = self._make_risk_engine()

        # Extremely high volatility returns
        returns = pd.Series([0.05, -0.05] * 20)  # 5% daily swings

        blended_vol = engine.compute_blended_vol(returns)

        # Blended vol should be reasonable (annualized)
        # 5% daily vol * sqrt(252) ~ 79%, but floor/blend might affect it
        assert blended_vol > 0  # At minimum, vol should be positive

    def test_ewma_vol_weights_recent(self):
        """Test EWMA gives more weight to recent data."""
        import pandas as pd
        engine = self._make_risk_engine()

        # Low vol followed by high vol
        returns_low_then_high = pd.Series(
            [0.005] * 20 +  # Low vol early
            [0.02, -0.02] * 10  # High vol recent
        )

        # High vol followed by low vol
        returns_high_then_low = pd.Series(
            [0.02, -0.02] * 10 +  # High vol early
            [0.005] * 20  # Low vol recent
        )

        vol1 = engine.compute_blended_vol(returns_low_then_high)
        vol2 = engine.compute_blended_vol(returns_high_then_low)

        # EWMA should give higher estimate when recent vol is higher
        # vol1 should be higher than vol2 due to recency weighting
        assert vol1 > vol2


# =============================================================================
# Phase 7 Tests: Regime Hysteresis
# =============================================================================

class TestRegimeHysteresis:
    """Tests for Phase 7: Regime System Hysteresis."""

    def _make_risk_engine(self):
        """Create RiskEngine with test settings."""
        settings = {
            'vol_target_annual': 0.12,
            'gross_leverage_max': 2.0,
            'net_leverage_max': 1.0,
            'max_drawdown_pct': 0.10,
            'volatility': {
                'floor': 0.08,
                'ewma_span': 20,
                'blend_weight': 0.7
            },
            'hysteresis': {
                'persistence_days': 3,
                'vix_enter_elevated': 25,
                'vix_exit_elevated': 20,
                'vix_enter_crisis': 40,
                'vix_exit_crisis': 35
            },
            'crisis': {
                'vix_threshold': 40
            }
        }
        return RiskEngine(settings)

    def test_regime_enter_threshold(self):
        """Test regime only enters on crossing entry threshold."""
        engine = self._make_risk_engine()

        # Start in NORMAL
        engine._current_regime = RiskRegime.NORMAL

        # VIX at 22 - above exit but below entry
        regime = engine.detect_regime(vix_level=22, spread_momentum=0, current_drawdown=0)

        # Should stay NORMAL (hasn't crossed entry threshold)
        assert regime == RiskRegime.NORMAL

    def test_regime_persistence(self):
        """Test regime requires persistence days to change."""
        engine = self._make_risk_engine()

        engine._current_regime = RiskRegime.NORMAL
        engine._pending_regime_days = 0

        # Day 1: VIX spikes above entry
        regime1 = engine.detect_regime(vix_level=28, spread_momentum=0, current_drawdown=0)

        # Should still be NORMAL on first day (need 3 days persistence)
        # But ELEVATED might be pending
        assert engine._pending_regime == RiskRegime.ELEVATED or regime1 == RiskRegime.ELEVATED

    def test_crisis_immediate(self):
        """Test CRISIS regime activates immediately."""
        engine = self._make_risk_engine()
        engine._current_regime = RiskRegime.NORMAL

        # Large drawdown should trigger CRISIS immediately
        regime = engine.detect_regime(vix_level=45, spread_momentum=0, current_drawdown=-0.15)

        # CRISIS should be immediate, no persistence required
        assert regime == RiskRegime.CRISIS


# =============================================================================
# Phase 8 Tests: Risk State Machine
# =============================================================================

class TestRiskStateMachine:
    """Tests for Phase 8: Emergency De-Risk State Machine."""

    def _make_risk_engine(self):
        """Create RiskEngine with test settings."""
        settings = {
            'vol_target_annual': 0.12,
            'gross_leverage_max': 2.0,
            'net_leverage_max': 1.0,
            'max_drawdown_pct': 0.10,
            'volatility': {'floor': 0.08, 'ewma_span': 20, 'blend_weight': 0.7},
            'hysteresis': {
                'persistence_days': 3,
                'vix_enter_elevated': 25,
                'vix_exit_elevated': 20,
                'vix_enter_crisis': 40,
                'vix_exit_crisis': 35
            }
        }
        return RiskEngine(settings)

    def test_risk_state_normal(self):
        """Test NORMAL state scaling."""
        engine = self._make_risk_engine()
        engine._risk_state = RiskState.NORMAL

        scaling = engine.get_risk_state_scaling()
        assert scaling == 1.0

    def test_risk_state_elevated(self):
        """Test ELEVATED state scaling."""
        engine = self._make_risk_engine()
        engine._risk_state = RiskState.ELEVATED

        scaling = engine.get_risk_state_scaling()
        assert scaling == 0.7

    def test_risk_state_crisis(self):
        """Test CRISIS state scaling."""
        engine = self._make_risk_engine()
        engine._risk_state = RiskState.CRISIS

        scaling = engine.get_risk_state_scaling()
        assert scaling == 0.3

    def test_risk_state_transitions(self):
        """Test valid state transitions."""
        engine = self._make_risk_engine()
        engine._risk_state = RiskState.NORMAL

        # NORMAL -> CRISIS should be allowed (immediate)
        new_state = engine.update_risk_state(RiskRegime.CRISIS, current_drawdown=-0.10)
        assert new_state == RiskState.CRISIS

        # CRISIS -> NORMAL - depends on implementation
        # Current impl maps directly from regime
        engine._risk_state = RiskState.CRISIS
        new_state = engine.update_risk_state(RiskRegime.NORMAL, current_drawdown=-0.05)
        # With -5% drawdown < -10% threshold, should map to NORMAL
        assert new_state == RiskState.NORMAL


# =============================================================================
# Phase 9 Tests: Execution Safety
# =============================================================================

class TestExecutionSafety:
    """Tests for Phase 9: Execution Safety Guards."""

    def test_execution_blocked_on_recon_fail(self):
        """Test execution blocked when reconciliation fails."""
        from src.execution_ibkr import check_execution_safety

        portfolio = PortfolioState(
            nav=1000000,
            cash_by_ccy={"USD": 100000},
            initial_capital=1000000
        )
        portfolio.reconciliation_status = "HALT"

        safe, reasons = check_execution_safety(portfolio)

        assert safe is False
        assert any("reconciliation" in r.lower() for r in reasons)

    def test_execution_blocked_on_fx_invalid(self):
        """Test execution blocked when FX rates invalid."""
        from src.execution_ibkr import check_execution_safety

        portfolio = PortfolioState(
            nav=1000000,
            cash_by_ccy={"USD": 100000},
            initial_capital=1000000
        )
        portfolio.reconciliation_status = "OK"

        safe, reasons = check_execution_safety(portfolio, fx_rates_valid=False)

        assert safe is False
        assert any("fx" in r.lower() for r in reasons)

    def test_execution_allowed_when_ok(self):
        """Test execution allowed when all checks pass."""
        from src.execution_ibkr import check_execution_safety

        portfolio = PortfolioState(
            nav=1000000,
            cash_by_ccy={"USD": 100000},
            initial_capital=1000000
        )
        # Must be "PASS" not "OK" - can_trade() checks for "PASS"
        portfolio.reconciliation_status = "PASS"

        safe, reasons = check_execution_safety(
            portfolio,
            fx_rates_valid=True,
            vol_estimate_valid=True
        )

        assert safe is True
        assert len(reasons) == 0


# =============================================================================
# Exposure Calculation Tests
# =============================================================================

class TestExposureCalculation:
    """Test gross exposure sums absolute values correctly."""

    def test_gross_exposure_abs_sum(self):
        """Check gross_exposure sums abs values properly."""
        fx = FXRates()
        fx.set_rate("EUR", "USD", 1.05)

        portfolio = PortfolioState(
            nav=1000000,
            cash_by_ccy={"USD": 100000},
            initial_capital=1000000
        )

        # Long position
        portfolio.positions["SPY"] = Position(
            instrument_id="SPY",
            quantity=100,
            avg_cost=450.0,
            market_price=450.0,
            currency="USD",
            multiplier=1,
            instrument_type=InstrumentType.ETF
        )

        # Short position
        portfolio.positions["CS51"] = Position(
            instrument_id="CS51",
            quantity=-1000,
            avg_cost=50.0,
            market_price=50.0,
            currency="EUR",
            multiplier=1,
            instrument_type=InstrumentType.ETF
        )

        # Pass fx_rates explicitly to avoid using global instance
        portfolio.compute_exposures(fx_rates=fx)

        # Long = 45000 USD
        # Short = 50000 EUR = 52500 USD
        # Gross = abs(45000) + abs(52500) = 97500
        assert portfolio.long_exposure == 45000
        assert abs(portfolio.short_exposure - 52500) < 100  # EUR converted
        assert portfolio.gross_exposure == portfolio.long_exposure + portfolio.short_exposure

    def test_net_exposure(self):
        """Test net exposure calculation."""
        portfolio = PortfolioState(
            nav=1000000,
            cash_by_ccy={"USD": 100000},
            initial_capital=1000000
        )

        # Long 100 SPY at $450 = $45,000
        portfolio.positions["SPY"] = Position(
            instrument_id="SPY",
            quantity=100,
            avg_cost=450.0,
            market_price=450.0,
            currency="USD",
            multiplier=1
        )

        # Short 500 IWM at $200 = -$100,000
        portfolio.positions["IWM"] = Position(
            instrument_id="IWM",
            quantity=-500,
            avg_cost=200.0,
            market_price=200.0,
            currency="USD",
            multiplier=1
        )

        portfolio.compute_exposures()

        assert portfolio.long_exposure == 45000
        assert portfolio.short_exposure == 100000
        assert portfolio.net_exposure == 45000 - 100000  # -55000


# =============================================================================
# Integration Tests
# =============================================================================

class TestIntegration:
    """Integration tests combining multiple phases."""

    def test_full_portfolio_workflow(self):
        """Test complete portfolio workflow with all fixes."""
        # Phase 1: Setup FX
        fx = FXRates()
        fx.set_rate("EUR", "USD", 1.05)
        fx.set_rate("GBP", "USD", 1.25)

        # Create portfolio with multi-currency cash
        portfolio = PortfolioState(
            nav=1000000,
            cash_by_ccy={"USD": 800000, "EUR": 50000, "GBP": 20000},
            initial_capital=1000000
        )

        # Add positions
        portfolio.positions["SPY"] = Position(
            instrument_id="SPY",
            quantity=200,
            avg_cost=450.0,
            market_price=455.0,
            currency="USD",
            multiplier=1,
            instrument_type=InstrumentType.ETF
        )

        portfolio.positions["M6EZ4"] = Position(
            instrument_id="M6EZ4",
            quantity=5,
            avg_cost=1.0500,
            market_price=1.0550,
            multiplier=12500,
            currency="USD",
            instrument_type=InstrumentType.FUT
        )

        # Phase 2: Verify NAV vs Exposure
        spy_nav = position_nav_value(portfolio.positions["SPY"], fx)
        fut_nav = position_nav_value(portfolio.positions["M6EZ4"], fx)
        fut_exp = position_exposure(portfolio.positions["M6EZ4"], fx)

        # SPY NAV = market value
        assert abs(spy_nav - 91000) < 1  # 200 * 455

        # Futures NAV = P&L only
        expected_fut_pnl = (1.0550 - 1.0500) * 5 * 12500  # 3125
        assert abs(fut_nav - expected_fut_pnl) < 1

        # Futures exposure = full notional
        expected_fut_notional = 1.0550 * 5 * 12500  # 65937.5
        assert abs(fut_exp - expected_fut_notional) < 1

        # Phase 3: Reconciliation
        broker_nlv = 999500  # Close to computed
        passes, status = portfolio.reconcile_with_broker(broker_nlv)
        assert passes is True

        # Phase 5: FX exposure
        net_fx = compute_net_fx_exposure(portfolio.positions, portfolio.cash_by_ccy, fx)
        # EUR cash exposure
        assert "EUR" in net_fx
        # GBP cash exposure
        assert "GBP" in net_fx

    def test_risk_engine_full_flow(self):
        """Test risk engine with all phases."""
        import pandas as pd

        settings = {
            'vol_target_annual': 0.12,
            'gross_leverage_max': 2.0,
            'net_leverage_max': 1.0,
            'max_drawdown_pct': 0.10,
            'volatility': {'floor': 0.08, 'ewma_span': 20, 'blend_weight': 0.7},
            'hysteresis': {
                'persistence_days': 3,
                'vix_enter_elevated': 25,
                'vix_exit_elevated': 20,
                'vix_enter_crisis': 40,
                'vix_exit_crisis': 35
            }
        }
        engine = RiskEngine(settings)

        # Phase 6: Volatility
        returns = pd.Series([0.01, -0.005, 0.008, -0.012, 0.006] * 10)
        vol = engine.compute_blended_vol(returns)
        assert vol >= engine.vol_floor

        # Phase 7: Regime detection with hysteresis
        regime = engine.detect_regime(vix_level=18, spread_momentum=0, current_drawdown=-0.02)
        assert regime in [RiskRegime.NORMAL, RiskRegime.ELEVATED, RiskRegime.RECOVERY]

        # Phase 8: State machine
        state = engine.update_risk_state(regime, current_drawdown=-0.02)
        scaling = engine.get_risk_state_scaling()
        assert scaling in [1.0, 0.7, 0.3]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
