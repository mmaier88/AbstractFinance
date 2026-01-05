"""
Hedge Ladder Engine for AbstractFinance.

v2.4: Implements sophisticated hedge program with 3-expiry ladder
and two-bucket structure for crash convexity and crisis monetizers.

Key Features:
- 3-expiry ladder (30/60/90 DTE)
- Two buckets: Crash Convexity (40%) and Crisis Monetizers (60%)
- Intelligent roll logic with VIX spike detection
- Budget allocation across 6 legs

Structure:
    Hedge Budget (35-50bps annual)
    |-- Crash Convexity Bucket (40%)
    |   |-- 30-DTE leg (33%)
    |   |-- 60-DTE leg (33%)
    |   `-- 90-DTE leg (34%)
    `-- Crisis Monetizers Bucket (60%)
        |-- 30-DTE leg (33%)
        |-- 60-DTE leg (33%)
        `-- 90-DTE leg (34%)
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Tuple, Any
from enum import Enum

from .portfolio import PortfolioState
from .strategy_logic import OrderSpec

logger = logging.getLogger(__name__)


class HedgeBucket(Enum):
    """Hedge bucket classification."""
    CRASH_CONVEXITY = "crash_convexity"    # Deep OTM puts for instant crash payoff
    CRISIS_MONETIZERS = "crisis_monetizers"  # Near-money puts for extended crisis


@dataclass
class HedgeLeg:
    """A single leg in the hedge ladder."""
    bucket: HedgeBucket
    target_dte: int  # 30, 60, or 90
    strike_pct_otm: float  # Percentage OTM (e.g., 0.18 = 18% OTM)
    current_dte: Optional[int] = None  # Current days to expiry
    current_strike: Optional[float] = None
    current_qty: int = 0
    current_symbol: Optional[str] = None
    current_expiry: Optional[date] = None
    budget_allocation: float = 0.0  # Fraction of total budget

    @property
    def needs_roll(self) -> bool:
        """Check if this leg needs to be rolled."""
        if self.current_dte is None:
            return True  # No position, needs initial entry
        return False  # Roll logic handled externally

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging."""
        return {
            "bucket": self.bucket.value,
            "target_dte": self.target_dte,
            "strike_pct_otm": self.strike_pct_otm,
            "current_dte": self.current_dte,
            "current_qty": self.current_qty,
            "budget_allocation": round(self.budget_allocation, 4),
        }


@dataclass
class HedgeLadderConfig:
    """Configuration for hedge ladder."""
    enabled: bool = True
    annual_budget_pct: float = 0.0040  # 40bps annual budget

    # Bucket allocations (sum to 1.0)
    crash_convexity_allocation: float = 0.40
    crisis_monetizers_allocation: float = 0.60

    # Strike selections by bucket
    crash_convexity_strike_pct_otm: float = 0.18  # 18% OTM for deep protection
    crisis_monetizers_strike_pct_otm: float = 0.08  # 8% OTM for faster payoff

    # Ladder structure
    target_dtes: List[int] = field(default_factory=lambda: [30, 60, 90])

    # Roll logic
    roll_trigger_dte: int = 21  # Roll when DTE reaches this
    low_vol_roll_dte: int = 7  # Roll at this DTE in low vol
    skip_roll_vix_spike_pct: float = 0.15  # Skip roll if VIX up >15%

    # Position sizing
    min_contract_value_usd: float = 100.0
    max_single_leg_pct: float = 0.25  # Max 25% of budget in one leg

    # Underlying symbols for puts
    primary_underlying: str = "SPY"  # Primary hedge instrument
    secondary_underlying: str = "EWG"  # Secondary (Europe exposure)

    @classmethod
    def from_settings(cls, settings: Dict[str, Any]) -> "HedgeLadderConfig":
        """Create config from settings dict."""
        hl_settings = settings.get('hedge_ladder', {})

        # Parse bucket settings
        buckets = hl_settings.get('buckets', {})
        crash = buckets.get('crash_convexity', {})
        crisis = buckets.get('crisis_monetizers', {})

        # Parse ladder settings
        ladder = hl_settings.get('ladder', {})

        return cls(
            enabled=hl_settings.get('enabled', True),
            annual_budget_pct=hl_settings.get('annual_budget_pct', 0.0040),
            crash_convexity_allocation=crash.get('allocation', 0.40),
            crisis_monetizers_allocation=crisis.get('allocation', 0.60),
            crash_convexity_strike_pct_otm=crash.get('strike_pct_otm', 0.18),
            crisis_monetizers_strike_pct_otm=crisis.get('strike_pct_otm', 0.08),
            target_dtes=ladder.get('legs', [30, 60, 90]),
            roll_trigger_dte=ladder.get('roll_trigger_dte', 21),
            low_vol_roll_dte=ladder.get('low_vol_roll_dte', 7),
            skip_roll_vix_spike_pct=ladder.get('skip_roll_vix_spike_pct', 0.15),
            primary_underlying=hl_settings.get('primary_underlying', 'SPY'),
            secondary_underlying=hl_settings.get('secondary_underlying', 'EWG'),
        )


@dataclass
class RollDecision:
    """Decision about rolling a hedge leg."""
    leg: HedgeLeg
    should_roll: bool
    reason: str
    close_order: Optional[OrderSpec] = None
    open_order: Optional[OrderSpec] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging."""
        return {
            "leg": self.leg.to_dict(),
            "should_roll": self.should_roll,
            "reason": self.reason,
            "has_close_order": self.close_order is not None,
            "has_open_order": self.open_order is not None,
        }


class HedgeLadderEngine:
    """
    Engine for computing and maintaining the hedge ladder.

    Manages 6 legs across 2 buckets with intelligent roll logic.
    """

    def __init__(self, config: Optional[HedgeLadderConfig] = None):
        """Initialize hedge ladder engine."""
        self.config = config or HedgeLadderConfig()

        # Current ladder state
        self._legs: Dict[Tuple[HedgeBucket, int], HedgeLeg] = {}

        # VIX tracking for roll decisions
        self._vix_history: List[Tuple[datetime, float]] = []
        self._last_vix: Optional[float] = None

        # Initialize legs
        self._initialize_legs()

    def _initialize_legs(self) -> None:
        """Initialize all 6 hedge legs."""
        # Crash Convexity legs (40% of budget, split across 3 DTEs)
        cc_per_leg = self.config.crash_convexity_allocation / len(self.config.target_dtes)
        for dte in self.config.target_dtes:
            self._legs[(HedgeBucket.CRASH_CONVEXITY, dte)] = HedgeLeg(
                bucket=HedgeBucket.CRASH_CONVEXITY,
                target_dte=dte,
                strike_pct_otm=self.config.crash_convexity_strike_pct_otm,
                budget_allocation=cc_per_leg,
            )

        # Crisis Monetizers legs (60% of budget, split across 3 DTEs)
        cm_per_leg = self.config.crisis_monetizers_allocation / len(self.config.target_dtes)
        for dte in self.config.target_dtes:
            self._legs[(HedgeBucket.CRISIS_MONETIZERS, dte)] = HedgeLeg(
                bucket=HedgeBucket.CRISIS_MONETIZERS,
                target_dte=dte,
                strike_pct_otm=self.config.crisis_monetizers_strike_pct_otm,
                budget_allocation=cm_per_leg,
            )

        logger.info(f"Initialized {len(self._legs)} hedge legs")

    def update_vix(self, vix_level: float) -> None:
        """Update VIX tracking for roll decisions."""
        now = datetime.now()
        self._vix_history.append((now, vix_level))

        # Keep last 30 days of history
        cutoff = now - timedelta(days=30)
        self._vix_history = [
            (ts, v) for ts, v in self._vix_history
            if ts > cutoff
        ]

        self._last_vix = vix_level

    def _detect_vix_spike(self) -> bool:
        """Detect if VIX has spiked significantly."""
        if len(self._vix_history) < 2:
            return False

        # Compare current to average of last 5 readings
        recent_vix = [v for _, v in self._vix_history[-6:-1]]
        if not recent_vix:
            return False

        avg_recent = sum(recent_vix) / len(recent_vix)
        current = self._vix_history[-1][1]

        spike_pct = (current - avg_recent) / avg_recent if avg_recent > 0 else 0
        return spike_pct > self.config.skip_roll_vix_spike_pct

    def compute_budget_allocation(
        self,
        nav: float,
        days_remaining_in_year: int = 252
    ) -> Dict[Tuple[HedgeBucket, int], float]:
        """
        Compute USD budget allocation per leg.

        Args:
            nav: Current portfolio NAV
            days_remaining_in_year: Trading days remaining

        Returns:
            Dict of (bucket, dte) -> USD budget for that leg
        """
        # Annual budget in USD
        annual_budget_usd = nav * self.config.annual_budget_pct

        # Daily budget (spread evenly through year)
        daily_budget_usd = annual_budget_usd / 252

        # For monthly rolls, budget is ~21 trading days worth
        # We allocate when rolling, not daily
        roll_budget_usd = daily_budget_usd * 21

        allocations = {}
        for key, leg in self._legs.items():
            allocations[key] = roll_budget_usd * leg.budget_allocation

        return allocations

    def compute_ladder_positions(
        self,
        portfolio: PortfolioState,
        underlying_price: float,
        today: Optional[date] = None
    ) -> List[HedgeLeg]:
        """
        Compute target positions for all 6 hedge legs.

        Args:
            portfolio: Current portfolio state
            underlying_price: Current price of underlying
            today: Current date

        Returns:
            List of HedgeLeg with target positions
        """
        today = today or date.today()

        # Get budget allocations
        budgets = self.compute_budget_allocation(portfolio.nav)

        positions = []
        for key, leg in self._legs.items():
            # Compute strike for this leg
            if leg.bucket == HedgeBucket.CRASH_CONVEXITY:
                strike_pct = self.config.crash_convexity_strike_pct_otm
            else:
                strike_pct = self.config.crisis_monetizers_strike_pct_otm

            strike = underlying_price * (1 - strike_pct)
            strike = round(strike, 0)  # Round to nearest dollar

            # Estimate option premium (simplified Black-Scholes estimate)
            # In production, would use actual option chain
            estimated_premium = self._estimate_put_premium(
                underlying_price, strike, leg.target_dte
            )

            # Compute quantity based on budget
            leg_budget = budgets.get(key, 0)
            if estimated_premium > 0:
                # Each contract is 100 shares
                contract_cost = estimated_premium * 100
                qty = int(leg_budget / contract_cost) if contract_cost > 0 else 0
                qty = max(qty, 1)  # At least 1 contract per leg
            else:
                qty = 1

            # Update leg with computed values
            leg.current_strike = strike
            leg.current_qty = qty
            positions.append(leg)

        return positions

    def _estimate_put_premium(
        self,
        underlying: float,
        strike: float,
        dte: int,
        vol: float = 0.20
    ) -> float:
        """
        Simplified put premium estimation.

        Uses approximate Black-Scholes for budgeting purposes.
        In production, use actual option chain prices.

        Args:
            underlying: Current underlying price
            strike: Put strike price
            dte: Days to expiry
            vol: Implied volatility assumption

        Returns:
            Estimated put premium per share
        """
        import math

        # Time to expiry in years
        t = dte / 365.0

        # Risk-free rate assumption
        r = 0.05

        # Simple approximation for OTM puts
        # Premium roughly proportional to sqrt(time) * vol * distance from ATM
        moneyness = underlying / strike
        otm_pct = max(moneyness - 1, 0)

        # Rough premium estimate
        if otm_pct > 0.15:
            # Deep OTM - premium is low
            premium = underlying * vol * math.sqrt(t) * 0.05 * math.exp(-otm_pct * 3)
        else:
            # Near the money
            premium = underlying * vol * math.sqrt(t) * 0.15 * math.exp(-otm_pct * 2)

        return max(premium, 0.10)  # Floor at $0.10

    def compute_roll_decisions(
        self,
        portfolio: PortfolioState,
        underlying_price: float,
        current_positions: Dict[str, int],
        today: Optional[date] = None
    ) -> List[RollDecision]:
        """
        Compute roll decisions for all legs.

        Args:
            portfolio: Current portfolio state
            underlying_price: Current underlying price
            current_positions: Current option positions {symbol: qty}
            today: Current date

        Returns:
            List of RollDecision for legs needing action
        """
        today = today or date.today()
        decisions = []

        # Check for VIX spike
        vix_spike = self._detect_vix_spike()

        for key, leg in self._legs.items():
            # Determine if this leg needs a roll
            should_roll = False
            reason = ""

            # Check DTE-based roll
            if leg.current_dte is not None:
                if leg.current_dte <= self.config.roll_trigger_dte:
                    should_roll = True
                    reason = f"DTE {leg.current_dte} <= trigger {self.config.roll_trigger_dte}"

                # In low vol, can wait longer
                if self._last_vix and self._last_vix < 15:
                    if leg.current_dte <= self.config.low_vol_roll_dte:
                        should_roll = True
                        reason = f"Low vol roll: DTE {leg.current_dte}"
                    elif leg.current_dte > self.config.low_vol_roll_dte:
                        should_roll = False
                        reason = "Low vol, waiting for closer to expiry"
            else:
                # No current position, need to initiate
                should_roll = True
                reason = "No current position"

            # Skip roll during VIX spike (wait for normalization)
            if should_roll and vix_spike and leg.current_dte is not None:
                if leg.current_dte > 7:  # Only skip if not too close to expiry
                    should_roll = False
                    reason = "VIX spike detected, delaying roll"

            decision = RollDecision(
                leg=leg,
                should_roll=should_roll,
                reason=reason
            )

            # Generate orders if rolling
            if should_roll:
                # Close existing position if any
                if leg.current_symbol and leg.current_qty > 0:
                    decision.close_order = OrderSpec(
                        instrument_id=leg.current_symbol,
                        quantity=-leg.current_qty,  # Negative to close long put
                        order_type="LMT",
                        sleeve="europe_vol_convex",
                        reason=f"Close {leg.bucket.value} {leg.target_dte}DTE leg"
                    )

                # Open new position
                new_expiry = today + timedelta(days=leg.target_dte)
                new_strike = underlying_price * (1 - leg.strike_pct_otm)
                new_strike = round(new_strike, 0)

                # Format option symbol (simplified - actual would use OCC format)
                new_symbol = f"{self.config.primary_underlying}_P_{new_strike}_{new_expiry.strftime('%Y%m%d')}"

                # Compute target quantity
                positions = self.compute_ladder_positions(portfolio, underlying_price, today)
                target_leg = next((p for p in positions if p.bucket == leg.bucket and p.target_dte == leg.target_dte), None)
                qty = target_leg.current_qty if target_leg else 1

                decision.open_order = OrderSpec(
                    instrument_id=new_symbol,
                    quantity=qty,  # Positive to buy put
                    order_type="LMT",
                    sleeve="europe_vol_convex",
                    reason=f"Open {leg.bucket.value} {leg.target_dte}DTE leg, strike={new_strike}"
                )

            decisions.append(decision)

        return decisions

    def update_leg_from_position(
        self,
        bucket: HedgeBucket,
        target_dte: int,
        symbol: str,
        qty: int,
        expiry: date,
        strike: float,
        today: Optional[date] = None
    ) -> None:
        """
        Update a leg's state from an actual position.

        Call this after syncing positions from broker.

        Args:
            bucket: Which bucket this position belongs to
            target_dte: Target DTE for this leg
            symbol: Option symbol
            qty: Position quantity
            expiry: Expiry date
            strike: Strike price
            today: Current date
        """
        today = today or date.today()
        key = (bucket, target_dte)

        if key in self._legs:
            leg = self._legs[key]
            leg.current_symbol = symbol
            leg.current_qty = qty
            leg.current_expiry = expiry
            leg.current_strike = strike
            leg.current_dte = (expiry - today).days

    def get_summary(self) -> Dict[str, Any]:
        """Get summary of hedge ladder state."""
        legs_summary = []
        for key, leg in self._legs.items():
            legs_summary.append(leg.to_dict())

        return {
            "enabled": self.config.enabled,
            "annual_budget_pct": self.config.annual_budget_pct,
            "buckets": {
                "crash_convexity": {
                    "allocation": self.config.crash_convexity_allocation,
                    "strike_pct_otm": self.config.crash_convexity_strike_pct_otm,
                },
                "crisis_monetizers": {
                    "allocation": self.config.crisis_monetizers_allocation,
                    "strike_pct_otm": self.config.crisis_monetizers_strike_pct_otm,
                },
            },
            "target_dtes": self.config.target_dtes,
            "legs": legs_summary,
            "last_vix": self._last_vix,
            "vix_spike_detected": self._detect_vix_spike(),
        }


def create_hedge_ladder_engine(
    settings: Dict[str, Any]
) -> HedgeLadderEngine:
    """Factory function to create HedgeLadderEngine from settings."""
    config = HedgeLadderConfig.from_settings(settings)
    return HedgeLadderEngine(config)
