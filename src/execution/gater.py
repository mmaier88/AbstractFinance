"""
Trade Gater - Cost-vs-benefit trade filtering.

Phase 2 Enhancement: Dynamic "no-trade zones" that skip low-value trades
when predicted cost exceeds predicted benefit.

Rules:
- Trade only if: benefit_usd >= cost_multiplier * predicted_cost_usd
- Always trade if: risk limits breached, emergency de-risk, or hedge corrections
- Stricter gating in elevated/crisis regimes
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Tuple, Any

from .types import OrderIntent


logger = logging.getLogger(__name__)


class RiskRegime(str, Enum):
    """Market risk regime."""
    NORMAL = "NORMAL"
    ELEVATED = "ELEVATED"
    CRISIS = "CRISIS"


@dataclass
class GatingConfig:
    """Configuration for trade gating."""
    enabled: bool = True
    min_drift_pct: float = 0.002          # 0.2% minimum drift
    cost_multiplier: float = 1.5          # Require benefit > 1.5x cost
    always_trade_if_limit_breach: bool = True
    commission_bps_default: float = 0.5   # 0.5 bps commission estimate
    regime_multipliers: Dict[str, float] = field(default_factory=lambda: {
        "NORMAL": 1.0,
        "ELEVATED": 1.5,
        "CRISIS": 2.5,
    })


@dataclass
class CostEstimate:
    """Estimated cost breakdown for a trade."""
    instrument_id: str
    notional_usd: float
    slippage_bps: float
    commission_bps: float
    borrow_bps_daily: float = 0.0        # For shorts
    dividend_buffer_bps: float = 0.0      # For shorts near ex-div
    total_cost_bps: float = 0.0
    total_cost_usd: float = 0.0

    def __post_init__(self):
        self.total_cost_bps = (
            self.slippage_bps +
            self.commission_bps +
            self.borrow_bps_daily +
            self.dividend_buffer_bps
        )
        self.total_cost_usd = abs(self.notional_usd) * (self.total_cost_bps / 10_000)


@dataclass
class GatingDecision:
    """Result of trade gating evaluation."""
    instrument_id: str
    should_trade: bool
    reason: str
    drift_pct: float
    predicted_cost_usd: float
    predicted_benefit_usd: float
    regime: RiskRegime
    is_override: bool = False             # True if trading due to override


@dataclass
class GatingOverrides:
    """Conditions that override normal gating logic."""
    gross_limit_breached: bool = False
    net_limit_breached: bool = False
    emergency_derisk_active: bool = False
    fx_band_breached: bool = False
    risk_reconciliation_failed: bool = False
    manual_override: bool = False


class TradeGater:
    """
    Filters trades based on cost-vs-benefit analysis.

    Phase 2 Enhancement: Dynamic no-trade zones prevent
    excessive turnover from small, low-value trades.

    Usage:
        gater = TradeGater(config, slippage_model, borrow_service)
        decisions = gater.filter_intents(intents, positions, targets, regime)
        tradeable = [i for i, d in zip(intents, decisions) if d.should_trade]
    """

    def __init__(
        self,
        config: Optional[GatingConfig] = None,
        slippage_model: Optional[Any] = None,   # SlippageModel
        borrow_service: Optional[Any] = None,   # BorrowService
        dividend_service: Optional[Any] = None, # CorporateActionsService
    ):
        """
        Initialize trade gater.

        Args:
            config: Gating configuration
            slippage_model: SlippageModel for cost prediction
            borrow_service: BorrowService for short costs
            dividend_service: CorporateActionsService for dividend awareness
        """
        self.config = config or GatingConfig()
        self.slippage_model = slippage_model
        self.borrow_service = borrow_service
        self.dividend_service = dividend_service

        # Tracking
        self.gated_today: List[GatingDecision] = []
        self.traded_today: List[GatingDecision] = []

    def filter_intents(
        self,
        intents: List[OrderIntent],
        current_positions: Dict[str, float],  # instrument_id -> notional_usd
        target_positions: Dict[str, float],   # instrument_id -> notional_usd
        nav_usd: float,
        regime: RiskRegime = RiskRegime.NORMAL,
        overrides: Optional[GatingOverrides] = None,
    ) -> List[GatingDecision]:
        """
        Filter order intents based on cost-vs-benefit.

        Args:
            intents: List of order intents to evaluate
            current_positions: Current position notionals
            target_positions: Target position notionals
            nav_usd: Current NAV for drift calculation
            regime: Current risk regime
            overrides: Override conditions

        Returns:
            List of GatingDecision for each intent
        """
        if not self.config.enabled:
            # Gating disabled - allow all trades
            return [
                GatingDecision(
                    instrument_id=i.instrument_id,
                    should_trade=True,
                    reason="Gating disabled",
                    drift_pct=0.0,
                    predicted_cost_usd=0.0,
                    predicted_benefit_usd=0.0,
                    regime=regime,
                )
                for i in intents
            ]

        overrides = overrides or GatingOverrides()
        decisions = []

        for intent in intents:
            decision = self._evaluate_intent(
                intent=intent,
                current_positions=current_positions,
                target_positions=target_positions,
                nav_usd=nav_usd,
                regime=regime,
                overrides=overrides,
            )
            decisions.append(decision)

            # Track
            if decision.should_trade:
                self.traded_today.append(decision)
            else:
                self.gated_today.append(decision)

        return decisions

    def _evaluate_intent(
        self,
        intent: OrderIntent,
        current_positions: Dict[str, float],
        target_positions: Dict[str, float],
        nav_usd: float,
        regime: RiskRegime,
        overrides: GatingOverrides,
    ) -> GatingDecision:
        """Evaluate a single intent."""
        inst_id = intent.instrument_id

        # Check for overrides first
        override_reason = self._check_overrides(intent, overrides)
        if override_reason:
            return GatingDecision(
                instrument_id=inst_id,
                should_trade=True,
                reason=f"Override: {override_reason}",
                drift_pct=0.0,
                predicted_cost_usd=0.0,
                predicted_benefit_usd=0.0,
                regime=regime,
                is_override=True,
            )

        # Calculate drift
        current = current_positions.get(inst_id, 0.0)
        target = target_positions.get(inst_id, 0.0)
        trade_notional = intent.notional_usd or abs(target - current)

        drift_pct = abs(current - target) / nav_usd if nav_usd > 0 else 0.0

        # Get regime multiplier
        regime_mult = self.config.regime_multipliers.get(regime.value, 1.0)

        # Check minimum drift threshold
        min_drift = self.config.min_drift_pct * regime_mult
        if drift_pct < min_drift:
            return GatingDecision(
                instrument_id=inst_id,
                should_trade=False,
                reason=f"Drift {drift_pct:.4f} < min {min_drift:.4f}",
                drift_pct=drift_pct,
                predicted_cost_usd=0.0,
                predicted_benefit_usd=0.0,
                regime=regime,
            )

        # Estimate costs
        cost_estimate = self._estimate_costs(intent, trade_notional, regime)

        # Estimate benefit (simplified: just use drift reduction as proxy)
        # More sophisticated: could use risk reduction, TE reduction, etc.
        benefit_usd = abs(trade_notional) * drift_pct

        # Apply regime-adjusted cost multiplier
        effective_cost_mult = self.config.cost_multiplier * regime_mult
        required_benefit = cost_estimate.total_cost_usd * effective_cost_mult

        # Decision
        should_trade = benefit_usd >= required_benefit

        if should_trade:
            reason = f"Benefit ${benefit_usd:.0f} >= {effective_cost_mult:.1f}x cost ${cost_estimate.total_cost_usd:.0f}"
        else:
            reason = f"Benefit ${benefit_usd:.0f} < {effective_cost_mult:.1f}x cost ${cost_estimate.total_cost_usd:.0f}"

        return GatingDecision(
            instrument_id=inst_id,
            should_trade=should_trade,
            reason=reason,
            drift_pct=drift_pct,
            predicted_cost_usd=cost_estimate.total_cost_usd,
            predicted_benefit_usd=benefit_usd,
            regime=regime,
        )

    def _check_overrides(
        self,
        intent: OrderIntent,
        overrides: GatingOverrides,
    ) -> Optional[str]:
        """Check if any override conditions apply."""
        if not self.config.always_trade_if_limit_breach:
            return None

        if overrides.gross_limit_breached:
            return "gross_limit_breach"

        if overrides.net_limit_breached:
            return "net_limit_breach"

        if overrides.emergency_derisk_active:
            return "emergency_derisk"

        if overrides.fx_band_breached:
            return "fx_hedge_correction"

        if overrides.risk_reconciliation_failed:
            return "reconciliation_failed"

        if overrides.manual_override:
            return "manual_override"

        # Check intent urgency
        if intent.urgency.value == "crisis":
            return "crisis_urgency"

        # Check intent reason
        if intent.reason in ("hedge", "crisis", "emergency"):
            return f"{intent.reason}_trade"

        return None

    def _estimate_costs(
        self,
        intent: OrderIntent,
        notional_usd: float,
        regime: RiskRegime,
    ) -> CostEstimate:
        """Estimate trading costs for an intent."""
        inst_id = intent.instrument_id

        # Slippage estimate
        slippage_bps = self._get_slippage_estimate(inst_id, intent.side)

        # Commission
        commission_bps = self.config.commission_bps_default

        # Borrow cost (for shorts)
        borrow_bps = 0.0
        if intent.side == "SELL":
            borrow_bps = self._get_borrow_cost_daily(inst_id)

        # Dividend buffer (for shorts)
        dividend_bps = 0.0
        if intent.side == "SELL":
            dividend_bps = self._get_dividend_buffer(inst_id)

        return CostEstimate(
            instrument_id=inst_id,
            notional_usd=notional_usd,
            slippage_bps=slippage_bps,
            commission_bps=commission_bps,
            borrow_bps_daily=borrow_bps,
            dividend_buffer_bps=dividend_bps,
        )

    def _get_slippage_estimate(
        self,
        instrument_id: str,
        side: str,
    ) -> float:
        """Get slippage estimate from model or use default."""
        if self.slippage_model is not None:
            try:
                return self.slippage_model.get_estimated_slippage_bps(
                    instrument_id=instrument_id,
                    side=side,
                )
            except Exception as e:
                logger.debug(f"SlippageModel error for {instrument_id}: {e}")

        # Default slippage estimates by rough asset class guess
        symbol = instrument_id.upper()
        if symbol.endswith(("FUT", "ES", "NQ", "FESX")):
            return 1.5  # Futures are liquid
        elif symbol in ("SPY", "QQQ", "IWM", "EEM", "VTI"):
            return 2.0  # Large ETFs
        else:
            return 5.0  # Default for stocks

    def _get_borrow_cost_daily(
        self,
        instrument_id: str,
    ) -> float:
        """Get daily borrow cost for shorts."""
        if self.borrow_service is not None:
            try:
                info = self.borrow_service.get_borrow_info(instrument_id)
                if info and info.fee_rate_annual_bps:
                    # Convert annual to daily
                    return info.fee_rate_annual_bps / 365.0
            except Exception as e:
                logger.debug(f"BorrowService error for {instrument_id}: {e}")

        # Default: assume 150 bps annual = ~0.41 bps daily
        return 0.41

    def _get_dividend_buffer(
        self,
        instrument_id: str,
    ) -> float:
        """Get dividend buffer for shorts near ex-div."""
        if self.dividend_service is not None:
            try:
                if self.dividend_service.is_near_ex_div(instrument_id):
                    return 5.0  # 5 bps buffer
            except Exception as e:
                logger.debug(f"DividendService error for {instrument_id}: {e}")

        return 0.0

    def get_daily_summary(self) -> Dict[str, Any]:
        """Get summary of gating decisions for the day."""
        total_gated = len(self.gated_today)
        total_traded = len(self.traded_today)
        total_override = sum(1 for d in self.traded_today if d.is_override)

        gated_notional = sum(d.predicted_benefit_usd for d in self.gated_today)
        traded_notional = sum(d.predicted_benefit_usd for d in self.traded_today)
        saved_cost = sum(d.predicted_cost_usd for d in self.gated_today)

        return {
            "total_evaluated": total_gated + total_traded,
            "total_gated": total_gated,
            "total_traded": total_traded,
            "total_override": total_override,
            "gated_notional_usd": gated_notional,
            "traded_notional_usd": traded_notional,
            "estimated_cost_saved_usd": saved_cost,
        }

    def reset_daily(self) -> None:
        """Reset daily tracking."""
        self.gated_today.clear()
        self.traded_today.clear()


# Singleton instance
_trade_gater: Optional[TradeGater] = None


def get_trade_gater() -> TradeGater:
    """Get singleton TradeGater instance."""
    global _trade_gater
    if _trade_gater is None:
        _trade_gater = TradeGater()
    return _trade_gater


def init_trade_gater(
    config: Optional[GatingConfig] = None,
    slippage_model: Optional[Any] = None,
    borrow_service: Optional[Any] = None,
    dividend_service: Optional[Any] = None,
) -> TradeGater:
    """Initialize the trade gater singleton."""
    global _trade_gater
    _trade_gater = TradeGater(
        config=config,
        slippage_model=slippage_model,
        borrow_service=borrow_service,
        dividend_service=dividend_service,
    )
    return _trade_gater
