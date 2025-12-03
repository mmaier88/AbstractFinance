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


class HedgeType(Enum):
    """Types of tail hedges."""
    EQUITY_PUT = "equity_put"          # SPX/SPY/SX5E puts
    VOL_CALL = "vol_call"              # VIX calls
    CREDIT_PUT = "credit_put"          # HYG/JNK puts
    SOVEREIGN_SPREAD = "sovereign"      # OAT-Bund spread
    BANK_PUT = "bank_put"              # French bank puts
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
    HEDGE_ALLOCATION = {
        HedgeType.EQUITY_PUT: 0.40,      # 40% to equity puts
        HedgeType.VOL_CALL: 0.20,         # 20% to VIX calls
        HedgeType.CREDIT_PUT: 0.15,       # 15% to credit puts
        HedgeType.SOVEREIGN_SPREAD: 0.15, # 15% to sovereign
        HedgeType.BANK_PUT: 0.10          # 10% to bank puts
    }

    # Minimum days to expiry before rolling
    MIN_DTE_ROLL = 21

    # OTM targets for puts
    OTM_TARGETS = {
        HedgeType.EQUITY_PUT: 0.15,   # 15% OTM
        HedgeType.CREDIT_PUT: 0.10,   # 10% OTM
        HedgeType.BANK_PUT: 0.20      # 20% OTM
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
        """Create orders for a specific hedge type."""
        orders = []

        if hedge_type == HedgeType.EQUITY_PUT:
            # SPY puts - 3-6 month, 15% OTM
            orders.extend(self._create_equity_puts(budget, data_feed, today))

        elif hedge_type == HedgeType.VOL_CALL:
            # VIX calls - 1-3 month OTM
            orders.extend(self._create_vix_calls(budget, data_feed, today))

        elif hedge_type == HedgeType.CREDIT_PUT:
            # HYG/JNK puts
            orders.extend(self._create_credit_puts(budget, data_feed, today))

        elif hedge_type == HedgeType.SOVEREIGN_SPREAD:
            # OAT-Bund spread (short FOAT, long FGBL)
            orders.extend(self._create_sovereign_spread(budget, data_feed))

        elif hedge_type == HedgeType.BANK_PUT:
            # French bank puts or short exposure
            orders.extend(self._create_bank_hedges(budget, data_feed, today))

        return orders

    def _create_equity_puts(
        self,
        budget: float,
        data_feed: DataFeed,
        today: date
    ) -> List[OrderSpec]:
        """Create equity put orders (SPY and FEZ)."""
        orders = []

        try:
            # SPY put - 60% of equity put budget
            spy_price = data_feed.get_last_price('SPY')
            spy_strike = spy_price * (1 - self.OTM_TARGETS[HedgeType.EQUITY_PUT])
            spy_strike = round(spy_strike)  # Round to whole dollar

            # Estimate premium ~2-4% of notional for 15% OTM 3-month put
            est_premium_pct = 0.025
            spy_premium = spy_price * est_premium_pct * 100  # Per contract

            spy_contracts = int((budget * 0.6) / spy_premium) if spy_premium > 0 else 0

            if spy_contracts > 0:
                orders.append(OrderSpec(
                    instrument_id="spy_put",
                    side="BUY",
                    quantity=spy_contracts,
                    order_type="MKT",
                    sleeve=Sleeve.CRISIS_ALPHA,
                    reason=f"SPY {spy_strike} Put - 3M expiry"
                ))

            # FEZ put - 40% of equity put budget
            fez_price = data_feed.get_last_price('FEZ')
            fez_strike = fez_price * (1 - self.OTM_TARGETS[HedgeType.EQUITY_PUT])
            fez_strike = round(fez_strike, 1)

            fez_premium = fez_price * est_premium_pct * 100
            fez_contracts = int((budget * 0.4) / fez_premium) if fez_premium > 0 else 0

            if fez_contracts > 0:
                orders.append(OrderSpec(
                    instrument_id="fez_put",
                    side="BUY",
                    quantity=fez_contracts,
                    order_type="MKT",
                    sleeve=Sleeve.CRISIS_ALPHA,
                    reason=f"FEZ {fez_strike} Put - 3M expiry"
                ))

        except Exception:
            pass

        return orders

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
        """Add additional hedges during crisis."""
        orders = []

        if self.budget is None:
            return orders

        # Use up to 50% of remaining budget for crisis hedges
        crisis_budget = self.budget.remaining * 0.5

        # Focus on VIX calls and equity puts during crisis
        vix_orders = self._create_vix_calls(crisis_budget * 0.6, data_feed, date.today())
        put_orders = self._create_equity_puts(crisis_budget * 0.4, data_feed, date.today())

        orders.extend(vix_orders)
        orders.extend(put_orders)

        return orders

    def get_hedge_summary(self) -> Dict[str, Any]:
        """Get summary of current hedge positions."""
        summary = {
            "total_hedges": len([h for h in self.active_hedges.values() if h.is_active]),
            "total_premium_paid": sum(h.premium_paid for h in self.active_hedges.values() if h.is_active),
            "total_current_value": sum(h.current_value for h in self.active_hedges.values() if h.is_active),
            "total_pnl": sum(h.pnl for h in self.active_hedges.values() if h.is_active),
            "by_type": {},
            "budget": None
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
