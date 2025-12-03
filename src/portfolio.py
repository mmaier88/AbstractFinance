"""
Multi-sleeve portfolio state management for AbstractFinance.
Tracks positions, NAV, P&L, exposures, and sleeve allocations.
"""

import pandas as pd
import numpy as np
from datetime import datetime, date
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import json
from pathlib import Path

from .data_feeds import DataFeed


class Sleeve(Enum):
    """Portfolio sleeve types."""
    CORE_INDEX_RV = "core_index_rv"
    SECTOR_RV = "sector_rv"
    SINGLE_NAME = "single_name"
    CREDIT_CARRY = "credit_carry"
    CRISIS_ALPHA = "crisis_alpha"
    CASH_BUFFER = "cash_buffer"


@dataclass
class Position:
    """Represents a single position in the portfolio."""
    instrument_id: str
    quantity: float
    avg_cost: float
    market_price: float = 0.0
    multiplier: float = 1.0
    currency: str = "USD"
    sleeve: Sleeve = Sleeve.CORE_INDEX_RV

    @property
    def market_value(self) -> float:
        """Calculate market value of position."""
        return self.quantity * self.market_price * self.multiplier

    @property
    def cost_basis(self) -> float:
        """Calculate cost basis of position."""
        return self.quantity * self.avg_cost * self.multiplier

    @property
    def unrealized_pnl(self) -> float:
        """Calculate unrealized P&L."""
        return self.market_value - self.cost_basis

    @property
    def unrealized_pnl_pct(self) -> float:
        """Calculate unrealized P&L percentage."""
        if self.cost_basis == 0:
            return 0.0
        return self.unrealized_pnl / abs(self.cost_basis)


@dataclass
class SleeveAllocation:
    """Allocation and performance for a single sleeve."""
    sleeve: Sleeve
    target_weight: float
    current_weight: float = 0.0
    gross_exposure: float = 0.0
    net_exposure: float = 0.0
    pnl_today: float = 0.0
    pnl_mtd: float = 0.0
    pnl_ytd: float = 0.0
    positions: List[str] = field(default_factory=list)


@dataclass
class PortfolioState:
    """
    Complete portfolio state including all sleeves.
    This is the primary data structure for portfolio management.
    """
    # Core metrics
    nav: float = 0.0
    cash: float = 0.0
    initial_capital: float = 0.0

    # Position tracking
    positions: Dict[str, Position] = field(default_factory=dict)

    # Sleeve allocations
    sleeve_allocations: Dict[Sleeve, SleeveAllocation] = field(default_factory=dict)

    # Exposure metrics
    gross_exposure: float = 0.0
    net_exposure: float = 0.0
    long_exposure: float = 0.0
    short_exposure: float = 0.0

    # Risk metrics
    realized_vol_annual: float = 0.0
    max_drawdown: float = 0.0
    current_drawdown: float = 0.0

    # P&L
    daily_pnl: float = 0.0
    daily_return: float = 0.0
    mtd_pnl: float = 0.0
    ytd_pnl: float = 0.0
    total_pnl: float = 0.0

    # Tail hedge tracking
    hedge_budget_used_ytd: float = 0.0
    hedge_budget_annual: float = 0.0

    # History
    pnl_history: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    nav_history: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))

    # Timestamps
    last_update: Optional[datetime] = None
    inception_date: Optional[date] = None

    def __post_init__(self):
        """Initialize sleeve allocations if empty."""
        if not self.sleeve_allocations:
            for sleeve in Sleeve:
                self.sleeve_allocations[sleeve] = SleeveAllocation(
                    sleeve=sleeve,
                    target_weight=0.0
                )

    def update_from_ib_positions(
        self,
        ib_positions: List[Any],
        instruments_map: Dict[str, Dict],
        sleeve_assignments: Optional[Dict[str, Sleeve]] = None
    ) -> None:
        """
        Update portfolio state from IB positions.

        Args:
            ib_positions: List of IB position objects
            instruments_map: Mapping of symbols to instrument specs
            sleeve_assignments: Manual sleeve assignments for positions
        """
        sleeve_assignments = sleeve_assignments or {}
        new_positions = {}

        for ib_pos in ib_positions:
            symbol = ib_pos.contract.symbol
            instrument_id = self._map_ib_to_instrument_id(symbol, instruments_map)

            if instrument_id:
                spec = instruments_map.get(instrument_id, {})
                sleeve = sleeve_assignments.get(instrument_id, self._infer_sleeve(instrument_id))

                position = Position(
                    instrument_id=instrument_id,
                    quantity=ib_pos.position,
                    avg_cost=ib_pos.avgCost,
                    market_price=ib_pos.marketPrice if hasattr(ib_pos, 'marketPrice') else ib_pos.avgCost,
                    multiplier=spec.get('multiplier', 1.0),
                    currency=spec.get('currency', 'USD'),
                    sleeve=sleeve
                )
                new_positions[instrument_id] = position

        self.positions = new_positions
        self.last_update = datetime.now()

    def _map_ib_to_instrument_id(
        self,
        symbol: str,
        instruments_map: Dict
    ) -> Optional[str]:
        """Map IB symbol to internal instrument ID."""
        for inst_id, spec in instruments_map.items():
            if spec.get('symbol') == symbol:
                return inst_id
        return None

    def _infer_sleeve(self, instrument_id: str) -> Sleeve:
        """Infer sleeve assignment from instrument ID."""
        inst_lower = instrument_id.lower()

        # Core index instruments
        if any(x in inst_lower for x in ['es', 'spy', 'fesx', 'fez', '6e', 'eurusd']):
            return Sleeve.CORE_INDEX_RV

        # Sector ETFs
        if any(x in inst_lower for x in ['xlk', 'qqq', 'xlv', 'xbi', 'eufn', 'qual', 'mtum']):
            return Sleeve.SECTOR_RV

        # Credit instruments
        if any(x in inst_lower for x in ['lqd', 'hyg', 'jnk', 'bkln', 'arcc', 'ieac', 'ihyg']):
            return Sleeve.CREDIT_CARRY

        # Crisis/hedge instruments
        if any(x in inst_lower for x in ['vix', 'put', 'foat', 'fgbl', 'bnp', 'gle']):
            return Sleeve.CRISIS_ALPHA

        # Single stocks
        if any(x in inst_lower for x in ['.de', '.pa', 'aapl', 'msft', 'googl']):
            return Sleeve.SINGLE_NAME

        return Sleeve.CORE_INDEX_RV

    def compute_nav(self, data_feed: DataFeed) -> float:
        """
        Compute current NAV from positions and cash.

        Args:
            data_feed: Data feed for current prices

        Returns:
            Current NAV
        """
        total_market_value = self.cash

        for inst_id, position in self.positions.items():
            try:
                current_price = data_feed.get_last_price(inst_id)
                position.market_price = current_price
                total_market_value += position.market_value
            except Exception:
                # Use last known price
                total_market_value += position.market_value

        self.nav = total_market_value
        self.last_update = datetime.now()
        return self.nav

    def compute_exposures(
        self,
        data_feed: Optional[DataFeed] = None,
        beta_estimates: Optional[Dict[str, float]] = None
    ) -> Tuple[float, float]:
        """
        Compute gross and net exposures.

        Args:
            data_feed: Data feed for price updates
            beta_estimates: Beta estimates for each instrument

        Returns:
            Tuple of (gross_exposure, net_exposure)
        """
        beta_estimates = beta_estimates or {}
        long_exp = 0.0
        short_exp = 0.0

        for inst_id, position in self.positions.items():
            beta = beta_estimates.get(inst_id, 1.0)
            beta_adjusted_value = position.market_value * beta

            if position.quantity > 0:
                long_exp += abs(beta_adjusted_value)
            else:
                short_exp += abs(beta_adjusted_value)

        self.long_exposure = long_exp
        self.short_exposure = short_exp
        self.gross_exposure = long_exp + short_exp
        self.net_exposure = long_exp - short_exp

        return self.gross_exposure, self.net_exposure

    def compute_sleeve_exposures(self) -> Dict[Sleeve, Tuple[float, float]]:
        """
        Compute gross and net exposures per sleeve.

        Returns:
            Dict mapping sleeve to (gross, net) exposure tuple
        """
        sleeve_exposures = {sleeve: [0.0, 0.0] for sleeve in Sleeve}  # [long, short]

        for inst_id, position in self.positions.items():
            value = position.market_value
            if position.quantity > 0:
                sleeve_exposures[position.sleeve][0] += abs(value)
            else:
                sleeve_exposures[position.sleeve][1] += abs(value)

        result = {}
        for sleeve, (long_exp, short_exp) in sleeve_exposures.items():
            gross = long_exp + short_exp
            net = long_exp - short_exp
            result[sleeve] = (gross, net)

            # Update sleeve allocation
            if sleeve in self.sleeve_allocations:
                self.sleeve_allocations[sleeve].gross_exposure = gross
                self.sleeve_allocations[sleeve].net_exposure = net
                if self.nav > 0:
                    self.sleeve_allocations[sleeve].current_weight = gross / self.nav

        return result

    def record_daily_pnl(self, daily_return: float, today: Optional[date] = None) -> None:
        """
        Record daily P&L and update history.

        Args:
            daily_return: Daily return as decimal (e.g., 0.01 for 1%)
            today: Date to record (defaults to today)
        """
        today = today or date.today()
        today_datetime = pd.Timestamp(today)

        # Update P&L
        self.daily_return = daily_return
        self.daily_pnl = self.nav * daily_return

        # Update history
        self.pnl_history[today_datetime] = daily_return
        self.nav_history[today_datetime] = self.nav

        # Update cumulative P&L
        self.total_pnl = self.nav - self.initial_capital
        if self.initial_capital > 0:
            self.ytd_pnl = self._compute_ytd_pnl()
            self.mtd_pnl = self._compute_mtd_pnl()

        # Update drawdown
        self._update_drawdown()

    def _compute_ytd_pnl(self) -> float:
        """Compute year-to-date P&L."""
        current_year = date.today().year
        ytd_returns = self.pnl_history[
            self.pnl_history.index >= pd.Timestamp(f"{current_year}-01-01")
        ]
        return (1 + ytd_returns).prod() - 1

    def _compute_mtd_pnl(self) -> float:
        """Compute month-to-date P&L."""
        today = date.today()
        mtd_start = pd.Timestamp(f"{today.year}-{today.month:02d}-01")
        mtd_returns = self.pnl_history[self.pnl_history.index >= mtd_start]
        return (1 + mtd_returns).prod() - 1

    def _update_drawdown(self) -> None:
        """Update current and max drawdown from NAV history."""
        if self.nav_history.empty:
            return

        # Compute equity curve
        equity_curve = self.nav_history

        # Rolling max
        rolling_max = equity_curve.cummax()

        # Drawdown series
        drawdown = (equity_curve - rolling_max) / rolling_max

        self.current_drawdown = drawdown.iloc[-1] if len(drawdown) > 0 else 0.0
        self.max_drawdown = drawdown.min() if len(drawdown) > 0 else 0.0

    def get_sleeve_weights(self) -> Dict[str, float]:
        """
        Get current sleeve weights as fraction of NAV.

        Returns:
            Dict mapping sleeve name to weight
        """
        weights = {}
        for sleeve, alloc in self.sleeve_allocations.items():
            weights[sleeve.value] = alloc.current_weight
        return weights

    def set_target_sleeve_weights(self, weights: Dict[str, float]) -> None:
        """
        Set target sleeve weights.

        Args:
            weights: Dict mapping sleeve name to target weight
        """
        for sleeve_name, weight in weights.items():
            try:
                sleeve = Sleeve(sleeve_name)
                if sleeve in self.sleeve_allocations:
                    self.sleeve_allocations[sleeve].target_weight = weight
            except ValueError:
                continue

    def get_positions_by_sleeve(self, sleeve: Sleeve) -> List[Position]:
        """
        Get all positions for a specific sleeve.

        Args:
            sleeve: Sleeve enum value

        Returns:
            List of positions in the sleeve
        """
        return [p for p in self.positions.values() if p.sleeve == sleeve]

    def allocate_pnl_to_sleeves(
        self,
        previous_prices: Dict[str, float],
        current_prices: Dict[str, float]
    ) -> Dict[Sleeve, float]:
        """
        Allocate P&L to sleeves based on position changes.

        Args:
            previous_prices: Previous day's prices
            current_prices: Current prices

        Returns:
            Dict mapping sleeve to P&L
        """
        sleeve_pnl = {sleeve: 0.0 for sleeve in Sleeve}

        for inst_id, position in self.positions.items():
            prev_price = previous_prices.get(inst_id, position.avg_cost)
            curr_price = current_prices.get(inst_id, position.market_price)

            pnl = position.quantity * (curr_price - prev_price) * position.multiplier
            sleeve_pnl[position.sleeve] += pnl

        # Update sleeve allocations
        for sleeve, pnl in sleeve_pnl.items():
            if sleeve in self.sleeve_allocations:
                self.sleeve_allocations[sleeve].pnl_today = pnl

        return sleeve_pnl

    def to_dict(self) -> Dict[str, Any]:
        """Convert portfolio state to dictionary for serialization."""
        return {
            "nav": self.nav,
            "cash": self.cash,
            "initial_capital": self.initial_capital,
            "gross_exposure": self.gross_exposure,
            "net_exposure": self.net_exposure,
            "long_exposure": self.long_exposure,
            "short_exposure": self.short_exposure,
            "realized_vol_annual": self.realized_vol_annual,
            "max_drawdown": self.max_drawdown,
            "current_drawdown": self.current_drawdown,
            "daily_pnl": self.daily_pnl,
            "daily_return": self.daily_return,
            "ytd_pnl": self.ytd_pnl,
            "total_pnl": self.total_pnl,
            "hedge_budget_used_ytd": self.hedge_budget_used_ytd,
            "hedge_budget_annual": self.hedge_budget_annual,
            "last_update": self.last_update.isoformat() if self.last_update else None,
            "inception_date": self.inception_date.isoformat() if self.inception_date else None,
            "positions": {
                inst_id: {
                    "quantity": pos.quantity,
                    "avg_cost": pos.avg_cost,
                    "market_price": pos.market_price,
                    "multiplier": pos.multiplier,
                    "currency": pos.currency,
                    "sleeve": pos.sleeve.value
                }
                for inst_id, pos in self.positions.items()
            },
            "sleeve_weights": self.get_sleeve_weights(),
            "pnl_history": self.pnl_history.to_dict() if not self.pnl_history.empty else {},
            "nav_history": self.nav_history.to_dict() if not self.nav_history.empty else {}
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PortfolioState":
        """Create portfolio state from dictionary."""
        state = cls(
            nav=data.get("nav", 0.0),
            cash=data.get("cash", 0.0),
            initial_capital=data.get("initial_capital", 0.0),
            gross_exposure=data.get("gross_exposure", 0.0),
            net_exposure=data.get("net_exposure", 0.0),
            long_exposure=data.get("long_exposure", 0.0),
            short_exposure=data.get("short_exposure", 0.0),
            realized_vol_annual=data.get("realized_vol_annual", 0.0),
            max_drawdown=data.get("max_drawdown", 0.0),
            current_drawdown=data.get("current_drawdown", 0.0),
            daily_pnl=data.get("daily_pnl", 0.0),
            daily_return=data.get("daily_return", 0.0),
            ytd_pnl=data.get("ytd_pnl", 0.0),
            total_pnl=data.get("total_pnl", 0.0),
            hedge_budget_used_ytd=data.get("hedge_budget_used_ytd", 0.0),
            hedge_budget_annual=data.get("hedge_budget_annual", 0.0)
        )

        # Parse timestamps
        if data.get("last_update"):
            state.last_update = datetime.fromisoformat(data["last_update"])
        if data.get("inception_date"):
            state.inception_date = date.fromisoformat(data["inception_date"])

        # Parse positions
        for inst_id, pos_data in data.get("positions", {}).items():
            state.positions[inst_id] = Position(
                instrument_id=inst_id,
                quantity=pos_data["quantity"],
                avg_cost=pos_data["avg_cost"],
                market_price=pos_data.get("market_price", pos_data["avg_cost"]),
                multiplier=pos_data.get("multiplier", 1.0),
                currency=pos_data.get("currency", "USD"),
                sleeve=Sleeve(pos_data.get("sleeve", "core_index_rv"))
            )

        # Parse history
        if data.get("pnl_history"):
            state.pnl_history = pd.Series(data["pnl_history"])
            state.pnl_history.index = pd.to_datetime(state.pnl_history.index)
        if data.get("nav_history"):
            state.nav_history = pd.Series(data["nav_history"])
            state.nav_history.index = pd.to_datetime(state.nav_history.index)

        return state


def save_portfolio_state(state: PortfolioState, filepath: str = "state/portfolio_state.json") -> None:
    """Save portfolio state to JSON file."""
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, 'w') as f:
        json.dump(state.to_dict(), f, indent=2, default=str)


def load_portfolio_state(filepath: str = "state/portfolio_state.json") -> Optional[PortfolioState]:
    """Load portfolio state from JSON file."""
    path = Path(filepath)

    if not path.exists():
        return None

    with open(path, 'r') as f:
        data = json.load(f)

    return PortfolioState.from_dict(data)


def save_returns_history(returns: pd.Series, filepath: str = "state/returns_history.csv") -> None:
    """Save returns history to CSV file."""
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    returns.to_csv(path, header=True)


def load_returns_history(filepath: str = "state/returns_history.csv") -> pd.Series:
    """Load returns history from CSV file."""
    path = Path(filepath)

    if not path.exists():
        return pd.Series(dtype=float)

    df = pd.read_csv(path, index_col=0, parse_dates=True)
    return df.iloc[:, 0] if len(df.columns) > 0 else pd.Series(dtype=float)
