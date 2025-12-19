"""
Instrument utilities for AbstractFinance.

Provides centralized handling for:
1. Instrument ID normalization (stripping expiry suffixes)
2. Price unit conversion (GBP/pence symmetric handling)

Usage:
    from src.utils.instruments import (
        normalize_instrument_id,
        PriceConverter,
    )
"""

import re
import logging
from typing import Dict, Optional, Set, Any

logger = logging.getLogger(__name__)


# =============================================================================
# Instrument ID Normalization
# =============================================================================

# Pattern for futures expiry suffix: _YYYYMMDD (8 digits)
FUTURES_EXPIRY_PATTERN = re.compile(r'^(.+)_(\d{8})$')


def normalize_instrument_id(instrument_id: str) -> str:
    """
    Normalize an instrument ID by stripping known suffixes.

    Handles:
    - Futures expiry suffixes: eurusd_micro_20260316 -> eurusd_micro

    Args:
        instrument_id: Raw instrument ID (may include suffix)

    Returns:
        Base instrument ID without suffix

    Examples:
        >>> normalize_instrument_id("eurusd_micro_20260316")
        'eurusd_micro'
        >>> normalize_instrument_id("us_index_etf")
        'us_index_etf'
        >>> normalize_instrument_id("M6E_20260316")
        'M6E'
    """
    # Check for futures expiry suffix
    match = FUTURES_EXPIRY_PATTERN.match(instrument_id)
    if match:
        return match.group(1)

    return instrument_id


def extract_expiry_from_id(instrument_id: str) -> Optional[str]:
    """
    Extract expiry date from instrument ID if present.

    Args:
        instrument_id: Instrument ID that may contain expiry suffix

    Returns:
        Expiry in YYYYMMDD format, or None if not present

    Examples:
        >>> extract_expiry_from_id("eurusd_micro_20260316")
        '20260316'
        >>> extract_expiry_from_id("us_index_etf")
        None
    """
    match = FUTURES_EXPIRY_PATTERN.match(instrument_id)
    if match:
        return match.group(2)
    return None


def extract_expiry_for_ibkr(instrument_id: str) -> Optional[str]:
    """
    Extract expiry in IBKR format (YYYYMM) from instrument ID.

    Args:
        instrument_id: Instrument ID that may contain expiry suffix

    Returns:
        Expiry in YYYYMM format for IBKR, or None if not present

    Examples:
        >>> extract_expiry_for_ibkr("eurusd_micro_20260316")
        '202603'
        >>> extract_expiry_for_ibkr("us_index_etf")
        None
    """
    expiry = extract_expiry_from_id(instrument_id)
    if expiry:
        return expiry[:6]  # YYYYMM
    return None


# =============================================================================
# Price Conversion (GBP/Pence)
# =============================================================================

class PriceConverter:
    """
    Handles symmetric price unit conversion for IBKR.

    IBKR quotes GBP-denominated LSE ETFs in pence (GBX), but our internal
    system uses pounds (GBP). This class ensures:
    - Inbound: pence -> GBP (divide by 100)
    - Outbound: GBP -> pence (multiply by 100)

    Usage:
        converter = PriceConverter(instruments_config)

        # When receiving prices from IBKR
        internal_price = converter.from_broker("IUKD", 912.5)  # Returns 9.125

        # When sending orders to IBKR
        broker_price = converter.to_broker("IUKD", 9.125)  # Returns 912.5
    """

    # GBP-denominated LSE ETFs quoted in pence (GBX) by IBKR
    # These need pence<->GBP conversion
    # IMPORTANT: Only TRUE GBP instruments should be here, NOT USD ETFs on LSE
    DEFAULT_GBX_SYMBOLS: Set[str] = {
        "SMEA",   # iShares Core MSCI Europe UCITS ETF - GBP
        "IUKD",   # iShares UK Dividend UCITS ETF - GBP
        "IEAC",   # iShares Core Corp Bond UCITS ETF - GBP
        "IHYG",   # iShares Euro High Yield Corp Bond UCITS ETF - GBP
    }

    def __init__(
        self,
        instruments_config: Optional[Dict[str, Any]] = None,
        additional_gbx_symbols: Optional[Set[str]] = None,
    ):
        """
        Initialize PriceConverter.

        Args:
            instruments_config: Optional instruments config to auto-detect GBP instruments
            additional_gbx_symbols: Additional symbols to treat as GBX-quoted
        """
        self._gbx_symbols = self.DEFAULT_GBX_SYMBOLS.copy()

        if additional_gbx_symbols:
            self._gbx_symbols.update(additional_gbx_symbols)

        # Auto-detect from config if provided
        if instruments_config:
            self._detect_gbx_from_config(instruments_config)

    def _detect_gbx_from_config(self, config: Dict[str, Any]) -> None:
        """
        Auto-detect GBX symbols from instruments config.

        Adds symbols where:
        - currency = "GBP"
        - exchange = "LSE"
        - sec_type = "STK" (ETFs/stocks)
        """
        for category, instruments in config.items():
            if not isinstance(instruments, dict):
                continue
            for inst_id, spec in instruments.items():
                if not isinstance(spec, dict):
                    continue

                currency = spec.get("currency", "USD")
                exchange = spec.get("exchange", "")
                sec_type = spec.get("sec_type", "STK")
                symbol = spec.get("symbol", inst_id)

                # GBP instruments on LSE are quoted in pence
                if currency == "GBP" and exchange == "LSE" and sec_type == "STK":
                    if symbol not in self._gbx_symbols:
                        logger.debug(f"Auto-detected GBX symbol: {symbol}")
                        self._gbx_symbols.add(symbol)

    @property
    def gbx_symbols(self) -> Set[str]:
        """Return the set of GBX-quoted symbols."""
        return self._gbx_symbols.copy()

    def is_gbx_quoted(self, symbol: str) -> bool:
        """Check if a symbol is quoted in pence (GBX)."""
        return symbol in self._gbx_symbols

    def from_broker(self, symbol: str, price: float) -> float:
        """
        Convert price FROM broker (IBKR) TO internal format.

        For GBX symbols: pence -> GBP (divide by 100)

        Args:
            symbol: Instrument symbol (e.g., "IUKD")
            price: Price from IBKR

        Returns:
            Price in internal format (GBP for GBX symbols)
        """
        if price is None:
            return None

        if self.is_gbx_quoted(symbol):
            converted = price / 100.0
            logger.debug(f"Price from broker: {symbol} {price}p -> {converted} GBP")
            return converted

        return price

    def to_broker(self, symbol: str, price: float) -> float:
        """
        Convert price TO broker (IBKR) FROM internal format.

        For GBX symbols: GBP -> pence (multiply by 100)

        Args:
            symbol: Instrument symbol (e.g., "IUKD")
            price: Price in internal format (GBP)

        Returns:
            Price for IBKR (pence for GBX symbols)
        """
        if price is None:
            return None

        if self.is_gbx_quoted(symbol):
            converted = round(price * 100, 2)
            logger.debug(f"Price to broker: {symbol} {price} GBP -> {converted}p")
            return converted

        return price

    def validate_order_price(
        self,
        symbol: str,
        order_price: float,
        market_price: float,
        tolerance_pct: float = 5.0,
    ) -> bool:
        """
        Validate that order price is reasonable vs market price.

        This catches unit conversion errors by checking that the order
        price is within tolerance of the market price.

        Args:
            symbol: Instrument symbol
            order_price: Price we're about to send (in broker units)
            market_price: Current market price (in broker units)
            tolerance_pct: Allowed deviation percentage

        Returns:
            True if price is valid, False otherwise
        """
        if market_price <= 0:
            return True  # Can't validate without market price

        deviation_pct = abs(order_price - market_price) / market_price * 100

        if deviation_pct > tolerance_pct:
            logger.warning(
                f"Order price validation failed for {symbol}: "
                f"order={order_price}, market={market_price}, "
                f"deviation={deviation_pct:.1f}% > {tolerance_pct}%"
            )
            return False

        return True


# =============================================================================
# Instrument Spec Lookup
# =============================================================================

def find_instrument_spec(
    instrument_id: str,
    instruments_config: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """
    Find instrument specification in config, handling ID variations.

    Searches for:
    1. Exact instrument_id match
    2. Symbol match
    3. Base ID match (after stripping futures expiry suffix)

    Args:
        instrument_id: Instrument ID to look up
        instruments_config: Full instruments configuration

    Returns:
        Instrument spec dict, or None if not found
    """
    # First try exact match
    for category, instruments in instruments_config.items():
        if not isinstance(instruments, dict):
            continue

        # Direct ID match
        if instrument_id in instruments:
            return instruments[instrument_id]

        # Symbol match
        for inst_key, spec in instruments.items():
            if isinstance(spec, dict) and spec.get('symbol') == instrument_id:
                return spec

    # Try normalized ID (strip futures expiry suffix)
    base_id = normalize_instrument_id(instrument_id)
    if base_id != instrument_id:
        for category, instruments in instruments_config.items():
            if not isinstance(instruments, dict):
                continue

            if base_id in instruments:
                return instruments[base_id]

            for inst_key, spec in instruments.items():
                if isinstance(spec, dict) and spec.get('symbol') == base_id:
                    return spec

    return None
