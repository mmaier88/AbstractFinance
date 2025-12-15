"""
Automatic Futures Rollover Module for AbstractFinance.

Detects expiring futures positions and automatically rolls them to the next contract.
Integrates with the daily scheduler to run before strategy execution.
"""

import re
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple, NamedTuple
from dataclasses import dataclass

try:
    from ib_insync import IB, Future, MarketOrder, Position as IBPosition
    IB_AVAILABLE = True
except ImportError:
    IB_AVAILABLE = False

from .logging_utils import TradingLogger, get_trading_logger
from .alerts import AlertManager, AlertType, AlertSeverity


# Futures expiry cycle definitions
QUARTERLY_MONTHS = [3, 6, 9, 12]  # H, M, U, Z
MONTHLY_MONTHS = list(range(1, 13))  # F, G, H, J, K, M, N, Q, U, V, X, Z

# Month code mapping
MONTH_CODES = {
    1: 'F', 2: 'G', 3: 'H', 4: 'J', 5: 'K', 6: 'M',
    7: 'N', 8: 'Q', 9: 'U', 10: 'V', 11: 'X', 12: 'Z',
}

# Futures with their expiry cycles and exchanges
FUTURES_CONFIG = {
    # FX Futures (Quarterly)
    'M6E': {'cycle': 'quarterly', 'exchange': 'CME', 'name': 'Micro EUR/USD'},
    '6E': {'cycle': 'quarterly', 'exchange': 'CME', 'name': 'EUR/USD'},
    'M6B': {'cycle': 'quarterly', 'exchange': 'CME', 'name': 'Micro GBP/USD'},
    '6B': {'cycle': 'quarterly', 'exchange': 'CME', 'name': 'GBP/USD'},
    'M6J': {'cycle': 'quarterly', 'exchange': 'CME', 'name': 'Micro JPY/USD'},
    '6J': {'cycle': 'quarterly', 'exchange': 'CME', 'name': 'JPY/USD'},

    # Equity Index Futures (Quarterly)
    'ES': {'cycle': 'quarterly', 'exchange': 'CME', 'name': 'E-mini S&P 500'},
    'MES': {'cycle': 'quarterly', 'exchange': 'CME', 'name': 'Micro E-mini S&P 500'},
    'NQ': {'cycle': 'quarterly', 'exchange': 'CME', 'name': 'E-mini NASDAQ'},
    'MNQ': {'cycle': 'quarterly', 'exchange': 'CME', 'name': 'Micro E-mini NASDAQ'},
    'FESX': {'cycle': 'quarterly', 'exchange': 'EUREX', 'name': 'Euro STOXX 50'},
    'FDAX': {'cycle': 'quarterly', 'exchange': 'EUREX', 'name': 'DAX'},

    # Bond Futures (Quarterly)
    'FGBL': {'cycle': 'quarterly', 'exchange': 'EUREX', 'name': 'Euro-Bund'},
    'FOAT': {'cycle': 'quarterly', 'exchange': 'EUREX', 'name': 'French OAT'},
    'FBTP': {'cycle': 'quarterly', 'exchange': 'EUREX', 'name': 'Italian BTP'},
    'ZN': {'cycle': 'quarterly', 'exchange': 'CBOT', 'name': '10-Year T-Note'},
    'ZB': {'cycle': 'quarterly', 'exchange': 'CBOT', 'name': '30-Year T-Bond'},

    # Volatility Futures (MONTHLY - needs more frequent rolling!)
    'VX': {'cycle': 'monthly', 'exchange': 'CFE', 'name': 'VIX'},
    'FVS': {'cycle': 'monthly', 'exchange': 'EUREX', 'name': 'VSTOXX Mini'},

    # CAC (Monthly)
    'FCE': {'cycle': 'monthly', 'exchange': 'MONEP', 'name': 'CAC 40'},
}


@dataclass
class RolloverCandidate:
    """A futures position that needs to be rolled."""
    symbol: str
    local_symbol: str
    expiry: str
    days_to_expiry: int
    quantity: float
    avg_cost: float
    contract: any  # IB Future contract
    next_expiry: str
    next_local_symbol: str
    exchange: str


@dataclass
class RolloverResult:
    """Result of a rollover operation."""
    symbol: str
    old_contract: str
    new_contract: str
    quantity: float
    close_fill_price: Optional[float] = None
    open_fill_price: Optional[float] = None
    success: bool = False
    error_message: Optional[str] = None


class FuturesRolloverManager:
    """
    Manages automatic futures rollover.

    Scans positions for expiring futures and rolls them to the next contract.
    """

    def __init__(
        self,
        ib: IB,
        days_before_expiry: int = 3,
        logger: Optional[TradingLogger] = None,
        alert_manager: Optional[AlertManager] = None,
        dry_run: bool = False
    ):
        """
        Initialize the rollover manager.

        Args:
            ib: Connected IB client
            days_before_expiry: Roll futures this many days before expiry
            logger: Trading logger instance
            alert_manager: Alert manager for notifications
            dry_run: If True, only detect and report, don't execute
        """
        self.ib = ib
        self.days_before_expiry = days_before_expiry
        self.logger = logger or get_trading_logger()
        self.alert_manager = alert_manager
        self.dry_run = dry_run

    def get_next_contract_expiry(self, symbol: str, current_expiry: str) -> Tuple[str, str]:
        """
        Calculate the next contract expiry for a futures symbol.

        Args:
            symbol: Futures symbol (e.g., 'M6E')
            current_expiry: Current expiry in YYYYMM or YYYYMMDD format

        Returns:
            Tuple of (next_expiry_YYYYMM, next_local_symbol)
        """
        config = FUTURES_CONFIG.get(symbol, {'cycle': 'quarterly'})
        cycle = config['cycle']

        # Parse current expiry
        if len(current_expiry) == 8:  # YYYYMMDD
            current_year = int(current_expiry[:4])
            current_month = int(current_expiry[4:6])
        else:  # YYYYMM
            current_year = int(current_expiry[:4])
            current_month = int(current_expiry[4:6])

        # Determine valid months
        if cycle == 'monthly':
            valid_months = MONTHLY_MONTHS
        else:  # quarterly
            valid_months = QUARTERLY_MONTHS

        # Find next valid month
        next_month = None
        next_year = current_year

        for month in valid_months:
            if month > current_month:
                next_month = month
                break

        if next_month is None:
            # Roll to next year
            next_month = valid_months[0]
            next_year = current_year + 1

        next_expiry = f"{next_year}{next_month:02d}"
        month_code = MONTH_CODES[next_month]
        next_local_symbol = f"{symbol}{month_code}{next_year % 10}"

        return next_expiry, next_local_symbol

    def calculate_days_to_expiry(self, expiry_str: str) -> int:
        """Calculate days until contract expiry."""
        today = date.today()

        if len(expiry_str) == 8:  # YYYYMMDD
            expiry_date = date(
                int(expiry_str[:4]),
                int(expiry_str[4:6]),
                int(expiry_str[6:8])
            )
        else:  # YYYYMM - assume 3rd Friday
            year = int(expiry_str[:4])
            month = int(expiry_str[4:6])
            # Find 3rd Friday (approximate expiry)
            first_day = date(year, month, 1)
            # Find first Friday
            days_until_friday = (4 - first_day.weekday()) % 7
            first_friday = first_day + timedelta(days=days_until_friday)
            # Third Friday
            third_friday = first_friday + timedelta(days=14)
            expiry_date = third_friday

        return (expiry_date - today).days

    def find_expiring_positions(self) -> List[RolloverCandidate]:
        """
        Find all futures positions that are expiring soon.

        Returns:
            List of RolloverCandidate objects
        """
        candidates = []
        positions = self.ib.positions()

        for pos in positions:
            contract = pos.contract

            # Only process futures
            if contract.secType != 'FUT':
                continue

            symbol = contract.symbol
            expiry = contract.lastTradeDateOrContractMonth
            days_to_expiry = self.calculate_days_to_expiry(expiry)

            # Check if within rollover window
            if days_to_expiry <= self.days_before_expiry:
                config = FUTURES_CONFIG.get(symbol, {})
                exchange = config.get('exchange', contract.exchange or 'CME')

                next_expiry, next_local_symbol = self.get_next_contract_expiry(symbol, expiry)

                candidate = RolloverCandidate(
                    symbol=symbol,
                    local_symbol=contract.localSymbol,
                    expiry=expiry,
                    days_to_expiry=days_to_expiry,
                    quantity=pos.position,
                    avg_cost=pos.avgCost,
                    contract=contract,
                    next_expiry=next_expiry,
                    next_local_symbol=next_local_symbol,
                    exchange=exchange
                )
                candidates.append(candidate)

                self.logger.logger.info(
                    "rollover_candidate_found",
                    symbol=symbol,
                    local_symbol=contract.localSymbol,
                    days_to_expiry=days_to_expiry,
                    quantity=pos.position,
                    next_contract=next_local_symbol
                )

        return candidates

    def execute_rollover(self, candidate: RolloverCandidate) -> RolloverResult:
        """
        Execute a single futures rollover.

        Args:
            candidate: The rollover candidate

        Returns:
            RolloverResult with execution details
        """
        result = RolloverResult(
            symbol=candidate.symbol,
            old_contract=candidate.local_symbol,
            new_contract=candidate.next_local_symbol,
            quantity=candidate.quantity
        )

        if self.dry_run:
            self.logger.logger.info(
                "rollover_dry_run",
                symbol=candidate.symbol,
                old=candidate.local_symbol,
                new=candidate.next_local_symbol,
                quantity=candidate.quantity
            )
            result.success = True
            return result

        try:
            # Create the new contract
            new_contract = Future(
                symbol=candidate.symbol,
                exchange=candidate.exchange,
                lastTradeDateOrContractMonth=candidate.next_expiry,
                currency=candidate.contract.currency
            )

            # Qualify the new contract
            self.ib.qualifyContracts(new_contract)

            abs_qty = abs(candidate.quantity)

            # Determine order directions
            if candidate.quantity < 0:  # Short position
                close_action = "BUY"
                open_action = "SELL"
            else:  # Long position
                close_action = "SELL"
                open_action = "BUY"

            # Step 1: Close the old position
            self.logger.logger.info(
                "rollover_closing_position",
                action=close_action,
                quantity=abs_qty,
                contract=candidate.local_symbol
            )

            close_order = MarketOrder(close_action, abs_qty)
            close_order.tif = "GTC"
            close_trade = self.ib.placeOrder(candidate.contract, close_order)

            # Wait for fill (up to 60 seconds)
            timeout = 60
            start_time = datetime.now()
            while not close_trade.isDone():
                self.ib.sleep(1)
                if (datetime.now() - start_time).seconds > timeout:
                    break

            if close_trade.orderStatus.status == "Filled":
                result.close_fill_price = close_trade.orderStatus.avgFillPrice
                self.logger.logger.info(
                    "rollover_close_filled",
                    price=result.close_fill_price
                )
            else:
                result.error_message = f"Close order not filled: {close_trade.orderStatus.status}"
                self.logger.logger.error("rollover_close_failed", status=close_trade.orderStatus.status)
                return result

            # Step 2: Open the new position
            self.logger.logger.info(
                "rollover_opening_position",
                action=open_action,
                quantity=abs_qty,
                contract=candidate.next_local_symbol
            )

            open_order = MarketOrder(open_action, abs_qty)
            open_order.tif = "GTC"
            open_trade = self.ib.placeOrder(new_contract, open_order)

            # Wait for fill
            start_time = datetime.now()
            while not open_trade.isDone():
                self.ib.sleep(1)
                if (datetime.now() - start_time).seconds > timeout:
                    break

            if open_trade.orderStatus.status == "Filled":
                result.open_fill_price = open_trade.orderStatus.avgFillPrice
                result.success = True
                self.logger.logger.info(
                    "rollover_open_filled",
                    price=result.open_fill_price
                )
            else:
                result.error_message = f"Open order not filled: {open_trade.orderStatus.status}"
                self.logger.logger.error("rollover_open_failed", status=open_trade.orderStatus.status)

        except Exception as e:
            result.error_message = str(e)
            self.logger.logger.error("rollover_error", error=str(e))

        return result

    def run_rollover_check(self) -> List[RolloverResult]:
        """
        Run the full rollover check and execute any needed rollovers.

        Returns:
            List of RolloverResult objects
        """
        results = []

        self.logger.logger.info("rollover_check_start", days_threshold=self.days_before_expiry)

        # Find expiring positions
        candidates = self.find_expiring_positions()

        if not candidates:
            self.logger.logger.info("rollover_check_complete", candidates_found=0)
            return results

        self.logger.logger.info("rollover_candidates_found", count=len(candidates))

        # Send alert about upcoming rollovers
        if self.alert_manager:
            candidate_list = "\n".join([
                f"  - {c.local_symbol} → {c.next_local_symbol} ({c.days_to_expiry}d, qty={c.quantity})"
                for c in candidates
            ])

            self.alert_manager.send_alert(
                alert_type=AlertType.SYSTEM,
                severity=AlertSeverity.WARNING,
                title="🔄 Futures Rollover Required",
                message=f"Found {len(candidates)} expiring futures:\n{candidate_list}\n\n"
                        f"{'DRY RUN - no action taken' if self.dry_run else 'Executing rollovers...'}",
                details={"candidates": len(candidates), "dry_run": self.dry_run}
            )

        # Execute rollovers
        for candidate in candidates:
            result = self.execute_rollover(candidate)
            results.append(result)

            # Send result alert
            if self.alert_manager and not self.dry_run:
                if result.success:
                    self.alert_manager.send_alert(
                        alert_type=AlertType.TRADE,
                        severity=AlertSeverity.INFO,
                        title="✅ Futures Rollover Complete",
                        message=f"Rolled {result.old_contract} → {result.new_contract}\n"
                                f"Quantity: {result.quantity}\n"
                                f"Close: ${result.close_fill_price:.5f}\n"
                                f"Open: ${result.open_fill_price:.5f}",
                        details={"symbol": result.symbol, "quantity": result.quantity}
                    )
                else:
                    self.alert_manager.send_alert(
                        alert_type=AlertType.TRADE,
                        severity=AlertSeverity.ERROR,
                        title="❌ Futures Rollover Failed",
                        message=f"Failed to roll {result.old_contract}\n"
                                f"Error: {result.error_message}",
                        details={"symbol": result.symbol, "error": result.error_message}
                    )

        self.logger.logger.info(
            "rollover_check_complete",
            total=len(results),
            successful=sum(1 for r in results if r.success),
            failed=sum(1 for r in results if not r.success)
        )

        return results


def check_and_roll_futures(
    ib: IB,
    days_before_expiry: int = 3,
    logger: Optional[TradingLogger] = None,
    alert_manager: Optional[AlertManager] = None,
    dry_run: bool = False
) -> List[RolloverResult]:
    """
    Convenience function to check and roll expiring futures.

    Args:
        ib: Connected IB client
        days_before_expiry: Roll futures this many days before expiry
        logger: Trading logger instance
        alert_manager: Alert manager for notifications
        dry_run: If True, only detect and report, don't execute

    Returns:
        List of RolloverResult objects
    """
    manager = FuturesRolloverManager(
        ib=ib,
        days_before_expiry=days_before_expiry,
        logger=logger,
        alert_manager=alert_manager,
        dry_run=dry_run
    )

    return manager.run_rollover_check()
