"""
FX Rates service for AbstractFinance.
Provides centralized currency conversion with consistent snapshots.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional, Tuple
import yfinance as yf


# Global base currency - all NAV calculations convert to this
BASE_CCY = "USD"


@dataclass
class FXRates:
    """
    Centralized FX rate service.
    Provides consistent snapshot-based currency conversion.
    """
    # Rates keyed by (from_ccy, to_ccy) -> rate
    # e.g., ("EUR", "USD") -> 1.05 means 1 EUR = 1.05 USD
    rates: Dict[Tuple[str, str], float] = field(default_factory=dict)
    timestamp: Optional[datetime] = None

    # Supported currencies
    SUPPORTED_CURRENCIES = ["USD", "EUR", "GBP", "CHF", "JPY", "CAD", "AUD"]

    def __post_init__(self):
        """Initialize with identity rates."""
        if not self.rates:
            # Add identity rates (USD -> USD = 1.0)
            for ccy in self.SUPPORTED_CURRENCIES:
                self.rates[(ccy, ccy)] = 1.0

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

        raise KeyError(f"No FX rate available for {from_ccy}/{to_ccy}")

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

        Args:
            ib: Optional IB connection for live rates

        Returns:
            True if refresh successful
        """
        try:
            # Primary: Try IBKR
            if ib and hasattr(ib, 'isConnected') and ib.isConnected():
                return self._refresh_from_ib(ib)

            # Fallback: Yahoo Finance
            return self._refresh_from_yfinance()

        except Exception:
            return False

    def _refresh_from_ib(self, ib: object) -> bool:
        """Refresh rates from IBKR."""
        from ib_insync import Forex

        pairs = [
            ("EUR", "USD"),
            ("GBP", "USD"),
            ("CHF", "USD"),
            ("JPY", "USD"),
            ("CAD", "USD"),
            ("AUD", "USD"),
        ]

        for from_ccy, to_ccy in pairs:
            try:
                contract = Forex(from_ccy + to_ccy)
                ib.qualifyContracts(contract)
                ticker = ib.reqMktData(contract, '', False, False)
                ib.sleep(0.5)

                rate = ticker.last or ticker.close
                if rate and rate > 0:
                    self.rates[(from_ccy, to_ccy)] = rate

                ib.cancelMktData(contract)
            except Exception:
                continue

        self.timestamp = datetime.now()
        return True

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

        for (from_ccy, to_ccy), yf_ticker in yf_pairs.items():
            try:
                data = yf.Ticker(yf_ticker)
                hist = data.history(period="1d")
                if not hist.empty:
                    rate = hist['Close'].iloc[-1]
                    if rate and rate > 0:
                        self.rates[(from_ccy, to_ccy)] = rate
            except Exception:
                continue

        self.timestamp = datetime.now()
        return True

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
            "base_ccy": BASE_CCY
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
