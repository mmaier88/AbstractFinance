"""
Tests for Risk Discipline Framework.

Phase M: v2.2 Roadmap validation.
"""

import pytest
from datetime import date, datetime, timedelta
from unittest.mock import patch

from src.risk_discipline import (
    DV01Config,
    DV01Matcher,
    SpreadLeg,
    PositionCapsConfig,
    PositionCapManager,
    DailyLossMonitor,
    KillSwitchConfig,
    KillSwitchManager,
    KillSwitchState,
    CorrelationBudgetConfig,
    CorrelationBudgetManager,
    RiskDiscipline,
    RiskDisciplineConfig,
)


class TestDV01Matcher:
    """Tests for DV01 matching in bond futures spreads."""

    def test_compute_spread_ratio_bund_btp(self):
        """Bund vs BTP spread ratio should be ~0.94."""
        matcher = DV01Matcher(DV01Config())

        # FGBL DV01 = 85, FBTP DV01 = 90
        ratio = matcher.compute_spread_ratio("FGBL", "FBTP")

        # 85/90 = 0.944
        assert 0.90 < ratio < 1.0
        assert abs(ratio - 85/90) < 0.01

    def test_compute_spread_ratio_bund_oat(self):
        """Bund vs OAT spread ratio should be ~0.98."""
        matcher = DV01Matcher(DV01Config())

        ratio = matcher.compute_spread_ratio("FGBL", "FOAT")

        # 85/87 = 0.977
        assert 0.95 < ratio < 1.0

    def test_validate_spread_passes_when_matched(self):
        """Properly matched spread should validate."""
        matcher = DV01Matcher(DV01Config())

        # Long 10 Bund (DV01=85), short 9 BTP (DV01=90)
        # Long DV01 = 10 * 85 = 850
        # Short DV01 = 9 * 90 = 810
        # Mismatch = |850-810|/810 = 4.9%
        long_leg = SpreadLeg("FGBL", 10)
        short_leg = SpreadLeg("FBTP", -9)

        is_valid, reason = matcher.validate_spread(long_leg, short_leg)

        assert is_valid
        assert "matched within" in reason.lower()

    def test_validate_spread_fails_when_mismatched(self):
        """Mismatched spread should fail validation."""
        matcher = DV01Matcher(DV01Config())

        # Long 10 Bund (DV01=85), short 5 BTP (DV01=90)
        # Long DV01 = 850, Short DV01 = 450
        # Mismatch = |850-450|/450 = 89% - way over 5%
        long_leg = SpreadLeg("FGBL", 10)
        short_leg = SpreadLeg("FBTP", -5)

        is_valid, reason = matcher.validate_spread(long_leg, short_leg)

        assert not is_valid
        assert "mismatch" in reason.lower()

    def test_compute_matched_quantities(self):
        """Should compute DV01-matched quantities."""
        matcher = DV01Matcher(DV01Config())

        # Want 10 long Bunds - how many BTPs to short?
        long_qty, short_qty = matcher.compute_matched_quantities("FGBL", "FBTP", 10)

        assert long_qty == 10
        # 10 * 85 / 90 = 9.4 -> rounds to 9
        assert short_qty == 9

    def test_custom_dv01_override(self):
        """Should use provided DV01 over estimates."""
        matcher = DV01Matcher(DV01Config())

        long_leg = SpreadLeg("FGBL", 10, dv01_per_contract=100.0)
        short_leg = SpreadLeg("FBTP", -10, dv01_per_contract=100.0)

        is_valid, _ = matcher.validate_spread(long_leg, short_leg)

        # With equal DV01s, 10 vs 10 is perfectly matched
        assert is_valid


class TestPositionCapManager:
    """Tests for position cap enforcement."""

    @pytest.fixture
    def cap_manager(self):
        return PositionCapManager(PositionCapsConfig())

    def test_sleeve_within_cap(self, cap_manager):
        """Sleeve below cap should pass."""
        nav = 1_000_000
        # core_index_rv cap is 50% = $500k
        is_ok, msg = cap_manager.check_sleeve_cap("core_index_rv", 400_000, nav)

        assert is_ok
        assert "within cap" in msg.lower()

    def test_sleeve_exceeds_cap(self, cap_manager):
        """Sleeve above cap should fail."""
        nav = 1_000_000
        # core_index_rv cap is 50% = $500k
        is_ok, msg = cap_manager.check_sleeve_cap("core_index_rv", 600_000, nav)

        assert not is_ok
        assert ">" in msg

    def test_gross_exposure_within_limit(self, cap_manager):
        """Gross exposure below 300% should pass."""
        nav = 1_000_000
        is_ok, _ = cap_manager.check_gross_exposure(2_500_000, nav)

        assert is_ok

    def test_gross_exposure_exceeds_limit(self, cap_manager):
        """Gross exposure above 300% should fail."""
        nav = 1_000_000
        is_ok, msg = cap_manager.check_gross_exposure(3_500_000, nav)

        assert not is_ok
        assert "gross" in msg.lower()

    def test_net_exposure_within_limit(self, cap_manager):
        """Net exposure below 100% should pass."""
        nav = 1_000_000
        is_ok, _ = cap_manager.check_net_exposure(800_000, nav)

        assert is_ok

    def test_net_exposure_exceeds_limit(self, cap_manager):
        """Net exposure above 100% should fail."""
        nav = 1_000_000
        is_ok, msg = cap_manager.check_net_exposure(1_200_000, nav)

        assert not is_ok
        assert "net" in msg.lower()

    def test_single_position_usd_cap(self, cap_manager):
        """Single position above $500k should fail."""
        nav = 10_000_000  # Large NAV so % cap doesn't trigger
        is_ok, msg = cap_manager.check_single_position(600_000, nav)

        assert not is_ok

    def test_single_position_pct_cap(self, cap_manager):
        """Single position above 20% NAV should fail."""
        nav = 1_000_000
        # 20% of $1M = $200k, but USD cap is $500k
        # Effective cap is $200k (lower of the two)
        is_ok, _ = cap_manager.check_single_position(250_000, nav)

        assert not is_ok

    def test_check_all_caps_multiple_violations(self, cap_manager):
        """Should report all violations."""
        nav = 1_000_000
        positions = {
            "core_index_rv": 600_000,  # Over 50% cap
            "sector_rv": 400_000,       # Over 30% cap
        }

        all_ok, violations = cap_manager.check_all_caps(positions, nav)

        assert not all_ok
        assert len(violations) >= 2


class TestDailyLossMonitor:
    """Tests for daily loss monitoring."""

    def test_no_loss_allows_trading(self):
        """Should allow trading when within limits."""
        monitor = DailyLossMonitor(PositionCapsConfig())
        monitor.set_start_of_day_nav(1_000_000)

        can_continue, _ = monitor.check_daily_loss(990_000)  # Down 1%

        assert can_continue

    def test_exceeding_daily_loss_triggers_halt(self):
        """Should halt when daily loss exceeds 3%."""
        monitor = DailyLossMonitor(PositionCapsConfig())
        monitor.set_start_of_day_nav(1_000_000)

        can_continue, msg = monitor.check_daily_loss(960_000)  # Down 4%

        assert not can_continue
        assert "halt" in msg.lower()

    def test_weekly_loss_reduces_sizing(self):
        """Should reduce sizing after weekly loss."""
        monitor = DailyLossMonitor(PositionCapsConfig())

        # Record 5 losing days
        for i in range(5):
            monitor.record_daily_pnl(
                date(2024, 1, 1) + timedelta(days=i),
                -20_000  # -2% daily on $1M
            )

        within_limit, multiplier = monitor.check_weekly_loss(1_000_000)

        # Down 10% in a week (5 * 2%), limit is 7%
        assert not within_limit
        assert multiplier < 1.0
        assert multiplier >= 0.3  # Min multiplier

    def test_no_start_nav_allows_trading(self):
        """Should allow trading if no start NAV recorded."""
        monitor = DailyLossMonitor(PositionCapsConfig())

        can_continue, _ = monitor.check_daily_loss(900_000)

        assert can_continue


class TestKillSwitchManager:
    """Tests for kill switch management."""

    def test_register_and_check_active(self):
        """Registered engine should be active."""
        manager = KillSwitchManager(KillSwitchConfig())
        manager.register_engine("test_engine")

        assert manager.is_active("test_engine")

    def test_unregistered_engine_inactive(self):
        """Unregistered engine should be inactive."""
        manager = KillSwitchManager(KillSwitchConfig())

        assert not manager.is_active("unknown_engine")

    def test_manual_disable(self):
        """Manually disabled engine should be inactive."""
        manager = KillSwitchManager(KillSwitchConfig())
        manager.register_engine("test_engine")
        manager.disable_engine("test_engine", "test reason")

        assert not manager.is_active("test_engine")

    def test_consecutive_losses_trigger_halt(self):
        """3 consecutive losing days should trigger auto-halt."""
        config = KillSwitchConfig(consecutive_losing_days=3)
        manager = KillSwitchManager(config)
        manager.register_engine("test_engine")

        # Record 3 losing days
        for _ in range(3):
            manager.record_daily_result("test_engine", is_profitable=False)

        assert not manager.is_active("test_engine")

    def test_profitable_day_resets_counter(self):
        """Profitable day should reset consecutive loss counter."""
        config = KillSwitchConfig(consecutive_losing_days=3)
        manager = KillSwitchManager(config)
        manager.register_engine("test_engine")

        manager.record_daily_result("test_engine", is_profitable=False)
        manager.record_daily_result("test_engine", is_profitable=False)
        manager.record_daily_result("test_engine", is_profitable=True)  # Reset
        manager.record_daily_result("test_engine", is_profitable=False)

        # Should still be active (only 1 loss after reset)
        assert manager.is_active("test_engine")

    def test_reconciliation_failures_trigger_halt(self):
        """2 reconciliation failures should trigger auto-halt."""
        config = KillSwitchConfig(reconciliation_fail_count=2)
        manager = KillSwitchManager(config)
        manager.register_engine("test_engine")

        manager.record_reconciliation_result("test_engine", passed=False)
        manager.record_reconciliation_result("test_engine", passed=False)

        assert not manager.is_active("test_engine")

    def test_drawdown_trigger(self):
        """Drawdown exceeding 15% should trigger halt."""
        config = KillSwitchConfig(max_drawdown_trigger_pct=15.0)
        manager = KillSwitchManager(config)
        manager.register_engine("test_engine")

        manager.check_drawdown("test_engine", -0.18)  # 18% drawdown

        assert not manager.is_active("test_engine")

    def test_enable_after_manual_review(self):
        """Should be able to re-enable after review."""
        manager = KillSwitchManager(KillSwitchConfig())
        manager.register_engine("test_engine")
        manager.disable_engine("test_engine")

        assert not manager.is_active("test_engine")

        manager.enable_engine("test_engine")

        assert manager.is_active("test_engine")

    def test_get_status(self):
        """Should return status for all engines."""
        manager = KillSwitchManager(KillSwitchConfig())
        manager.register_engine("engine_a")
        manager.register_engine("engine_b")
        manager.record_daily_result("engine_a", is_profitable=False)

        status = manager.get_status()

        assert "engine_a" in status
        assert "engine_b" in status
        assert status["engine_a"]["consecutive_losses"] == 1


class TestCorrelationBudgetManager:
    """Tests for correlation budget monitoring."""

    def test_within_correlation_budget(self):
        """Should pass when correlation groups within budget."""
        config = CorrelationBudgetConfig(
            max_correlated_allocation_pct=50.0,
            correlation_groups={
                "equity": ["sleeve_a", "sleeve_b"],
            }
        )
        manager = CorrelationBudgetManager(config)

        allocations = {
            "sleeve_a": 20.0,
            "sleeve_b": 20.0,  # Total: 40%
        }

        within_budget, violations = manager.check_correlation_budget(allocations)

        assert within_budget
        assert len(violations) == 0

    def test_exceeds_correlation_budget(self):
        """Should fail when correlation group exceeds budget."""
        config = CorrelationBudgetConfig(
            max_correlated_allocation_pct=50.0,
            correlation_groups={
                "equity": ["sleeve_a", "sleeve_b"],
            }
        )
        manager = CorrelationBudgetManager(config)

        allocations = {
            "sleeve_a": 35.0,
            "sleeve_b": 25.0,  # Total: 60%
        }

        within_budget, violations = manager.check_correlation_budget(allocations)

        assert not within_budget
        assert len(violations) == 1
        assert "equity" in violations[0]


class TestRiskDiscipline:
    """Tests for the main RiskDiscipline class."""

    @pytest.fixture
    def discipline(self):
        return RiskDiscipline()

    def test_validate_spread_trade(self, discipline):
        """Should validate DV01-matched spread."""
        long_leg = SpreadLeg("FGBL", 10)
        short_leg = SpreadLeg("FBTP", -9)

        is_valid, _ = discipline.validate_spread_trade(long_leg, short_leg)

        assert is_valid

    def test_check_position_limits(self, discipline):
        """Should check all position caps."""
        nav = 1_000_000
        positions = {
            "core_index_rv": 400_000,
            "sector_rv": 200_000,
        }

        all_ok, violations = discipline.check_position_limits(positions, nav)

        assert all_ok
        assert len(violations) == 0

    def test_pre_trade_check_passes(self, discipline):
        """Pre-trade check should pass for valid trade."""
        discipline.kill_switches.register_engine("test_engine")
        discipline.loss_monitor.set_start_of_day_nav(1_000_000)

        nav = 1_000_000
        positions = {
            "core_index_rv": 300_000,
        }

        can_trade, issues = discipline.pre_trade_check(
            "test_engine", positions, nav, 990_000
        )

        assert can_trade
        assert len(issues) == 0

    def test_pre_trade_check_fails_halted_engine(self, discipline):
        """Pre-trade check should fail for halted engine."""
        discipline.kill_switches.register_engine("test_engine")
        discipline.kill_switches.disable_engine("test_engine")

        nav = 1_000_000
        positions = {"core_index_rv": 300_000}

        can_trade, issues = discipline.pre_trade_check(
            "test_engine", positions, nav, 990_000
        )

        assert not can_trade
        assert any("halted" in i.lower() for i in issues)

    def test_pre_trade_check_fails_cap_violation(self, discipline):
        """Pre-trade check should fail for cap violation."""
        discipline.kill_switches.register_engine("test_engine")

        nav = 1_000_000
        positions = {
            "core_index_rv": 600_000,  # Over 50% cap
        }

        can_trade, issues = discipline.pre_trade_check(
            "test_engine", positions, nav, 990_000
        )

        assert not can_trade
        assert any("cap" in i.lower() or ">" in i for i in issues)

    def test_pre_trade_check_fails_daily_loss(self, discipline):
        """Pre-trade check should fail when daily loss exceeded."""
        discipline.kill_switches.register_engine("test_engine")
        discipline.loss_monitor.set_start_of_day_nav(1_000_000)

        nav = 1_000_000
        positions = {"core_index_rv": 300_000}

        # Current NAV down 5% (over 3% limit)
        can_trade, issues = discipline.pre_trade_check(
            "test_engine", positions, nav, 950_000
        )

        assert not can_trade
        assert any("halt" in i.lower() for i in issues)


class TestIntegration:
    """Integration tests for risk discipline."""

    def test_full_workflow(self):
        """Test complete risk discipline workflow."""
        discipline = RiskDiscipline()

        # Register engine
        discipline.kill_switches.register_engine("eu_sovereign_spreads")

        # Set start of day NAV
        discipline.loss_monitor.set_start_of_day_nav(1_000_000)

        # Validate spread trade (Bund vs BTP)
        long_leg = SpreadLeg("FGBL", 10)
        short_leg = SpreadLeg("FBTP", -9)

        spread_ok, _ = discipline.validate_spread_trade(long_leg, short_leg)
        assert spread_ok

        # Pre-trade check
        positions = {
            "eu_sovereign_spreads": 100_000,
        }

        can_trade, issues = discipline.pre_trade_check(
            "eu_sovereign_spreads",
            positions,
            nav=1_000_000,
            current_nav=995_000,  # Down 0.5%
        )

        assert can_trade
        assert len(issues) == 0

        # Record end of day - profitable
        discipline.kill_switches.record_daily_result(
            "eu_sovereign_spreads",
            is_profitable=True
        )

        # Engine should still be active
        assert discipline.is_engine_active("eu_sovereign_spreads")

    def test_crisis_scenario_triggers_halt(self):
        """Test that crisis scenario properly triggers halt."""
        discipline = RiskDiscipline()

        discipline.kill_switches.register_engine("test_engine")
        discipline.loss_monitor.set_start_of_day_nav(1_000_000)

        # Simulate 3 consecutive losing days
        for _ in range(3):
            discipline.kill_switches.record_daily_result(
                "test_engine",
                is_profitable=False
            )

        # Engine should be halted
        assert not discipline.is_engine_active("test_engine")

        # Pre-trade should fail
        positions = {"test_sleeve": 100_000}
        can_trade, issues = discipline.pre_trade_check(
            "test_engine", positions, 900_000, 880_000
        )

        assert not can_trade
