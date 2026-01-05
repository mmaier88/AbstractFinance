"""
Portfolio Attribution Engine for AbstractFinance.

v2.4: Provides daily sleeve-level P&L attribution and factor exposure reporting.

Key Features:
- Sleeve-level P&L breakdown
- Factor exposure calculation (beta, duration, credit, FX)
- Hedge effectiveness tracking
- Telegram-formatted reporting
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Dict, List, Optional, Any, Tuple

from .portfolio import PortfolioState, Position, Sleeve

logger = logging.getLogger(__name__)


@dataclass
class SleeveAttribution:
    """Attribution for a single sleeve."""
    sleeve: Sleeve
    pnl_usd: float
    pnl_pct: float
    gross_exposure: float
    net_exposure: float
    position_count: int


@dataclass
class FactorExposure:
    """Portfolio factor exposures."""
    equity_beta: float
    duration_years: float
    credit_spread_sensitivity: float
    fx_exposure_usd: float
    fx_exposure_pct_nav: float


@dataclass
class HedgeEffectiveness:
    """Hedge effectiveness metrics."""
    vol_hedge_pnl: float
    core_drawdown: float
    offset_ratio: float  # How much hedge P&L offset core losses
    cost_efficiency: float  # Hedge P&L / hedge cost


@dataclass
class AttributionReport:
    """Complete attribution report."""
    report_date: date
    nav: float
    daily_pnl_usd: float
    daily_pnl_pct: float
    sleeve_attribution: Dict[Sleeve, SleeveAttribution]
    factor_exposure: FactorExposure
    hedge_effectiveness: Optional[HedgeEffectiveness]
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging."""
        return {
            "report_date": self.report_date.isoformat(),
            "nav": self.nav,
            "daily_pnl_usd": round(self.daily_pnl_usd, 2),
            "daily_pnl_pct": round(self.daily_pnl_pct, 4),
            "sleeves": {
                s.value: {
                    "pnl_usd": round(a.pnl_usd, 2),
                    "pnl_pct": round(a.pnl_pct, 4),
                }
                for s, a in self.sleeve_attribution.items()
            },
            "factors": {
                "equity_beta": round(self.factor_exposure.equity_beta, 2),
                "duration": round(self.factor_exposure.duration_years, 1),
                "credit_spread": round(self.factor_exposure.credit_spread_sensitivity, 3),
                "fx_pct_nav": round(self.factor_exposure.fx_exposure_pct_nav, 3),
            },
        }

    def to_telegram_format(self) -> str:
        """Format report for Telegram notification."""
        sign = "+" if self.daily_pnl_usd >= 0 else ""
        pnl_emoji = "\U0001f4c8" if self.daily_pnl_usd >= 0 else "\U0001f4c9"

        lines = [
            f"\U0001f4ca Daily Attribution ({self.report_date.strftime('%Y-%m-%d')})",
            "",
            f"NAV: ${self.nav:,.0f} ({sign}${self.daily_pnl_usd:,.0f} / {sign}{self.daily_pnl_pct:.2%}) {pnl_emoji}",
            "",
            "Sleeve P&L:",
        ]

        # Sort sleeves by P&L
        sorted_sleeves = sorted(
            self.sleeve_attribution.items(),
            key=lambda x: x[1].pnl_usd,
            reverse=True
        )

        for sleeve, attr in sorted_sleeves:
            sign = "+" if attr.pnl_usd >= 0 else ""
            sleeve_name = sleeve.value.replace("_", " ").title()
            lines.append(f"  {sleeve_name}: {sign}${attr.pnl_usd:,.0f} ({sign}{attr.pnl_pct:.2%})")

        lines.extend([
            "",
            "Factor Exposure:",
            f"  Equity Beta: {self.factor_exposure.equity_beta:.2f}",
            f"  Duration: {self.factor_exposure.duration_years:.1f} years",
            f"  Credit Spread: {self.factor_exposure.credit_spread_sensitivity:.3f}",
            f"  EUR/USD: ${self.factor_exposure.fx_exposure_usd:,.0f} ({self.factor_exposure.fx_exposure_pct_nav:.1%} NAV)",
        ])

        if self.hedge_effectiveness:
            he = self.hedge_effectiveness
            lines.extend([
                "",
                "Hedge Effectiveness:",
                f"  Vol Hedge P&L: ${he.vol_hedge_pnl:,.0f}",
            ])
            if he.core_drawdown < 0:
                lines.append(f"  Offset Ratio: {he.offset_ratio:.1%}")

        return "\n".join(lines)


class AttributionEngine:
    """
    Engine for computing portfolio attribution.

    Provides daily sleeve-level P&L attribution and factor exposure calculation.
    """

    # Factor betas by instrument type (simplified)
    EQUITY_BETAS = {
        "CSPX": 1.0,    # S&P 500
        "CS51": 1.1,    # EURO STOXX 50
        "EXV1": 1.2,    # EU Financials
        "IUKD": 0.9,    # UK Dividend
        "EXV3": 1.3,    # EU Technology
        "EXV4": 0.8,    # EU Healthcare
    }

    DURATION_YEARS = {
        "LQDE": 6.0,    # IG Corp bonds
        "IHYU": 4.0,    # High Yield
        "FLOT": 0.3,    # Floating rate
        "FGBL": 8.0,    # Bund futures
        "FBTP": 7.0,    # BTP futures
    }

    CREDIT_SENSITIVITY = {
        "LQDE": 0.05,   # IG spreads
        "IHYU": 0.12,   # HY spreads
        "ARCC": 0.15,   # BDC credit
        "FLOT": 0.02,   # Floating rate
    }

    def __init__(self):
        """Initialize attribution engine."""
        self._previous_nav: Optional[float] = None
        self._previous_positions: Dict[str, float] = {}
        self._daily_sleeve_pnl: Dict[Sleeve, float] = {}

    def compute_attribution(
        self,
        portfolio: PortfolioState,
        previous_nav: Optional[float] = None,
        today: Optional[date] = None
    ) -> AttributionReport:
        """
        Compute full attribution report.

        Args:
            portfolio: Current portfolio state
            previous_nav: Previous day's NAV (uses stored if not provided)
            today: Report date

        Returns:
            AttributionReport with all metrics
        """
        today = today or date.today()
        previous_nav = previous_nav or self._previous_nav or portfolio.nav

        # Compute daily P&L
        daily_pnl_usd = portfolio.nav - previous_nav
        daily_pnl_pct = daily_pnl_usd / previous_nav if previous_nav > 0 else 0

        # Compute sleeve attribution
        sleeve_attribution = self._compute_sleeve_attribution(portfolio, previous_nav)

        # Compute factor exposures
        factor_exposure = self._compute_factor_exposure(portfolio)

        # Compute hedge effectiveness
        hedge_effectiveness = self._compute_hedge_effectiveness(
            portfolio, sleeve_attribution
        )

        # Update state for next day
        self._previous_nav = portfolio.nav
        self._previous_positions = {
            pos_id: pos.market_value
            for pos_id, pos in portfolio.positions.items()
        }

        report = AttributionReport(
            report_date=today,
            nav=portfolio.nav,
            daily_pnl_usd=daily_pnl_usd,
            daily_pnl_pct=daily_pnl_pct,
            sleeve_attribution=sleeve_attribution,
            factor_exposure=factor_exposure,
            hedge_effectiveness=hedge_effectiveness,
        )

        logger.info(f"Attribution computed: daily_pnl={daily_pnl_pct:.2%}")
        return report

    def _compute_sleeve_attribution(
        self,
        portfolio: PortfolioState,
        previous_nav: float
    ) -> Dict[Sleeve, SleeveAttribution]:
        """Compute P&L attribution by sleeve."""
        attribution = {}

        for sleeve in Sleeve:
            # Get positions in this sleeve
            sleeve_positions = [
                pos for pos in portfolio.positions.values()
                if pos.sleeve == sleeve
            ]

            if not sleeve_positions:
                attribution[sleeve] = SleeveAttribution(
                    sleeve=sleeve,
                    pnl_usd=0.0,
                    pnl_pct=0.0,
                    gross_exposure=0.0,
                    net_exposure=0.0,
                    position_count=0,
                )
                continue

            # Compute P&L from position changes
            # Simplified: use unrealized P&L from positions
            sleeve_pnl = sum(pos.unrealized_pnl for pos in sleeve_positions)

            # Get from stored daily P&L if available
            if sleeve in self._daily_sleeve_pnl:
                sleeve_pnl = self._daily_sleeve_pnl[sleeve]

            gross_exposure = sum(abs(pos.market_value) for pos in sleeve_positions)
            net_exposure = sum(pos.market_value for pos in sleeve_positions)

            attribution[sleeve] = SleeveAttribution(
                sleeve=sleeve,
                pnl_usd=sleeve_pnl,
                pnl_pct=sleeve_pnl / previous_nav if previous_nav > 0 else 0,
                gross_exposure=gross_exposure,
                net_exposure=net_exposure,
                position_count=len(sleeve_positions),
            )

        return attribution

    def _compute_factor_exposure(
        self,
        portfolio: PortfolioState
    ) -> FactorExposure:
        """Compute portfolio factor exposures."""
        nav = portfolio.nav

        # Compute weighted equity beta
        total_equity_exposure = 0.0
        weighted_beta = 0.0

        for pos in portfolio.positions.values():
            symbol = pos.symbol.upper()
            if symbol in self.EQUITY_BETAS:
                beta = self.EQUITY_BETAS[symbol]
                weighted_beta += pos.market_value * beta
                total_equity_exposure += abs(pos.market_value)

        equity_beta = weighted_beta / nav if nav > 0 else 0

        # Compute duration
        weighted_duration = 0.0
        for pos in portfolio.positions.values():
            symbol = pos.symbol.upper()
            if symbol in self.DURATION_YEARS:
                dur = self.DURATION_YEARS[symbol]
                weighted_duration += abs(pos.market_value) * dur

        duration_years = weighted_duration / nav if nav > 0 else 0

        # Compute credit spread sensitivity
        weighted_credit = 0.0
        for pos in portfolio.positions.values():
            symbol = pos.symbol.upper()
            if symbol in self.CREDIT_SENSITIVITY:
                sens = self.CREDIT_SENSITIVITY[symbol]
                weighted_credit += abs(pos.market_value) * sens

        credit_spread_sensitivity = weighted_credit / nav if nav > 0 else 0

        # Compute FX exposure (EUR and GBP positions)
        fx_exposure_usd = 0.0
        for pos in portfolio.positions.values():
            if pos.currency in ("EUR", "GBP"):
                fx_exposure_usd += pos.market_value

        fx_exposure_pct_nav = fx_exposure_usd / nav if nav > 0 else 0

        return FactorExposure(
            equity_beta=equity_beta,
            duration_years=duration_years,
            credit_spread_sensitivity=credit_spread_sensitivity,
            fx_exposure_usd=fx_exposure_usd,
            fx_exposure_pct_nav=fx_exposure_pct_nav,
        )

    def _compute_hedge_effectiveness(
        self,
        portfolio: PortfolioState,
        sleeve_attribution: Dict[Sleeve, SleeveAttribution]
    ) -> Optional[HedgeEffectiveness]:
        """Compute hedge effectiveness metrics."""
        # Get vol hedge P&L (europe_vol_convex sleeve)
        vol_attr = sleeve_attribution.get(Sleeve.EUROPE_VOL_CONVEX)
        vol_hedge_pnl = vol_attr.pnl_usd if vol_attr else 0

        # Get core strategy P&L (core_index_rv + sector_rv)
        core_pnl = 0.0
        for sleeve in [Sleeve.CORE_INDEX_RV, Sleeve.SECTOR_RV]:
            if sleeve in sleeve_attribution:
                core_pnl += sleeve_attribution[sleeve].pnl_usd

        # Core drawdown (negative P&L)
        core_drawdown = min(core_pnl, 0)

        # Offset ratio: how much hedge offset core losses
        if core_drawdown < 0:
            offset_ratio = -vol_hedge_pnl / core_drawdown if core_drawdown != 0 else 0
        else:
            offset_ratio = 0

        # Cost efficiency (simplified - compare to budget)
        # Would need historical cost tracking for accuracy
        cost_efficiency = 0.0

        return HedgeEffectiveness(
            vol_hedge_pnl=vol_hedge_pnl,
            core_drawdown=core_drawdown,
            offset_ratio=offset_ratio,
            cost_efficiency=cost_efficiency,
        )

    def update_sleeve_pnl(self, sleeve: Sleeve, pnl: float) -> None:
        """Update daily P&L for a specific sleeve."""
        self._daily_sleeve_pnl[sleeve] = pnl

    def reset_daily_pnl(self) -> None:
        """Reset daily P&L tracking for new day."""
        self._daily_sleeve_pnl = {}

    def get_summary(self) -> Dict[str, Any]:
        """Get current state summary."""
        return {
            "previous_nav": self._previous_nav,
            "daily_sleeve_pnl": {
                s.value: round(p, 2) for s, p in self._daily_sleeve_pnl.items()
            },
            "tracked_positions": len(self._previous_positions),
        }


def create_attribution_engine() -> AttributionEngine:
    """Factory function to create AttributionEngine."""
    return AttributionEngine()
