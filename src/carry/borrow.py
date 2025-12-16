"""
Borrow Service - Stock borrow availability and fee tracking.

Phase 2 Enhancement: Provides borrow information for short selling
decisions and cost estimation.

Attempts to use IBKR data when available, falls back to conservative
defaults otherwise.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional, Any, List

logger = logging.getLogger(__name__)


@dataclass
class BorrowConfig:
    """Configuration for borrow service."""
    enabled: bool = True
    deny_new_short_if_unavailable: bool = True
    default_borrow_fee_bps_annual: float = 150.0    # 1.5%
    max_borrow_fee_bps_annual: float = 600.0        # 6%
    hard_to_borrow_threshold_bps: float = 300.0     # 3%
    cache_ttl_seconds: int = 300                    # 5 minutes


@dataclass
class BorrowInfo:
    """Borrow information for an instrument."""
    instrument_id: str
    available: bool
    shares_available: Optional[int] = None
    fee_rate_annual_bps: Optional[float] = None
    source: str = "DEFAULT"  # "IBKR" or "DEFAULT"
    is_hard_to_borrow: bool = False
    last_updated: Optional[datetime] = None
    warning: Optional[str] = None


class BorrowService:
    """
    Service for checking stock borrow availability and fees.

    Integrates with IBKR when available, otherwise uses conservative
    defaults with appropriate warnings.

    Usage:
        service = BorrowService(config, ib_client)
        info = service.get_borrow_info("AAPL")
        if not info.available and config.deny_new_short_if_unavailable:
            # Skip short order
            pass
    """

    def __init__(
        self,
        config: Optional[BorrowConfig] = None,
        ib_client: Optional[Any] = None,
    ):
        """
        Initialize borrow service.

        Args:
            config: Borrow configuration
            ib_client: Optional IBClient for IBKR data
        """
        self.config = config or BorrowConfig()
        self.ib_client = ib_client

        # Cache
        self._cache: Dict[str, BorrowInfo] = {}
        self._cache_times: Dict[str, datetime] = {}

        # Tracking
        self.warnings_today: List[Dict[str, Any]] = []

    def get_borrow_info(
        self,
        instrument_id: str,
        force_refresh: bool = False,
    ) -> BorrowInfo:
        """
        Get borrow information for an instrument.

        Args:
            instrument_id: Instrument identifier
            force_refresh: Force refresh from source

        Returns:
            BorrowInfo with availability and fee data
        """
        if not self.config.enabled:
            return BorrowInfo(
                instrument_id=instrument_id,
                available=True,
                source="DISABLED",
            )

        # Check cache
        if not force_refresh and instrument_id in self._cache:
            cache_time = self._cache_times.get(instrument_id)
            if cache_time:
                age = (datetime.now() - cache_time).total_seconds()
                if age < self.config.cache_ttl_seconds:
                    return self._cache[instrument_id]

        # Try IBKR
        info = self._fetch_ibkr_borrow_info(instrument_id)

        if info is None:
            # Fall back to defaults
            info = self._get_default_info(instrument_id)

        # Check for hard to borrow
        if info.fee_rate_annual_bps and info.fee_rate_annual_bps >= self.config.hard_to_borrow_threshold_bps:
            info.is_hard_to_borrow = True
            info.warning = f"Hard to borrow: {info.fee_rate_annual_bps:.0f} bps annual"
            self._record_warning(instrument_id, "hard_to_borrow", info.warning)

        # Check for high fee
        if info.fee_rate_annual_bps and info.fee_rate_annual_bps >= self.config.max_borrow_fee_bps_annual:
            info.warning = f"Very high borrow fee: {info.fee_rate_annual_bps:.0f} bps annual"
            self._record_warning(instrument_id, "high_fee", info.warning)

        # Cache
        self._cache[instrument_id] = info
        self._cache_times[instrument_id] = datetime.now()

        return info

    def _fetch_ibkr_borrow_info(
        self,
        instrument_id: str,
    ) -> Optional[BorrowInfo]:
        """Fetch borrow info from IBKR."""
        if self.ib_client is None:
            return None

        try:
            # IBKR provides shortable shares and fee rate
            # This depends on your IBClient implementation
            if hasattr(self.ib_client, 'get_shortable_shares'):
                shares = self.ib_client.get_shortable_shares(instrument_id)
                fee_rate = self.ib_client.get_borrow_fee_rate(instrument_id)

                return BorrowInfo(
                    instrument_id=instrument_id,
                    available=shares is not None and shares > 0,
                    shares_available=shares,
                    fee_rate_annual_bps=fee_rate * 100 if fee_rate else None,  # Convert to bps
                    source="IBKR",
                    last_updated=datetime.now(),
                )

            # Alternative: try ib_insync directly
            if hasattr(self.ib_client, 'ib') and self.ib_client.ib is not None:
                return self._fetch_from_ib_insync(instrument_id)

            return None

        except Exception as e:
            logger.warning(f"Failed to fetch borrow info from IBKR for {instrument_id}: {e}")
            return None

    def _fetch_from_ib_insync(
        self,
        instrument_id: str,
    ) -> Optional[BorrowInfo]:
        """Fetch borrow info directly from ib_insync."""
        try:
            ib = self.ib_client.ib

            # Find contract
            contract = None
            for pos in ib.positions():
                if pos.contract.symbol == instrument_id:
                    contract = pos.contract
                    break

            if contract is None:
                # Try to create stock contract
                from ib_insync import Stock
                contract = Stock(instrument_id, 'SMART', 'USD')
                ib.qualifyContracts(contract)

            # Request shortable shares
            # Note: This is simplified - actual IBKR API is more complex
            # In practice, you'd use reqScannerData or other methods

            # For now, return default with IBKR source as partial info
            return BorrowInfo(
                instrument_id=instrument_id,
                available=True,  # Assume available unless we have info otherwise
                source="IBKR_PARTIAL",
                fee_rate_annual_bps=self.config.default_borrow_fee_bps_annual,
                last_updated=datetime.now(),
                warning="Borrow data partially available",
            )

        except Exception as e:
            logger.debug(f"ib_insync borrow fetch error: {e}")
            return None

    def _get_default_info(
        self,
        instrument_id: str,
    ) -> BorrowInfo:
        """Get default borrow info with conservative assumptions."""
        # ETFs are generally easier to borrow
        symbol = instrument_id.upper()
        if symbol in ["SPY", "QQQ", "IWM", "DIA", "EEM", "VTI", "EFA"]:
            fee_rate = 25.0  # 0.25% for liquid ETFs
        elif symbol.endswith("ETF") or len(symbol) == 3:
            fee_rate = 50.0  # 0.5% for other ETFs
        else:
            fee_rate = self.config.default_borrow_fee_bps_annual

        return BorrowInfo(
            instrument_id=instrument_id,
            available=True,
            fee_rate_annual_bps=fee_rate,
            source="DEFAULT",
            last_updated=datetime.now(),
            warning="Using default borrow assumptions",
        )

    def can_short(
        self,
        instrument_id: str,
        quantity: int,
    ) -> tuple[bool, str]:
        """
        Check if a short position can be opened.

        Args:
            instrument_id: Instrument to short
            quantity: Number of shares to short

        Returns:
            Tuple of (can_short, reason)
        """
        if not self.config.enabled:
            return True, "Borrow checks disabled"

        info = self.get_borrow_info(instrument_id)

        if not info.available and self.config.deny_new_short_if_unavailable:
            return False, "Not available for shorting"

        if info.shares_available is not None and info.shares_available < quantity:
            return False, f"Insufficient shares: {info.shares_available} < {quantity}"

        if info.fee_rate_annual_bps and info.fee_rate_annual_bps > self.config.max_borrow_fee_bps_annual:
            return False, f"Borrow fee too high: {info.fee_rate_annual_bps:.0f} bps"

        return True, "OK"

    def get_daily_borrow_cost_bps(
        self,
        instrument_id: str,
    ) -> float:
        """Get daily borrow cost in basis points."""
        info = self.get_borrow_info(instrument_id)

        if info.fee_rate_annual_bps:
            return info.fee_rate_annual_bps / 365.0

        return self.config.default_borrow_fee_bps_annual / 365.0

    def _record_warning(
        self,
        instrument_id: str,
        warning_type: str,
        message: str,
    ) -> None:
        """Record a borrow warning."""
        self.warnings_today.append({
            "instrument_id": instrument_id,
            "type": warning_type,
            "message": message,
            "timestamp": datetime.now().isoformat(),
        })

    def get_warnings_summary(self) -> Dict[str, Any]:
        """Get summary of today's warnings."""
        return {
            "total_warnings": len(self.warnings_today),
            "hard_to_borrow": sum(1 for w in self.warnings_today if w["type"] == "hard_to_borrow"),
            "high_fee": sum(1 for w in self.warnings_today if w["type"] == "high_fee"),
            "unavailable": sum(1 for w in self.warnings_today if w["type"] == "unavailable"),
        }

    def reset_daily(self) -> None:
        """Reset daily tracking."""
        self.warnings_today.clear()


# Singleton instance
_borrow_service: Optional[BorrowService] = None


def get_borrow_service() -> BorrowService:
    """Get singleton BorrowService instance."""
    global _borrow_service
    if _borrow_service is None:
        _borrow_service = BorrowService()
    return _borrow_service


def init_borrow_service(
    config: Optional[BorrowConfig] = None,
    ib_client: Optional[Any] = None,
) -> BorrowService:
    """Initialize the borrow service singleton."""
    global _borrow_service
    _borrow_service = BorrowService(config=config, ib_client=ib_client)
    return _borrow_service
