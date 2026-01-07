"""
Strategy Integration for Risk Parity + Sovereign Overlay.

Phase 3: Merges risk parity weights with sovereign overlay positions
and integrates with existing strategy logic.

Key Features:
- Risk parity weight integration with sleeve targets
- Sovereign overlay position merging
- Constraint enforcement (max country exposure, hedge budget, leverage)
- Unified order generation
"""

import logging
from datetime import datetime, date
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field

from .portfolio import PortfolioState, Sleeve
from .strategy_logic import (
    Strategy, StrategyOutput, SleeveTargets, OrderSpec, FXHedgePolicy
)
from .risk_engine import RiskEngine, RiskDecision, RiskRegime
from .risk_parity import RiskParityAllocator, RiskParityWeights, RiskParityConfig, Regime
from .sovereign_overlay import (
    SovereignCrisisOverlay, OverlayConfig, SOVEREIGN_PROXIES
)
from .tail_hedge import TailHedgeManager
from .hedge_ladder import (
    HedgeLadderEngine, HedgeLadderConfig, HedgeBucket
)

# v3.0: EU Sovereign Fragility Short
try:
    from .sovereign_rates_short import (
        SovereignRatesShortEngine, SovereignRatesShortConfig,
        FragmentationSignal, create_sovereign_rates_short_engine
    )
    SOVEREIGN_RATES_SHORT_AVAILABLE = True
except ImportError:
    SOVEREIGN_RATES_SHORT_AVAILABLE = False
from .data_feeds import DataFeed
from .fx_rates import FXRates, get_fx_rates

logger = logging.getLogger(__name__)


@dataclass
class IntegratedStrategyConfig:
    """Configuration for integrated strategy."""
    # Risk parity settings
    use_risk_parity: bool = True
    risk_parity_weight: float = 0.7  # Blend with existing weights

    # Sovereign overlay settings (v3.0: disabled, replaced by sovereign_rates_short)
    use_sovereign_overlay: bool = False
    sovereign_budget_pct: float = 0.00  # Disabled

    # v2.4: Hedge ladder settings
    use_hedge_ladder: bool = True
    hedge_ladder_budget_pct: float = 0.004  # 40bps

    # v3.0: EU Sovereign Fragility Short settings
    use_sovereign_rates_short: bool = True  # Replaces sovereign_overlay

    # Constraints
    max_single_country_pct: float = 0.15  # Max 15% per country
    max_hedge_budget_pct: float = 0.05    # Max 5% on hedges total
    max_gross_leverage: float = 2.0

    # Blending mode
    blend_mode: str = "weighted_average"  # or "risk_parity_override"

    @classmethod
    def from_settings(cls, settings: Dict[str, Any]) -> "IntegratedStrategyConfig":
        """Create config from settings dict."""
        int_settings = settings.get('strategy_integration', {})

        # v3.0: Check if sovereign_rates_short is enabled in settings
        srs_settings = settings.get('sovereign_rates_short', {})
        use_srs = srs_settings.get('enabled', True)

        # v3.0: Disable sovereign_overlay if sovereign_rates_short is enabled
        overlay_settings = settings.get('sovereign_overlay', {})
        use_overlay = overlay_settings.get('enabled', False) and not use_srs

        return cls(
            use_risk_parity=int_settings.get('use_risk_parity', True),
            risk_parity_weight=int_settings.get('risk_parity_weight', 0.7),
            use_sovereign_overlay=use_overlay,
            sovereign_budget_pct=int_settings.get('sovereign_budget_pct', 0.00),
            use_hedge_ladder=int_settings.get('use_hedge_ladder', True),
            hedge_ladder_budget_pct=int_settings.get('hedge_ladder_budget_pct', 0.004),
            use_sovereign_rates_short=use_srs,
            max_single_country_pct=int_settings.get('max_single_country_pct', 0.15),
            max_hedge_budget_pct=int_settings.get('max_hedge_budget_pct', 0.05),
            max_gross_leverage=int_settings.get('max_gross_leverage', 2.0),
            blend_mode=int_settings.get('blend_mode', 'weighted_average'),
        )


@dataclass
class IntegratedStrategyOutput:
    """Complete output from integrated strategy."""
    # Base strategy output
    base_output: StrategyOutput

    # Risk parity adjustments
    risk_parity_weights: Optional[RiskParityWeights]
    final_sleeve_weights: Dict[Sleeve, float]

    # Sovereign overlay orders (legacy, v3.0: disabled)
    sovereign_orders: List[OrderSpec]

    # v2.4: Hedge ladder orders
    hedge_ladder_orders: List[OrderSpec] = field(default_factory=list)

    # v3.0: EU Sovereign Fragility Short orders
    sovereign_rates_short_orders: List[OrderSpec] = field(default_factory=list)

    # Combined orders
    all_orders: List[OrderSpec] = field(default_factory=list)

    # Constraints applied
    constraints_applied: List[str] = field(default_factory=list)

    # Summary
    commentary: str = ""
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging/metrics."""
        return {
            "base_regime": self.base_output.regime.value,
            "base_scaling": self.base_output.scaling_factor,
            "risk_parity": self.risk_parity_weights.to_dict() if self.risk_parity_weights else None,
            "final_weights": {k.value: round(v, 4) for k, v in self.final_sleeve_weights.items()},
            "order_count": len(self.all_orders),
            "sovereign_order_count": len(self.sovereign_orders),
            "hedge_ladder_order_count": len(self.hedge_ladder_orders),
            "sovereign_rates_short_order_count": len(self.sovereign_rates_short_orders),
            "constraints": self.constraints_applied,
            "timestamp": self.timestamp.isoformat(),
        }


class IntegratedStrategy:
    """
    Integrated Strategy with Risk Parity + Sovereign Overlay.

    Combines:
    1. Base multi-sleeve strategy (Strategy class)
    2. Risk parity allocation (RiskParityAllocator)
    3. Sovereign crisis overlay (SovereignCrisisOverlay)
    4. Tail hedge management (TailHedgeManager)

    Enforces portfolio-level constraints and generates unified orders.
    """

    def __init__(
        self,
        settings: Dict[str, Any],
        instruments_config: Dict[str, Any],
        risk_engine: RiskEngine,
        tail_hedge_manager: Optional[TailHedgeManager] = None
    ):
        """
        Initialize integrated strategy.

        Args:
            settings: Application settings
            instruments_config: Instrument configurations
            risk_engine: Risk engine instance
            tail_hedge_manager: Optional tail hedge manager
        """
        self.settings = settings
        self.instruments = instruments_config
        self.risk_engine = risk_engine

        # Configuration
        self.config = IntegratedStrategyConfig.from_settings(settings)

        # Base strategy
        self.base_strategy = Strategy(
            settings=settings,
            instruments_config=instruments_config,
            risk_engine=risk_engine,
            tail_hedge_manager=tail_hedge_manager
        )

        # Risk parity allocator
        self.risk_parity: Optional[RiskParityAllocator] = None
        if self.config.use_risk_parity:
            rp_config = RiskParityConfig.from_settings(settings)
            self.risk_parity = RiskParityAllocator(rp_config)

        # Sovereign overlay
        self.sovereign_overlay: Optional[SovereignCrisisOverlay] = None
        if self.config.use_sovereign_overlay:
            overlay_config = OverlayConfig.from_settings(settings)
            overlay_config.annual_budget_pct = self.config.sovereign_budget_pct
            self.sovereign_overlay = SovereignCrisisOverlay(overlay_config)

        # v2.4: Hedge ladder
        self.hedge_ladder: Optional[HedgeLadderEngine] = None
        if self.config.use_hedge_ladder:
            ladder_config = HedgeLadderConfig.from_settings(settings)
            ladder_config.annual_budget_pct = self.config.hedge_ladder_budget_pct
            self.hedge_ladder = HedgeLadderEngine(ladder_config)

        # v3.0: EU Sovereign Fragility Short
        self.sovereign_rates_short: Optional['SovereignRatesShortEngine'] = None
        if self.config.use_sovereign_rates_short and SOVEREIGN_RATES_SHORT_AVAILABLE:
            self.sovereign_rates_short = create_sovereign_rates_short_engine(settings)
            logger.info("Sovereign rates short engine initialized")

        # Tail hedge manager (passed or created)
        self.tail_hedge_manager = tail_hedge_manager or TailHedgeManager(
            settings=settings,
            instruments_config=instruments_config
        )

        # State tracking
        self._last_output: Optional[IntegratedStrategyOutput] = None

    def compute_strategy(
        self,
        portfolio: PortfolioState,
        data_feed: DataFeed,
        risk_decision: RiskDecision,
        fx_rates: Optional[FXRates] = None,
        today: Optional[date] = None
    ) -> IntegratedStrategyOutput:
        """
        Compute integrated strategy output.

        Main entry point for strategy computation.

        Args:
            portfolio: Current portfolio state
            data_feed: Data feed for prices
            risk_decision: Risk engine decision
            fx_rates: FX rates for currency conversion
            today: Current date

        Returns:
            IntegratedStrategyOutput with all targets and orders
        """
        today = today or date.today()
        fx_rates = fx_rates or get_fx_rates()
        constraints_applied = []

        # Step 1: Compute base strategy output
        base_output = self.base_strategy.compute_all_sleeve_targets(
            portfolio=portfolio,
            data_feed=data_feed,
            risk_decision=risk_decision,
            fx_rates=fx_rates
        )

        # Step 2: Compute risk parity weights (if enabled)
        rp_weights = None
        if self.risk_parity and self.config.use_risk_parity:
            # Update sleeve returns from portfolio
            self._update_risk_parity_returns(portfolio)

            # v2.4: Convert risk regime to risk parity regime for regime-aware blending
            rp_regime = self._convert_to_rp_regime(risk_decision.regime)

            # Compute weights with regime-aware blending
            rp_weights = self.risk_parity.compute_risk_parity_weights(
                portfolio_state=portfolio,
                today=today,
                regime=rp_regime
            )

            logger.info(
                f"Risk parity: regime={rp_regime.value}, "
                f"expected_vol={rp_weights.expected_portfolio_vol:.2%}, "
                f"scaling={rp_weights.scaling_factor:.2f}"
            )

        # Step 3: Blend weights
        final_weights = self._blend_weights(
            base_weights={
                sleeve: targets.target_weight
                for sleeve, targets in base_output.sleeve_targets.items()
            },
            rp_weights=rp_weights
        )

        # Step 4: Apply constraints to weights
        final_weights, weight_constraints = self._apply_weight_constraints(
            final_weights, portfolio.nav
        )
        constraints_applied.extend(weight_constraints)

        # Step 5: Generate sovereign overlay orders (if enabled)
        sovereign_orders = []
        if self.sovereign_overlay and self.config.use_sovereign_overlay:
            sovereign_orders = self.sovereign_overlay.ensure_overlay_coverage(
                portfolio_state=portfolio,
                data_feed=data_feed,
                today=today
            )
            logger.info(f"Sovereign overlay: {len(sovereign_orders)} orders generated")

        # Step 5b (v2.4): Generate hedge ladder orders (if enabled)
        hedge_ladder_orders = []
        if self.hedge_ladder and self.config.use_hedge_ladder:
            try:
                # Get current VIX for roll decisions
                vix_level = data_feed.get_last_price("vix_index") or 15.0

                # Compute target positions for the ladder
                ladder_positions = self.hedge_ladder.compute_ladder_positions(
                    nav=portfolio.nav,
                    underlying_price=data_feed.get_last_price("spy_etf") or 500.0,
                    today=today
                )

                # Compute roll decisions based on existing positions and VIX
                roll_orders = self.hedge_ladder.compute_roll_decisions(
                    current_positions=portfolio.positions,
                    current_vix=vix_level,
                    today=today
                )
                hedge_ladder_orders.extend(roll_orders)

                logger.info(
                    f"Hedge ladder: {len(ladder_positions)} target legs, "
                    f"{len(roll_orders)} roll orders, VIX={vix_level:.1f}"
                )
            except Exception as e:
                logger.warning(f"Hedge ladder computation failed: {e}")

        # Step 5c (v3.0): Generate EU Sovereign Fragility Short orders
        sovereign_rates_short_orders = []
        if self.sovereign_rates_short and self.config.use_sovereign_rates_short:
            try:
                # Get required market data for fragmentation signal
                vix_level = data_feed.get_last_price("vix_index") or 15.0
                stress_score = risk_decision.scaling_diagnostics.get('stress_score', 0.0) \
                    if risk_decision.scaling_diagnostics else 0.0

                # Get yield data (if available) or use defaults
                # In production, these would come from a bond data feed
                btp_yield = data_feed.get_last_price("btp_10y_yield") or 4.0
                bund_yield = data_feed.get_last_price("bund_10y_yield") or 2.5

                # Compute fragmentation signal
                signal = self.sovereign_rates_short.compute_fragmentation_signal(
                    btp_yield=btp_yield,
                    bund_yield=bund_yield,
                    vix_level=vix_level,
                    stress_score=stress_score,
                    as_of=today
                )

                # Convert risk regime for the engine
                from .risk_engine import RiskRegime as RReg
                srs_regime = RReg(risk_decision.regime.value)

                # Check if futures are available (use ETF fallback in paper account)
                use_etf_fallback = True  # TODO: Check IBKR permissions for EUREX futures

                # Generate orders
                sovereign_rates_short_orders = self.sovereign_rates_short.generate_orders(
                    portfolio_state=portfolio,
                    signal=signal,
                    regime=srs_regime,
                    use_etf_fallback=use_etf_fallback,
                    today=today
                )

                logger.info(
                    f"Sovereign rates short: {len(sovereign_rates_short_orders)} orders, "
                    f"spread_z={signal.spread_z:.2f}, deflation_guard={signal.deflation_guard}, "
                    f"VIX={vix_level:.1f}"
                )
            except Exception as e:
                logger.warning(f"Sovereign rates short computation failed: {e}")

        # Step 6: Combine all orders
        all_orders = list(base_output.orders)
        all_orders.extend(sovereign_orders)
        all_orders.extend(hedge_ladder_orders)
        all_orders.extend(sovereign_rates_short_orders)

        # Step 7: Apply order-level constraints
        all_orders, order_constraints = self._apply_order_constraints(
            all_orders, portfolio, data_feed
        )
        constraints_applied.extend(order_constraints)

        # Step 8: Build commentary
        commentary = self._build_commentary(
            base_output, rp_weights, final_weights, sovereign_orders,
            hedge_ladder_orders, constraints_applied, risk_decision,
            sovereign_rates_short_orders
        )

        output = IntegratedStrategyOutput(
            base_output=base_output,
            risk_parity_weights=rp_weights,
            final_sleeve_weights=final_weights,
            sovereign_orders=sovereign_orders,
            hedge_ladder_orders=hedge_ladder_orders,
            sovereign_rates_short_orders=sovereign_rates_short_orders,
            all_orders=all_orders,
            constraints_applied=constraints_applied,
            commentary=commentary
        )

        self._last_output = output
        return output

    def _update_risk_parity_returns(self, portfolio: PortfolioState) -> None:
        """Update sleeve returns for risk parity calculation."""
        if not self.risk_parity:
            return

        # Extract returns from portfolio positions by sleeve
        for sleeve in Sleeve:
            sleeve_positions = [
                pos for pos in portfolio.positions.values()
                if pos.sleeve == sleeve
            ]

            if not sleeve_positions:
                continue

            # Use portfolio-level returns as proxy
            # In production, would compute actual sleeve returns
            if hasattr(portfolio, 'returns_series') and portfolio.returns_series is not None:
                self.risk_parity.update_sleeve_returns(sleeve, portfolio.returns_series)

    def _convert_to_rp_regime(self, risk_regime: RiskRegime) -> Regime:
        """
        Convert RiskRegime from risk_engine to Regime for risk_parity.

        v2.4: Maps the existing risk regime system to the risk parity
        regime-aware blending system.

        Args:
            risk_regime: Risk regime from risk_engine

        Returns:
            Regime enum for risk_parity
        """
        # Map RiskRegime enum values to Regime enum values
        regime_map = {
            RiskRegime.NORMAL: Regime.NORMAL,
            RiskRegime.ELEVATED: Regime.ELEVATED,
            RiskRegime.CRISIS: Regime.CRISIS,
        }

        return regime_map.get(risk_regime, Regime.NORMAL)

    def _blend_weights(
        self,
        base_weights: Dict[Sleeve, float],
        rp_weights: Optional[RiskParityWeights]
    ) -> Dict[Sleeve, float]:
        """
        Blend base strategy weights with risk parity weights.

        Args:
            base_weights: Weights from base strategy
            rp_weights: Weights from risk parity allocator

        Returns:
            Blended weights
        """
        if rp_weights is None or not self.config.use_risk_parity:
            return base_weights

        rp_weight = self.config.risk_parity_weight
        base_weight = 1.0 - rp_weight

        blended = {}
        all_sleeves = set(base_weights.keys()) | set(rp_weights.weights.keys())

        for sleeve in all_sleeves:
            base = base_weights.get(sleeve, 0.0)
            rp = rp_weights.weights.get(sleeve, 0.0)

            if self.config.blend_mode == "risk_parity_override":
                # Full override to risk parity weights
                blended[sleeve] = rp if rp > 0 else base
            else:
                # Weighted average (default)
                blended[sleeve] = base_weight * base + rp_weight * rp

        # Normalize to sum to 1.0
        total = sum(blended.values())
        if total > 0:
            blended = {k: v / total for k, v in blended.items()}

        return blended

    def _apply_weight_constraints(
        self,
        weights: Dict[Sleeve, float],
        nav: float
    ) -> Tuple[Dict[Sleeve, float], List[str]]:
        """
        Apply portfolio-level weight constraints.

        Args:
            weights: Unconstrained weights
            nav: Current NAV

        Returns:
            Tuple of (constrained_weights, constraints_applied)
        """
        constraints = []
        constrained = dict(weights)

        # Max hedge budget constraint
        hedge_sleeves = [Sleeve.EUROPE_VOL_CONVEX]
        hedge_weight = sum(constrained.get(s, 0) for s in hedge_sleeves)

        if hedge_weight > self.config.max_hedge_budget_pct:
            reduction_factor = self.config.max_hedge_budget_pct / hedge_weight
            for sleeve in hedge_sleeves:
                if sleeve in constrained:
                    constrained[sleeve] *= reduction_factor
            constraints.append(
                f"Hedge budget capped: {hedge_weight:.1%} -> {self.config.max_hedge_budget_pct:.1%}"
            )

        # Normalize
        total = sum(constrained.values())
        if total > 0:
            constrained = {k: v / total for k, v in constrained.items()}

        return constrained, constraints

    def _apply_order_constraints(
        self,
        orders: List[OrderSpec],
        portfolio: PortfolioState,
        data_feed: DataFeed
    ) -> Tuple[List[OrderSpec], List[str]]:
        """
        Apply order-level constraints.

        Args:
            orders: Unconstrained orders
            portfolio: Current portfolio state
            data_feed: Data feed for prices

        Returns:
            Tuple of (constrained_orders, constraints_applied)
        """
        constraints = []
        constrained_orders = []

        # Check gross leverage after all orders
        current_gross = portfolio.gross_exposure
        nav = portfolio.nav

        for order in orders:
            try:
                # Estimate order impact on gross exposure
                price = data_feed.get_last_price(order.instrument_id)
                order_notional = order.quantity * price

                # Check if order would exceed leverage limit
                if order.side == "BUY":
                    new_gross = current_gross + order_notional
                else:
                    new_gross = current_gross  # Sells reduce exposure

                if new_gross / nav > self.config.max_gross_leverage:
                    # Reduce order size to fit leverage limit
                    max_notional = (self.config.max_gross_leverage * nav) - current_gross
                    if max_notional > 0:
                        reduced_qty = int(max_notional / price)
                        if reduced_qty > 0:
                            order.quantity = reduced_qty
                            constraints.append(
                                f"Order {order.instrument_id} reduced for leverage: "
                                f"{order.quantity} -> {reduced_qty}"
                            )
                            constrained_orders.append(order)
                    else:
                        constraints.append(
                            f"Order {order.instrument_id} skipped: leverage limit"
                        )
                else:
                    constrained_orders.append(order)
                    if order.side == "BUY":
                        current_gross = new_gross

            except Exception as e:
                # If can't validate, include the order
                logger.debug(f"Order constraint check failed for {order.instrument_id}: {e}")
                constrained_orders.append(order)

        return constrained_orders, constraints

    def _build_commentary(
        self,
        base_output: StrategyOutput,
        rp_weights: Optional[RiskParityWeights],
        final_weights: Dict[Sleeve, float],
        sovereign_orders: List[OrderSpec],
        hedge_ladder_orders: List[OrderSpec],
        constraints: List[str],
        risk_decision: RiskDecision,
        sovereign_rates_short_orders: Optional[List[OrderSpec]] = None
    ) -> str:
        """Build comprehensive strategy commentary."""
        sovereign_rates_short_orders = sovereign_rates_short_orders or []

        lines = [
            f"=== Integrated Strategy Update - {datetime.now().strftime('%Y-%m-%d %H:%M')} ===",
            "",
            f"Regime: {risk_decision.regime.value}",
            f"Base Scaling: {base_output.scaling_factor:.2f}",
        ]

        if rp_weights:
            lines.extend([
                "",
                "Risk Parity:",
                f"  Expected Vol: {rp_weights.expected_portfolio_vol:.2%}",
                f"  Target Vol: {rp_weights.target_vol:.2%}",
                f"  Scaling Factor: {rp_weights.scaling_factor:.2f}",
                f"  Rebalance: {rp_weights.rebalance_reason}",
            ])

        lines.extend([
            "",
            "Final Sleeve Weights:",
        ])
        for sleeve, weight in sorted(final_weights.items(), key=lambda x: -x[1]):
            lines.append(f"  {sleeve.value}: {weight:.1%}")

        if sovereign_orders:
            lines.extend([
                "",
                f"Sovereign Overlay: {len(sovereign_orders)} orders",
            ])
            for order in sovereign_orders[:5]:  # First 5
                lines.append(f"  {order.side} {order.quantity} {order.instrument_id}")

        if hedge_ladder_orders:
            lines.extend([
                "",
                f"Hedge Ladder: {len(hedge_ladder_orders)} orders",
            ])
            for order in hedge_ladder_orders[:5]:  # First 5
                lines.append(f"  {order.side} {order.quantity} {order.instrument_id}")

        if sovereign_rates_short_orders:
            lines.extend([
                "",
                f"Sovereign Rates Short: {len(sovereign_rates_short_orders)} orders",
            ])
            for order in sovereign_rates_short_orders[:5]:  # First 5
                lines.append(f"  {order.side} {order.quantity} {order.instrument_id}")

            # Add engine state if available
            if self.sovereign_rates_short:
                srs_summary = self.sovereign_rates_short.get_summary()
                if srs_summary.get('last_signal'):
                    lines.append(f"  Spread Z: {srs_summary['last_signal'].get('spread_z', 'N/A')}")
                    lines.append(f"  Deflation Guard: {srs_summary['last_signal'].get('deflation_guard', 'N/A')}")
                if srs_summary.get('last_sizing'):
                    lines.append(f"  Target Weight: {srs_summary['last_sizing'].get('target_weight', 0):.1%}")

        if constraints:
            lines.extend([
                "",
                "Constraints Applied:",
            ])
            for constraint in constraints:
                lines.append(f"  - {constraint}")

        if risk_decision.warnings:
            lines.extend([
                "",
                "Risk Warnings:",
            ])
            for warning in risk_decision.warnings:
                lines.append(f"  - {warning}")

        return "\n".join(lines)

    def get_summary(self) -> Dict[str, Any]:
        """Get summary of integrated strategy state."""
        summary = {
            "config": {
                "use_risk_parity": self.config.use_risk_parity,
                "risk_parity_weight": self.config.risk_parity_weight,
                "use_sovereign_overlay": self.config.use_sovereign_overlay,
                "sovereign_budget_pct": self.config.sovereign_budget_pct,
                "use_hedge_ladder": self.config.use_hedge_ladder,
                "hedge_ladder_budget_pct": self.config.hedge_ladder_budget_pct,
                "use_sovereign_rates_short": self.config.use_sovereign_rates_short,
                "max_gross_leverage": self.config.max_gross_leverage,
                "blend_mode": self.config.blend_mode,
            },
            "risk_parity": self.risk_parity.get_summary() if self.risk_parity else None,
            "sovereign_overlay": self.sovereign_overlay.get_summary() if self.sovereign_overlay else None,
            "hedge_ladder": self.hedge_ladder.get_summary() if self.hedge_ladder else None,
            "sovereign_rates_short": self.sovereign_rates_short.get_summary() if self.sovereign_rates_short else None,
            "last_output": self._last_output.to_dict() if self._last_output else None,
        }
        return summary


def create_integrated_strategy(
    settings: Dict[str, Any],
    instruments_config: Dict[str, Any],
    risk_engine: Optional[RiskEngine] = None,
    tail_hedge_manager: Optional[TailHedgeManager] = None
) -> IntegratedStrategy:
    """
    Factory function to create integrated strategy.

    Args:
        settings: Application settings
        instruments_config: Instrument configurations
        risk_engine: Optional risk engine (created if not provided)
        tail_hedge_manager: Optional tail hedge manager

    Returns:
        Configured IntegratedStrategy instance
    """
    # Create risk engine if not provided
    if risk_engine is None:
        risk_engine = RiskEngine(settings)

    return IntegratedStrategy(
        settings=settings,
        instruments_config=instruments_config,
        risk_engine=risk_engine,
        tail_hedge_manager=tail_hedge_manager
    )
