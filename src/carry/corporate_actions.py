"""
Corporate Actions Service - Dividend and ex-div tracking.

Phase 2 Enhancement: Provides dividend awareness for short positions
to warn about upcoming ex-div dates and apply cost buffers.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from typing import Dict, Optional, Any, List

logger = logging.getLogger(__name__)


@dataclass
class DividendConfig:
    """Configuration for dividend service."""
    enabled: bool = True
    warn_on_upcoming_ex_div_days: int = 3
    default_short_dividend_buffer_bps: float = 5.0
    cache_ttl_hours: int = 24


@dataclass
class DividendInfo:
    """Dividend information for an instrument."""
    instrument_id: str
    has_dividend: bool = False
    next_ex_div_date: Optional[date] = None
    dividend_amount: Optional[float] = None
    dividend_yield: Optional[float] = None
    frequency: Optional[str] = None  # "quarterly", "annual", etc.
    days_until_ex_div: Optional[int] = None
    is_near_ex_div: bool = False
    source: str = "DEFAULT"
    last_updated: Optional[datetime] = None


class CorporateActionsService:
    """
    Service for tracking dividends and corporate actions.

    Used to:
    - Warn about shorts near ex-div dates
    - Apply dividend buffers to cost estimates
    - Track dividend exposure for reporting

    Usage:
        service = CorporateActionsService(config)
        if service.is_near_ex_div("AAPL"):
            # Add dividend buffer to short cost
            pass
    """

    def __init__(
        self,
        config: Optional[DividendConfig] = None,
        research_data: Optional[Any] = None,  # ResearchMarketData for Yahoo
    ):
        """
        Initialize corporate actions service.

        Args:
            config: Dividend configuration
            research_data: Optional ResearchMarketData for dividend info
        """
        self.config = config or DividendConfig()
        self.research_data = research_data

        # Cache
        self._cache: Dict[str, DividendInfo] = {}
        self._cache_times: Dict[str, datetime] = {}

        # Known dividend stocks (hardcoded for common ones)
        self._known_dividend_stocks = {
            "SPY": {"yield": 0.013, "frequency": "quarterly"},
            "QQQ": {"yield": 0.005, "frequency": "quarterly"},
            "IWM": {"yield": 0.012, "frequency": "quarterly"},
            "VTI": {"yield": 0.014, "frequency": "quarterly"},
            "EFA": {"yield": 0.024, "frequency": "semi-annual"},
            "DIA": {"yield": 0.016, "frequency": "monthly"},
            "XLF": {"yield": 0.018, "frequency": "quarterly"},
            "XLE": {"yield": 0.035, "frequency": "quarterly"},
            "XLU": {"yield": 0.028, "frequency": "quarterly"},
        }

        # Tracking
        self.warnings_today: List[Dict[str, Any]] = []

    def get_dividend_info(
        self,
        instrument_id: str,
        force_refresh: bool = False,
    ) -> DividendInfo:
        """
        Get dividend information for an instrument.

        Args:
            instrument_id: Instrument identifier
            force_refresh: Force refresh from source

        Returns:
            DividendInfo with dividend data
        """
        if not self.config.enabled:
            return DividendInfo(instrument_id=instrument_id, source="DISABLED")

        # Check cache
        if not force_refresh and instrument_id in self._cache:
            cache_time = self._cache_times.get(instrument_id)
            if cache_time:
                age = (datetime.now() - cache_time).total_seconds() / 3600
                if age < self.config.cache_ttl_hours:
                    return self._cache[instrument_id]

        # Try to fetch from research data (Yahoo)
        info = self._fetch_dividend_info(instrument_id)

        if info is None:
            info = self._get_default_info(instrument_id)

        # Check if near ex-div
        if info.next_ex_div_date:
            days_until = (info.next_ex_div_date - date.today()).days
            info.days_until_ex_div = days_until
            info.is_near_ex_div = 0 <= days_until <= self.config.warn_on_upcoming_ex_div_days

            if info.is_near_ex_div:
                self._record_warning(instrument_id, info)

        # Cache
        self._cache[instrument_id] = info
        self._cache_times[instrument_id] = datetime.now()

        return info

    def _fetch_dividend_info(
        self,
        instrument_id: str,
    ) -> Optional[DividendInfo]:
        """Fetch dividend info from research data source."""
        if self.research_data is None:
            return None

        try:
            # Try to get dividend calendar from Yahoo
            dividends = self.research_data.get_dividend_calendar(instrument_id)

            if dividends is None or dividends.empty:
                return None

            # Get most recent dividend and estimate next
            latest = dividends.iloc[-1]
            latest_date = latest.name if hasattr(latest, 'name') else None

            # Estimate next ex-div based on frequency
            fundamentals = self.research_data.get_fundamentals(instrument_id)
            div_yield = fundamentals.get("dividend_yield") if fundamentals else None

            # Estimate next ex-div (quarterly assumption)
            next_ex_div = None
            if latest_date:
                if isinstance(latest_date, datetime):
                    latest_date = latest_date.date()
                # Assume quarterly - next one ~90 days after last
                next_ex_div = latest_date + timedelta(days=90)
                if next_ex_div < date.today():
                    next_ex_div = next_ex_div + timedelta(days=90)

            return DividendInfo(
                instrument_id=instrument_id,
                has_dividend=True,
                next_ex_div_date=next_ex_div,
                dividend_yield=div_yield,
                frequency="quarterly",  # Assumption
                source="YAHOO",
                last_updated=datetime.now(),
            )

        except Exception as e:
            logger.debug(f"Failed to fetch dividend info for {instrument_id}: {e}")
            return None

    def _get_default_info(
        self,
        instrument_id: str,
    ) -> DividendInfo:
        """Get default dividend info."""
        symbol = instrument_id.upper()

        # Check known dividend stocks
        if symbol in self._known_dividend_stocks:
            known = self._known_dividend_stocks[symbol]
            return DividendInfo(
                instrument_id=instrument_id,
                has_dividend=True,
                dividend_yield=known.get("yield"),
                frequency=known.get("frequency"),
                source="KNOWN",
                last_updated=datetime.now(),
            )

        # Assume ETFs have dividends, stocks may or may not
        if symbol in ["SPY", "QQQ", "IWM", "DIA", "VTI", "EFA", "EEM"]:
            return DividendInfo(
                instrument_id=instrument_id,
                has_dividend=True,
                source="ASSUMED_ETF",
                last_updated=datetime.now(),
            )

        return DividendInfo(
            instrument_id=instrument_id,
            has_dividend=False,
            source="DEFAULT",
            last_updated=datetime.now(),
        )

    def is_near_ex_div(
        self,
        instrument_id: str,
    ) -> bool:
        """Check if instrument is near ex-div date."""
        info = self.get_dividend_info(instrument_id)
        return info.is_near_ex_div

    def get_dividend_buffer_bps(
        self,
        instrument_id: str,
    ) -> float:
        """
        Get dividend buffer for short cost estimation.

        Returns buffer to apply when shorting near ex-div.
        """
        info = self.get_dividend_info(instrument_id)

        if not info.has_dividend:
            return 0.0

        if info.is_near_ex_div:
            return self.config.default_short_dividend_buffer_bps

        return 0.0

    def _record_warning(
        self,
        instrument_id: str,
        info: DividendInfo,
    ) -> None:
        """Record a dividend warning."""
        self.warnings_today.append({
            "instrument_id": instrument_id,
            "type": "near_ex_div",
            "days_until": info.days_until_ex_div,
            "ex_div_date": info.next_ex_div_date.isoformat() if info.next_ex_div_date else None,
            "timestamp": datetime.now().isoformat(),
        })

    def get_warnings_summary(self) -> Dict[str, Any]:
        """Get summary of today's warnings."""
        return {
            "total_warnings": len(self.warnings_today),
            "near_ex_div_count": len(self.warnings_today),
            "instruments": [w["instrument_id"] for w in self.warnings_today],
        }

    def reset_daily(self) -> None:
        """Reset daily tracking."""
        self.warnings_today.clear()


# Singleton instance
_corporate_actions_service: Optional[CorporateActionsService] = None


def get_corporate_actions_service() -> CorporateActionsService:
    """Get singleton CorporateActionsService instance."""
    global _corporate_actions_service
    if _corporate_actions_service is None:
        _corporate_actions_service = CorporateActionsService()
    return _corporate_actions_service


def init_corporate_actions_service(
    config: Optional[DividendConfig] = None,
    research_data: Optional[Any] = None,
) -> CorporateActionsService:
    """Initialize the corporate actions service singleton."""
    global _corporate_actions_service
    _corporate_actions_service = CorporateActionsService(
        config=config,
        research_data=research_data,
    )
    return _corporate_actions_service
