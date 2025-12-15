#!/usr/bin/env python3
"""
Futures Contract Rollover Script for AbstractFinance.

This script handles the rollover of expiring futures contracts by:
1. Closing the position in the expiring contract
2. Opening a new position in the next contract month

Usage:
    python3 scripts/rollover_futures.py [--dry-run] [--symbol M6E]

For M6E (Micro EUR/USD), quarterly contracts are: H (Mar), M (Jun), U (Sep), Z (Dec)
"""

import argparse
import sys
import time
from datetime import date, datetime
from typing import Optional, Tuple

try:
    from ib_insync import IB, Future, MarketOrder
except ImportError:
    print("ERROR: ib_insync not installed. Run: pip install ib_insync")
    sys.exit(1)


# CME FX futures month codes (quarterly)
FX_FUTURE_MONTHS = {
    3: 'H',   # March
    6: 'M',   # June
    9: 'U',   # September
    12: 'Z',  # December
}

# All futures month codes
ALL_MONTH_CODES = {
    1: 'F', 2: 'G', 3: 'H', 4: 'J', 5: 'K', 6: 'M',
    7: 'N', 8: 'Q', 9: 'U', 10: 'V', 11: 'X', 12: 'Z',
}


def get_next_quarterly_contract(current_month: int, current_year: int) -> Tuple[int, int]:
    """Get the next quarterly contract month and year."""
    quarterly_months = [3, 6, 9, 12]

    for month in quarterly_months:
        if month > current_month:
            return month, current_year

    # Next year's March
    return 3, current_year + 1


def get_contract_expiry(symbol: str, year: int, month: int) -> str:
    """
    Get the contract expiry string in YYYYMMDD format.

    For CME FX futures, expiry is typically the third Wednesday of the contract month,
    with last trading day being 2 business days prior (usually Monday).
    """
    # For simplicity, return YYYYMM format which IBKR accepts
    return f"{year}{month:02d}"


def create_future_contract(symbol: str, expiry: str, exchange: str = "CME") -> Future:
    """Create a Future contract object."""
    contract = Future(
        symbol=symbol,
        exchange=exchange,
        lastTradeDateOrContractMonth=expiry,
        currency="USD"
    )
    return contract


def rollover_position(
    ib: IB,
    symbol: str,
    exchange: str = "CME",
    dry_run: bool = True
) -> bool:
    """
    Roll over a futures position from expiring contract to next contract.

    Args:
        ib: Connected IB client
        symbol: Futures symbol (e.g., "M6E")
        exchange: Exchange (default: "CME")
        dry_run: If True, only simulate the rollover

    Returns:
        True if successful, False otherwise
    """
    print(f"\n{'='*60}")
    print(f"FUTURES ROLLOVER: {symbol}")
    print(f"{'='*60}")
    print(f"Mode: {'DRY RUN (simulation)' if dry_run else 'LIVE EXECUTION'}")
    print(f"Time: {datetime.now()}")

    # Find existing position in this symbol
    positions = ib.positions()
    existing_pos = None

    for pos in positions:
        if pos.contract.symbol == symbol and pos.contract.secType == "FUT":
            existing_pos = pos
            break

    if not existing_pos:
        print(f"\nNo existing position found for {symbol}")
        return False

    contract = existing_pos.contract
    quantity = existing_pos.position

    print(f"\nExisting Position:")
    print(f"  Contract: {contract.localSymbol}")
    print(f"  Expiry: {contract.lastTradeDateOrContractMonth}")
    print(f"  Quantity: {quantity} (negative = short)")
    print(f"  Avg Cost: ${existing_pos.avgCost:.4f}")

    # Parse expiry to determine next contract
    expiry_str = contract.lastTradeDateOrContractMonth
    if len(expiry_str) == 8:  # YYYYMMDD
        current_year = int(expiry_str[:4])
        current_month = int(expiry_str[4:6])
    elif len(expiry_str) == 6:  # YYYYMM
        current_year = int(expiry_str[:4])
        current_month = int(expiry_str[4:6])
    else:
        print(f"ERROR: Cannot parse expiry: {expiry_str}")
        return False

    # Calculate next contract
    next_month, next_year = get_next_quarterly_contract(current_month, current_year)
    next_expiry = get_contract_expiry(symbol, next_year, next_month)

    month_code = FX_FUTURE_MONTHS.get(next_month, ALL_MONTH_CODES.get(next_month, '?'))
    next_local_symbol = f"{symbol}{month_code}{next_year % 10}"

    print(f"\nNext Contract:")
    print(f"  Symbol: {next_local_symbol}")
    print(f"  Expiry: {next_expiry}")

    # Create contracts
    old_contract = Future(
        conId=contract.conId,  # Use existing conId
        symbol=symbol,
        exchange=exchange,
        lastTradeDateOrContractMonth=expiry_str,
        currency="USD"
    )

    new_contract = create_future_contract(symbol, next_expiry, exchange)

    # Qualify the new contract
    try:
        ib.qualifyContracts(new_contract)
        print(f"\nNew contract qualified:")
        print(f"  ConId: {new_contract.conId}")
        print(f"  LocalSymbol: {new_contract.localSymbol}")
        print(f"  Multiplier: {new_contract.multiplier}")
    except Exception as e:
        print(f"ERROR: Failed to qualify new contract: {e}")
        return False

    # Determine order actions
    # If we're short (negative quantity), we need to:
    # 1. BUY to close the old position
    # 2. SELL to open the new position
    abs_qty = abs(quantity)

    if quantity < 0:  # Short position
        close_action = "BUY"
        open_action = "SELL"
    else:  # Long position
        close_action = "SELL"
        open_action = "BUY"

    print(f"\nRollover Plan:")
    print(f"  Step 1: {close_action} {abs_qty} {contract.localSymbol} (close)")
    print(f"  Step 2: {open_action} {abs_qty} {new_contract.localSymbol} (open)")

    if dry_run:
        print(f"\n*** DRY RUN - No orders placed ***")
        print(f"To execute, run with: --execute")
        return True

    # Execute the rollover
    print(f"\nExecuting rollover...")

    # Step 1: Close the old position
    print(f"\nStep 1: Closing {contract.localSymbol}...")
    close_order = MarketOrder(close_action, abs_qty)
    close_order.tif = "GTC"  # Good Till Cancelled

    close_trade = ib.placeOrder(old_contract, close_order)

    # Wait for fill
    timeout = 30
    start_time = time.time()
    while not close_trade.isDone() and (time.time() - start_time) < timeout:
        ib.sleep(1)

    if close_trade.orderStatus.status == "Filled":
        fill_price = close_trade.orderStatus.avgFillPrice
        print(f"  FILLED at ${fill_price:.5f}")
    else:
        print(f"  Status: {close_trade.orderStatus.status}")
        print(f"  WARNING: Close order not filled immediately")
        if close_trade.orderStatus.status in ["Cancelled", "Inactive"]:
            print(f"  ERROR: Order failed - {close_trade.log[-1].message if close_trade.log else 'unknown'}")
            return False

    # Step 2: Open new position
    print(f"\nStep 2: Opening {new_contract.localSymbol}...")
    open_order = MarketOrder(open_action, abs_qty)
    open_order.tif = "GTC"

    open_trade = ib.placeOrder(new_contract, open_order)

    # Wait for fill
    start_time = time.time()
    while not open_trade.isDone() and (time.time() - start_time) < timeout:
        ib.sleep(1)

    if open_trade.orderStatus.status == "Filled":
        fill_price = open_trade.orderStatus.avgFillPrice
        print(f"  FILLED at ${fill_price:.5f}")
    else:
        print(f"  Status: {open_trade.orderStatus.status}")
        print(f"  WARNING: Open order not filled immediately")

    print(f"\n{'='*60}")
    print(f"ROLLOVER COMPLETE")
    print(f"{'='*60}")

    return True


def main():
    parser = argparse.ArgumentParser(description="Futures contract rollover script")
    parser.add_argument("--symbol", default="M6E", help="Futures symbol (default: M6E)")
    parser.add_argument("--exchange", default="CME", help="Exchange (default: CME)")
    parser.add_argument("--execute", action="store_true", help="Execute the rollover (default: dry run)")
    parser.add_argument("--host", default="127.0.0.1", help="IB Gateway host")
    parser.add_argument("--port", type=int, default=4000, help="IB Gateway port")
    parser.add_argument("--client-id", type=int, default=99, help="Client ID")

    args = parser.parse_args()

    dry_run = not args.execute

    print("="*60)
    print("ABSTRACTFINANCE FUTURES ROLLOVER")
    print("="*60)
    print(f"Symbol: {args.symbol}")
    print(f"Exchange: {args.exchange}")
    print(f"Mode: {'LIVE EXECUTION' if args.execute else 'DRY RUN'}")
    print()

    # Connect to IB Gateway
    print(f"Connecting to IB Gateway at {args.host}:{args.port}...")
    ib = IB()

    try:
        ib.connect(args.host, args.port, clientId=args.client_id)
        print("Connected successfully")

        # Get account info
        account_values = ib.accountSummary()
        for av in account_values:
            if av.tag == "NetLiquidation":
                print(f"Account Net Liquidation: ${float(av.value):,.2f}")
                break

        # Perform rollover
        success = rollover_position(
            ib=ib,
            symbol=args.symbol,
            exchange=args.exchange,
            dry_run=dry_run
        )

        if success:
            print("\nRollover completed successfully!")
        else:
            print("\nRollover failed or no action needed")
            sys.exit(1)

    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    finally:
        ib.disconnect()
        print("\nDisconnected from IB Gateway")


if __name__ == "__main__":
    main()
