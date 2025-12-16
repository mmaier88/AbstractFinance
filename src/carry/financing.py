"""
Financing Service - Cash carry and financing cost estimation.

Phase 2 Enhancement: Estimates daily financing costs/income
based on cash balances by currency.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, date
from typing import Dict, Optional, Any, List

logger = logging.getLogger(__name__)


@dataclass
class FinancingConfig:
    """Configuration for financing service."""
    enabled: bool = True
    default_cash_rate_by_ccy: Dict[str, float] = None

    def __post_init__(self):
        if self.default_cash_rate_by_ccy is None:
            self.default_cash_rate_by_ccy = {
                "USD": 0.045,   # 4.5%
                "EUR": 0.030,   # 3.0%
                "GBP": 0.045,   # 4.5%
                "CHF": 0.015,   # 1.5%
                "JPY": 0.001,   # 0.1%
            }


@dataclass
class CarryEstimate:
    """Estimated carry for a day or period."""
    date: date
    total_carry_usd: float = 0.0
    by_currency: Dict[str, float] = None
    borrow_cost_usd: float = 0.0
    dividend_exposure_usd: float = 0.0
    net_carry_usd: float = 0.0

    def __post_init__(self):
        if self.by_currency is None:
            self.by_currency = {}


@dataclass
class CurrencyBalance:
    """Cash balance in a currency."""
    currency: str
    balance: float
    rate: float  # Interest rate
    daily_carry: float = 0.0


class FinancingService:
    """
    Service for estimating financing costs and carry.

    Calculates:
    - Interest earned/paid on cash balances
    - Estimated borrow costs
    - Dividend exposure
    - Net carry P&L

    Usage:
        service = FinancingService(config)
        estimate = service.calculate_daily_carry(cash_balances, short_notional)
    """

    def __init__(
        self,
        config: Optional[FinancingConfig] = None,
        borrow_service: Optional[Any] = None,
    ):
        """
        Initialize financing service.

        Args:
            config: Financing configuration
            borrow_service: Optional BorrowService for borrow costs
        """
        self.config = config or FinancingConfig()
        self.borrow_service = borrow_service

        # Daily estimates
        self.daily_estimates: List[CarryEstimate] = []

    def calculate_daily_carry(
        self,
        cash_balances: Dict[str, float],
        short_positions: Optional[Dict[str, float]] = None,
        for_date: Optional[date] = None,
    ) -> CarryEstimate:
        """
        Calculate estimated daily carry.

        Args:
            cash_balances: Dict of currency -> balance (positive = long cash)
            short_positions: Dict of instrument_id -> notional_usd
            for_date: Date to calculate for (default: today)

        Returns:
            CarryEstimate with breakdown
        """
        if not self.config.enabled:
            return CarryEstimate(date=for_date or date.today())

        target_date = for_date or date.today()
        short_positions = short_positions or {}

        # Calculate cash carry by currency
        by_currency = {}
        total_carry = 0.0

        for ccy, balance in cash_balances.items():
            rate = self.config.default_cash_rate_by_ccy.get(ccy, 0.03)  # Default 3%
            daily_rate = rate / 365.0

            # Positive balance = earn interest, negative = pay interest
            daily_carry = balance * daily_rate
            by_currency[ccy] = daily_carry
            total_carry += daily_carry

        # Estimate borrow costs for short positions
        borrow_cost = 0.0
        if short_positions and self.borrow_service:
            for inst_id, notional in short_positions.items():
                if notional < 0:  # Short position
                    daily_bps = self.borrow_service.get_daily_borrow_cost_bps(inst_id)
                    cost = abs(notional) * daily_bps / 10000.0
                    borrow_cost += cost

        # Net carry
        net_carry = total_carry - borrow_cost

        estimate = CarryEstimate(
            date=target_date,
            total_carry_usd=total_carry,
            by_currency=by_currency,
            borrow_cost_usd=borrow_cost,
            net_carry_usd=net_carry,
        )

        self.daily_estimates.append(estimate)
        return estimate

    def estimate_period_carry(
        self,
        cash_balances: Dict[str, float],
        days: int = 30,
    ) -> Dict[str, float]:
        """
        Estimate carry over a period.

        Args:
            cash_balances: Current cash balances
            days: Number of days to estimate

        Returns:
            Dict with period estimates
        """
        total_carry = 0.0
        by_currency = {}

        for ccy, balance in cash_balances.items():
            rate = self.config.default_cash_rate_by_ccy.get(ccy, 0.03)
            period_carry = balance * rate * (days / 365.0)
            by_currency[ccy] = period_carry
            total_carry += period_carry

        return {
            "period_days": days,
            "estimated_carry_usd": total_carry,
            "by_currency": by_currency,
            "annualized_rate": total_carry / sum(cash_balances.values()) * (365 / days)
            if sum(cash_balances.values()) != 0 else 0,
        }

    def get_currency_rate(
        self,
        currency: str,
    ) -> float:
        """Get interest rate for a currency."""
        return self.config.default_cash_rate_by_ccy.get(currency, 0.03)

    def calculate_holding_cost(
        self,
        instrument_id: str,
        notional_usd: float,
        side: str,
        holding_days: int = 1,
    ) -> float:
        """
        Calculate holding cost for a position.

        Args:
            instrument_id: Instrument identifier
            notional_usd: Position notional
            side: "BUY" or "SELL"
            holding_days: Number of days to hold

        Returns:
            Estimated holding cost in USD
        """
        if side == "BUY":
            # Long position: opportunity cost of cash
            rate = self.config.default_cash_rate_by_ccy.get("USD", 0.045)
            return notional_usd * rate * (holding_days / 365.0)
        else:
            # Short position: borrow cost
            if self.borrow_service:
                daily_bps = self.borrow_service.get_daily_borrow_cost_bps(instrument_id)
            else:
                daily_bps = 150.0 / 365.0  # Default 1.5% annual

            return abs(notional_usd) * daily_bps / 10000.0 * holding_days

    def get_daily_summary(self) -> Dict[str, Any]:
        """Get summary of today's financing."""
        today = date.today()
        today_estimates = [e for e in self.daily_estimates if e.date == today]

        if not today_estimates:
            return {
                "date": today.isoformat(),
                "has_data": False,
            }

        latest = today_estimates[-1]
        return {
            "date": today.isoformat(),
            "has_data": True,
            "total_carry_usd": latest.total_carry_usd,
            "borrow_cost_usd": latest.borrow_cost_usd,
            "net_carry_usd": latest.net_carry_usd,
            "by_currency": latest.by_currency,
        }

    def get_telegram_summary(self) -> str:
        """Generate Telegram-formatted financing summary."""
        summary = self.get_daily_summary()

        if not summary.get("has_data"):
            return "No financing data for today"

        lines = [
            f"*Financing Summary* - {summary['date']}",
            "",
            f"Cash carry: ${summary['total_carry_usd']:.2f}",
            f"Borrow cost: ${summary['borrow_cost_usd']:.2f}",
            f"*Net carry: ${summary['net_carry_usd']:.2f}*",
        ]

        if summary.get("by_currency"):
            lines.append("")
            lines.append("By currency:")
            for ccy, carry in summary["by_currency"].items():
                lines.append(f"  {ccy}: ${carry:.2f}")

        return "\n".join(lines)

    def reset_daily(self) -> None:
        """Reset daily tracking (keep last 30 days)."""
        cutoff = date.today()
        self.daily_estimates = [
            e for e in self.daily_estimates
            if (cutoff - e.date).days <= 30
        ]


# Singleton instance
_financing_service: Optional[FinancingService] = None


def get_financing_service() -> FinancingService:
    """Get singleton FinancingService instance."""
    global _financing_service
    if _financing_service is None:
        _financing_service = FinancingService()
    return _financing_service


def init_financing_service(
    config: Optional[FinancingConfig] = None,
    borrow_service: Optional[Any] = None,
) -> FinancingService:
    """Initialize the financing service singleton."""
    global _financing_service
    _financing_service = FinancingService(
        config=config,
        borrow_service=borrow_service,
    )
    return _financing_service
