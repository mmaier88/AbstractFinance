"""
Sovereign Crisis Options Overlay for AbstractFinance.

Phase 2: Put spreads on periphery sovereign exposure.
Implements sovereign stress detection and hedging using
US-listed ETF proxies (EWI, EWQ, FXE, EUFN).

Key Features:
- Sovereign stress monitoring (spread-to-Bund proxies)
- Put spreads on Italy, France, EUR/USD, EU banks
- Budget: 25-50bps annual (0.25% - 0.50% of NAV)
- Tiered response to stress levels
- Integration with TailHedgeManager
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

logger = logging.getLogger(__name__)


class SovereignCountry(Enum):
    """Target countries for sovereign overlay."""
    ITALY = "italy"
    FRANCE = "france"
    SPAIN = "spain"
    PORTUGAL = "portugal"


class StressLevel(Enum):
    """Sovereign stress levels."""
    LOW = "low"           # Normal conditions
    ELEVATED = "elevated"  # Widening spreads
    HIGH = "high"         # Significant stress
    CRISIS = "crisis"     # Crisis conditions


class OverlayAction(Enum):
    """Actions for overlay management."""
    HOLD = "hold"         # No change
    ADD = "add"           # Add protection
    INCREASE = "increase" # Increase protection
    MONETIZE = "monetize" # Take profits
    ROLL = "roll"         # Roll positions


@dataclass
class SovereignProxy:
    """
    US-listed proxy for sovereign exposure.

    Since EUREX instruments aren't available, we use
    US-listed ETFs as proxies for European sovereign risk.
    """
    country: SovereignCountry
    symbol: str           # US-listed ETF symbol
    description: str
    correlation: float    # Correlation to sovereign stress
    options_available: bool
    multiplier: float = 100  # Standard options multiplier

    # Option targeting
    otm_pct: float = 0.10      # Target OTM percentage for puts
    spread_width: float = 0.05  # Put spread width as % of strike


# US-listed proxies for European sovereign risk
SOVEREIGN_PROXIES = {
    SovereignCountry.ITALY: SovereignProxy(
        country=SovereignCountry.ITALY,
        symbol="EWI",
        description="iShares MSCI Italy ETF",
        correlation=0.85,
        options_available=True,
        otm_pct=0.10,
        spread_width=0.05,
    ),
    SovereignCountry.FRANCE: SovereignProxy(
        country=SovereignCountry.FRANCE,
        symbol="EWQ",
        description="iShares MSCI France ETF",
        correlation=0.75,
        options_available=True,
        otm_pct=0.08,
        spread_width=0.04,
    ),
    # EUR/USD as currency proxy for all periphery
    "EUR_USD": SovereignProxy(
        country=SovereignCountry.ITALY,  # Mapped to Italy for simplicity
        symbol="FXE",
        description="Invesco CurrencyShares Euro Trust",
        correlation=0.70,
        options_available=True,
        otm_pct=0.05,
        spread_width=0.03,
    ),
    # EU Banks as systemic risk proxy
    "EU_BANKS": SovereignProxy(
        country=SovereignCountry.ITALY,
        symbol="EUFN",
        description="iShares MSCI Europe Financials ETF",
        correlation=0.90,
        options_available=True,
        otm_pct=0.12,
        spread_width=0.06,
    ),
}


@dataclass
class SovereignStressSignal:
    """Sovereign stress signal output."""
    country: SovereignCountry
    stress_level: StressLevel
    stress_score: float  # 0.0 to 1.0
    spread_proxy: float  # Proxy for spread-to-Bund
    trend: str  # "widening", "stable", "tightening"
    action: OverlayAction
    commentary: str


@dataclass
class OverlayPosition:
    """A position in the sovereign overlay."""
    position_id: str
    proxy: SovereignProxy
    structure: str  # "put", "put_spread", "collar"
    quantity: int
    long_strike: float
    short_strike: Optional[float]
    expiry: date
    premium_paid: float
    current_value: float = 0.0
    delta: float = 0.0

    @property
    def days_to_expiry(self) -> int:
        """Days until expiration."""
        return (self.expiry - date.today()).days

    @property
    def pnl(self) -> float:
        """Current P&L."""
        return self.current_value - self.premium_paid


@dataclass
class OverlayBudget:
    """Budget for sovereign overlay."""
    annual_budget_pct: float  # 0.0025 to 0.0050 (25-50bps)
    nav_at_year_start: float
    used_ytd: float = 0.0
    realized_gains_ytd: float = 0.0

    @property
    def total_budget(self) -> float:
        """Total annual budget in dollars."""
        return self.nav_at_year_start * self.annual_budget_pct

    @property
    def remaining(self) -> float:
        """Remaining budget."""
        # Can recycle 50% of realized gains
        return max(0, self.total_budget - self.used_ytd + self.realized_gains_ytd * 0.5)

    @property
    def monthly_budget(self) -> float:
        """Monthly budget allocation."""
        return self.total_budget / 12


@dataclass
class OverlayConfig:
    """Configuration for sovereign overlay."""
    # Budget settings
    annual_budget_pct: float = 0.0035  # 35bps default (middle of 25-50)
    min_budget_pct: float = 0.0025     # 25bps minimum
    max_budget_pct: float = 0.0050     # 50bps maximum

    # Stress thresholds (proxy-based since no CDS access)
    stress_threshold_elevated: float = 0.25  # 25% drawdown in proxy
    stress_threshold_high: float = 0.40      # 40% drawdown
    stress_threshold_crisis: float = 0.55    # 55% drawdown

    # Position sizing
    max_single_country_pct: float = 0.30  # Max 30% of budget per country
    min_dte_roll: int = 21  # Roll 21 days before expiry
    target_dte: int = 60    # Target 60-day expiry

    # Put spread parameters
    use_spreads: bool = True  # Use put spreads vs naked puts
    spread_width_pct: float = 0.05  # 5% spread width default

    # Country allocations (sum to 1.0)
    country_allocations: Dict[str, float] = field(default_factory=lambda: {
        "italy": 0.35,      # Highest risk
        "france": 0.25,     # Moderate risk
        "eur_usd": 0.20,    # Currency hedge
        "eu_banks": 0.20,   # Systemic risk
    })

    @classmethod
    def from_settings(cls, settings: Dict[str, Any]) -> "OverlayConfig":
        """Create config from settings dict."""
        overlay_settings = settings.get('sovereign_overlay', {})

        return cls(
            annual_budget_pct=overlay_settings.get('annual_budget_pct', 0.0035),
            min_budget_pct=overlay_settings.get('min_budget_pct', 0.0025),
            max_budget_pct=overlay_settings.get('max_budget_pct', 0.0050),
            stress_threshold_elevated=overlay_settings.get('stress_threshold_elevated', 0.25),
            stress_threshold_high=overlay_settings.get('stress_threshold_high', 0.40),
            stress_threshold_crisis=overlay_settings.get('stress_threshold_crisis', 0.55),
            max_single_country_pct=overlay_settings.get('max_single_country_pct', 0.30),
            min_dte_roll=overlay_settings.get('min_dte_roll', 21),
            target_dte=overlay_settings.get('target_dte', 60),
            use_spreads=overlay_settings.get('use_spreads', True),
            spread_width_pct=overlay_settings.get('spread_width_pct', 0.05),
        )


class SovereignCrisisOverlay:
    """
    Sovereign Crisis Options Overlay Manager.

    Monitors sovereign stress and manages protective positions
    using US-listed ETF proxies for European sovereign exposure.

    Key Strategies:
    1. Put spreads on Italy (EWI) - highest periphery risk
    2. Put spreads on France (EWQ) - moderate risk
    3. EUR/USD puts (FXE) - currency hedge
    4. EU banks puts (EUFN) - systemic risk hedge
    """

    def __init__(
        self,
        config: Optional[OverlayConfig] = None,
        settings: Optional[Dict[str, Any]] = None
    ):
        """
        Initialize sovereign overlay manager.

        Args:
            config: Overlay configuration
            settings: Application settings
        """
        self.config = config or (
            OverlayConfig.from_settings(settings) if settings else OverlayConfig()
        )

        # State tracking
        self._positions: Dict[str, OverlayPosition] = {}
        self._budget: Optional[OverlayBudget] = None
        self._stress_signals: Dict[str, SovereignStressSignal] = {}

        # Price history for stress detection
        self._price_history: Dict[str, pd.Series] = {}

        # Last update
        self._last_update: Optional[datetime] = None

    def initialize_budget(
        self,
        nav: float,
        year_start_nav: Optional[float] = None
    ) -> None:
        """Initialize or reset the overlay budget."""
        self._budget = OverlayBudget(
            annual_budget_pct=self.config.annual_budget_pct,
            nav_at_year_start=year_start_nav or nav
        )

    def update_price_history(
        self,
        symbol: str,
        prices: pd.Series
    ) -> None:
        """
        Update price history for a proxy.

        Args:
            symbol: ETF symbol (EWI, EWQ, FXE, EUFN)
            prices: Price series
        """
        self._price_history[symbol] = prices

    def compute_stress_signal(
        self,
        proxy_key: str,
        current_price: float
    ) -> SovereignStressSignal:
        """
        Compute stress signal for a sovereign proxy.

        Args:
            proxy_key: Key in SOVEREIGN_PROXIES
            current_price: Current price of proxy

        Returns:
            SovereignStressSignal with stress level and action
        """
        proxy = SOVEREIGN_PROXIES.get(proxy_key)
        if proxy is None:
            raise ValueError(f"Unknown proxy: {proxy_key}")

        # Get price history
        prices = self._price_history.get(proxy.symbol)

        # Default if no history
        if prices is None or len(prices) < 20:
            return SovereignStressSignal(
                country=proxy.country,
                stress_level=StressLevel.LOW,
                stress_score=0.0,
                spread_proxy=0.0,
                trend="stable",
                action=OverlayAction.HOLD,
                commentary="Insufficient price history"
            )

        # Compute drawdown from high
        high_52w = prices.tail(252).max() if len(prices) >= 252 else prices.max()
        drawdown = (current_price - high_52w) / high_52w

        # Compute trend (20-day momentum)
        if len(prices) >= 20:
            momentum_20d = (current_price - prices.iloc[-20]) / prices.iloc[-20]
            if momentum_20d < -0.05:
                trend = "widening"
            elif momentum_20d > 0.03:
                trend = "tightening"
            else:
                trend = "stable"
        else:
            momentum_20d = 0.0
            trend = "stable"

        # Compute stress score (0 to 1)
        stress_score = min(1.0, max(0.0, -drawdown / 0.50))

        # Determine stress level
        if -drawdown >= self.config.stress_threshold_crisis:
            stress_level = StressLevel.CRISIS
        elif -drawdown >= self.config.stress_threshold_high:
            stress_level = StressLevel.HIGH
        elif -drawdown >= self.config.stress_threshold_elevated:
            stress_level = StressLevel.ELEVATED
        else:
            stress_level = StressLevel.LOW

        # Determine action
        action = self._determine_action(stress_level, trend, proxy_key)

        # Commentary
        commentary = self._build_commentary(
            proxy, stress_level, drawdown, trend, action
        )

        signal = SovereignStressSignal(
            country=proxy.country,
            stress_level=stress_level,
            stress_score=stress_score,
            spread_proxy=-drawdown,  # Use drawdown as spread proxy
            trend=trend,
            action=action,
            commentary=commentary
        )

        self._stress_signals[proxy_key] = signal
        return signal

    def _determine_action(
        self,
        stress_level: StressLevel,
        trend: str,
        proxy_key: str
    ) -> OverlayAction:
        """Determine overlay action based on stress and trend."""
        # Check existing coverage
        has_position = any(
            p.proxy.symbol == SOVEREIGN_PROXIES[proxy_key].symbol
            for p in self._positions.values()
            if p.days_to_expiry > self.config.min_dte_roll
        )

        if stress_level == StressLevel.CRISIS:
            if has_position:
                return OverlayAction.MONETIZE  # Take profits in crisis
            else:
                return OverlayAction.ADD  # Should have had protection

        elif stress_level == StressLevel.HIGH:
            if has_position and trend == "tightening":
                return OverlayAction.MONETIZE  # Take profits if stress reducing
            elif not has_position:
                return OverlayAction.ADD  # Add protection

        elif stress_level == StressLevel.ELEVATED:
            if not has_position and trend == "widening":
                return OverlayAction.ADD  # Add on widening
            elif has_position:
                return OverlayAction.HOLD

        # LOW stress
        if not has_position:
            # Consider adding cheap protection when vol is low
            return OverlayAction.ADD

        return OverlayAction.HOLD

    def _build_commentary(
        self,
        proxy: SovereignProxy,
        stress_level: StressLevel,
        drawdown: float,
        trend: str,
        action: OverlayAction
    ) -> str:
        """Build commentary for stress signal."""
        return (
            f"{proxy.description} ({proxy.symbol}): "
            f"Stress={stress_level.value}, Drawdown={drawdown:.1%}, "
            f"Trend={trend}, Action={action.value}"
        )

    def ensure_overlay_coverage(
        self,
        portfolio_state: PortfolioState,
        data_feed: Any,
        today: Optional[date] = None
    ) -> List[OrderSpec]:
        """
        Ensure adequate overlay coverage.

        Main entry point - checks all proxies and generates orders.

        Args:
            portfolio_state: Current portfolio state
            data_feed: Data feed for prices
            today: Current date

        Returns:
            List of orders to execute
        """
        today = today or date.today()
        orders = []

        # Initialize budget if needed
        if self._budget is None:
            self.initialize_budget(portfolio_state.nav)

        # Check remaining budget
        if self._budget.remaining <= 0:
            logger.warning("Sovereign overlay budget exhausted")
            return orders

        # Update stress signals for each proxy
        for proxy_key, proxy in SOVEREIGN_PROXIES.items():
            try:
                current_price = data_feed.get_last_price(proxy.symbol)
                signal = self.compute_stress_signal(proxy_key, current_price)

                # Generate orders based on signal
                proxy_orders = self._generate_orders_for_signal(
                    signal, proxy, current_price, today
                )
                orders.extend(proxy_orders)

            except Exception as e:
                logger.debug(f"Failed to process {proxy_key}: {e}")

        # Check and roll expiring positions
        roll_orders = self._check_and_roll_positions(data_feed, today)
        orders.extend(roll_orders)

        self._last_update = datetime.now()
        return orders

    def _generate_orders_for_signal(
        self,
        signal: SovereignStressSignal,
        proxy: SovereignProxy,
        current_price: float,
        today: date
    ) -> List[OrderSpec]:
        """Generate orders based on stress signal."""
        orders = []

        if signal.action == OverlayAction.HOLD:
            return orders

        if signal.action == OverlayAction.ADD:
            orders.extend(self._create_put_spread_orders(
                proxy, current_price, today
            ))

        elif signal.action == OverlayAction.INCREASE:
            # Add more protection
            orders.extend(self._create_put_spread_orders(
                proxy, current_price, today, size_multiplier=1.5
            ))

        elif signal.action == OverlayAction.MONETIZE:
            # Close profitable positions
            orders.extend(self._create_monetization_orders(proxy))

        elif signal.action == OverlayAction.ROLL:
            # Handled separately in _check_and_roll_positions
            pass

        return orders

    def _create_put_spread_orders(
        self,
        proxy: SovereignProxy,
        current_price: float,
        today: date,
        size_multiplier: float = 1.0
    ) -> List[OrderSpec]:
        """
        Create put spread orders for a proxy.

        Args:
            proxy: Sovereign proxy
            current_price: Current ETF price
            today: Current date
            size_multiplier: Size multiplier (for increasing positions)

        Returns:
            List of orders for put spread
        """
        orders = []

        if self._budget is None or self._budget.remaining <= 0:
            return orders

        # Get budget allocation for this proxy
        allocation_key = proxy.symbol.lower()
        if allocation_key == "ewi":
            allocation_key = "italy"
        elif allocation_key == "ewq":
            allocation_key = "france"
        elif allocation_key == "fxe":
            allocation_key = "eur_usd"
        elif allocation_key == "eufn":
            allocation_key = "eu_banks"

        allocation = self.config.country_allocations.get(allocation_key, 0.20)

        # Calculate budget for this position
        position_budget = min(
            self._budget.remaining * allocation * size_multiplier,
            self._budget.remaining * self.config.max_single_country_pct
        )

        if position_budget < 100:  # Minimum $100 per position
            return orders

        # Calculate strikes
        long_strike = round(current_price * (1 - proxy.otm_pct), 1)
        short_strike = round(current_price * (1 - proxy.otm_pct - proxy.spread_width), 1)

        # Estimate premium (rough: ~2-4% for OTM puts)
        est_spread_premium = current_price * 0.015  # ~1.5% for spread
        est_premium_per_contract = est_spread_premium * proxy.multiplier

        if est_premium_per_contract <= 0:
            return orders

        # Calculate contracts
        contracts = int(position_budget / est_premium_per_contract)
        contracts = max(1, min(contracts, 50))  # Cap at 50 contracts

        if contracts > 0:
            # Buy long put (lower strike)
            orders.append(OrderSpec(
                instrument_id=f"{proxy.symbol}_put_{long_strike}",
                side="BUY",
                quantity=contracts,
                order_type="LMT",
                limit_price=est_spread_premium * 0.6,  # Long leg
                sleeve=Sleeve.EUROPE_VOL_CONVEX,
                reason=f"Sovereign overlay: Buy {proxy.symbol} {long_strike} Put"
            ))

            if self.config.use_spreads:
                # Sell short put (even lower strike)
                orders.append(OrderSpec(
                    instrument_id=f"{proxy.symbol}_put_{short_strike}",
                    side="SELL",
                    quantity=contracts,
                    order_type="LMT",
                    limit_price=est_spread_premium * 0.4,  # Short leg
                    sleeve=Sleeve.EUROPE_VOL_CONVEX,
                    reason=f"Sovereign overlay: Sell {proxy.symbol} {short_strike} Put"
                ))

            logger.info(
                f"Sovereign overlay: {proxy.symbol} {long_strike}/{short_strike} "
                f"put spread x{contracts}, budget ${position_budget:.0f}"
            )

        return orders

    def _create_monetization_orders(
        self,
        proxy: SovereignProxy
    ) -> List[OrderSpec]:
        """Create orders to close profitable positions."""
        orders = []

        for pos_id, pos in list(self._positions.items()):
            if pos.proxy.symbol != proxy.symbol:
                continue

            # Only monetize profitable positions
            if pos.pnl <= 0:
                continue

            # Close long leg
            orders.append(OrderSpec(
                instrument_id=f"{proxy.symbol}_put_{pos.long_strike}",
                side="SELL",
                quantity=pos.quantity,
                order_type="MKT",
                sleeve=Sleeve.EUROPE_VOL_CONVEX,
                urgency="urgent",
                reason=f"Monetize: {proxy.symbol} put, PnL ${pos.pnl:.0f}"
            ))

            if pos.short_strike:
                # Close short leg
                orders.append(OrderSpec(
                    instrument_id=f"{proxy.symbol}_put_{pos.short_strike}",
                    side="BUY",
                    quantity=pos.quantity,
                    order_type="MKT",
                    sleeve=Sleeve.EUROPE_VOL_CONVEX,
                    urgency="urgent",
                    reason=f"Monetize: Close {proxy.symbol} short put"
                ))

            # Record realized gain
            if self._budget:
                self._budget.realized_gains_ytd += pos.pnl

            # Remove position
            del self._positions[pos_id]

            logger.info(
                f"Monetized {proxy.symbol} position: PnL ${pos.pnl:.0f}"
            )

        return orders

    def _check_and_roll_positions(
        self,
        data_feed: Any,
        today: date
    ) -> List[OrderSpec]:
        """Check for expiring positions and roll them."""
        orders = []

        for pos_id, pos in list(self._positions.items()):
            if pos.days_to_expiry <= self.config.min_dte_roll:
                # Close current position
                orders.append(OrderSpec(
                    instrument_id=f"{pos.proxy.symbol}_put_{pos.long_strike}",
                    side="SELL",
                    quantity=pos.quantity,
                    order_type="MKT",
                    sleeve=Sleeve.EUROPE_VOL_CONVEX,
                    reason=f"Roll: Close expiring {pos.proxy.symbol} put"
                ))

                if pos.short_strike:
                    orders.append(OrderSpec(
                        instrument_id=f"{pos.proxy.symbol}_put_{pos.short_strike}",
                        side="BUY",
                        quantity=pos.quantity,
                        order_type="MKT",
                        sleeve=Sleeve.EUROPE_VOL_CONVEX,
                        reason=f"Roll: Close expiring {pos.proxy.symbol} short put"
                    ))

                # Create new position with further expiry
                try:
                    current_price = data_feed.get_last_price(pos.proxy.symbol)
                    roll_orders = self._create_put_spread_orders(
                        pos.proxy, current_price, today
                    )
                    orders.extend(roll_orders)
                except Exception as e:
                    logger.warning(f"Failed to roll {pos.proxy.symbol}: {e}")

                # Remove old position
                del self._positions[pos_id]

        return orders

    def get_summary(self) -> Dict[str, Any]:
        """Get summary of overlay state."""
        return {
            "positions": {
                pos_id: {
                    "symbol": pos.proxy.symbol,
                    "structure": pos.structure,
                    "quantity": pos.quantity,
                    "strikes": f"{pos.long_strike}/{pos.short_strike or 'naked'}",
                    "dte": pos.days_to_expiry,
                    "premium_paid": pos.premium_paid,
                    "current_value": pos.current_value,
                    "pnl": pos.pnl,
                }
                for pos_id, pos in self._positions.items()
            },
            "stress_signals": {
                key: {
                    "country": sig.country.value,
                    "level": sig.stress_level.value,
                    "score": round(sig.stress_score, 3),
                    "trend": sig.trend,
                    "action": sig.action.value,
                }
                for key, sig in self._stress_signals.items()
            },
            "budget": {
                "annual": self._budget.total_budget if self._budget else 0,
                "used_ytd": self._budget.used_ytd if self._budget else 0,
                "remaining": self._budget.remaining if self._budget else 0,
                "realized_gains": self._budget.realized_gains_ytd if self._budget else 0,
            } if self._budget else None,
            "config": {
                "annual_budget_pct": self.config.annual_budget_pct,
                "use_spreads": self.config.use_spreads,
                "target_dte": self.config.target_dte,
            },
            "last_update": self._last_update.isoformat() if self._last_update else None,
        }

    def get_total_delta(self) -> float:
        """Get total delta exposure from overlay positions."""
        return sum(pos.delta * pos.quantity for pos in self._positions.values())

    def get_total_premium_at_risk(self) -> float:
        """Get total premium paid (max loss)."""
        return sum(pos.premium_paid for pos in self._positions.values())
