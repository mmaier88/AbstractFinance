"""
Tail hedge and crisis management for AbstractFinance.
Manages protective options, sovereign stress trades, and crisis playbook execution.
"""

import pandas as pd
import numpy as np
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from enum import Enum

from .portfolio import PortfolioState, Sleeve
from .strategy_logic import OrderSpec
from .data_feeds import DataFeed
from .options.validator import (
    OptionValidator, OptionValidationConfig, OptionQuote, OptionValidationResult
)


class HedgeType(Enum):
    """Types of tail hedges - EUROPE-CENTRIC (Strategy Evolution)."""
    # PRIMARY: Europe-specific hedges
    EU_VOL_CALL = "eu_vol_call"        # VSTOXX calls (PRIMARY vol hedge)
    EU_EQUITY_PUT = "eu_equity_put"    # SX5E puts (PRIMARY equity hedge)
    EU_BANK_PUT = "eu_bank_put"        # SX7E/EU bank puts

    # SECONDARY: US hedges
    US_VOL_CALL = "us_vol_call"        # VIX calls (secondary)
    US_EQUITY_PUT = "us_equity_put"    # SPY puts (secondary)

    # TERTIARY: Other hedges
    CREDIT_PUT = "credit_put"          # HYG/JNK puts
    SOVEREIGN_SPREAD = "sovereign"     # OAT-Bund spread
    FX_HEDGE = "fx_hedge"              # EUR/USD hedges


@dataclass
class HedgePosition:
    """Represents a tail hedge position."""
    hedge_id: str
    hedge_type: HedgeType
    instrument_id: str
    underlying: str
    quantity: int
    strike: Optional[float] = None
    expiry: Optional[date] = None
    premium_paid: float = 0.0
    current_value: float = 0.0
    delta: float = 0.0
    is_active: bool = True

    @property
    def pnl(self) -> float:
        """Calculate P&L on the hedge."""
        return self.current_value - self.premium_paid

    @property
    def days_to_expiry(self) -> int:
        """Days until expiration."""
        if self.expiry is None:
            return 999
        return (self.expiry - date.today()).days


@dataclass
class HedgeBudget:
    """Tracks hedge budget usage."""
    annual_budget_pct: float
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
        return max(0, self.total_budget - self.used_ytd + self.realized_gains_ytd * 0.5)

    @property
    def usage_pct(self) -> float:
        """Percentage of budget used."""
        if self.total_budget <= 0:
            return 0.0
        return self.used_ytd / self.total_budget


@dataclass
class CrisisAction:
    """Action to take during a crisis event."""
    action_type: str  # "realize_hedges", "increase_hedges", "reduce_exposure"
    orders: List[OrderSpec]
    rebalance_instruction: str
    urgency: str = "normal"
    reason: str = ""


class TailHedgeManager:
    """
    Manages tail hedges and crisis response for the portfolio.
    """

    # Target hedge allocations as % of hedge budget
    # STRATEGY EVOLUTION: Europe-centric allocation
    # Primary (60%): EU vol + EU equity
    # Secondary (25%): US vol + US equity
    # Tertiary (15%): Credit + Sovereign
    HEDGE_ALLOCATION = {
        # PRIMARY: Europe-specific (60% total)
        HedgeType.EU_VOL_CALL: 0.30,       # 30% to VSTOXX calls (PRIMARY)
        HedgeType.EU_EQUITY_PUT: 0.20,     # 20% to SX5E puts (PRIMARY)
        HedgeType.EU_BANK_PUT: 0.10,       # 10% to EU bank puts

        # SECONDARY: US hedges (25% total)
        HedgeType.US_VOL_CALL: 0.15,       # 15% to VIX calls
        HedgeType.US_EQUITY_PUT: 0.10,     # 10% to SPY puts

        # TERTIARY: Other (15% total)
        HedgeType.SOVEREIGN_SPREAD: 0.10,  # 10% to OAT-Bund
        HedgeType.CREDIT_PUT: 0.05,        # 5% to credit puts
    }

    # Minimum days to expiry before rolling
    MIN_DTE_ROLL = 21

    # OTM targets for puts/calls
    OTM_TARGETS = {
        # Europe puts
        HedgeType.EU_EQUITY_PUT: 0.10,    # 10% OTM (SX5E)
        HedgeType.EU_BANK_PUT: 0.15,      # 15% OTM (EU banks)
        # US puts
        HedgeType.US_EQUITY_PUT: 0.15,    # 15% OTM (SPY)
        HedgeType.CREDIT_PUT: 0.10,       # 10% OTM (HYG)
    }

    # Strike offsets for vol calls (points OTM)
    VOL_CALL_STRIKES = {
        HedgeType.EU_VOL_CALL: 5.0,       # VSTOXX: current + 5 points
        HedgeType.US_VOL_CALL: 10.0,      # VIX: current + 10 points
    }

    def __init__(
        self,
        settings: Dict[str, Any],
        instruments_config: Dict[str, Any]
    ):
        """
        Initialize tail hedge manager.

        Args:
            settings: Application settings
            instruments_config: Instrument configuration
        """
        self.settings = settings
        self.instruments = instruments_config

        # Crisis settings
        crisis_settings = settings.get('crisis', {})
        self.vix_threshold = crisis_settings.get('vix_threshold', 40)
        self.pnl_spike_threshold = crisis_settings.get('pnl_spike_threshold_pct', 0.10)
        self.crisis_redeploy_fraction = crisis_settings.get('crisis_redeploy_fraction', 0.6)

        # Hedge budget
        self.hedge_budget_annual_pct = settings.get('hedge_budget_annual_pct', 0.025)

        # Active hedges
        self.active_hedges: Dict[str, HedgePosition] = {}
        self.budget: Optional[HedgeBudget] = None

        # ROADMAP Phase D: Option validator
        validator_settings = settings.get('option_validator', {})
        validator_config = OptionValidationConfig(
            max_spread_pct_equity_puts=validator_settings.get('max_spread_pct_equity_puts', 0.08),
            max_spread_pct_vix_calls=validator_settings.get('max_spread_pct_vix_calls', 0.12),
            max_spread_pct_credit_puts=validator_settings.get('max_spread_pct_credit_puts', 0.10),
            min_volume_equity_puts=validator_settings.get('min_volume_equity_puts', 100),
            min_volume_vix_calls=validator_settings.get('min_volume_vix_calls', 50),
            min_open_interest_equity_puts=validator_settings.get('min_open_interest_equity_puts', 500),
            max_premium_per_leg_usd=validator_settings.get('max_premium_per_leg_usd', 50000),
            max_premium_pct_budget=validator_settings.get('max_premium_pct_budget', 0.25),
            min_dte=validator_settings.get('min_dte', 14),
        )
        self.option_validator = OptionValidator(validator_config)

        # Track validation stats
        self._validation_stats = {
            'total_validated': 0,
            'total_rejected': 0,
            'rejection_reasons': {}
        }

    def initialize_budget(self, nav: float, year_start_nav: Optional[float] = None) -> None:
        """Initialize or reset the hedge budget."""
        self.budget = HedgeBudget(
            annual_budget_pct=self.hedge_budget_annual_pct,
            nav_at_year_start=year_start_nav or nav
        )

    def ensure_tail_hedges(
        self,
        portfolio_state: PortfolioState,
        data_feed: DataFeed,
        today: Optional[date] = None
    ) -> List[OrderSpec]:
        """
        Ensure adequate tail hedge coverage.
        Creates new hedges or rolls expiring ones as needed.

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
        if self.budget is None:
            self.initialize_budget(portfolio_state.nav)

        # Check remaining budget
        if self.budget.remaining <= 0:
            return orders  # No budget remaining

        # Check and roll expiring hedges
        roll_orders = self._check_and_roll_hedges(data_feed, today)
        orders.extend(roll_orders)

        # Check coverage gaps and add new hedges
        gap_orders = self._fill_coverage_gaps(portfolio_state, data_feed, today)
        orders.extend(gap_orders)

        return orders

    def handle_crisis_if_any(
        self,
        portfolio_state: PortfolioState,
        data_feed: DataFeed,
        vix_level: float,
        daily_pnl: float
    ) -> Tuple[List[OrderSpec], CrisisAction]:
        """
        Check for crisis conditions and execute playbook if triggered.

        Args:
            portfolio_state: Current portfolio state
            data_feed: Data feed
            vix_level: Current VIX level
            daily_pnl: Daily P&L as decimal

        Returns:
            Tuple of (orders, crisis_action)
        """
        orders = []
        action = CrisisAction(
            action_type="none",
            orders=[],
            rebalance_instruction="hold"
        )

        # Check crisis triggers
        is_vix_crisis = vix_level >= self.vix_threshold
        is_pnl_spike = daily_pnl >= self.pnl_spike_threshold  # Large positive = hedges paying off

        if not is_vix_crisis and not is_pnl_spike:
            return orders, action

        # Crisis detected
        if is_pnl_spike:
            # Hedges are paying off - realize some profits
            realize_orders, realized_value = self._realize_itm_hedges(
                portfolio_state, data_feed, self.crisis_redeploy_fraction
            )
            orders.extend(realize_orders)

            # Update budget with realized gains
            if self.budget:
                self.budget.realized_gains_ytd += realized_value

            action = CrisisAction(
                action_type="realize_hedges",
                orders=realize_orders,
                rebalance_instruction="increase_core_exposure",
                urgency="urgent",
                reason=f"Hedge payoff detected: daily PnL {daily_pnl:.1%}"
            )

        elif is_vix_crisis:
            # VIX spike - consider adding more hedges if budget allows
            if self.budget and self.budget.remaining > 0:
                hedge_orders = self._add_crisis_hedges(portfolio_state, data_feed)
                orders.extend(hedge_orders)

            action = CrisisAction(
                action_type="increase_hedges",
                orders=orders,
                rebalance_instruction="reduce_exposure",
                urgency="immediate",
                reason=f"VIX crisis: {vix_level:.1f} >= threshold {self.vix_threshold}"
            )

        return orders, action

    def _check_and_roll_hedges(
        self,
        data_feed: DataFeed,
        today: date
    ) -> List[OrderSpec]:
        """Check for expiring hedges and roll them."""
        orders = []

        for hedge_id, hedge in list(self.active_hedges.items()):
            if not hedge.is_active:
                continue

            # Check if needs rolling
            if hedge.days_to_expiry <= self.MIN_DTE_ROLL:
                # Create close order
                close_order = OrderSpec(
                    instrument_id=hedge.instrument_id,
                    side="SELL" if hedge.quantity > 0 else "BUY",
                    quantity=abs(hedge.quantity),
                    order_type="MKT",
                    sleeve=Sleeve.CRISIS_ALPHA,
                    reason=f"Roll expiring hedge: {hedge.days_to_expiry} DTE"
                )
                orders.append(close_order)

                # Create new hedge order (further out expiry)
                new_hedge_order = self._create_replacement_hedge(hedge, data_feed, today)
                if new_hedge_order:
                    orders.append(new_hedge_order)

                # Mark old hedge as inactive
                hedge.is_active = False

        return orders

    def _fill_coverage_gaps(
        self,
        portfolio_state: PortfolioState,
        data_feed: DataFeed,
        today: date
    ) -> List[OrderSpec]:
        """Fill gaps in hedge coverage."""
        orders = []

        if self.budget is None or self.budget.remaining <= 0:
            return orders

        # Calculate current coverage by type
        coverage = {ht: 0.0 for ht in HedgeType}
        for hedge in self.active_hedges.values():
            if hedge.is_active:
                coverage[hedge.hedge_type] += hedge.premium_paid

        total_coverage = sum(coverage.values())
        target_coverage = self.budget.remaining * 0.8  # Use 80% of remaining budget

        # Fill gaps
        for hedge_type, target_alloc in self.HEDGE_ALLOCATION.items():
            current_alloc = coverage[hedge_type] / total_coverage if total_coverage > 0 else 0
            target_value = target_coverage * target_alloc

            if coverage[hedge_type] < target_value * 0.5:  # Less than 50% of target
                gap = target_value - coverage[hedge_type]
                new_orders = self._create_hedge_orders(
                    hedge_type, gap, portfolio_state, data_feed, today
                )
                orders.extend(new_orders)

        return orders

    def _create_hedge_orders(
        self,
        hedge_type: HedgeType,
        budget: float,
        portfolio_state: PortfolioState,
        data_feed: DataFeed,
        today: date
    ) -> List[OrderSpec]:
        """Create orders for a specific hedge type - EUROPE-CENTRIC."""
        orders = []

        # PRIMARY: Europe-specific hedges
        if hedge_type == HedgeType.EU_VOL_CALL:
            # VSTOXX calls - PRIMARY vol hedge for Europe insurance
            orders.extend(self._create_vstoxx_calls(budget, data_feed, today))

        elif hedge_type == HedgeType.EU_EQUITY_PUT:
            # SX5E puts - PRIMARY equity hedge for Europe
            orders.extend(self._create_sx5e_puts(budget, data_feed, today))

        elif hedge_type == HedgeType.EU_BANK_PUT:
            # EU bank puts (SX7E or bank ETF)
            orders.extend(self._create_eu_bank_puts(budget, data_feed, today))

        # SECONDARY: US hedges
        elif hedge_type == HedgeType.US_VOL_CALL:
            # VIX calls - secondary vol hedge
            orders.extend(self._create_vix_calls(budget, data_feed, today))

        elif hedge_type == HedgeType.US_EQUITY_PUT:
            # SPY puts - secondary equity hedge
            orders.extend(self._create_spy_puts(budget, data_feed, today))

        # TERTIARY: Other hedges
        elif hedge_type == HedgeType.CREDIT_PUT:
            # HYG/JNK puts
            orders.extend(self._create_credit_puts(budget, data_feed, today))

        elif hedge_type == HedgeType.SOVEREIGN_SPREAD:
            # OAT-Bund spread (short FOAT, long FGBL)
            orders.extend(self._create_sovereign_spread(budget, data_feed))

        return orders

    # =========================================================================
    # PRIMARY: Europe-Specific Hedge Methods (Strategy Evolution)
    # =========================================================================

    def _create_vstoxx_calls(
        self,
        budget: float,
        data_feed: DataFeed,
        today: date
    ) -> List[OrderSpec]:
        """
        Create VSTOXX call orders - PRIMARY vol hedge for Europe insurance.

        VSTOXX (V2X) is the Euro STOXX 50 implied volatility index.
        Buying calls here provides convexity when Europe stress rises.
        """
        orders = []

        try:
            # Get current VSTOXX level
            v2x_level = data_feed.get_v2x_level() if hasattr(data_feed, 'get_v2x_level') else 20.0

            # Target strike: current + offset (buy OTM for convexity)
            strike_offset = self.VOL_CALL_STRIKES.get(HedgeType.EU_VOL_CALL, 5.0)
            v2x_strike = round(v2x_level + strike_offset)

            # VSTOXX call premium estimation (~2-3 EUR per point for 2-month OTM)
            est_premium_per_contract = 250.0  # EUR (multiplier = 100)

            vstoxx_contracts = int(budget / est_premium_per_contract) if est_premium_per_contract > 0 else 0

            if vstoxx_contracts > 0:
                orders.append(OrderSpec(
                    instrument_id="vstoxx_call",
                    side="BUY",
                    quantity=vstoxx_contracts,
                    order_type="LMT",  # Use limit for options
                    sleeve=Sleeve.CRISIS_ALPHA,
                    reason=f"VSTOXX {v2x_strike} Call - 2M expiry (EU vol hedge)"
                ))

        except Exception:
            pass

        return orders

    def _create_sx5e_puts(
        self,
        budget: float,
        data_feed: DataFeed,
        today: date
    ) -> List[OrderSpec]:
        """
        Create SX5E (Euro STOXX 50) put orders - PRIMARY equity hedge.

        These are the core "insurance for Europeans" hedge - pay off when
        European equities decline.
        """
        orders = []

        try:
            # Get current SX5E level
            sx5e_level = data_feed.get_last_price('FESX')  # Use futures as proxy

            # Target strike: 10% OTM
            otm_pct = self.OTM_TARGETS.get(HedgeType.EU_EQUITY_PUT, 0.10)
            sx5e_strike = round(sx5e_level * (1 - otm_pct))

            # SX5E option premium estimation (~1-2% for 10% OTM 3-month)
            # Multiplier = 10, so notional = strike * 10
            est_premium_pct = 0.015
            premium_per_contract = sx5e_level * est_premium_pct * 10

            sx5e_contracts = int(budget / premium_per_contract) if premium_per_contract > 0 else 0

            if sx5e_contracts > 0:
                orders.append(OrderSpec(
                    instrument_id="sx5e_put",
                    side="BUY",
                    quantity=sx5e_contracts,
                    order_type="LMT",
                    sleeve=Sleeve.CRISIS_ALPHA,
                    reason=f"SX5E {sx5e_strike} Put - 3M expiry (EU equity hedge)"
                ))

        except Exception:
            pass

        return orders

    def _create_eu_bank_puts(
        self,
        budget: float,
        data_feed: DataFeed,
        today: date
    ) -> List[OrderSpec]:
        """
        Create EU bank sector puts - hedges European financial stress.

        Uses SX7E (Euro STOXX Banks) options or EXV1 ETF puts.
        European banks are often the epicenter of EU-specific stress.
        """
        orders = []

        try:
            # Try to get EU bank index level (SX7E or ETF proxy)
            try:
                bank_level = data_feed.get_last_price('EXV1')  # EU banks ETF
            except Exception:
                bank_level = 10.0  # Fallback

            # Target strike: 15% OTM (banks are more volatile)
            otm_pct = self.OTM_TARGETS.get(HedgeType.EU_BANK_PUT, 0.15)
            bank_strike = round(bank_level * (1 - otm_pct), 1)

            # Premium estimation (~2-3% for 15% OTM)
            est_premium_pct = 0.025
            premium_per_contract = bank_level * est_premium_pct * 100

            bank_contracts = int(budget / premium_per_contract) if premium_per_contract > 0 else 0

            if bank_contracts > 0:
                orders.append(OrderSpec(
                    instrument_id="eu_bank_put",
                    side="BUY",
                    quantity=bank_contracts,
                    order_type="LMT",
                    sleeve=Sleeve.CRISIS_ALPHA,
                    reason=f"EU Banks {bank_strike} Put - 2M expiry (financial stress hedge)"
                ))

        except Exception:
            pass

        return orders

    # =========================================================================
    # SECONDARY: US Hedge Methods
    # =========================================================================

    def _create_spy_puts(
        self,
        budget: float,
        data_feed: DataFeed,
        today: date
    ) -> List[OrderSpec]:
        """Create SPY put orders - secondary equity hedge."""
        orders = []

        try:
            spy_price = data_feed.get_last_price('SPY')
            otm_pct = self.OTM_TARGETS.get(HedgeType.US_EQUITY_PUT, 0.15)
            spy_strike = round(spy_price * (1 - otm_pct))

            # Estimate premium ~2-3% of notional for 15% OTM 3-month put
            est_premium_pct = 0.025
            spy_premium = spy_price * est_premium_pct * 100  # Per contract

            spy_contracts = int(budget / spy_premium) if spy_premium > 0 else 0

            if spy_contracts > 0:
                orders.append(OrderSpec(
                    instrument_id="spy_put",
                    side="BUY",
                    quantity=spy_contracts,
                    order_type="LMT",
                    sleeve=Sleeve.CRISIS_ALPHA,
                    reason=f"SPY {spy_strike} Put - 3M expiry (US equity hedge)"
                ))

        except Exception:
            pass

        return orders

    def _create_equity_puts(
        self,
        budget: float,
        data_feed: DataFeed,
        today: date
    ) -> List[OrderSpec]:
        """Create equity put orders - DEPRECATED, use specific methods."""
        # Split budget between EU (primary) and US (secondary)
        eu_orders = self._create_sx5e_puts(budget * 0.6, data_feed, today)
        us_orders = self._create_spy_puts(budget * 0.4, data_feed, today)
        return eu_orders + us_orders

    def _create_vix_calls(
        self,
        budget: float,
        data_feed: DataFeed,
        today: date
    ) -> List[OrderSpec]:
        """Create VIX call orders."""
        orders = []

        try:
            vix_level = data_feed.get_vix_level()

            # Target strike ~30-35 for VIX calls
            vix_strike = max(30, vix_level * 1.5)
            vix_strike = round(vix_strike)

            # VIX call premium estimation
            est_premium = 2.0 * 100  # ~$2 per contract

            vix_contracts = int(budget / est_premium) if est_premium > 0 else 0

            if vix_contracts > 0:
                orders.append(OrderSpec(
                    instrument_id="vix_call",
                    side="BUY",
                    quantity=vix_contracts,
                    order_type="MKT",
                    sleeve=Sleeve.CRISIS_ALPHA,
                    reason=f"VIX {vix_strike} Call - 2M expiry"
                ))

        except Exception:
            pass

        return orders

    def _create_credit_puts(
        self,
        budget: float,
        data_feed: DataFeed,
        today: date
    ) -> List[OrderSpec]:
        """Create credit put orders (HYG puts)."""
        orders = []

        try:
            hyg_price = data_feed.get_last_price('HYG')
            hyg_strike = hyg_price * (1 - self.OTM_TARGETS[HedgeType.CREDIT_PUT])
            hyg_strike = round(hyg_strike)

            est_premium = hyg_price * 0.02 * 100  # ~2% premium
            hyg_contracts = int(budget / est_premium) if est_premium > 0 else 0

            if hyg_contracts > 0:
                orders.append(OrderSpec(
                    instrument_id="hyg_put",
                    side="BUY",
                    quantity=hyg_contracts,
                    order_type="MKT",
                    sleeve=Sleeve.CRISIS_ALPHA,
                    reason=f"HYG {hyg_strike} Put - 3M expiry"
                ))

        except Exception:
            pass

        return orders

    def _create_sovereign_spread(
        self,
        budget: float,
        data_feed: DataFeed
    ) -> List[OrderSpec]:
        """Create OAT-Bund sovereign spread (short France, long Germany)."""
        orders = []

        # FOAT/FGBL spread - each contract ~100k EUR notional
        # Short FOAT (French OAT), Long FGBL (German Bund)

        contracts = int(budget / 5000)  # Rough margin estimate

        if contracts > 0:
            # Short FOAT (France sovereign risk)
            orders.append(OrderSpec(
                instrument_id="france_oat",
                side="SELL",
                quantity=contracts,
                order_type="MKT",
                sleeve=Sleeve.CRISIS_ALPHA,
                reason="Short France 10Y - sovereign stress hedge"
            ))

            # Long FGBL (Germany safe haven)
            orders.append(OrderSpec(
                instrument_id="germany_bund",
                side="BUY",
                quantity=contracts,
                order_type="MKT",
                sleeve=Sleeve.CRISIS_ALPHA,
                reason="Long Germany 10Y - sovereign stress hedge"
            ))

        return orders

    def _create_bank_hedges(
        self,
        budget: float,
        data_feed: DataFeed,
        today: date
    ) -> List[OrderSpec]:
        """Create French bank hedge positions."""
        orders = []

        # Use EUFN (European financials ETF) puts as proxy
        try:
            eufn_price = data_feed.get_last_price('EUFN')
            eufn_strike = eufn_price * (1 - self.OTM_TARGETS[HedgeType.BANK_PUT])
            eufn_strike = round(eufn_strike, 1)

            est_premium = eufn_price * 0.03 * 100  # ~3% premium for 20% OTM
            eufn_contracts = int(budget / est_premium) if est_premium > 0 else 0

            if eufn_contracts > 0:
                orders.append(OrderSpec(
                    instrument_id="eufn_put",
                    side="BUY",
                    quantity=eufn_contracts,
                    order_type="MKT",
                    sleeve=Sleeve.CRISIS_ALPHA,
                    reason=f"EUFN {eufn_strike} Put - EU bank stress hedge"
                ))

        except Exception:
            pass

        return orders

    def _create_replacement_hedge(
        self,
        old_hedge: HedgePosition,
        data_feed: DataFeed,
        today: date
    ) -> Optional[OrderSpec]:
        """Create a replacement hedge with further expiry."""
        # Simple replacement - same type and size
        return OrderSpec(
            instrument_id=old_hedge.instrument_id,
            side="BUY" if old_hedge.quantity > 0 else "SELL",
            quantity=abs(old_hedge.quantity),
            order_type="MKT",
            sleeve=Sleeve.CRISIS_ALPHA,
            reason=f"Roll hedge from {old_hedge.expiry}"
        )

    def _realize_itm_hedges(
        self,
        portfolio_state: PortfolioState,
        data_feed: DataFeed,
        realize_fraction: float
    ) -> Tuple[List[OrderSpec], float]:
        """
        Realize in-the-money hedges.

        Returns:
            Tuple of (orders, realized_value)
        """
        orders = []
        realized_value = 0.0

        for hedge_id, hedge in list(self.active_hedges.items()):
            if not hedge.is_active:
                continue

            # Check if hedge is profitable
            if hedge.pnl > 0:
                # Calculate contracts to close
                contracts_to_close = int(abs(hedge.quantity) * realize_fraction)

                if contracts_to_close > 0:
                    order = OrderSpec(
                        instrument_id=hedge.instrument_id,
                        side="SELL" if hedge.quantity > 0 else "BUY",
                        quantity=contracts_to_close,
                        order_type="MKT",
                        sleeve=Sleeve.CRISIS_ALPHA,
                        urgency="urgent",
                        reason=f"Realize ITM hedge: PnL {hedge.pnl:.2f}"
                    )
                    orders.append(order)

                    # Estimate realized value
                    realized_value += (hedge.pnl / abs(hedge.quantity)) * contracts_to_close

                    # Update hedge quantity
                    hedge.quantity -= contracts_to_close if hedge.quantity > 0 else -contracts_to_close
                    if hedge.quantity == 0:
                        hedge.is_active = False

        return orders, realized_value

    def _add_crisis_hedges(
        self,
        portfolio_state: PortfolioState,
        data_feed: DataFeed
    ) -> List[OrderSpec]:
        """Add additional hedges during crisis - EUROPE-CENTRIC."""
        orders = []

        if self.budget is None:
            return orders

        # Use up to 50% of remaining budget for crisis hedges
        crisis_budget = self.budget.remaining * 0.5

        # STRATEGY EVOLUTION: Focus on VSTOXX and SX5E during crisis
        # Europe vol is the primary insurance channel
        vstoxx_orders = self._create_vstoxx_calls(crisis_budget * 0.40, data_feed, date.today())
        sx5e_orders = self._create_sx5e_puts(crisis_budget * 0.30, data_feed, date.today())
        # VIX as secondary
        vix_orders = self._create_vix_calls(crisis_budget * 0.20, data_feed, date.today())
        # EU banks for financial stress
        bank_orders = self._create_eu_bank_puts(crisis_budget * 0.10, data_feed, date.today())

        orders.extend(vstoxx_orders)
        orders.extend(sx5e_orders)
        orders.extend(vix_orders)
        orders.extend(bank_orders)

        return orders

    def validate_option_order(
        self,
        quote: OptionQuote,
        hedge_type: HedgeType,
        quantity: int = 1
    ) -> OptionValidationResult:
        """
        Validate an option order before submission.

        ROADMAP Phase D: Uses OptionValidator to check liquidity,
        spreads, and budget constraints.

        Args:
            quote: Option quote data
            hedge_type: Type of hedge
            quantity: Number of contracts

        Returns:
            OptionValidationResult with pass/fail and details
        """
        # Map hedge type to validator hedge type string
        hedge_type_map = {
            # Europe-centric (primary)
            HedgeType.EU_VOL_CALL: "vix_call",       # Use same thresholds as VIX
            HedgeType.EU_EQUITY_PUT: "equity_put",
            HedgeType.EU_BANK_PUT: "equity_put",
            # US (secondary)
            HedgeType.US_VOL_CALL: "vix_call",
            HedgeType.US_EQUITY_PUT: "equity_put",
            # Other
            HedgeType.CREDIT_PUT: "credit_put",
            HedgeType.SOVEREIGN_SPREAD: "equity_put",  # Not an option, but same thresholds
        }
        validator_type = hedge_type_map.get(hedge_type, "equity_put")

        # Get remaining budget
        budget_remaining = self.budget.remaining if self.budget else 0

        # Validate
        result = self.option_validator.validate(
            quote=quote,
            hedge_type=validator_type,
            budget_remaining=budget_remaining,
            quantity=quantity
        )

        # Track stats
        self._validation_stats['total_validated'] += 1
        if not result.is_valid:
            self._validation_stats['total_rejected'] += 1
            for failure in result.failures:
                reason = failure.value
                self._validation_stats['rejection_reasons'][reason] = \
                    self._validation_stats['rejection_reasons'].get(reason, 0) + 1

        return result

    def get_validation_stats(self) -> Dict[str, Any]:
        """Get option validation statistics."""
        return {
            **self._validation_stats,
            'validator_metrics': self.option_validator.get_metrics()
        }

    def reset_validation_stats(self) -> None:
        """Reset validation statistics (e.g., daily)."""
        self._validation_stats = {
            'total_validated': 0,
            'total_rejected': 0,
            'rejection_reasons': {}
        }
        self.option_validator.reset_metrics()

    def get_hedge_summary(self) -> Dict[str, Any]:
        """Get summary of current hedge positions."""
        summary = {
            "total_hedges": len([h for h in self.active_hedges.values() if h.is_active]),
            "total_premium_paid": sum(h.premium_paid for h in self.active_hedges.values() if h.is_active),
            "total_current_value": sum(h.current_value for h in self.active_hedges.values() if h.is_active),
            "total_pnl": sum(h.pnl for h in self.active_hedges.values() if h.is_active),
            "by_type": {},
            "budget": None,
            "validation_stats": self._validation_stats  # Phase D
        }

        # Group by type
        for hedge_type in HedgeType:
            type_hedges = [h for h in self.active_hedges.values()
                         if h.is_active and h.hedge_type == hedge_type]
            summary["by_type"][hedge_type.value] = {
                "count": len(type_hedges),
                "premium": sum(h.premium_paid for h in type_hedges),
                "value": sum(h.current_value for h in type_hedges),
                "pnl": sum(h.pnl for h in type_hedges)
            }

        # Budget info
        if self.budget:
            summary["budget"] = {
                "annual_budget": self.budget.total_budget,
                "used_ytd": self.budget.used_ytd,
                "remaining": self.budget.remaining,
                "usage_pct": self.budget.usage_pct
            }

        return summary
