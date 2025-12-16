"""
Europe Vol Convexity Module.

PRIMARY insurance channel for AbstractFinance.

This module implements:
1. VSTOXX term structure signal (contango/backwardation)
2. Vol-of-vol jump signal (large 1-3 day moves)
3. Structure selection (spreads vs outrights based on regime)
4. Roll and monetization rules

Instruments:
- VSTOXX Mini Futures (FVS) - EUREX
- VSTOXX Options on Futures (OVS2) - EUREX
- SX5E Index Options (OESX) - EUREX
- SX7E (Euro Banks) Options - EUREX
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Dict, List, Optional, Tuple
import numpy as np

logger = logging.getLogger(__name__)


class VolRegime(Enum):
    """Volatility regime for structure selection."""
    LOW = "low"           # V2X < 18: Vol is cheap, buy outrights
    NORMAL = "normal"     # V2X 18-25: Use spreads to reduce bleed
    ELEVATED = "elevated" # V2X 25-35: Add tails, widen spreads
    CRISIS = "crisis"     # V2X > 35: Monetize winners, selective re-up


class TermStructure(Enum):
    """VSTOXX term structure state."""
    CONTANGO = "contango"           # Front < Back: Normal, vol is "cheap"
    FLAT = "flat"                   # Front ~ Back: Transition
    BACKWARDATION = "backwardation" # Front > Back: Stress, vol expensive


@dataclass
class VolSignal:
    """Combined volatility signal for position sizing."""
    # Current levels
    v2x_spot: float
    v2x_front: float  # Front month future
    v2x_back: float   # Second month future

    # Derived signals
    term_structure: TermStructure
    term_spread: float  # Back - Front (positive = contango)
    term_spread_zscore: float  # Z-score vs history

    vol_of_vol: float  # Recent V2X volatility
    vol_jump: bool     # Large 1-3 day move detected
    vol_jump_magnitude: float  # Size of jump if detected

    # Regime
    vol_regime: VolRegime

    # Action signals
    should_add_convexity: bool
    should_monetize: bool
    structure_preference: str  # "spreads", "outrights", "tails"

    # Sizing multiplier (0.5 to 2.0)
    sizing_multiplier: float


@dataclass
class ConvexityPosition:
    """Target convexity position."""
    # VSTOXX call structures
    vstoxx_call_strike: float
    vstoxx_call_dte: int
    vstoxx_call_notional: float
    vstoxx_call_is_spread: bool
    vstoxx_call_spread_width: float

    # SX5E put structures
    sx5e_put_strike_pct: float  # As % of spot (e.g., 0.90 = 10% OTM)
    sx5e_put_dte: int
    sx5e_put_notional: float
    sx5e_put_is_spread: bool
    sx5e_put_spread_width_pct: float

    # EU Banks puts (SX7E)
    eu_banks_put_strike_pct: float
    eu_banks_put_notional: float

    # Roll signals
    should_roll: bool
    days_to_roll: int


class EuropeVolEngine:
    """
    Engine for managing Europe vol convexity positions.

    This is the PRIMARY insurance channel. It should:
    1. Generate convex payoff when Europe is stressed
    2. Control bleed in normal markets via spreads
    3. Use term structure to time entries
    4. Detect vol jumps for position adjustments
    """

    # Configuration defaults
    DEFAULT_CONFIG = {
        # Term structure thresholds
        "contango_threshold": 0.5,      # V2X points
        "backwardation_threshold": -0.5,

        # Vol regime thresholds
        "low_vol_threshold": 18.0,
        "elevated_vol_threshold": 25.0,
        "crisis_vol_threshold": 35.0,

        # Vol-of-vol parameters
        "vol_of_vol_lookback": 20,      # Days
        "vol_jump_threshold": 2.0,       # Std devs for jump detection
        "vol_jump_window": 3,            # Days to detect jump

        # Position parameters
        "target_dte_normal": 75,         # Days to expiry target
        "target_dte_elevated": 60,
        "roll_dte_threshold": 25,        # Roll when DTE < this

        # VSTOXX call parameters
        "vstoxx_otm_points_normal": 5,   # Points OTM for calls
        "vstoxx_otm_points_elevated": 3,
        "vstoxx_spread_width_normal": 10, # Points for call spread
        "vstoxx_spread_width_elevated": 15,

        # SX5E put parameters
        "sx5e_otm_pct_normal": 0.10,     # 10% OTM
        "sx5e_otm_pct_elevated": 0.07,   # 7% OTM (closer)
        "sx5e_spread_width_pct": 0.05,   # 5% wide spread

        # EU Banks put parameters
        "eu_banks_otm_pct": 0.15,        # 15% OTM (deeper)

        # Allocation within sleeve
        "vstoxx_allocation": 0.50,
        "sx5e_allocation": 0.35,
        "eu_banks_allocation": 0.15,

        # Z-score history
        "zscore_lookback": 60,           # Days for term spread z-score
    }

    def __init__(self, config: Optional[Dict] = None):
        """Initialize with optional config overrides."""
        self.config = {**self.DEFAULT_CONFIG, **(config or {})}

        # Historical data for z-scores
        self._term_spread_history: List[float] = []
        self._v2x_history: List[float] = []
        self._v2x_return_history: List[float] = []

    def compute_signal(
        self,
        v2x_spot: float,
        v2x_front: float,
        v2x_back: float,
        v2x_history: Optional[List[float]] = None
    ) -> VolSignal:
        """
        Compute combined volatility signal.

        Args:
            v2x_spot: Current V2X index level
            v2x_front: Front month VSTOXX future
            v2x_back: Second month VSTOXX future
            v2x_history: Optional historical V2X levels (last 60 days)

        Returns:
            VolSignal with all derived signals and recommendations
        """
        # Update history
        if v2x_history:
            self._v2x_history = v2x_history[-self.config["zscore_lookback"]:]
        self._v2x_history.append(v2x_spot)
        if len(self._v2x_history) > self.config["zscore_lookback"]:
            self._v2x_history = self._v2x_history[-self.config["zscore_lookback"]:]

        # Compute term structure
        term_spread = v2x_back - v2x_front
        self._term_spread_history.append(term_spread)
        if len(self._term_spread_history) > self.config["zscore_lookback"]:
            self._term_spread_history = self._term_spread_history[-self.config["zscore_lookback"]:]

        term_structure = self._classify_term_structure(term_spread)
        term_spread_zscore = self._compute_zscore(
            term_spread, self._term_spread_history
        )

        # Compute vol-of-vol
        vol_of_vol = self._compute_vol_of_vol()
        vol_jump, vol_jump_magnitude = self._detect_vol_jump()

        # Determine regime
        vol_regime = self._classify_vol_regime(v2x_spot)

        # Compute action signals
        should_add, should_monetize, structure_pref = self._compute_actions(
            v2x_spot, term_structure, term_spread_zscore, vol_regime, vol_jump
        )

        # Compute sizing multiplier
        sizing_mult = self._compute_sizing_multiplier(
            v2x_spot, term_structure, term_spread_zscore, vol_regime
        )

        return VolSignal(
            v2x_spot=v2x_spot,
            v2x_front=v2x_front,
            v2x_back=v2x_back,
            term_structure=term_structure,
            term_spread=term_spread,
            term_spread_zscore=term_spread_zscore,
            vol_of_vol=vol_of_vol,
            vol_jump=vol_jump,
            vol_jump_magnitude=vol_jump_magnitude,
            vol_regime=vol_regime,
            should_add_convexity=should_add,
            should_monetize=should_monetize,
            structure_preference=structure_pref,
            sizing_multiplier=sizing_mult
        )

    def compute_target_positions(
        self,
        signal: VolSignal,
        sleeve_nav: float,
        sx5e_spot: float,
        current_dte: Optional[int] = None
    ) -> ConvexityPosition:
        """
        Compute target convexity positions based on signal.

        Args:
            signal: VolSignal from compute_signal()
            sleeve_nav: NAV allocated to Europe vol convexity sleeve
            sx5e_spot: Current Euro STOXX 50 level
            current_dte: Current position DTE (for roll decisions)

        Returns:
            ConvexityPosition with target structures
        """
        cfg = self.config
        regime = signal.vol_regime

        # Determine target DTE
        target_dte = (
            cfg["target_dte_elevated"]
            if regime in [VolRegime.ELEVATED, VolRegime.CRISIS]
            else cfg["target_dte_normal"]
        )

        # Determine if should roll
        should_roll = current_dte is not None and current_dte < cfg["roll_dte_threshold"]
        days_to_roll = current_dte - cfg["roll_dte_threshold"] if current_dte else target_dte

        # VSTOXX call parameters
        vstoxx_otm = (
            cfg["vstoxx_otm_points_elevated"]
            if regime in [VolRegime.ELEVATED, VolRegime.CRISIS]
            else cfg["vstoxx_otm_points_normal"]
        )
        vstoxx_strike = signal.v2x_front + vstoxx_otm

        # Use spreads in NORMAL/LOW, outrights in ELEVATED/CRISIS
        vstoxx_is_spread = regime in [VolRegime.LOW, VolRegime.NORMAL]
        vstoxx_spread_width = (
            cfg["vstoxx_spread_width_elevated"]
            if regime == VolRegime.ELEVATED
            else cfg["vstoxx_spread_width_normal"]
        )

        # SX5E put parameters
        sx5e_otm_pct = (
            cfg["sx5e_otm_pct_elevated"]
            if regime in [VolRegime.ELEVATED, VolRegime.CRISIS]
            else cfg["sx5e_otm_pct_normal"]
        )
        sx5e_strike_pct = 1.0 - sx5e_otm_pct

        # Use spreads in NORMAL, outrights in ELEVATED/CRISIS
        sx5e_is_spread = regime in [VolRegime.LOW, VolRegime.NORMAL]

        # Compute notionals with sizing multiplier
        sizing = signal.sizing_multiplier
        vstoxx_notional = sleeve_nav * cfg["vstoxx_allocation"] * sizing
        sx5e_notional = sleeve_nav * cfg["sx5e_allocation"] * sizing
        eu_banks_notional = sleeve_nav * cfg["eu_banks_allocation"] * sizing

        return ConvexityPosition(
            vstoxx_call_strike=vstoxx_strike,
            vstoxx_call_dte=target_dte,
            vstoxx_call_notional=vstoxx_notional,
            vstoxx_call_is_spread=vstoxx_is_spread,
            vstoxx_call_spread_width=vstoxx_spread_width if vstoxx_is_spread else 0,
            sx5e_put_strike_pct=sx5e_strike_pct,
            sx5e_put_dte=target_dte,
            sx5e_put_notional=sx5e_notional,
            sx5e_put_is_spread=sx5e_is_spread,
            sx5e_put_spread_width_pct=cfg["sx5e_spread_width_pct"] if sx5e_is_spread else 0,
            eu_banks_put_strike_pct=1.0 - cfg["eu_banks_otm_pct"],
            eu_banks_put_notional=eu_banks_notional,
            should_roll=should_roll,
            days_to_roll=max(0, days_to_roll)
        )

    def _classify_term_structure(self, term_spread: float) -> TermStructure:
        """Classify term structure state."""
        cfg = self.config
        if term_spread > cfg["contango_threshold"]:
            return TermStructure.CONTANGO
        elif term_spread < cfg["backwardation_threshold"]:
            return TermStructure.BACKWARDATION
        else:
            return TermStructure.FLAT

    def _classify_vol_regime(self, v2x: float) -> VolRegime:
        """Classify volatility regime."""
        cfg = self.config
        if v2x < cfg["low_vol_threshold"]:
            return VolRegime.LOW
        elif v2x < cfg["elevated_vol_threshold"]:
            return VolRegime.NORMAL
        elif v2x < cfg["crisis_vol_threshold"]:
            return VolRegime.ELEVATED
        else:
            return VolRegime.CRISIS

    def _compute_vol_of_vol(self) -> float:
        """Compute volatility of V2X (vol-of-vol)."""
        if len(self._v2x_history) < 2:
            return 0.0

        # Compute daily returns
        returns = []
        for i in range(1, len(self._v2x_history)):
            if self._v2x_history[i-1] > 0:
                ret = (self._v2x_history[i] - self._v2x_history[i-1]) / self._v2x_history[i-1]
                returns.append(ret)

        if not returns:
            return 0.0

        return float(np.std(returns) * np.sqrt(252))  # Annualized

    def _detect_vol_jump(self) -> Tuple[bool, float]:
        """
        Detect large 1-3 day move in V2X.

        Returns:
            (is_jump, magnitude) tuple
        """
        cfg = self.config
        window = cfg["vol_jump_window"]
        threshold = cfg["vol_jump_threshold"]

        if len(self._v2x_history) < window + 20:
            return False, 0.0

        # Compute recent move
        recent = self._v2x_history[-window:]
        recent_move = (recent[-1] - recent[0]) / recent[0] if recent[0] > 0 else 0

        # Compute historical move distribution
        moves = []
        for i in range(window, len(self._v2x_history)):
            prev = self._v2x_history[i - window]
            curr = self._v2x_history[i]
            if prev > 0:
                moves.append((curr - prev) / prev)

        if not moves:
            return False, 0.0

        mean_move = np.mean(moves)
        std_move = np.std(moves)

        if std_move == 0:
            return False, 0.0

        zscore = (recent_move - mean_move) / std_move

        is_jump = abs(zscore) > threshold
        return is_jump, recent_move

    def _compute_zscore(self, value: float, history: List[float]) -> float:
        """Compute z-score of value vs history."""
        if len(history) < 10:
            return 0.0

        mean = np.mean(history)
        std = np.std(history)

        if std == 0:
            return 0.0

        return (value - mean) / std

    def _compute_actions(
        self,
        v2x: float,
        term_structure: TermStructure,
        term_zscore: float,
        vol_regime: VolRegime,
        vol_jump: bool
    ) -> Tuple[bool, bool, str]:
        """
        Compute action signals.

        Returns:
            (should_add, should_monetize, structure_preference)
        """
        should_add = False
        should_monetize = False
        structure_pref = "spreads"

        # In CRISIS: monetize winners, selective re-up
        if vol_regime == VolRegime.CRISIS:
            should_monetize = True
            structure_pref = "outrights"  # If re-upping, use outrights
            # Only add if vol just spiked and term structure inverted
            if vol_jump and term_structure == TermStructure.BACKWARDATION:
                should_add = True

        # In ELEVATED: add tails, widen spreads
        elif vol_regime == VolRegime.ELEVATED:
            should_add = True
            structure_pref = "tails"  # Add disaster tails

        # In NORMAL: use spreads, time entries with term structure
        elif vol_regime == VolRegime.NORMAL:
            structure_pref = "spreads"
            # Add when contango is high (vol cheap vs futures)
            if term_structure == TermStructure.CONTANGO and term_zscore > 0.5:
                should_add = True

        # In LOW: vol is cheap, add via outrights
        else:  # VolRegime.LOW
            should_add = True
            structure_pref = "outrights"

        return should_add, should_monetize, structure_pref

    def _compute_sizing_multiplier(
        self,
        v2x: float,
        term_structure: TermStructure,
        term_zscore: float,
        vol_regime: VolRegime
    ) -> float:
        """
        Compute position sizing multiplier.

        Returns:
            0.5 to 2.0 multiplier
        """
        multiplier = 1.0

        # Regime adjustments
        if vol_regime == VolRegime.LOW:
            # Vol cheap, size up
            multiplier *= 1.3
        elif vol_regime == VolRegime.ELEVATED:
            # Add protection
            multiplier *= 1.2
        elif vol_regime == VolRegime.CRISIS:
            # Be selective
            multiplier *= 0.7

        # Term structure adjustments
        if term_structure == TermStructure.CONTANGO:
            # Vol futures cheap vs spot, good entry
            multiplier *= 1.1
        elif term_structure == TermStructure.BACKWARDATION:
            # Vol expensive, reduce size
            multiplier *= 0.8

        # Z-score adjustment (extreme contango = cheap vol)
        if term_zscore > 1.5:
            multiplier *= 1.2
        elif term_zscore < -1.5:
            multiplier *= 0.8

        # Clamp to reasonable range
        return max(0.5, min(2.0, multiplier))

    def estimate_daily_return(
        self,
        signal: VolSignal,
        v2x_change_pct: float,
        sx5e_return: float
    ) -> float:
        """
        Estimate daily return for Europe vol convexity sleeve.

        This is for backtesting - uses simplified option return model.

        Args:
            signal: Current VolSignal
            v2x_change_pct: Daily V2X percentage change
            sx5e_return: Daily SX5E return

        Returns:
            Estimated daily sleeve return
        """
        regime = signal.vol_regime
        cfg = self.config

        # Base theta decay (negative in normal markets)
        # Spreads have lower decay than outrights
        if signal.structure_preference == "spreads":
            base_decay = -0.0002  # ~5% annual
        elif signal.structure_preference == "tails":
            base_decay = -0.0004  # ~10% annual
        else:
            base_decay = -0.0003  # ~7.5% annual

        # VSTOXX call return (50% of sleeve)
        # Simplified: delta ~ 0.3 for OTM calls, gamma adds convexity
        vstoxx_delta = 0.3 if regime in [VolRegime.NORMAL, VolRegime.LOW] else 0.5
        vstoxx_gamma = 0.05  # Convexity

        vstoxx_return = (
            vstoxx_delta * v2x_change_pct +
            vstoxx_gamma * (v2x_change_pct ** 2) * np.sign(v2x_change_pct) +
            base_decay
        )

        # SX5E put return (35% of sleeve)
        # Puts profit from negative SX5E returns
        sx5e_delta = -0.25 if regime in [VolRegime.NORMAL, VolRegime.LOW] else -0.40
        sx5e_gamma = 0.03

        sx5e_put_return = (
            sx5e_delta * sx5e_return +
            sx5e_gamma * (sx5e_return ** 2) * (-np.sign(sx5e_return)) +
            base_decay
        )

        # EU Banks put return (15% of sleeve)
        # Banks are higher beta, so higher delta
        eu_banks_delta = -0.35 if regime in [VolRegime.NORMAL, VolRegime.LOW] else -0.50
        eu_banks_return = (
            eu_banks_delta * sx5e_return * 1.3 +  # Banks ~ 1.3x SX5E beta
            base_decay
        )

        # Weight returns by allocation
        total_return = (
            vstoxx_return * cfg["vstoxx_allocation"] +
            sx5e_put_return * cfg["sx5e_allocation"] +
            eu_banks_return * cfg["eu_banks_allocation"]
        )

        # Apply sizing multiplier
        return total_return * signal.sizing_multiplier


# Convenience function for backtest
def compute_europe_vol_return(
    v2x: float,
    v2x_prev: float,
    sx5e_return: float,
    regime: str,
    v2x_history: Optional[List[float]] = None
) -> float:
    """
    Compute Europe vol convexity return for backtest.

    Simplified interface for backtest.py.
    """
    engine = EuropeVolEngine()

    # Estimate front/back futures from spot (simplified)
    v2x_front = v2x * 0.98  # Front typically at small discount
    v2x_back = v2x * 1.02   # Back typically at premium (contango)

    signal = engine.compute_signal(
        v2x_spot=v2x,
        v2x_front=v2x_front,
        v2x_back=v2x_back,
        v2x_history=v2x_history
    )

    v2x_change_pct = (v2x - v2x_prev) / v2x_prev if v2x_prev > 0 else 0

    return engine.estimate_daily_return(signal, v2x_change_pct, sx5e_return)
