"""
Runtime invariant checks for AbstractFinance.

These assertions catch bugs early by validating assumptions at runtime.
They are designed to fail fast with clear error messages rather than
allowing silent corruption to propagate through the system.

Usage:
    from src.utils.invariants import (
        assert_position_id_valid,
        assert_no_conflicting_orders,
        assert_gbx_whitelist_valid,
        validate_instruments_config,
    )
"""

from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple
import logging

logger = logging.getLogger(__name__)


class InvariantError(Exception):
    """Raised when a runtime invariant is violated."""

    pass


def assert_position_id_valid(
    instrument_id: str,
    instruments_config: Dict[str, Any],
    context: str = "",
) -> None:
    """
    Verify position ID exists in instruments config as a config ID (not IBKR symbol).

    This catches Issue 21: IBKR symbols being used instead of internal IDs.

    Args:
        instrument_id: The ID to validate
        instruments_config: The instruments configuration dict
        context: Additional context for error messages

    Raises:
        InvariantError: If ID is invalid or is an IBKR symbol instead of config ID
    """
    # Handle futures with expiry suffix
    base_id = instrument_id.split("_")[0] if "_" in instrument_id else instrument_id

    # Build lookup of config IDs and symbols
    config_ids: Set[str] = set()
    symbol_to_config_id: Dict[str, str] = {}

    for sleeve, instruments in instruments_config.items():
        if not isinstance(instruments, dict):
            continue
        for inst_id, spec in instruments.items():
            config_ids.add(inst_id)
            if isinstance(spec, dict):
                symbol = spec.get("symbol", inst_id)
                symbol_to_config_id[symbol] = inst_id

    # Check if it's a valid config ID
    if instrument_id in config_ids:
        return  # Valid

    # Check if base_id (without expiry) is a config ID (for futures)
    if base_id in config_ids:
        return  # Valid future with expiry suffix

    # Check if it's an IBKR symbol instead of config ID
    if instrument_id in symbol_to_config_id:
        correct_id = symbol_to_config_id[instrument_id]
        raise InvariantError(
            f"Position uses IBKR symbol '{instrument_id}' instead of config ID '{correct_id}'. "
            f"Check _contract_to_instrument_id() reverse mapping. {context}"
        )

    # Unknown ID - might be acceptable for new instruments
    logger.warning(
        f"Unknown instrument_id '{instrument_id}' not in config. {context}"
    )


def assert_no_conflicting_orders(
    orders: List[Any],
    context: str = "",
) -> None:
    """
    Verify no BUY and SELL orders exist for the same instrument.

    This catches bugs like Issue 21 where position sync issues caused
    both BUY us_index_etf and SELL CSPX orders (same instrument, different IDs).

    Args:
        orders: List of order objects with instrument_id and side attributes
        context: Additional context for error messages

    Raises:
        InvariantError: If conflicting orders detected
    """
    by_instrument: Dict[str, List[str]] = defaultdict(list)

    for order in orders:
        # Support both OrderSpec and dict-like objects
        if hasattr(order, "instrument_id"):
            inst_id = order.instrument_id
            side = order.side
        else:
            inst_id = order.get("instrument_id", order.get("symbol"))
            side = order.get("side")

        if inst_id and side:
            by_instrument[inst_id].append(side)

    conflicts = []
    for inst_id, sides in by_instrument.items():
        has_buy = any(s.upper() in ("BUY", "B") for s in sides)
        has_sell = any(s.upper() in ("SELL", "S") for s in sides)
        if has_buy and has_sell:
            conflicts.append(f"{inst_id}: {sides}")

    if conflicts:
        raise InvariantError(
            f"Conflicting BUY/SELL orders for same instrument(s): {conflicts}. "
            f"This likely indicates an ID mapping bug. {context}"
        )


def assert_gbx_whitelist_valid(
    gbx_symbols: Set[str],
    instruments_config: Dict[str, Any],
    context: str = "",
) -> None:
    """
    Verify GBX whitelist only contains GBP-denominated instruments.

    This catches Issue 20: USD ETFs incorrectly in GBX whitelist.

    Args:
        gbx_symbols: Set of symbols in GBX_QUOTED_ETFS
        instruments_config: The instruments configuration dict
        context: Additional context for error messages

    Raises:
        InvariantError: If non-GBP instruments found in whitelist
    """
    # Build symbol -> currency mapping
    symbol_to_currency: Dict[str, str] = {}
    symbol_to_config_id: Dict[str, str] = {}

    for sleeve, instruments in instruments_config.items():
        if not isinstance(instruments, dict):
            continue
        for inst_id, spec in instruments.items():
            if isinstance(spec, dict):
                symbol = spec.get("symbol", inst_id)
                currency = spec.get("currency", "USD")
                symbol_to_currency[symbol] = currency
                symbol_to_config_id[symbol] = inst_id

    # Validate each symbol in whitelist
    non_gbp = []
    for symbol in gbx_symbols:
        currency = symbol_to_currency.get(symbol)
        if currency and currency != "GBP":
            config_id = symbol_to_config_id.get(symbol, "unknown")
            non_gbp.append(f"{symbol} ({config_id}): {currency}")

    if non_gbp:
        raise InvariantError(
            f"GBX_QUOTED_ETFS contains non-GBP instruments: {non_gbp}. "
            f"Only GBP instruments should be in this list. {context}"
        )


def validate_instruments_config(
    instruments_config: Dict[str, Any],
) -> Tuple[bool, List[str]]:
    """
    Validate instruments configuration for common issues.

    Checks:
    1. All symbols are unique across sleeves
    2. All config IDs are unique
    3. No symbol matches another config ID (ambiguity)

    Args:
        instruments_config: The instruments configuration dict

    Returns:
        Tuple of (is_valid, list of error messages)
    """
    errors: List[str] = []

    # Collect all IDs and symbols
    all_config_ids: Set[str] = set()
    all_symbols: Set[str] = set()
    symbol_locations: Dict[str, List[str]] = defaultdict(list)
    id_locations: Dict[str, List[str]] = defaultdict(list)

    for sleeve, instruments in instruments_config.items():
        if not isinstance(instruments, dict):
            continue

        for inst_id, spec in instruments.items():
            # Track config ID
            if inst_id in all_config_ids:
                errors.append(f"Duplicate config ID '{inst_id}' in sleeve '{sleeve}'")
            all_config_ids.add(inst_id)
            id_locations[inst_id].append(sleeve)

            # Track symbol
            if isinstance(spec, dict):
                symbol = spec.get("symbol", inst_id)
                if symbol in all_symbols and symbol != inst_id:
                    errors.append(
                        f"Duplicate symbol '{symbol}' in sleeve '{sleeve}' "
                        f"(previously in {symbol_locations[symbol]})"
                    )
                all_symbols.add(symbol)
                symbol_locations[symbol].append(f"{sleeve}/{inst_id}")

    # Check for ambiguity: symbol matching config ID of different instrument
    for symbol in all_symbols:
        if symbol in all_config_ids:
            # Find which config ID this symbol belongs to
            for sleeve, instruments in instruments_config.items():
                if not isinstance(instruments, dict):
                    continue
                for inst_id, spec in instruments.items():
                    if isinstance(spec, dict) and spec.get("symbol") == symbol:
                        if inst_id != symbol:
                            errors.append(
                                f"Symbol '{symbol}' matches config ID of different "
                                f"instrument (belongs to '{inst_id}'). "
                                f"This can cause ID mapping confusion."
                            )

    return len(errors) == 0, errors


def build_id_mappings(
    instruments_config: Dict[str, Any],
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """
    Build bidirectional mappings between config IDs and IBKR symbols.

    Args:
        instruments_config: The instruments configuration dict

    Returns:
        Tuple of (config_id_to_symbol, symbol_to_config_id)
    """
    config_id_to_symbol: Dict[str, str] = {}
    symbol_to_config_id: Dict[str, str] = {}

    for sleeve, instruments in instruments_config.items():
        if not isinstance(instruments, dict):
            continue
        for inst_id, spec in instruments.items():
            if isinstance(spec, dict):
                symbol = spec.get("symbol", inst_id)
            else:
                symbol = inst_id

            config_id_to_symbol[inst_id] = symbol
            symbol_to_config_id[symbol] = inst_id

    return config_id_to_symbol, symbol_to_config_id


def assert_all_positions_synced(
    internal_positions: Dict[str, Any],
    broker_positions: Dict[str, Any],
    tolerance: float = 0.01,
    context: str = "",
) -> None:
    """
    Verify internal positions match broker positions.

    Args:
        internal_positions: Portfolio positions dict
        broker_positions: Positions from IBKR
        tolerance: Allowed quantity difference (for rounding)
        context: Additional context for error messages

    Raises:
        InvariantError: If positions don't match
    """
    internal_set = set(internal_positions.keys())
    broker_set = set(broker_positions.keys())

    missing_internal = broker_set - internal_set
    missing_broker = internal_set - broker_set

    if missing_internal:
        raise InvariantError(
            f"Broker positions not in internal state: {missing_internal}. "
            f"Position sync may have failed. {context}"
        )

    if missing_broker:
        # This is less critical - could be closed positions
        logger.warning(
            f"Internal positions not at broker: {missing_broker}. {context}"
        )

    # Check quantities match
    mismatches = []
    for inst_id in internal_set & broker_set:
        internal_qty = getattr(internal_positions[inst_id], "quantity", 0)
        broker_qty = getattr(broker_positions[inst_id], "quantity", 0)

        if abs(internal_qty - broker_qty) > tolerance:
            mismatches.append(
                f"{inst_id}: internal={internal_qty}, broker={broker_qty}"
            )

    if mismatches:
        raise InvariantError(
            f"Position quantity mismatches: {mismatches}. {context}"
        )
