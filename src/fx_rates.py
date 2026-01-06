"""
FX Rates service for AbstractFinance.
Provides centralized currency conversion with consistent snapshots.

v2.5: Added robust fallback rates to prevent trading failures when
      live FX data is unavailable.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional, Tuple
import yfinance as yf


logger = logging.getLogger(__name__)

# Global base currency - all NAV calculations convert to this
BASE_CCY = "USD"

# Hardcoded fallback rates - updated periodically, used when live data unavailable
# These are approximate rates and should be close enough for position sizing
# Last updated: 2026-01-06
FALLBACK_RATES = {
    ("EUR", "USD"): 1.03,   # 1 EUR = 1.03 USD
    ("GBP", "USD"): 1.25,   # 1 GBP = 1.25 USD
    ("CHF", "USD"): 1.12,   # 1 CHF = 1.12 USD
    ("JPY", "USD"): 0.0064, # 1 JPY = 0.0064 USD (approx 156 JPY/USD)
    ("CAD", "USD"): 0.70,   # 1 CAD = 0.70 USD
    ("AUD", "USD"): 0.62,   # 1 AUD = 0.62 USD
}


@dataclass
class FXRates:
    """
    Centralized FX rate service.
    Provides consistent snapshot-based currency conversion.

    Fallback hierarchy:
    1. Live IBKR rates (preferred)
    2. Yahoo Finance rates
    3. Hardcoded fallback rates (always available)
    """
    # Rates keyed by (from_ccy, to_ccy) -> rate
    # e.g., ("EUR", "USD") -> 1.05 means 1 EUR = 1.05 USD
    rates: Dict[Tuple[str, str], float] = field(default_factory=dict)
    timestamp: Optional[datetime] = None
    source: str = "fallback"  # Track where rates came from

    # Supported currencies
    SUPPORTED_CURRENCIES = ["USD", "EUR", "GBP", "CHF", "JPY", "CAD", "AUD"]

    def __post_init__(self):
        """Initialize with identity and fallback rates."""
        if not self.rates:
            # Add identity rates (USD -> USD = 1.0)
            for ccy in self.SUPPORTED_CURRENCIES:
                self.rates[(ccy, ccy)] = 1.0
            # Add fallback rates
            self._apply_fallback_rates()

    def get_rate(self, from_ccy: str, to_ccy: str) -> float:
        """
        Get FX rate from one currency to another.

        Args:
            from_ccy: Source currency (e.g., "EUR")
            to_ccy: Target currency (e.g., "USD")

        Returns:
            Exchange rate (from_ccy units per to_ccy unit)

        Raises:
            KeyError: If rate not available
        """
        if from_ccy == to_ccy:
            return 1.0

        # Direct rate
        if (from_ccy, to_ccy) in self.rates:
            return self.rates[(from_ccy, to_ccy)]

        # Inverse rate
        if (to_ccy, from_ccy) in self.rates:
            return 1.0 / self.rates[(to_ccy, from_ccy)]

        # Cross rate via USD
        if from_ccy != "USD" and to_ccy != "USD":
            from_usd = self.get_rate(from_ccy, "USD")
            to_usd = self.get_rate(to_ccy, "USD")
            return from_usd / to_usd

        # This should never happen now due to fallback rates
        logger.error(f"No FX rate available for {from_ccy}/{to_ccy} - using 1.0")
        return 1.0  # Safe default rather than crashing

    def _apply_fallback_rates(self) -> None:
        """Apply hardcoded fallback rates."""
        for pair, rate in FALLBACK_RATES.items():
            if pair not in self.rates:
                self.rates[pair] = rate
        self.source = "fallback"
        logger.debug(f"Applied fallback FX rates: {len(FALLBACK_RATES)} pairs")

    def convert(self, amount: float, from_ccy: str, to_ccy: str) -> float:
        """
        Convert amount from one currency to another.

        Args:
            amount: Amount in from_ccy
            from_ccy: Source currency
            to_ccy: Target currency

        Returns:
            Amount in to_ccy
        """
        return amount * self.get_rate(from_ccy, to_ccy)

    def to_base(self, amount: float, from_ccy: str) -> float:
        """
        Convert amount to base currency (USD).

        Args:
            amount: Amount in from_ccy
            from_ccy: Source currency

        Returns:
            Amount in BASE_CCY (USD)
        """
        return self.convert(amount, from_ccy, BASE_CCY)

    def set_rate(self, from_ccy: str, to_ccy: str, rate: float) -> None:
        """
        Set FX rate.

        Args:
            from_ccy: Source currency
            to_ccy: Target currency
            rate: Exchange rate
        """
        self.rates[(from_ccy, to_ccy)] = rate
        self.timestamp = datetime.now()

    def refresh(self, ib: Optional[object] = None) -> bool:
        """
        Refresh all FX rates from market data.

        Fallback hierarchy:
        1. Live IBKR rates (preferred)
        2. Yahoo Finance rates
        3. Hardcoded fallback rates (always available)

        Args:
            ib: Optional IB connection for live rates

        Returns:
            True if refresh successful (always True due to fallbacks)
        """
        rates_before = len([k for k in self.rates.keys() if k[0] != k[1]])

        # Primary: Try IBKR
        if ib and hasattr(ib, 'isConnected') and ib.isConnected():
            try:
                if self._refresh_from_ib(ib):
                    rates_after = len([k for k in self.rates.keys() if k[0] != k[1]])
                    if rates_after > rates_before:
                        self.source = "ibkr"
                        logger.info(f"FX rates refreshed from IBKR: {rates_after} pairs")
                        return True
            except Exception as e:
                logger.warning(f"IBKR FX refresh failed: {e}")

        # Secondary: Try Yahoo Finance
        try:
            if self._refresh_from_yfinance():
                rates_after = len([k for k in self.rates.keys() if k[0] != k[1]])
                if rates_after > rates_before:
                    self.source = "yfinance"
                    logger.info(f"FX rates refreshed from Yahoo Finance: {rates_after} pairs")
                    return True
        except Exception as e:
            logger.warning(f"Yahoo Finance FX refresh failed: {e}")

        # Tertiary: Apply hardcoded fallbacks (always succeeds)
        self._apply_fallback_rates()
        logger.warning("Using hardcoded fallback FX rates")
        return True  # Always return True - fallbacks guarantee rates exist

    def _refresh_from_ib(self, ib: object) -> bool:
        """Refresh rates from IBKR."""
        from ib_insync import Forex

        # IBKR forex pairs with proper convention
        # Format: (from_ccy, to_ccy, ib_pair, needs_invert)
        # needs_invert=True when IBKR pair is USD/XXX but we want XXX/USD
        pairs = [
            ("EUR", "USD", "EURUSD", False),  # 1 EUR = X USD
            ("GBP", "USD", "GBPUSD", False),  # 1 GBP = X USD
            ("CHF", "USD", "USDCHF", True),   # IBKR: 1 USD = X CHF, we want 1 CHF = X USD
            ("JPY", "USD", "USDJPY", True),   # IBKR: 1 USD = X JPY, we want 1 JPY = X USD
            ("CAD", "USD", "USDCAD", True),   # IBKR: 1 USD = X CAD, we want 1 CAD = X USD
            ("AUD", "USD", "AUDUSD", False),  # 1 AUD = X USD
        ]

        success_count = 0
        for from_ccy, to_ccy, ib_pair, needs_invert in pairs:
            try:
                contract = Forex(ib_pair)
                ib.qualifyContracts(contract)
                ticker = ib.reqMktData(contract, '', False, False)
                ib.sleep(0.5)

                rate = ticker.last or ticker.close
                if rate and rate > 0:
                    if needs_invert:
                        rate = 1.0 / rate
                    self.rates[(from_ccy, to_ccy)] = rate
                    success_count += 1
                    logger.debug(f"IBKR FX: {from_ccy}/{to_ccy} = {rate:.6f}")

                ib.cancelMktData(contract)
            except Exception as e:
                logger.debug(f"IBKR FX fetch failed for {ib_pair}: {e}")
                continue

        self.timestamp = datetime.now()
        logger.debug(f"IBKR FX refresh: {success_count}/{len(pairs)} pairs fetched")
        return success_count > 0

    def _refresh_from_yfinance(self) -> bool:
        """Refresh rates from Yahoo Finance."""
        yf_pairs = {
            ("EUR", "USD"): "EURUSD=X",
            ("GBP", "USD"): "GBPUSD=X",
            ("CHF", "USD"): "CHFUSD=X",
            ("JPY", "USD"): "JPYUSD=X",
            ("CAD", "USD"): "CADUSD=X",
            ("AUD", "USD"): "AUDUSD=X",
        }

        success_count = 0
        for (from_ccy, to_ccy), yf_ticker in yf_pairs.items():
            try:
                data = yf.Ticker(yf_ticker)
                hist = data.history(period="1d")
                if not hist.empty:
                    rate = hist['Close'].iloc[-1]
                    if rate and rate > 0:
                        self.rates[(from_ccy, to_ccy)] = rate
                        success_count += 1
                        logger.debug(f"Yahoo FX: {from_ccy}/{to_ccy} = {rate:.6f}")
            except Exception as e:
                logger.debug(f"Yahoo FX fetch failed for {yf_ticker}: {e}")
                continue

        self.timestamp = datetime.now()
        logger.debug(f"Yahoo FX refresh: {success_count}/{len(yf_pairs)} pairs fetched")
        return success_count > 0

    def is_stale(self, max_age_seconds: int = 300) -> bool:
        """
        Check if rates are stale.

        Args:
            max_age_seconds: Maximum age in seconds (default 5 minutes)

        Returns:
            True if rates are older than max_age_seconds
        """
        if self.timestamp is None:
            return True
        return (datetime.now() - self.timestamp).total_seconds() > max_age_seconds

    def to_dict(self) -> Dict:
        """Serialize to dictionary."""
        return {
            "rates": {f"{k[0]}/{k[1]}": v for k, v in self.rates.items()},
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "base_ccy": BASE_CCY,
            "source": self.source
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "FXRates":
        """Deserialize from dictionary."""
        rates = {}
        for key, value in data.get("rates", {}).items():
            from_ccy, to_ccy = key.split("/")
            rates[(from_ccy, to_ccy)] = value

        timestamp = None
        if data.get("timestamp"):
            timestamp = datetime.fromisoformat(data["timestamp"])

        return cls(rates=rates, timestamp=timestamp)


def cash_in_base_ccy(cash_by_ccy: Dict[str, float], fx_rates: FXRates) -> float:
    """
    Convert multi-currency cash balances to base currency.

    Args:
        cash_by_ccy: Dict mapping currency to cash amount
        fx_rates: FX rates service

    Returns:
        Total cash in BASE_CCY (USD)
    """
    total = 0.0
    for ccy, amount in cash_by_ccy.items():
        total += fx_rates.to_base(amount, ccy)
    return total


def compute_net_fx_exposure(
    positions: Dict[str, object],
    cash_by_ccy: Dict[str, float],
    fx_rates: FXRates
) -> Dict[str, float]:
    """
    Compute net FX exposure by currency.

    This is used for portfolio-level FX hedging (Phase 5).

    Args:
        positions: Dict of instrument_id -> Position
        cash_by_ccy: Cash balances by currency
        fx_rates: FX rates service

    Returns:
        Dict mapping currency to net exposure in that currency
        Example: {"EUR": -1_400_000, "GBP": 250_000}
    """
    exposure_by_ccy: Dict[str, float] = {}

    # Add position exposures
    for inst_id, position in positions.items():
        ccy = position.currency
        # Use market_value for exposure (not NAV contribution)
        value = position.quantity * position.market_price * position.multiplier
        exposure_by_ccy[ccy] = exposure_by_ccy.get(ccy, 0.0) + value

    # Add cash balances
    for ccy, amount in cash_by_ccy.items():
        exposure_by_ccy[ccy] = exposure_by_ccy.get(ccy, 0.0) + amount

    # Remove USD (base currency - no hedge needed)
    exposure_by_ccy.pop(BASE_CCY, None)

    return exposure_by_ccy


def compute_fx_hedge_quantities(
    net_fx_exposure: Dict[str, float],
    fx_rates: FXRates,
    hedge_ratio: float = 1.0,
    contract_sizes: Optional[Dict[str, float]] = None
) -> Dict[str, int]:
    """
    Compute FX hedge quantities for each currency.

    Args:
        net_fx_exposure: Net exposure by currency (from compute_net_fx_exposure)
        fx_rates: FX rates service
        hedge_ratio: Hedge ratio (0.0 to 1.0, default 1.0 = full hedge)
        contract_sizes: Contract size per currency (default: M6E=12500 EUR)

    Returns:
        Dict mapping currency to number of futures contracts (negative = short)
    """
    contract_sizes = contract_sizes or {
        "EUR": 12500,  # M6E micro futures
        "GBP": 6250,   # M6B micro futures
        "CHF": 12500,  # M6S micro futures
        "JPY": 1250000,  # M6J micro futures
        "CAD": 10000,  # MCD micro futures
        "AUD": 10000,  # M6A micro futures
    }

    hedge_contracts: Dict[str, int] = {}

    for ccy, exposure in net_fx_exposure.items():
        if ccy not in contract_sizes:
            continue

        # Hedge notional = exposure * hedge_ratio
        hedge_notional = exposure * hedge_ratio

        # Number of contracts (round, not floor)
        contract_size = contract_sizes[ccy]
        contracts = round(hedge_notional / contract_size)

        # Negative because we're hedging (short the currency)
        # If we have positive EUR exposure, we short EUR futures
        hedge_contracts[ccy] = -contracts

    return hedge_contracts


# Default FX rates (will be refreshed at runtime)
_default_fx_rates: Optional[FXRates] = None


def get_fx_rates(refresh: bool = False, ib: Optional[object] = None) -> FXRates:
    """
    Get global FX rates instance.

    Args:
        refresh: Force refresh from market data
        ib: Optional IB connection for live rates

    Returns:
        FXRates instance
    """
    global _default_fx_rates

    if _default_fx_rates is None:
        _default_fx_rates = FXRates()
        _default_fx_rates.refresh(ib)
    elif refresh or _default_fx_rates.is_stale():
        _default_fx_rates.refresh(ib)

    return _default_fx_rates
