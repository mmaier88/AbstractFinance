"""
EU Sovereign Fragility Short Engine for AbstractFinance.

Phase T (v3.0): Significant rates fragmentation hedge using BTP-Bund spread trades.

Key Features:
- DV01-neutral spread trade: Short BTP (Italy), Long Bund (Germany)
- Isolates fragmentation risk (not pure "rates up" bet)
- Deflation guard prevents losses in 2008/2020-style panics
- Hard/soft kill-switches for risk management
- Take-profit rules to capture fragmentation premium

Philosophy:
This is the cornerstone insurance for European fragmentation risk. When
periphery spreads widen vs core Germany, this sleeve profits. Critical:
deflation guard completely exits in risk-off + rates-down scenarios.
"""

import logging
import numpy as np
import pandas as pd
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from enum import Enum

from .portfolio import PortfolioState, Sleeve
from .strategy_logic import OrderSpec
from .risk_engine import RiskRegime

logger = logging.getLogger(__name__)


class KillSwitchType(Enum):
    """Kill-switch trigger types."""
    NONE = "none"
    SOFT = "soft"          # 50% reduction
    HARD = "hard"          # Full flatten


class SleeveState(Enum):
    """State of the sovereign rates short sleeve."""
    ACTIVE = "active"
    SOFT_KILLED = "soft_killed"
    HARD_KILLED = "hard_killed"
    REENABLE_PENDING = "reenable_pending"


@dataclass
class FragmentationSignal:
    """Fragmentation signal output."""
    spread_bps: float           # BTP-Bund spread in bps
    spread_z: float             # Z-score of spread (252-day lookback)
    spread_mom_20d: float       # 20-day change in spread (bps)
    bund_yield_mom_60d: float   # 60-day change in Bund yield (bps)
    bund_yield_change_5d: float # 5-day change in Bund yield (bps)
    bund_yield_mom_20d: float   # 20-day change in Bund yield (bps)
    vix_level: float
    stress_score: float
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def risk_off(self) -> bool:
        """Is market in risk-off mode?"""
        return self.vix_level > 30 or self.stress_score > 0.75

    @property
    def rates_down_shock(self) -> bool:
        """Is there a rates-down shock (bonds rallying)?"""
        return self.bund_yield_change_5d < -30 or self.bund_yield_mom_20d < -40

    @property
    def deflation_guard(self) -> bool:
        """Should we exit completely? (risk-off + rates rally)"""
        return self.risk_off and self.rates_down_shock


@dataclass
class DeflationScalerTier:
    """Configuration for a single deflation scaler tier."""
    vix_threshold: float
    bund_yield_5d_drop_bps: float
    scaler: float  # 0.0, 0.25, 0.5, 1.0


@dataclass
class SizingResult:
    """Position sizing output."""
    target_weight: float
    base_weight: float
    frag_multiplier: float
    rates_multiplier: float
    deflation_scaler: float  # v3.0: continuous scaler (1.0/0.5/0.25/0.0)
    max_weight: float
    deflation_guard: bool  # Legacy: kept for compatibility
    soft_kill: bool
    regime: RiskRegime
    reason: str


@dataclass
class DV01Position:
    """DV01-neutral position specification."""
    btp_contracts: int          # Short contracts (negative = short)
    bund_contracts: int         # Long contracts (positive = long)
    target_dv01: float          # Target DV01 exposure
    actual_net_dv01: float      # Actual net DV01 after rounding
    dv01_per_btp: float
    dv01_per_bund: float

    @property
    def is_neutral(self) -> bool:
        """Check if position is DV01-neutral (within 5%)."""
        if self.target_dv01 == 0:
            return self.btp_contracts == 0 and self.bund_contracts == 0
        return abs(self.actual_net_dv01) < abs(self.target_dv01) * 0.05


@dataclass
class SleeveTracker:
    """Tracks sleeve state for kill-switch and re-enable logic."""
    state: SleeveState = SleeveState.ACTIVE
    days_at_zero: int = 0
    entry_spread_avg_bps: float = 0.0
    entry_date: Optional[date] = None
    last_profit_take_date: Optional[date] = None
    cumulative_pnl: float = 0.0
    daily_pnl_history: List[float] = field(default_factory=list)

    def update_daily_pnl(self, pnl: float) -> None:
        """Update daily P&L history (keep last 10 days)."""
        self.daily_pnl_history.append(pnl)
        if len(self.daily_pnl_history) > 10:
            self.daily_pnl_history = self.daily_pnl_history[-10:]
        self.cumulative_pnl += pnl

    @property
    def rolling_10d_pnl(self) -> float:
        """Get rolling 10-day P&L."""
        return sum(self.daily_pnl_history)


@dataclass
class SovereignRatesShortConfig:
    """Configuration for EU Sovereign Fragility Short sleeve."""
    enabled: bool = True
    target_weight_pct: float = 0.12

    # Regime-based weights
    base_weights: Dict[str, float] = field(default_factory=lambda: {
        "normal": 0.06,
        "elevated": 0.12,
        "crisis": 0.16,
    })
    max_weights: Dict[str, float] = field(default_factory=lambda: {
        "normal": 0.10,
        "elevated": 0.16,
        "crisis": 0.20,
    })

    # DV01 budget
    dv01_budget_per_nav: float = 0.0007  # 7bps of NAV per 100bp move

    # Instruments
    btp_symbol: str = "FBTP"
    bund_symbol: str = "FGBL"
    oat_symbol: str = "FOAT"

    # Use ETF fallback if futures unavailable
    use_etf_fallback: bool = True
    etf_btp_proxy: str = "EWI"
    etf_bund_proxy: str = "EWG"

    # Signal lookbacks
    spread_z_lookback_days: int = 252
    spread_mom_lookback_days: int = 20
    bund_yield_mom_lookback_days: int = 60

    # Fragmentation multiplier thresholds
    frag_mult_z_low: float = 0.0
    frag_mult_z_mid: float = 1.0
    frag_mult_z_high: float = 2.0

    # Rates-up multiplier thresholds (bps)
    rates_mult_low_bps: float = 10.0
    rates_mult_high_bps: float = 40.0

    # Deflation guard thresholds (legacy - kept for compatibility)
    deflation_vix_threshold: float = 30.0
    deflation_stress_threshold: float = 0.75
    deflation_bund_5d_drop_bps: float = -30.0
    deflation_bund_20d_drop_bps: float = -40.0

    # v3.0: 3-tier continuous deflation scaler (replaces binary guard)
    deflation_scaler_enabled: bool = True
    deflation_fragmentation_bypass_z: float = 0.5  # spread_z >= this = keep full position
    deflation_tier1_vix: float = 35.0
    deflation_tier1_bund_5d_bps: float = -30.0
    deflation_tier2_vix: float = 45.0
    deflation_tier2_bund_5d_bps: float = -40.0
    deflation_tier3_vix: float = 55.0
    deflation_tier3_bund_5d_bps: float = -60.0

    # Kill-switch thresholds
    hard_kill_daily_loss_pct: float = 0.006   # 0.6% NAV
    hard_kill_10d_drawdown_pct: float = 0.015  # 1.5% NAV
    soft_kill_spread_z: float = -0.5
    soft_kill_bund_mom_20d_bps: float = -20.0
    reenable_days: int = 5

    # Take-profit rules
    take_profit_spread_z: float = 2.5
    take_profit_spread_widening_bps: float = 120.0
    profit_take_pct: float = 0.50
    recycle_wait_days: int = 3

    # DV01 per contract (monthly calibration)
    dv01_per_contract: Dict[str, float] = field(default_factory=lambda: {
        "FGBL": 80.0,
        "FBTP": 78.0,
        "FOAT": 79.0,
    })

    @classmethod
    def from_settings(cls, settings: Dict[str, Any]) -> "SovereignRatesShortConfig":
        """Create config from settings dict."""
        srs_settings = settings.get('sovereign_rates_short', {})

        if not srs_settings:
            return cls()

        # Default values for dict fields (can't use cls.field for default_factory fields)
        default_base_weights = {'normal': 0.06, 'elevated': 0.12, 'crisis': 0.16}
        default_max_weights = {'normal': 0.10, 'elevated': 0.16, 'crisis': 0.20}
        default_dv01_per_contract = {'FGBL': 80.0, 'FBTP': 78.0, 'FOAT': 79.0}

        base_weights = srs_settings.get('base_weights', default_base_weights)
        max_weights = srs_settings.get('max_weights', default_max_weights)
        dv01_per_contract = srs_settings.get('dv01_per_contract', default_dv01_per_contract)

        # Parse nested configs
        signals = srs_settings.get('signals', {})
        frag_mult = srs_settings.get('frag_mult_thresholds', {})
        rates_mult = srs_settings.get('rates_up_mult_thresholds', {})
        deflation = srs_settings.get('deflation_guard', {})
        deflation_scaler = srs_settings.get('deflation_scaler', {})
        kill_switches = srs_settings.get('kill_switches', {})
        take_profit = srs_settings.get('take_profit', {})
        instruments = srs_settings.get('instruments', {})

        return cls(
            enabled=srs_settings.get('enabled', True),
            target_weight_pct=srs_settings.get('target_weight_pct', 0.12),
            base_weights=base_weights if isinstance(base_weights, dict) else default_base_weights,
            max_weights=max_weights if isinstance(max_weights, dict) else default_max_weights,
            dv01_budget_per_nav=srs_settings.get('dv01_budget_per_nav', 0.0007),
            btp_symbol=instruments.get('btp', 'FBTP'),
            bund_symbol=instruments.get('bund', 'FGBL'),
            oat_symbol=instruments.get('oat', 'FOAT'),
            spread_z_lookback_days=signals.get('spread_z_lookback_days', 252),
            spread_mom_lookback_days=signals.get('spread_mom_lookback_days', 20),
            bund_yield_mom_lookback_days=signals.get('bund_yield_mom_lookback_days', 60),
            frag_mult_z_low=frag_mult.get('z_low', 0.0),
            frag_mult_z_mid=frag_mult.get('z_mid', 1.0),
            frag_mult_z_high=frag_mult.get('z_high', 2.0),
            rates_mult_low_bps=rates_mult.get('low_bps', 10.0),
            rates_mult_high_bps=rates_mult.get('high_bps', 40.0),
            deflation_vix_threshold=deflation.get('vix_threshold', 30.0),
            deflation_stress_threshold=deflation.get('stress_score_threshold', 0.75),
            deflation_bund_5d_drop_bps=deflation.get('bund_yield_5d_drop_bps', -30.0),
            deflation_bund_20d_drop_bps=deflation.get('bund_yield_20d_drop_bps', -40.0),
            # v3.0: 3-tier deflation scaler
            deflation_scaler_enabled=bool(deflation_scaler) or False,
            deflation_fragmentation_bypass_z=deflation_scaler.get('fragmentation_bypass_spread_z', 0.5),
            deflation_tier1_vix=deflation_scaler.get('tier1', {}).get('vix_threshold', 35.0),
            deflation_tier1_bund_5d_bps=deflation_scaler.get('tier1', {}).get('bund_yield_5d_drop_bps', -30.0),
            deflation_tier2_vix=deflation_scaler.get('tier2', {}).get('vix_threshold', 45.0),
            deflation_tier2_bund_5d_bps=deflation_scaler.get('tier2', {}).get('bund_yield_5d_drop_bps', -40.0),
            deflation_tier3_vix=deflation_scaler.get('tier3', {}).get('vix_threshold', 55.0),
            deflation_tier3_bund_5d_bps=deflation_scaler.get('tier3', {}).get('bund_yield_5d_drop_bps', -60.0),
            hard_kill_daily_loss_pct=kill_switches.get('hard_kill_daily_loss_pct', 0.006),
            hard_kill_10d_drawdown_pct=kill_switches.get('hard_kill_10d_drawdown_pct', 0.015),
            soft_kill_spread_z=kill_switches.get('soft_kill_spread_z', -0.5),
            soft_kill_bund_mom_20d_bps=kill_switches.get('soft_kill_bund_yield_mom_20d_bps', -20.0),
            reenable_days=kill_switches.get('reenable_days', 5),
            take_profit_spread_z=take_profit.get('spread_z_threshold', 2.5),
            take_profit_spread_widening_bps=take_profit.get('spread_widening_bps', 120.0),
            profit_take_pct=take_profit.get('profit_take_pct', 0.50),
            recycle_wait_days=take_profit.get('recycle_wait_days', 3),
            dv01_per_contract=dv01_per_contract if isinstance(dv01_per_contract, dict) else default_dv01_per_contract,
        )


class SovereignRatesShortEngine:
    """
    EU Sovereign Fragility Short Engine.

    Implements BTP-Bund spread trade with:
    - DV01-neutral position construction
    - Fragmentation signal sizing
    - Deflation guard for risk-off protection
    - Kill-switches and take-profit rules
    """

    def __init__(
        self,
        config: Optional[SovereignRatesShortConfig] = None,
        settings: Optional[Dict[str, Any]] = None
    ):
        """Initialize sovereign rates short engine."""
        self.config = config or (
            SovereignRatesShortConfig.from_settings(settings) if settings
            else SovereignRatesShortConfig()
        )

        # State tracking
        self._tracker = SleeveTracker()
        self._last_signal: Optional[FragmentationSignal] = None
        self._last_sizing: Optional[SizingResult] = None
        self._last_position: Optional[DV01Position] = None

        # Price history for signal calculation
        self._btp_yield_history: pd.Series = pd.Series(dtype=float)
        self._bund_yield_history: pd.Series = pd.Series(dtype=float)
        self._spread_history: pd.Series = pd.Series(dtype=float)

    def update_yield_history(
        self,
        btp_yield: float,
        bund_yield: float,
        as_of: Optional[date] = None
    ) -> None:
        """Update yield history for signal calculation."""
        as_of = as_of or date.today()

        # Append to history
        self._btp_yield_history[as_of] = btp_yield
        self._bund_yield_history[as_of] = bund_yield
        self._spread_history[as_of] = (btp_yield - bund_yield) * 100  # Convert to bps

        # Keep last 300 days
        if len(self._spread_history) > 300:
            self._spread_history = self._spread_history.iloc[-300:]
            self._btp_yield_history = self._btp_yield_history.iloc[-300:]
            self._bund_yield_history = self._bund_yield_history.iloc[-300:]

    def compute_fragmentation_signal(
        self,
        btp_yield: float,
        bund_yield: float,
        vix_level: float,
        stress_score: float,
        as_of: Optional[date] = None
    ) -> FragmentationSignal:
        """
        Compute fragmentation signal for sizing.

        Args:
            btp_yield: Current BTP 10Y yield (percent)
            bund_yield: Current Bund 10Y yield (percent)
            vix_level: Current VIX level
            stress_score: Current stress score from risk engine
            as_of: Date for calculation

        Returns:
            FragmentationSignal with all metrics
        """
        as_of = as_of or date.today()

        # Update history
        self.update_yield_history(btp_yield, bund_yield, as_of)

        # Current spread in bps
        spread_bps = (btp_yield - bund_yield) * 100

        # Spread Z-score (252-day lookback)
        if len(self._spread_history) >= 20:
            lookback = min(len(self._spread_history), self.config.spread_z_lookback_days)
            spread_mean = self._spread_history.iloc[-lookback:].mean()
            spread_std = self._spread_history.iloc[-lookback:].std()
            if spread_std > 0:
                spread_z = (spread_bps - spread_mean) / spread_std
            else:
                spread_z = 0.0
        else:
            spread_z = 0.0

        # Spread momentum (20-day)
        if len(self._spread_history) >= 20:
            spread_mom_20d = spread_bps - self._spread_history.iloc[-20]
        else:
            spread_mom_20d = 0.0

        # Bund yield momentum
        if len(self._bund_yield_history) >= 60:
            bund_yield_mom_60d = (bund_yield - self._bund_yield_history.iloc[-60]) * 100
        elif len(self._bund_yield_history) >= 20:
            bund_yield_mom_60d = (bund_yield - self._bund_yield_history.iloc[-20]) * 100
        else:
            bund_yield_mom_60d = 0.0

        if len(self._bund_yield_history) >= 5:
            bund_yield_change_5d = (bund_yield - self._bund_yield_history.iloc[-5]) * 100
        else:
            bund_yield_change_5d = 0.0

        if len(self._bund_yield_history) >= 20:
            bund_yield_mom_20d = (bund_yield - self._bund_yield_history.iloc[-20]) * 100
        else:
            bund_yield_mom_20d = 0.0

        signal = FragmentationSignal(
            spread_bps=spread_bps,
            spread_z=spread_z,
            spread_mom_20d=spread_mom_20d,
            bund_yield_mom_60d=bund_yield_mom_60d,
            bund_yield_change_5d=bund_yield_change_5d,
            bund_yield_mom_20d=bund_yield_mom_20d,
            vix_level=vix_level,
            stress_score=stress_score,
        )

        self._last_signal = signal
        return signal

    def _compute_deflation_scaler(self, signal: FragmentationSignal) -> Tuple[float, str]:
        """
        Compute 3-tier deflation scaler (v3.0).

        Returns:
            Tuple of (scaler, reason)
            scaler: 1.0 (no scaling), 0.5 (tier1), 0.25 (tier2), 0.0 (tier3)
        """
        if not self.config.deflation_scaler_enabled:
            return 1.0, "deflation_scaler disabled"

        # Fragmentation bypass: if spread is widening, keep full position
        # Rationale: fragmentation = stress, we WANT the position
        if signal.spread_z >= self.config.deflation_fragmentation_bypass_z:
            return 1.0, f"frag_bypass (z={signal.spread_z:.2f} >= {self.config.deflation_fragmentation_bypass_z})"

        vix = signal.vix_level
        bund_5d = signal.bund_yield_change_5d

        # Tier 3 (0.0x): VIX >= 55 AND bund yield -60bps/5d
        if vix >= self.config.deflation_tier3_vix and bund_5d <= self.config.deflation_tier3_bund_5d_bps:
            return 0.0, f"tier3_kill (VIX={vix:.0f}, bund_5d={bund_5d:.0f}bps)"

        # Tier 2 (0.25x): VIX >= 45 AND bund yield -40bps/5d
        if vix >= self.config.deflation_tier2_vix and bund_5d <= self.config.deflation_tier2_bund_5d_bps:
            return 0.25, f"tier2 (VIX={vix:.0f}, bund_5d={bund_5d:.0f}bps)"

        # Tier 1 (0.5x): VIX >= 35 AND bund yield -30bps/5d
        if vix >= self.config.deflation_tier1_vix and bund_5d <= self.config.deflation_tier1_bund_5d_bps:
            return 0.5, f"tier1 (VIX={vix:.0f}, bund_5d={bund_5d:.0f}bps)"

        return 1.0, "no_deflation"

    def compute_target_weight(
        self,
        signal: FragmentationSignal,
        regime: RiskRegime,
        nav: float,
        current_daily_pnl: float = 0.0
    ) -> SizingResult:
        """
        Compute target weight using deterministic sizing rules.

        v3.0 formula:
            target_w = base_w_by_regime * frag_mult * rates_up_mult * deflation_scaler
            target_w = min(target_w, max_w_by_regime)

        Args:
            signal: Fragmentation signal
            regime: Current market regime
            nav: Current NAV
            current_daily_pnl: Today's P&L for this sleeve

        Returns:
            SizingResult with target weight and reasoning
        """
        # Get base weight for regime
        regime_key = regime.value.lower()
        base_w = self.config.base_weights.get(regime_key, 0.10)
        max_w = self.config.max_weights.get(regime_key, 0.12)

        # Compute 3-tier deflation scaler (v3.0)
        deflation_scaler, deflation_reason = self._compute_deflation_scaler(signal)

        # Hard kill if scaler is 0.0 (tier 3)
        if deflation_scaler == 0.0:
            result = SizingResult(
                target_weight=0.0,
                base_weight=base_w,
                frag_multiplier=0.0,
                rates_multiplier=0.0,
                deflation_scaler=0.0,
                max_weight=max_w,
                deflation_guard=True,  # Legacy compatibility
                soft_kill=False,
                regime=regime,
                reason=f"DEFLATION KILL: {deflation_reason}"
            )
            self._last_sizing = result
            return result

        # Check kill-switches (loss-based)
        kill_type = self._check_kill_switches(signal, nav, current_daily_pnl)

        if kill_type == KillSwitchType.HARD:
            result = SizingResult(
                target_weight=0.0,
                base_weight=base_w,
                frag_multiplier=0.0,
                rates_multiplier=0.0,
                deflation_scaler=deflation_scaler,
                max_weight=max_w,
                deflation_guard=False,
                soft_kill=False,
                regime=regime,
                reason="HARD KILL: Loss threshold breached"
            )
            self._last_sizing = result
            return result

        # Compute fragmentation multiplier
        if signal.spread_z < self.config.frag_mult_z_low:
            frag_mult = 0.5
        elif signal.spread_z < self.config.frag_mult_z_mid:
            frag_mult = 1.0
        elif signal.spread_z < self.config.frag_mult_z_high:
            frag_mult = 1.3
        else:
            frag_mult = 1.6

        # Compute rates-up multiplier
        if signal.bund_yield_mom_60d < self.config.rates_mult_low_bps:
            rates_mult = 0.8
        elif signal.bund_yield_mom_60d < self.config.rates_mult_high_bps:
            rates_mult = 1.0
        else:
            rates_mult = 1.2

        # v3.0 formula: target_w = base_w * frag_mult * rates_mult * deflation_scaler
        target_w = base_w * frag_mult * rates_mult * deflation_scaler

        # Apply soft kill (50% reduction)
        soft_kill = kill_type == KillSwitchType.SOFT
        if soft_kill:
            target_w *= 0.5

        # Clamp to max
        target_w = max(0.0, min(target_w, max_w))

        # Build reason string
        reason_parts = []
        reason_parts.append(f"regime={regime_key}")
        reason_parts.append(f"base={base_w:.2%}")
        reason_parts.append(f"frag_mult={frag_mult:.1f} (z={signal.spread_z:.2f})")
        reason_parts.append(f"rates_mult={rates_mult:.1f} (bund_60d={signal.bund_yield_mom_60d:.0f}bps)")
        if deflation_scaler < 1.0:
            reason_parts.append(f"defl_scaler={deflation_scaler:.2f} ({deflation_reason})")
        if soft_kill:
            reason_parts.append("SOFT_KILL (-50%)")

        result = SizingResult(
            target_weight=target_w,
            base_weight=base_w,
            frag_multiplier=frag_mult,
            rates_multiplier=rates_mult,
            deflation_scaler=deflation_scaler,
            max_weight=max_w,
            deflation_guard=(deflation_scaler == 0.0),  # Legacy
            soft_kill=soft_kill,
            regime=regime,
            reason="; ".join(reason_parts)
        )

        self._last_sizing = result
        return result

    def _check_kill_switches(
        self,
        signal: FragmentationSignal,
        nav: float,
        current_daily_pnl: float
    ) -> KillSwitchType:
        """Check kill-switch conditions."""
        # Hard kill: daily loss exceeds threshold
        daily_loss_pct = -current_daily_pnl / nav if nav > 0 else 0
        if daily_loss_pct > self.config.hard_kill_daily_loss_pct:
            logger.warning(
                f"HARD KILL: Daily loss {daily_loss_pct:.2%} > "
                f"{self.config.hard_kill_daily_loss_pct:.2%} threshold"
            )
            self._tracker.state = SleeveState.HARD_KILLED
            return KillSwitchType.HARD

        # Hard kill: 10-day drawdown exceeds threshold
        rolling_10d_pnl_pct = self._tracker.rolling_10d_pnl / nav if nav > 0 else 0
        if rolling_10d_pnl_pct < -self.config.hard_kill_10d_drawdown_pct:
            logger.warning(
                f"HARD KILL: 10-day drawdown {rolling_10d_pnl_pct:.2%} > "
                f"{self.config.hard_kill_10d_drawdown_pct:.2%} threshold"
            )
            self._tracker.state = SleeveState.HARD_KILLED
            return KillSwitchType.HARD

        # Soft kill: spread compressing strongly
        if signal.spread_z < self.config.soft_kill_spread_z:
            logger.info(
                f"SOFT KILL: Spread z={signal.spread_z:.2f} < "
                f"{self.config.soft_kill_spread_z} threshold"
            )
            self._tracker.state = SleeveState.SOFT_KILLED
            return KillSwitchType.SOFT

        # Soft kill: rates rallying (bonds up)
        if signal.bund_yield_mom_20d < self.config.soft_kill_bund_mom_20d_bps:
            logger.info(
                f"SOFT KILL: Bund mom 20d={signal.bund_yield_mom_20d:.0f}bps < "
                f"{self.config.soft_kill_bund_mom_20d_bps}bps threshold"
            )
            self._tracker.state = SleeveState.SOFT_KILLED
            return KillSwitchType.SOFT

        # Clear soft kill if conditions no longer apply
        if self._tracker.state == SleeveState.SOFT_KILLED:
            self._tracker.state = SleeveState.ACTIVE

        return KillSwitchType.NONE

    def compute_dv01_position(
        self,
        target_weight: float,
        nav: float,
        use_etf_fallback: bool = False
    ) -> DV01Position:
        """
        Compute DV01-neutral position.

        Args:
            target_weight: Target weight (0.0 to 1.0)
            nav: Current NAV
            use_etf_fallback: Use ETF proxies instead of futures

        Returns:
            DV01Position with contract counts
        """
        if target_weight <= 0:
            return DV01Position(
                btp_contracts=0,
                bund_contracts=0,
                target_dv01=0.0,
                actual_net_dv01=0.0,
                dv01_per_btp=0.0,
                dv01_per_bund=0.0,
            )

        # Calculate target DV01
        target_dv01 = target_weight * nav * self.config.dv01_budget_per_nav

        # Get DV01 per contract
        if use_etf_fallback:
            # For ETFs, use notional-based sizing (simplified)
            dv01_per_btp = 10.0  # Approximate
            dv01_per_bund = 10.0
        else:
            dv01_per_btp = self.config.dv01_per_contract.get(
                self.config.btp_symbol, 78.0
            )
            dv01_per_bund = self.config.dv01_per_contract.get(
                self.config.bund_symbol, 80.0
            )

        # Compute contracts (BTP is short, Bund is long)
        btp_contracts = -round(target_dv01 / dv01_per_btp)  # Negative = short

        # Match DV01 on Bund side for neutrality
        bund_dv01_needed = abs(btp_contracts) * dv01_per_btp
        bund_contracts = round(bund_dv01_needed / dv01_per_bund)  # Positive = long

        # Calculate actual net DV01
        actual_net_dv01 = (
            btp_contracts * dv01_per_btp + bund_contracts * dv01_per_bund
        )

        position = DV01Position(
            btp_contracts=btp_contracts,
            bund_contracts=bund_contracts,
            target_dv01=target_dv01,
            actual_net_dv01=actual_net_dv01,
            dv01_per_btp=dv01_per_btp,
            dv01_per_bund=dv01_per_bund,
        )

        self._last_position = position

        logger.info(
            f"DV01 position: BTP={btp_contracts} ({dv01_per_btp:.0f} DV01/ct), "
            f"Bund={bund_contracts} ({dv01_per_bund:.0f} DV01/ct), "
            f"target_dv01={target_dv01:.0f}, net_dv01={actual_net_dv01:.0f}, "
            f"neutral={position.is_neutral}"
        )

        return position

    def check_take_profit(
        self,
        signal: FragmentationSignal,
        today: Optional[date] = None
    ) -> Tuple[bool, float, str]:
        """
        Check if take-profit conditions are met.

        Args:
            signal: Current fragmentation signal
            today: Current date

        Returns:
            Tuple of (should_take_profit, take_pct, reason)
        """
        today = today or date.today()

        # Check recycle wait period
        if self._tracker.last_profit_take_date:
            days_since_profit = (today - self._tracker.last_profit_take_date).days
            if days_since_profit < self.config.recycle_wait_days:
                return False, 0.0, "Within recycle wait period"

        # Check spread z-score threshold
        if signal.spread_z >= self.config.take_profit_spread_z:
            logger.info(
                f"TAKE PROFIT: Spread z={signal.spread_z:.2f} >= "
                f"{self.config.take_profit_spread_z} threshold"
            )
            return True, self.config.profit_take_pct, "Spread z-score threshold"

        # Check spread widening from entry
        if self._tracker.entry_spread_avg_bps > 0:
            widening = signal.spread_bps - self._tracker.entry_spread_avg_bps
            if widening >= self.config.take_profit_spread_widening_bps:
                logger.info(
                    f"TAKE PROFIT: Spread widened {widening:.0f}bps >= "
                    f"{self.config.take_profit_spread_widening_bps}bps threshold"
                )
                return True, self.config.profit_take_pct, "Spread widening threshold"

        return False, 0.0, "No take-profit conditions met"

    def generate_orders(
        self,
        portfolio_state: PortfolioState,
        signal: FragmentationSignal,
        regime: RiskRegime,
        use_etf_fallback: bool = False,
        today: Optional[date] = None
    ) -> List[OrderSpec]:
        """
        Generate orders for sovereign rates short sleeve.

        Args:
            portfolio_state: Current portfolio state
            signal: Fragmentation signal
            regime: Current regime
            use_etf_fallback: Use ETF proxies if futures unavailable
            today: Current date

        Returns:
            List of OrderSpec for execution
        """
        today = today or date.today()
        orders = []

        if not self.config.enabled:
            return orders

        nav = portfolio_state.nav

        # Update P&L tracking
        current_daily_pnl = self._estimate_daily_pnl(portfolio_state)
        self._tracker.update_daily_pnl(current_daily_pnl)

        # Compute target weight
        sizing = self.compute_target_weight(signal, regime, nav, current_daily_pnl)

        # Check re-enable conditions if killed
        if self._tracker.state in (SleeveState.HARD_KILLED, SleeveState.REENABLE_PENDING):
            if self._should_reenable(signal):
                logger.info("Re-enabling sovereign rates short sleeve")
                self._tracker.state = SleeveState.ACTIVE
                self._tracker.days_at_zero = 0
            else:
                # Stay killed
                if sizing.target_weight == 0:
                    self._tracker.days_at_zero += 1
                return self._generate_flatten_orders(portfolio_state, use_etf_fallback)

        # Check take-profit
        should_take_profit, take_pct, take_reason = self.check_take_profit(signal, today)

        # Compute DV01 position
        if should_take_profit:
            # Reduce position by take_pct
            adjusted_weight = sizing.target_weight * (1 - take_pct)
            position = self.compute_dv01_position(adjusted_weight, nav, use_etf_fallback)
            self._tracker.last_profit_take_date = today
            logger.info(f"Taking profit ({take_pct:.0%}): {take_reason}")
        else:
            position = self.compute_dv01_position(sizing.target_weight, nav, use_etf_fallback)

        # Track entry if new position
        if position.btp_contracts != 0 and self._tracker.entry_date is None:
            self._tracker.entry_date = today
            self._tracker.entry_spread_avg_bps = signal.spread_bps

        # Generate orders for BTP leg (short)
        btp_symbol = self.config.etf_btp_proxy if use_etf_fallback else self.config.btp_symbol
        current_btp = self._get_current_position(portfolio_state, btp_symbol)
        btp_delta = position.btp_contracts - current_btp

        if btp_delta != 0:
            orders.append(OrderSpec(
                instrument_id=btp_symbol,
                side="SELL" if btp_delta < 0 else "BUY",
                quantity=abs(btp_delta),
                order_type="LMT",
                sleeve=Sleeve.EUROPE_VOL_CONVEX,  # Tagged as insurance
                reason=f"SovRatesShort: BTP leg ({sizing.reason})"
            ))

        # Generate orders for Bund leg (long)
        bund_symbol = self.config.etf_bund_proxy if use_etf_fallback else self.config.bund_symbol
        current_bund = self._get_current_position(portfolio_state, bund_symbol)
        bund_delta = position.bund_contracts - current_bund

        if bund_delta != 0:
            orders.append(OrderSpec(
                instrument_id=bund_symbol,
                side="BUY" if bund_delta > 0 else "SELL",
                quantity=abs(bund_delta),
                order_type="LMT",
                sleeve=Sleeve.EUROPE_VOL_CONVEX,  # Tagged as insurance
                reason=f"SovRatesShort: Bund leg ({sizing.reason})"
            ))

        return orders

    def _get_current_position(
        self,
        portfolio_state: PortfolioState,
        symbol: str
    ) -> int:
        """Get current position for a symbol."""
        for pos_id, pos in portfolio_state.positions.items():
            if pos_id == symbol or getattr(pos, 'symbol', None) == symbol:
                return int(pos.quantity)
        return 0

    def _estimate_daily_pnl(self, portfolio_state: PortfolioState) -> float:
        """Estimate daily P&L for this sleeve (simplified)."""
        # In production, would track actual fills and mark-to-market
        return 0.0

    def _should_reenable(self, signal: FragmentationSignal) -> bool:
        """Check if sleeve should be re-enabled after kill."""
        # Need N consecutive days without deflation guard
        if signal.deflation_guard:
            self._tracker.days_at_zero = 0
            return False

        # Need spread_z >= 0 or elevated/crisis regime
        if signal.spread_z < 0 and self._last_sizing and \
           self._last_sizing.regime == RiskRegime.NORMAL:
            return False

        return self._tracker.days_at_zero >= self.config.reenable_days

    def _generate_flatten_orders(
        self,
        portfolio_state: PortfolioState,
        use_etf_fallback: bool
    ) -> List[OrderSpec]:
        """Generate orders to flatten all positions."""
        orders = []

        btp_symbol = self.config.etf_btp_proxy if use_etf_fallback else self.config.btp_symbol
        current_btp = self._get_current_position(portfolio_state, btp_symbol)
        if current_btp != 0:
            orders.append(OrderSpec(
                instrument_id=btp_symbol,
                side="BUY" if current_btp < 0 else "SELL",
                quantity=abs(current_btp),
                order_type="MKT",
                urgency="urgent",
                sleeve=Sleeve.EUROPE_VOL_CONVEX,
                reason="SovRatesShort: KILL - Flatten BTP"
            ))

        bund_symbol = self.config.etf_bund_proxy if use_etf_fallback else self.config.bund_symbol
        current_bund = self._get_current_position(portfolio_state, bund_symbol)
        if current_bund != 0:
            orders.append(OrderSpec(
                instrument_id=bund_symbol,
                side="SELL" if current_bund > 0 else "BUY",
                quantity=abs(current_bund),
                order_type="MKT",
                urgency="urgent",
                sleeve=Sleeve.EUROPE_VOL_CONVEX,
                reason="SovRatesShort: KILL - Flatten Bund"
            ))

        return orders

    def get_summary(self) -> Dict[str, Any]:
        """Get summary of engine state."""
        return {
            "enabled": self.config.enabled,
            "state": self._tracker.state.value,
            "days_at_zero": self._tracker.days_at_zero,
            "cumulative_pnl": self._tracker.cumulative_pnl,
            "rolling_10d_pnl": self._tracker.rolling_10d_pnl,
            "entry_spread_avg_bps": self._tracker.entry_spread_avg_bps,
            "entry_date": self._tracker.entry_date.isoformat() if self._tracker.entry_date else None,
            "last_profit_take": (
                self._tracker.last_profit_take_date.isoformat()
                if self._tracker.last_profit_take_date else None
            ),
            "last_signal": {
                "spread_bps": self._last_signal.spread_bps,
                "spread_z": round(self._last_signal.spread_z, 3),
                "deflation_guard": self._last_signal.deflation_guard,
                "vix_level": self._last_signal.vix_level,
            } if self._last_signal else None,
            "last_sizing": {
                "target_weight": round(self._last_sizing.target_weight, 4),
                "base_weight": self._last_sizing.base_weight,
                "frag_mult": self._last_sizing.frag_multiplier,
                "rates_mult": self._last_sizing.rates_multiplier,
                "deflation_scaler": self._last_sizing.deflation_scaler,
                "regime": self._last_sizing.regime.value,
                "reason": self._last_sizing.reason,
            } if self._last_sizing else None,
            "last_position": {
                "btp_contracts": self._last_position.btp_contracts,
                "bund_contracts": self._last_position.bund_contracts,
                "target_dv01": round(self._last_position.target_dv01, 2),
                "net_dv01": round(self._last_position.actual_net_dv01, 2),
                "is_neutral": self._last_position.is_neutral,
            } if self._last_position else None,
            "config": {
                "target_weight_pct": self.config.target_weight_pct,
                "dv01_budget_per_nav": self.config.dv01_budget_per_nav,
                "btp_symbol": self.config.btp_symbol,
                "bund_symbol": self.config.bund_symbol,
            },
        }

    def get_daily_report(self, sleeve_pnl: float = 0.0) -> str:
        """
        Generate daily report line item for logging/Telegram.

        Format: base_w | frag_mult | rates_mult | defl_scaler | target | contracts | PnL

        Args:
            sleeve_pnl: Today's P&L for this sleeve (from attribution engine)

        Returns:
            Formatted report string
        """
        if not self._last_sizing:
            return "SovRatesShort: No sizing data available"

        sizing = self._last_sizing
        signal = self._last_signal
        position = self._last_position

        # Format components
        base_w_str = f"base={sizing.base_weight:.1%}"
        frag_str = f"frag×{sizing.frag_multiplier:.1f}"
        rates_str = f"rates×{sizing.rates_multiplier:.1f}"
        defl_str = f"defl×{sizing.deflation_scaler:.2f}"
        target_str = f"target={sizing.target_weight:.2%}"

        # Contracts
        if position:
            btp_str = f"BTP={position.btp_contracts:+d}"
            bund_str = f"Bund={position.bund_contracts:+d}"
            contracts_str = f"{btp_str}, {bund_str}"
        else:
            contracts_str = "no position"

        # P&L
        pnl_str = f"PnL=${sleeve_pnl:+,.0f}" if sleeve_pnl != 0 else "PnL=$0"

        # Signal info
        if signal:
            sig_str = f"z={signal.spread_z:.2f}, VIX={signal.vix_level:.0f}"
        else:
            sig_str = "no signal"

        report = (
            f"SovRatesShort [{sizing.regime.value}]: "
            f"{base_w_str} | {frag_str} | {rates_str} | {defl_str} → {target_str} | "
            f"{contracts_str} | {pnl_str} | ({sig_str})"
        )

        # Log it
        logger.info(report)

        return report

    def get_daily_report_dict(self, sleeve_pnl: float = 0.0) -> Dict[str, Any]:
        """
        Get daily report as structured dict for JSON logging.

        Args:
            sleeve_pnl: Today's P&L for this sleeve

        Returns:
            Dict with all daily metrics
        """
        if not self._last_sizing:
            return {"error": "no sizing data"}

        sizing = self._last_sizing
        signal = self._last_signal
        position = self._last_position

        return {
            "sleeve": "sovereign_rates_short",
            "regime": sizing.regime.value,
            "base_weight": sizing.base_weight,
            "frag_multiplier": sizing.frag_multiplier,
            "rates_multiplier": sizing.rates_multiplier,
            "deflation_scaler": sizing.deflation_scaler,
            "target_weight": sizing.target_weight,
            "max_weight": sizing.max_weight,
            "soft_kill": sizing.soft_kill,
            "btp_contracts": position.btp_contracts if position else 0,
            "bund_contracts": position.bund_contracts if position else 0,
            "target_dv01": position.target_dv01 if position else 0,
            "is_dv01_neutral": position.is_neutral if position else True,
            "spread_z": signal.spread_z if signal else 0,
            "spread_bps": signal.spread_bps if signal else 0,
            "vix_level": signal.vix_level if signal else 0,
            "bund_yield_5d_change": signal.bund_yield_change_5d if signal else 0,
            "sleeve_pnl": sleeve_pnl,
            "cumulative_pnl": self._tracker.cumulative_pnl,
            "state": self._tracker.state.value,
            "reason": sizing.reason,
        }


def create_sovereign_rates_short_engine(
    settings: Dict[str, Any]
) -> SovereignRatesShortEngine:
    """Factory function to create sovereign rates short engine."""
    config = SovereignRatesShortConfig.from_settings(settings)
    return SovereignRatesShortEngine(config=config)
