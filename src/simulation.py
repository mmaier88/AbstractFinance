"""
Simulation module for AbstractFinance.

Provides enhanced dry-run capabilities to validate the full execution pipeline
without actually trading. Useful for:
1. Pre-deploy validation
2. Scenario testing
3. Debugging order generation
4. Verifying glidepath behavior

Usage:
    from src.simulation import SimulationRunner, SimulationScenario

    runner = SimulationRunner(instruments_config, settings)
    scenario = SimulationScenario(
        name="Day 1 Glidepath",
        mock_positions={...},
        mock_prices={...},
    )
    report = runner.run_scenario(scenario)
    print(report.summary())
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict

from .utils.invariants import (
    assert_position_id_valid,
    assert_no_conflicting_orders,
    validate_instruments_config,
    build_id_mappings,
    InvariantError,
)

logger = logging.getLogger(__name__)


@dataclass
class SimulationScenario:
    """
    Defines a simulation scenario for testing.

    Attributes:
        name: Human-readable name for the scenario
        mock_positions: Dict of instrument_id -> quantity (uses internal IDs)
        mock_prices: Dict of instrument_id -> price
        glidepath_day: Which day of glidepath to simulate (0-10+)
        vix_level: Mock VIX level for risk calculations
        nav: Mock NAV value
        fx_rates: Mock FX rates (currency pair -> rate)
    """
    name: str
    mock_positions: Dict[str, float] = field(default_factory=dict)
    mock_prices: Dict[str, float] = field(default_factory=dict)
    glidepath_day: int = 0
    vix_level: float = 16.0
    nav: float = 280000.0
    fx_rates: Dict[str, float] = field(default_factory=lambda: {
        "EURUSD": 1.05,
        "GBPUSD": 1.27,
    })


@dataclass
class SimulationOrder:
    """Represents an order that would be generated."""
    instrument_id: str
    side: str
    quantity: float
    limit_price: Optional[float] = None
    reason: str = ""


@dataclass
class SimulationResult:
    """Result of running a single scenario."""
    scenario_name: str
    success: bool
    orders: List[SimulationOrder]
    invariant_violations: List[str]
    warnings: List[str]
    position_changes: Dict[str, Tuple[float, float]]  # instrument_id -> (before, after)
    gross_exposure_before: float = 0.0
    gross_exposure_after: float = 0.0
    execution_time_ms: float = 0.0

    def summary(self) -> str:
        """Generate a human-readable summary."""
        lines = [
            f"=== Scenario: {self.scenario_name} ===",
            f"Status: {'PASS' if self.success else 'FAIL'}",
        ]

        if self.invariant_violations:
            lines.append(f"Invariant Violations ({len(self.invariant_violations)}):")
            for v in self.invariant_violations:
                lines.append(f"  - {v}")

        if self.warnings:
            lines.append(f"Warnings ({len(self.warnings)}):")
            for w in self.warnings:
                lines.append(f"  - {w}")

        lines.append(f"Orders Generated: {len(self.orders)}")
        for order in self.orders:
            lines.append(f"  {order.side} {order.quantity} {order.instrument_id}")

        lines.append(f"Position Changes: {len(self.position_changes)}")
        for inst_id, (before, after) in self.position_changes.items():
            if before != after:
                lines.append(f"  {inst_id}: {before} -> {after}")

        lines.append(f"Gross Exposure: {self.gross_exposure_before:.2f} -> {self.gross_exposure_after:.2f}")
        lines.append(f"Execution Time: {self.execution_time_ms:.2f}ms")

        return "\n".join(lines)


@dataclass
class SimulationReport:
    """Report summarizing all simulation scenarios."""
    results: List[SimulationResult] = field(default_factory=list)
    total_scenarios: int = 0
    passed_scenarios: int = 0
    failed_scenarios: int = 0
    total_invariant_violations: int = 0

    def add_result(self, result: SimulationResult) -> None:
        """Add a scenario result to the report."""
        self.results.append(result)
        self.total_scenarios += 1
        if result.success:
            self.passed_scenarios += 1
        else:
            self.failed_scenarios += 1
        self.total_invariant_violations += len(result.invariant_violations)

    def summary(self) -> str:
        """Generate overall summary."""
        lines = [
            "=" * 60,
            "SIMULATION REPORT",
            "=" * 60,
            f"Total Scenarios: {self.total_scenarios}",
            f"Passed: {self.passed_scenarios}",
            f"Failed: {self.failed_scenarios}",
            f"Total Invariant Violations: {self.total_invariant_violations}",
            "",
        ]

        for result in self.results:
            status = "✓" if result.success else "✗"
            lines.append(f"{status} {result.scenario_name}: {len(result.orders)} orders")

        lines.append("")
        lines.append("=" * 60)

        return "\n".join(lines)

    @property
    def all_passed(self) -> bool:
        """Check if all scenarios passed."""
        return self.failed_scenarios == 0


class SimulationRunner:
    """
    Runs simulation scenarios to validate execution pipeline.

    This validates:
    1. Position IDs are all internal config IDs
    2. No conflicting BUY/SELL orders
    3. Glidepath blending is correct
    4. Prices are available for all instruments
    5. FX rates are applied correctly
    """

    def __init__(
        self,
        instruments_config: Dict[str, Any],
        settings: Optional[Dict[str, Any]] = None,
    ):
        """
        Initialize the simulation runner.

        Args:
            instruments_config: Instrument configuration dict
            settings: Application settings (optional)
        """
        self.instruments_config = instruments_config
        self.settings = settings or {}

        # Validate config at init
        is_valid, errors = validate_instruments_config(instruments_config)
        if not is_valid:
            raise InvariantError(f"Invalid instruments config: {errors}")

        # Build ID mappings
        self.config_to_symbol, self.symbol_to_config = build_id_mappings(
            instruments_config
        )

        logger.info(
            f"SimulationRunner initialized with {len(self.config_to_symbol)} instruments"
        )

    def run_scenario(self, scenario: SimulationScenario) -> SimulationResult:
        """
        Run a single simulation scenario.

        Args:
            scenario: The scenario to simulate

        Returns:
            SimulationResult with orders and validation results
        """
        start_time = datetime.now()
        violations = []
        warnings = []
        orders = []
        position_changes = {}

        logger.info(f"Running simulation: {scenario.name}")

        # Step 1: Validate position IDs
        for inst_id in scenario.mock_positions.keys():
            try:
                assert_position_id_valid(
                    inst_id,
                    self.instruments_config,
                    context=f"simulation:{scenario.name}"
                )
            except InvariantError as e:
                violations.append(str(e))

        # Step 2: Check for missing prices
        for inst_id in scenario.mock_positions.keys():
            if inst_id not in scenario.mock_prices:
                warnings.append(f"No price for position {inst_id}")

        # Step 3: Simulate glidepath blending
        if scenario.glidepath_day >= 0:
            alpha = min(scenario.glidepath_day / 10.0, 1.0)

            # Get initial and target positions (simplified - in real system from strategy)
            initial_positions = scenario.mock_positions.copy()
            target_positions = self._compute_mock_targets(scenario)

            # Compute blended positions
            all_instruments = set(initial_positions) | set(target_positions)
            blended_positions = {}

            for inst_id in all_instruments:
                initial = initial_positions.get(inst_id, 0.0)
                target = target_positions.get(inst_id, 0.0)
                blended = alpha * target + (1 - alpha) * initial
                blended_positions[inst_id] = blended

                if initial != blended:
                    position_changes[inst_id] = (initial, blended)

            # Step 4: Generate orders from position differences
            for inst_id, blended_qty in blended_positions.items():
                current_qty = scenario.mock_positions.get(inst_id, 0.0)
                diff = blended_qty - current_qty

                if abs(diff) > 0.5:  # Threshold
                    side = "BUY" if diff > 0 else "SELL"
                    price = scenario.mock_prices.get(inst_id)

                    orders.append(SimulationOrder(
                        instrument_id=inst_id,
                        side=side,
                        quantity=abs(diff),
                        limit_price=price,
                        reason=f"glidepath_day_{scenario.glidepath_day}"
                    ))

        # Step 5: Validate orders for conflicts
        try:
            assert_no_conflicting_orders(
                orders,
                context=f"simulation:{scenario.name}"
            )
        except InvariantError as e:
            violations.append(str(e))

        # Calculate execution time
        elapsed_ms = (datetime.now() - start_time).total_seconds() * 1000

        # Calculate gross exposure
        gross_before = self._calc_gross_exposure(
            scenario.mock_positions,
            scenario.mock_prices,
            scenario.fx_rates,
        )
        gross_after = self._calc_gross_exposure(
            {inst_id: b for inst_id, (_, b) in position_changes.items()},
            scenario.mock_prices,
            scenario.fx_rates,
        )

        result = SimulationResult(
            scenario_name=scenario.name,
            success=len(violations) == 0,
            orders=orders,
            invariant_violations=violations,
            warnings=warnings,
            position_changes=position_changes,
            gross_exposure_before=gross_before,
            gross_exposure_after=gross_after,
            execution_time_ms=elapsed_ms,
        )

        logger.info(f"Simulation {scenario.name}: {'PASS' if result.success else 'FAIL'}")
        return result

    def run_scenarios(
        self, scenarios: List[SimulationScenario]
    ) -> SimulationReport:
        """
        Run multiple simulation scenarios.

        Args:
            scenarios: List of scenarios to run

        Returns:
            SimulationReport summarizing all results
        """
        report = SimulationReport()

        for scenario in scenarios:
            result = self.run_scenario(scenario)
            report.add_result(result)

        return report

    def run_predeploy_checks(
        self,
        current_positions: Dict[str, float],
        current_prices: Dict[str, float],
    ) -> SimulationReport:
        """
        Run standard pre-deploy validation scenarios.

        Args:
            current_positions: Current portfolio positions
            current_prices: Current market prices

        Returns:
            SimulationReport with validation results
        """
        scenarios = [
            # Scenario 1: Normal day (current state)
            SimulationScenario(
                name="Current State Validation",
                mock_positions=current_positions,
                mock_prices=current_prices,
                glidepath_day=-1,  # Skip glidepath
            ),

            # Scenario 2: Day 0 glidepath
            SimulationScenario(
                name="Glidepath Day 0",
                mock_positions=current_positions,
                mock_prices=current_prices,
                glidepath_day=0,
            ),

            # Scenario 3: Day 1 glidepath
            SimulationScenario(
                name="Glidepath Day 1",
                mock_positions=current_positions,
                mock_prices=current_prices,
                glidepath_day=1,
            ),

            # Scenario 4: Day 5 glidepath (mid-point)
            SimulationScenario(
                name="Glidepath Day 5",
                mock_positions=current_positions,
                mock_prices=current_prices,
                glidepath_day=5,
            ),

            # Scenario 5: Day 10+ (full targets)
            SimulationScenario(
                name="Glidepath Day 10+",
                mock_positions=current_positions,
                mock_prices=current_prices,
                glidepath_day=10,
            ),
        ]

        return self.run_scenarios(scenarios)

    def _compute_mock_targets(
        self, scenario: SimulationScenario
    ) -> Dict[str, float]:
        """
        Compute mock target positions for simulation.

        In real system, this would come from strategy.compute().
        For simulation, we use a simplified model.
        """
        # Simple target: scale current positions toward a target gross exposure
        target_gross_pct = 1.6  # 160% gross
        current_gross = self._calc_gross_exposure(
            scenario.mock_positions,
            scenario.mock_prices,
            scenario.fx_rates,
        )

        if current_gross == 0:
            return scenario.mock_positions.copy()

        scale_factor = (scenario.nav * target_gross_pct) / max(current_gross, 1)

        targets = {}
        for inst_id, qty in scenario.mock_positions.items():
            targets[inst_id] = qty * min(scale_factor, 2.0)  # Cap at 2x

        return targets

    def _calc_gross_exposure(
        self,
        positions: Dict[str, float],
        prices: Dict[str, float],
        fx_rates: Dict[str, float],
    ) -> float:
        """Calculate gross exposure from positions."""
        total = 0.0
        for inst_id, qty in positions.items():
            price = prices.get(inst_id, 0.0)
            notional = abs(qty) * price

            # Apply FX conversion (simplified)
            for sleeve, instruments in self.instruments_config.items():
                if inst_id in instruments:
                    spec = instruments[inst_id]
                    currency = spec.get("currency", "USD")
                    if currency == "EUR":
                        notional *= fx_rates.get("EURUSD", 1.0)
                    elif currency == "GBP":
                        notional *= fx_rates.get("GBPUSD", 1.0)
                    break

            total += notional

        return total


def create_standard_scenarios(
    instruments_config: Dict[str, Any],
) -> List[SimulationScenario]:
    """
    Create standard simulation scenarios for testing.

    Args:
        instruments_config: Instrument configuration

    Returns:
        List of standard scenarios
    """
    # Build sample positions using internal IDs
    sample_positions = {}
    sample_prices = {}

    for sleeve, instruments in instruments_config.items():
        for inst_id, spec in instruments.items():
            if isinstance(spec, dict):
                # Add a small position
                sample_positions[inst_id] = 10.0
                sample_prices[inst_id] = 100.0  # Default price

    return [
        SimulationScenario(
            name="Empty Portfolio",
            mock_positions={},
            mock_prices=sample_prices,
            glidepath_day=0,
        ),
        SimulationScenario(
            name="Single Position",
            mock_positions={list(sample_positions.keys())[0]: 10.0},
            mock_prices=sample_prices,
            glidepath_day=1,
        ),
        SimulationScenario(
            name="Full Portfolio",
            mock_positions=sample_positions,
            mock_prices=sample_prices,
            glidepath_day=5,
        ),
        SimulationScenario(
            name="High VIX",
            mock_positions=sample_positions,
            mock_prices=sample_prices,
            vix_level=35.0,
            glidepath_day=5,
        ),
    ]
