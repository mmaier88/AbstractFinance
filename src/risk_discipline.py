"""
Risk Discipline Framework for AbstractFinance.

Phase M: v2.2 Roadmap - Hard constraints before any new sleeve activation.

Key components:
1. DV01 matching for bond futures spreads
2. Hard caps (per-sleeve, per-bet, daily loss)
3. Correlation budget monitoring
4. Kill switches (per-engine and global)

Usage:
    discipline = RiskDiscipline(config)

    # Check spread trade
    if discipline.validate_dv01_spread(long_leg, short_leg):
        execute_spread()

    # Check position limits
    if discipline.check_position_caps(positions):
        proceed()

    # Daily loss check
    if discipline.check_daily_loss(pnl):
        continue_trading()
    else:
        flatten_positions()
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from enum import Enum
import numpy as np

logger = logging.getLogger(__name__)


class KillSwitchState(Enum):
    """Kill switch states."""
    ACTIVE = "active"      # Engine is running
    DISABLED = "disabled"  # Manually disabled
    AUTO_HALT = "auto_halt"  # Automatically halted by telemetry


@dataclass
class DV01Config:
    """Configuration for DV01 matching."""
    # Maximum allowed DV01 mismatch (as ratio)
    max_dv01_mismatch_pct: float = 5.0  # 5% max mismatch

    # Bond futures DV01 estimates (per contract, in USD)
    # These are approximate and should be updated based on actual CTD
    dv01_estimates: Dict[str, float] = field(default_factory=lambda: {
        "FGBL": 85.0,   # Euro-Bund (~10yr)
        "FBTP": 90.0,   # BTP (~10yr, higher duration)
        "FOAT": 87.0,   # OAT (~10yr)
        "FGBM": 45.0,   # Euro-Bobl (~5yr)
        "FGBS": 20.0,   # Euro-Schatz (~2yr)
        "ZN": 70.0,     # US 10yr
        "ZF": 40.0,     # US 5yr
        "ZT": 20.0,     # US 2yr
    })


@dataclass
class PositionCapsConfig:
    """Configuration for position caps."""
    # Per-sleeve caps (as % of NAV)
    sleeve_caps_pct_nav: Dict[str, float] = field(default_factory=lambda: {
        "core_index_rv": 50.0,
        "sector_rv": 30.0,
        "europe_vol_convex": 20.0,
        "crisis_alpha": 15.0,
        "credit_carry": 20.0,
        "eu_sovereign_spreads": 15.0,  # NEW: v2.2
        "energy_shock": 10.0,           # NEW: v2.2
        "conditional_duration": 15.0,   # NEW: v2.2
    })

    # Global caps
    max_gross_exposure_pct_nav: float = 300.0  # 3x gross leverage max
    max_net_exposure_pct_nav: float = 100.0   # 1x net leverage max

    # Per-bet caps (single position)
    max_single_position_pct_nav: float = 20.0
    max_single_position_usd: float = 500_000.0

    # Daily loss caps
    max_daily_loss_pct_nav: float = 3.0    # Halt trading if down 3% in a day
    max_weekly_loss_pct_nav: float = 7.0   # Reduce sizing if down 7% in a week


@dataclass
class KillSwitchConfig:
    """Configuration for kill switches."""
    # Auto-halt triggers
    consecutive_losing_days: int = 3
    max_drawdown_trigger_pct: float = 15.0
    reconciliation_fail_count: int = 2

    # Cooldown periods
    auto_halt_cooldown_hours: int = 24
    manual_review_required: bool = True


@dataclass
class CorrelationBudgetConfig:
    """Configuration for correlation monitoring."""
    # Maximum combined allocation to correlated sleeves
    max_correlated_allocation_pct: float = 50.0

    # Sleeve correlation groups (sleeves that tend to move together in stress)
    correlation_groups: Dict[str, List[str]] = field(default_factory=lambda: {
        "equity_directional": ["core_index_rv", "sector_rv", "single_name"],
        "vol_long": ["europe_vol_convex", "crisis_alpha"],
        "duration": ["conditional_duration", "eu_sovereign_spreads"],
    })


@dataclass
class SpreadLeg:
    """Represents one leg of a spread trade."""
    instrument: str  # e.g., "FGBL", "FBTP"
    quantity: int    # Number of contracts (positive = long, negative = short)
    dv01_per_contract: Optional[float] = None


@dataclass
class RiskDisciplineConfig:
    """Complete risk discipline configuration."""
    dv01: DV01Config = field(default_factory=DV01Config)
    caps: PositionCapsConfig = field(default_factory=PositionCapsConfig)
    kill_switches: KillSwitchConfig = field(default_factory=KillSwitchConfig)
    correlation: CorrelationBudgetConfig = field(default_factory=CorrelationBudgetConfig)


class DV01Matcher:
    """
    DV01 matching for bond futures spreads.

    Ensures spread trades are duration-neutral (no hidden directional bets).
    """

    def __init__(self, config: DV01Config):
        self.config = config

    def get_dv01(self, instrument: str) -> float:
        """Get DV01 estimate for an instrument."""
        return self.config.dv01_estimates.get(instrument, 0.0)

    def compute_spread_ratio(
        self,
        long_instrument: str,
        short_instrument: str,
    ) -> float:
        """
        Compute the DV01-matched spread ratio.

        For a DV01-neutral spread:
        long_qty * dv01_long = short_qty * dv01_short

        Returns:
            Ratio of short contracts per long contract for DV01 neutrality
        """
        dv01_long = self.get_dv01(long_instrument)
        dv01_short = self.get_dv01(short_instrument)

        if dv01_short == 0:
            logger.warning(f"Unknown DV01 for {short_instrument}")
            return 1.0

        return dv01_long / dv01_short

    def validate_spread(
        self,
        long_leg: SpreadLeg,
        short_leg: SpreadLeg,
    ) -> Tuple[bool, str]:
        """
        Validate that a spread is DV01-matched within tolerance.

        Args:
            long_leg: Long leg of spread
            short_leg: Short leg of spread

        Returns:
            (is_valid, reason)
        """
        # Get DV01s
        dv01_long = long_leg.dv01_per_contract or self.get_dv01(long_leg.instrument)
        dv01_short = short_leg.dv01_per_contract or self.get_dv01(short_leg.instrument)

        if dv01_long == 0 or dv01_short == 0:
            return False, f"Unknown DV01 for {long_leg.instrument} or {short_leg.instrument}"

        # Compute DV01 exposures
        long_dv01_exposure = abs(long_leg.quantity) * dv01_long
        short_dv01_exposure = abs(short_leg.quantity) * dv01_short

        # Check mismatch
        if short_dv01_exposure == 0:
            return False, "Short leg DV01 exposure is zero"

        mismatch_pct = abs(long_dv01_exposure - short_dv01_exposure) / short_dv01_exposure * 100

        if mismatch_pct > self.config.max_dv01_mismatch_pct:
            return False, f"DV01 mismatch {mismatch_pct:.1f}% > {self.config.max_dv01_mismatch_pct}%"

        return True, f"DV01 matched within {mismatch_pct:.1f}%"

    def compute_matched_quantities(
        self,
        long_instrument: str,
        short_instrument: str,
        target_long_qty: int,
    ) -> Tuple[int, int]:
        """
        Compute DV01-matched quantities for a spread.

        Args:
            long_instrument: Long leg instrument
            short_instrument: Short leg instrument
            target_long_qty: Desired long quantity

        Returns:
            (long_qty, short_qty) - DV01-matched quantities
        """
        ratio = self.compute_spread_ratio(long_instrument, short_instrument)
        short_qty = round(target_long_qty * ratio)

        return target_long_qty, short_qty


class PositionCapManager:
    """
    Manages position caps and limits.

    Enforces hard limits on:
    - Per-sleeve allocation
    - Gross/net exposure
    - Single position size
    """

    def __init__(self, config: PositionCapsConfig):
        self.config = config

    def check_sleeve_cap(
        self,
        sleeve: str,
        sleeve_exposure: float,
        nav: float,
    ) -> Tuple[bool, str]:
        """Check if sleeve is within its allocation cap."""
        cap_pct = self.config.sleeve_caps_pct_nav.get(sleeve, 100.0)
        cap_value = nav * cap_pct / 100

        if sleeve_exposure > cap_value:
            return False, f"{sleeve} exposure ${sleeve_exposure:,.0f} > cap ${cap_value:,.0f} ({cap_pct}% NAV)"

        return True, f"{sleeve} within cap: ${sleeve_exposure:,.0f} / ${cap_value:,.0f}"

    def check_gross_exposure(
        self,
        gross_exposure: float,
        nav: float,
    ) -> Tuple[bool, str]:
        """Check gross exposure cap."""
        cap_value = nav * self.config.max_gross_exposure_pct_nav / 100

        if gross_exposure > cap_value:
            return False, f"Gross exposure ${gross_exposure:,.0f} > cap ${cap_value:,.0f}"

        return True, f"Gross exposure OK: ${gross_exposure:,.0f} / ${cap_value:,.0f}"

    def check_net_exposure(
        self,
        net_exposure: float,
        nav: float,
    ) -> Tuple[bool, str]:
        """Check net exposure cap."""
        cap_value = nav * self.config.max_net_exposure_pct_nav / 100

        if abs(net_exposure) > cap_value:
            return False, f"Net exposure ${abs(net_exposure):,.0f} > cap ${cap_value:,.0f}"

        return True, f"Net exposure OK: ${net_exposure:,.0f}"

    def check_single_position(
        self,
        position_value: float,
        nav: float,
    ) -> Tuple[bool, str]:
        """Check single position caps."""
        pct_cap = nav * self.config.max_single_position_pct_nav / 100
        abs_cap = self.config.max_single_position_usd

        effective_cap = min(pct_cap, abs_cap)

        if abs(position_value) > effective_cap:
            return False, f"Position ${abs(position_value):,.0f} > cap ${effective_cap:,.0f}"

        return True, f"Position size OK"

    def check_all_caps(
        self,
        positions: Dict[str, float],  # sleeve -> exposure
        nav: float,
    ) -> Tuple[bool, List[str]]:
        """
        Check all position caps.

        Returns:
            (all_passed, list_of_violations)
        """
        violations = []

        # Check each sleeve
        for sleeve, exposure in positions.items():
            passed, msg = self.check_sleeve_cap(sleeve, abs(exposure), nav)
            if not passed:
                violations.append(msg)

        # Check gross exposure
        gross = sum(abs(e) for e in positions.values())
        passed, msg = self.check_gross_exposure(gross, nav)
        if not passed:
            violations.append(msg)

        # Check net exposure
        net = sum(positions.values())
        passed, msg = self.check_net_exposure(net, nav)
        if not passed:
            violations.append(msg)

        return len(violations) == 0, violations


class DailyLossMonitor:
    """
    Monitors daily/weekly P&L for loss limits.

    Triggers halt if losses exceed thresholds.
    """

    def __init__(self, config: PositionCapsConfig):
        self.config = config
        self._daily_pnl: List[Tuple[date, float]] = []
        self._start_of_day_nav: Optional[float] = None

    def set_start_of_day_nav(self, nav: float) -> None:
        """Record NAV at start of day."""
        self._start_of_day_nav = nav

    def record_daily_pnl(self, dt: date, pnl: float) -> None:
        """Record daily P&L."""
        self._daily_pnl.append((dt, pnl))
        # Keep only last 30 days
        self._daily_pnl = self._daily_pnl[-30:]

    def check_daily_loss(
        self,
        current_nav: float,
    ) -> Tuple[bool, str]:
        """
        Check if daily loss limit breached.

        Returns:
            (can_continue, reason)
        """
        if self._start_of_day_nav is None:
            return True, "No start-of-day NAV recorded"

        daily_return = (current_nav / self._start_of_day_nav) - 1
        limit = -self.config.max_daily_loss_pct_nav / 100

        if daily_return < limit:
            return False, f"Daily loss {daily_return:.2%} < limit {limit:.2%} - HALT"

        return True, f"Daily P&L: {daily_return:.2%}"

    def check_weekly_loss(self, nav: float) -> Tuple[bool, float]:
        """
        Check weekly loss and return sizing multiplier.

        Returns:
            (within_limit, sizing_multiplier)
        """
        # Get last 5 trading days
        recent = self._daily_pnl[-5:] if len(self._daily_pnl) >= 5 else self._daily_pnl

        if not recent:
            return True, 1.0

        weekly_pnl = sum(pnl for _, pnl in recent)
        weekly_return = weekly_pnl / nav if nav > 0 else 0
        limit = -self.config.max_weekly_loss_pct_nav / 100

        if weekly_return < limit:
            # Reduce sizing proportionally
            multiplier = max(0.3, 1.0 + (weekly_return - limit) / abs(limit))
            return False, multiplier

        return True, 1.0


class KillSwitchManager:
    """
    Manages kill switches for engines.

    Can be triggered manually or automatically based on telemetry.
    """

    def __init__(self, config: KillSwitchConfig):
        self.config = config
        self._states: Dict[str, KillSwitchState] = {}
        self._halt_times: Dict[str, datetime] = {}
        self._consecutive_losses: Dict[str, int] = {}
        self._reconciliation_fails: Dict[str, int] = {}

    def register_engine(self, engine_name: str) -> None:
        """Register an engine with kill switch."""
        self._states[engine_name] = KillSwitchState.ACTIVE
        self._consecutive_losses[engine_name] = 0
        self._reconciliation_fails[engine_name] = 0

    def is_active(self, engine_name: str) -> bool:
        """Check if engine is active."""
        state = self._states.get(engine_name, KillSwitchState.DISABLED)

        # Check cooldown
        if state == KillSwitchState.AUTO_HALT:
            halt_time = self._halt_times.get(engine_name)
            if halt_time:
                cooldown = timedelta(hours=self.config.auto_halt_cooldown_hours)
                if datetime.now() > halt_time + cooldown:
                    # Cooldown expired - but need manual review if configured
                    if not self.config.manual_review_required:
                        self._states[engine_name] = KillSwitchState.ACTIVE
                        return True
                    return False  # Still halted, needs manual review

        return state == KillSwitchState.ACTIVE

    def disable_engine(self, engine_name: str, reason: str = "manual") -> None:
        """Manually disable an engine."""
        self._states[engine_name] = KillSwitchState.DISABLED
        logger.warning(f"Kill switch: {engine_name} DISABLED - {reason}")

    def enable_engine(self, engine_name: str) -> None:
        """Re-enable an engine after manual review."""
        self._states[engine_name] = KillSwitchState.ACTIVE
        self._consecutive_losses[engine_name] = 0
        self._reconciliation_fails[engine_name] = 0
        logger.info(f"Kill switch: {engine_name} ENABLED")

    def record_daily_result(
        self,
        engine_name: str,
        is_profitable: bool,
    ) -> None:
        """Record daily result for consecutive loss tracking."""
        if engine_name not in self._consecutive_losses:
            self._consecutive_losses[engine_name] = 0

        if is_profitable:
            self._consecutive_losses[engine_name] = 0
        else:
            self._consecutive_losses[engine_name] += 1

            # Check trigger
            if self._consecutive_losses[engine_name] >= self.config.consecutive_losing_days:
                self._trigger_auto_halt(
                    engine_name,
                    f"Consecutive losing days: {self._consecutive_losses[engine_name]}"
                )

    def record_reconciliation_result(
        self,
        engine_name: str,
        passed: bool,
    ) -> None:
        """Record reconciliation result."""
        if engine_name not in self._reconciliation_fails:
            self._reconciliation_fails[engine_name] = 0

        if passed:
            self._reconciliation_fails[engine_name] = 0
        else:
            self._reconciliation_fails[engine_name] += 1

            if self._reconciliation_fails[engine_name] >= self.config.reconciliation_fail_count:
                self._trigger_auto_halt(
                    engine_name,
                    f"Reconciliation failures: {self._reconciliation_fails[engine_name]}"
                )

    def check_drawdown(
        self,
        engine_name: str,
        current_drawdown: float,
    ) -> None:
        """Check if drawdown trigger hit."""
        trigger = self.config.max_drawdown_trigger_pct / 100

        if abs(current_drawdown) > trigger:
            self._trigger_auto_halt(
                engine_name,
                f"Drawdown {current_drawdown:.1%} > trigger {trigger:.1%}"
            )

    def _trigger_auto_halt(self, engine_name: str, reason: str) -> None:
        """Trigger automatic halt."""
        self._states[engine_name] = KillSwitchState.AUTO_HALT
        self._halt_times[engine_name] = datetime.now()
        logger.error(f"Kill switch AUTO-HALT: {engine_name} - {reason}")

    def get_status(self) -> Dict[str, Any]:
        """Get status of all engines."""
        return {
            name: {
                "state": state.value,
                "consecutive_losses": self._consecutive_losses.get(name, 0),
                "reconciliation_fails": self._reconciliation_fails.get(name, 0),
            }
            for name, state in self._states.items()
        }


class CorrelationBudgetManager:
    """
    Monitors correlation budget across sleeves.

    Prevents over-allocation to correlated strategies.
    """

    def __init__(self, config: CorrelationBudgetConfig):
        self.config = config

    def check_correlation_budget(
        self,
        sleeve_allocations: Dict[str, float],  # sleeve -> allocation %
    ) -> Tuple[bool, List[str]]:
        """
        Check if correlation budget is within limits.

        Returns:
            (within_budget, violations)
        """
        violations = []

        for group_name, sleeves in self.config.correlation_groups.items():
            group_allocation = sum(
                sleeve_allocations.get(s, 0)
                for s in sleeves
            )

            if group_allocation > self.config.max_correlated_allocation_pct:
                violations.append(
                    f"{group_name} group: {group_allocation:.1f}% > "
                    f"{self.config.max_correlated_allocation_pct}% limit"
                )

        return len(violations) == 0, violations


class RiskDiscipline:
    """
    Main risk discipline manager.

    Integrates all risk controls:
    - DV01 matching
    - Position caps
    - Daily loss monitoring
    - Kill switches
    - Correlation budget
    """

    def __init__(self, config: Optional[RiskDisciplineConfig] = None):
        self.config = config or RiskDisciplineConfig()

        self.dv01_matcher = DV01Matcher(self.config.dv01)
        self.cap_manager = PositionCapManager(self.config.caps)
        self.loss_monitor = DailyLossMonitor(self.config.caps)
        self.kill_switches = KillSwitchManager(self.config.kill_switches)
        self.correlation_manager = CorrelationBudgetManager(self.config.correlation)

    def validate_spread_trade(
        self,
        long_leg: SpreadLeg,
        short_leg: SpreadLeg,
    ) -> Tuple[bool, str]:
        """Validate a spread trade is DV01-matched."""
        return self.dv01_matcher.validate_spread(long_leg, short_leg)

    def check_position_limits(
        self,
        positions: Dict[str, float],
        nav: float,
    ) -> Tuple[bool, List[str]]:
        """Check all position caps."""
        return self.cap_manager.check_all_caps(positions, nav)

    def check_daily_loss(self, current_nav: float) -> Tuple[bool, str]:
        """Check daily loss limit."""
        return self.loss_monitor.check_daily_loss(current_nav)

    def is_engine_active(self, engine_name: str) -> bool:
        """Check if engine is active (not killed)."""
        return self.kill_switches.is_active(engine_name)

    def pre_trade_check(
        self,
        engine_name: str,
        proposed_positions: Dict[str, float],
        nav: float,
        current_nav: float,
    ) -> Tuple[bool, List[str]]:
        """
        Comprehensive pre-trade check.

        Returns:
            (can_trade, list_of_issues)
        """
        issues = []

        # Check kill switch
        if not self.is_engine_active(engine_name):
            issues.append(f"Engine {engine_name} is halted")

        # Check position caps
        caps_ok, cap_issues = self.check_position_limits(proposed_positions, nav)
        issues.extend(cap_issues)

        # Check daily loss
        loss_ok, loss_msg = self.check_daily_loss(current_nav)
        if not loss_ok:
            issues.append(loss_msg)

        # Check correlation budget
        allocations = {
            s: abs(v) / nav * 100
            for s, v in proposed_positions.items()
        }
        corr_ok, corr_issues = self.correlation_manager.check_correlation_budget(allocations)
        issues.extend(corr_issues)

        return len(issues) == 0, issues
