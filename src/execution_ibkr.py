"""
IBKR execution engine for AbstractFinance.
Handles order placement, position management, and account data via ib_insync.
"""

import time
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum

try:
    from ib_insync import (
        IB, Contract, Stock, Future, Forex, Option,
        MarketOrder, LimitOrder, StopOrder, Order,
        Trade, Fill, Position as IBPosition
    )
    IB_AVAILABLE = True
except ImportError:
    IB_AVAILABLE = False

from .strategy_logic import OrderSpec
from .portfolio import Position, Sleeve
from .logging_utils import TradingLogger, get_trading_logger


class OrderStatus(Enum):
    """Order status states."""
    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    ERROR = "error"


@dataclass
class ExecutionReport:
    """Report of order execution results."""
    order_id: str
    instrument_id: str
    status: OrderStatus
    filled_qty: float = 0.0
    avg_fill_price: float = 0.0
    commission: float = 0.0
    fill_time: Optional[datetime] = None
    error_message: Optional[str] = None
    ib_order_id: Optional[int] = None


@dataclass
class AccountSummary:
    """IB account summary data."""
    account_id: str
    net_liquidation: float = 0.0
    total_cash: float = 0.0
    buying_power: float = 0.0
    gross_position_value: float = 0.0
    maintenance_margin: float = 0.0
    available_funds: float = 0.0
    excess_liquidity: float = 0.0
    currency: str = "USD"


class IBClient:
    """
    Interactive Brokers client wrapper using ib_insync.
    Handles connection, order management, and position queries.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 4002,
        client_id: int = 1,
        timeout: int = 30,
        readonly: bool = False,
        logger: Optional[TradingLogger] = None
    ):
        """
        Initialize IB client.

        Args:
            host: IB Gateway host
            port: IB Gateway port (4001=live, 4002=paper)
            client_id: Unique client identifier
            timeout: Connection timeout in seconds
            readonly: If True, don't place orders
            logger: Trading logger instance
        """
        if not IB_AVAILABLE:
            raise ImportError("ib_insync is required for IBKR integration")

        self.host = host
        self.port = port
        self.client_id = client_id
        self.timeout = timeout
        self.readonly = readonly
        self.logger = logger or get_trading_logger()

        self.ib = IB()
        self._connected = False
        self._instruments_cache: Dict[str, Contract] = {}
        self._pending_orders: Dict[str, Trade] = {}

    def connect(self) -> bool:
        """
        Connect to IB Gateway.

        Returns:
            True if connection successful
        """
        try:
            self.ib.connect(
                host=self.host,
                port=self.port,
                clientId=self.client_id,
                timeout=self.timeout,
                readonly=self.readonly
            )
            self._connected = self.ib.isConnected()

            self.logger.log_connection_event(
                event_type="connect",
                host=self.host,
                port=self.port,
                success=self._connected
            )

            return self._connected

        except Exception as e:
            self.logger.log_connection_event(
                event_type="connect",
                host=self.host,
                port=self.port,
                success=False,
                error_message=str(e)
            )
            return False

    def disconnect(self) -> None:
        """Disconnect from IB Gateway."""
        if self._connected:
            self.ib.disconnect()
            self._connected = False
            self.logger.log_connection_event(
                event_type="disconnect",
                host=self.host,
                port=self.port,
                success=True
            )

    def is_connected(self) -> bool:
        """Check if connected to IB Gateway."""
        return self.ib.isConnected()

    def get_account_summary(self) -> AccountSummary:
        """
        Get account summary information.

        Returns:
            AccountSummary with account data
        """
        if not self.is_connected():
            raise ConnectionError("Not connected to IB Gateway")

        account_values = self.ib.accountSummary()
        summary = AccountSummary(account_id=self.ib.managedAccounts()[0] if self.ib.managedAccounts() else "")

        for av in account_values:
            if av.tag == "NetLiquidation":
                summary.net_liquidation = float(av.value)
            elif av.tag == "TotalCashValue":
                summary.total_cash = float(av.value)
            elif av.tag == "BuyingPower":
                summary.buying_power = float(av.value)
            elif av.tag == "GrossPositionValue":
                summary.gross_position_value = float(av.value)
            elif av.tag == "MaintMarginReq":
                summary.maintenance_margin = float(av.value)
            elif av.tag == "AvailableFunds":
                summary.available_funds = float(av.value)
            elif av.tag == "ExcessLiquidity":
                summary.excess_liquidity = float(av.value)

        return summary

    def get_positions(self) -> Dict[str, Position]:
        """
        Get current positions.

        Returns:
            Dict mapping instrument_id to Position
        """
        if not self.is_connected():
            raise ConnectionError("Not connected to IB Gateway")

        positions = {}
        ib_positions = self.ib.positions()

        for ib_pos in ib_positions:
            contract = ib_pos.contract
            instrument_id = self._contract_to_instrument_id(contract)

            position = Position(
                instrument_id=instrument_id,
                quantity=ib_pos.position,
                avg_cost=ib_pos.avgCost,
                market_price=ib_pos.avgCost,  # Will be updated with market data
                multiplier=float(contract.multiplier) if contract.multiplier else 1.0,
                currency=contract.currency
            )

            # Try to get current market price
            try:
                ticker = self.ib.reqMktData(contract, '', False, False)
                self.ib.sleep(1)
                if ticker.last and ticker.last > 0:
                    position.market_price = ticker.last
                elif ticker.close and ticker.close > 0:
                    position.market_price = ticker.close
                self.ib.cancelMktData(contract)
            except Exception:
                pass

            positions[instrument_id] = position

            self.logger.log_position(
                instrument_id=instrument_id,
                quantity=position.quantity,
                avg_cost=position.avg_cost,
                market_value=position.market_value,
                unrealized_pnl=position.unrealized_pnl
            )

        return positions

    def _contract_to_instrument_id(self, contract: Any) -> str:
        """Convert IB contract to instrument ID."""
        if contract.secType == "STK":
            return f"{contract.symbol}"
        elif contract.secType == "FUT":
            return f"{contract.symbol}_{contract.lastTradeDateOrContractMonth}"
        elif contract.secType == "CASH":
            return f"{contract.symbol}{contract.currency}"
        elif contract.secType == "OPT":
            return f"{contract.symbol}_{contract.strike}_{contract.right}_{contract.lastTradeDateOrContractMonth}"
        return contract.symbol

    def build_contract(
        self,
        instrument_id: str,
        instruments_config: Dict[str, Any]
    ) -> Optional[Any]:
        """
        Build IB contract from instrument configuration.

        Args:
            instrument_id: Internal instrument identifier
            instruments_config: Instrument configuration dict

        Returns:
            IB Contract object
        """
        # Check cache
        if instrument_id in self._instruments_cache:
            return self._instruments_cache[instrument_id]

        # Find instrument spec in config
        spec = self._find_instrument_spec(instrument_id, instruments_config)
        if not spec:
            return None

        contract = None
        sec_type = spec.get('sec_type', 'STK')
        symbol = spec.get('symbol', instrument_id)
        exchange = spec.get('exchange', 'SMART')
        currency = spec.get('currency', 'USD')

        if sec_type == 'STK':
            # For European exchanges, use SMART routing with correct primaryExchange
            # IBKR uses LSEETF for LSE ETFs, IBIS2 for XETRA
            primary_exchange_map = {
                'LSE': 'LSEETF',
                'XETRA': 'IBIS',  # Try IBIS instead of IBIS2
                'SBF': 'SBF',
                'IBIS': 'IBIS',
            }
            if exchange in primary_exchange_map:
                primary = primary_exchange_map[exchange]
                contract = Stock(symbol, 'SMART', currency, primaryExchange=primary)
            else:
                contract = Stock(symbol, exchange, currency)

        elif sec_type == 'FUT':
            # Calculate front month expiry (YYYYMM format)
            from datetime import date
            today = date.today()
            # Use next month if we're past the 15th, otherwise current month
            if today.day > 15:
                month = today.month + 1
                year = today.year
                if month > 12:
                    month = 1
                    year += 1
            else:
                month = today.month
                year = today.year
            expiry = f"{year}{month:02d}"
            contract = Future(symbol, exchange=exchange, lastTradeDateOrContractMonth=expiry)

        elif sec_type == 'CASH':
            # Forex
            contract = Forex(symbol + currency)

        elif sec_type == 'OPT':
            # Options require more parameters - handle separately
            underlying = spec.get('underlying', symbol)
            contract = Option(underlying, exchange=exchange)

        if contract:
            try:
                self.ib.qualifyContracts(contract)
                self._instruments_cache[instrument_id] = contract
            except Exception:
                pass

        return contract

    def _find_instrument_spec(
        self,
        instrument_id: str,
        instruments_config: Dict
    ) -> Optional[Dict]:
        """Find instrument specification in config."""
        for category, instruments in instruments_config.items():
            if isinstance(instruments, dict):
                if instrument_id in instruments:
                    return instruments[instrument_id]
                for inst_key, spec in instruments.items():
                    if isinstance(spec, dict) and spec.get('symbol') == instrument_id:
                        return spec
        return None

    def place_order(
        self,
        order_spec: OrderSpec,
        instruments_config: Dict[str, Any]
    ) -> ExecutionReport:
        """
        Place a single order.

        Args:
            order_spec: Order specification
            instruments_config: Instrument configuration

        Returns:
            ExecutionReport with results
        """
        if not self.is_connected():
            return ExecutionReport(
                order_id=str(id(order_spec)),
                instrument_id=order_spec.instrument_id,
                status=OrderStatus.ERROR,
                error_message="Not connected to IB Gateway"
            )

        if self.readonly:
            return ExecutionReport(
                order_id=str(id(order_spec)),
                instrument_id=order_spec.instrument_id,
                status=OrderStatus.REJECTED,
                error_message="Client is in readonly mode"
            )

        # Build contract
        contract = self.build_contract(order_spec.instrument_id, instruments_config)
        if not contract:
            return ExecutionReport(
                order_id=str(id(order_spec)),
                instrument_id=order_spec.instrument_id,
                status=OrderStatus.ERROR,
                error_message=f"Could not build contract for {order_spec.instrument_id}"
            )

        # Build order
        action = order_spec.side
        quantity = order_spec.quantity

        if order_spec.order_type == "MKT":
            order = MarketOrder(action, quantity)
        elif order_spec.order_type == "LMT":
            order = LimitOrder(action, quantity, order_spec.limit_price)
        elif order_spec.order_type == "STP":
            order = StopOrder(action, quantity, order_spec.stop_price)
        else:
            order = MarketOrder(action, quantity)

        # Log order submission
        order_id = str(id(order_spec))
        self.logger.log_order(
            order_id=order_id,
            instrument_id=order_spec.instrument_id,
            side=action,
            quantity=quantity,
            order_type=order_spec.order_type,
            price=order_spec.limit_price,
            status="submitted"
        )

        # Place order
        try:
            trade = self.ib.placeOrder(contract, order)
            self._pending_orders[order_id] = trade

            # Wait for fill (with timeout)
            timeout_seconds = 30
            start_time = time.time()

            while time.time() - start_time < timeout_seconds:
                self.ib.sleep(0.5)

                if trade.isDone():
                    break

            # Build execution report
            status = self._map_order_status(trade.orderStatus.status)
            filled_qty = trade.orderStatus.filled
            avg_price = trade.orderStatus.avgFillPrice

            # Calculate commission from fills
            commission = sum(fill.commissionReport.commission
                           for fill in trade.fills
                           if fill.commissionReport)

            report = ExecutionReport(
                order_id=order_id,
                instrument_id=order_spec.instrument_id,
                status=status,
                filled_qty=filled_qty,
                avg_fill_price=avg_price,
                commission=commission,
                fill_time=datetime.now() if filled_qty > 0 else None,
                ib_order_id=trade.order.orderId
            )

            # Log fill if completed
            if filled_qty > 0:
                self.logger.log_fill(
                    order_id=order_id,
                    instrument_id=order_spec.instrument_id,
                    side=action,
                    quantity=filled_qty,
                    fill_price=avg_price,
                    commission=commission
                )

            return report

        except Exception as e:
            return ExecutionReport(
                order_id=order_id,
                instrument_id=order_spec.instrument_id,
                status=OrderStatus.ERROR,
                error_message=str(e)
            )

    def place_orders(
        self,
        orders: List[OrderSpec],
        instruments_config: Dict[str, Any]
    ) -> List[ExecutionReport]:
        """
        Place multiple orders.

        Args:
            orders: List of order specifications
            instruments_config: Instrument configuration

        Returns:
            List of ExecutionReport
        """
        reports = []

        for order_spec in orders:
            report = self.place_order(order_spec, instruments_config)
            reports.append(report)

            # Small delay between orders
            if self.is_connected():
                self.ib.sleep(0.1)

        return reports

    def cancel_order(self, order_id: str) -> bool:
        """
        Cancel a pending order.

        Args:
            order_id: Order ID to cancel

        Returns:
            True if cancellation successful
        """
        if order_id not in self._pending_orders:
            return False

        trade = self._pending_orders[order_id]

        try:
            self.ib.cancelOrder(trade.order)
            self.ib.sleep(1)
            return trade.orderStatus.status == "Cancelled"
        except Exception:
            return False

    def cancel_all_orders(self) -> int:
        """
        Cancel all pending orders.

        Returns:
            Number of orders cancelled
        """
        cancelled = 0

        for order_id in list(self._pending_orders.keys()):
            if self.cancel_order(order_id):
                cancelled += 1

        return cancelled

    def _map_order_status(self, ib_status: str) -> OrderStatus:
        """Map IB order status to internal status."""
        status_map = {
            "PendingSubmit": OrderStatus.PENDING,
            "PreSubmitted": OrderStatus.PENDING,
            "Submitted": OrderStatus.SUBMITTED,
            "Filled": OrderStatus.FILLED,
            "PartiallyFilled": OrderStatus.PARTIAL,
            "Cancelled": OrderStatus.CANCELLED,
            "ApiCancelled": OrderStatus.CANCELLED,
            "Inactive": OrderStatus.REJECTED
        }
        return status_map.get(ib_status, OrderStatus.PENDING)

    def get_open_orders(self) -> List[Dict[str, Any]]:
        """Get list of open orders."""
        if not self.is_connected():
            return []

        open_orders = self.ib.openOrders()
        return [
            {
                "order_id": order.orderId,
                "symbol": order.contract.symbol if hasattr(order, 'contract') else "N/A",
                "action": order.action,
                "quantity": order.totalQuantity,
                "order_type": order.orderType,
                "status": order.status if hasattr(order, 'status') else "Unknown"
            }
            for order in open_orders
        ]

    def get_executions(self, days_back: int = 1) -> List[Dict[str, Any]]:
        """Get recent executions."""
        if not self.is_connected():
            return []

        executions = self.ib.executions()
        return [
            {
                "exec_id": ex.execId,
                "symbol": ex.contract.symbol,
                "side": ex.side,
                "quantity": ex.shares,
                "price": ex.price,
                "time": ex.time
            }
            for ex in executions
        ]


class ExecutionEngine:
    """
    High-level execution engine for strategy orders.
    Wraps IBClient with additional logic for order management.
    """

    def __init__(
        self,
        ib_client: IBClient,
        instruments_config: Dict[str, Any],
        logger: Optional[TradingLogger] = None
    ):
        """
        Initialize execution engine.

        Args:
            ib_client: Connected IB client
            instruments_config: Instrument configuration
            logger: Trading logger
        """
        self.ib_client = ib_client
        self.instruments_config = instruments_config
        self.logger = logger or get_trading_logger()

    def execute_strategy_orders(
        self,
        orders: List[OrderSpec],
        dry_run: bool = False
    ) -> Tuple[List[ExecutionReport], Dict[str, Any]]:
        """
        Execute strategy orders with validation.

        Args:
            orders: List of orders to execute
            dry_run: If True, validate but don't execute

        Returns:
            Tuple of (reports, summary_stats)
        """
        # Validate orders
        valid_orders, invalid_orders = self._validate_orders(orders)

        if dry_run:
            # Return mock reports for valid orders
            reports = [
                ExecutionReport(
                    order_id=str(i),
                    instrument_id=order.instrument_id,
                    status=OrderStatus.PENDING
                )
                for i, order in enumerate(valid_orders)
            ]
        else:
            # Execute valid orders
            reports = self.ib_client.place_orders(valid_orders, self.instruments_config)

        # Build summary
        summary = self._build_execution_summary(reports, invalid_orders)

        return reports, summary

    def _validate_orders(
        self,
        orders: List[OrderSpec]
    ) -> Tuple[List[OrderSpec], List[Tuple[OrderSpec, str]]]:
        """Validate orders before execution."""
        valid = []
        invalid = []

        for order in orders:
            # Check quantity
            if order.quantity <= 0:
                invalid.append((order, "Invalid quantity"))
                continue

            # Check side
            if order.side not in ["BUY", "SELL"]:
                invalid.append((order, "Invalid side"))
                continue

            # Check instrument exists
            spec = self._find_instrument_spec(order.instrument_id)
            if not spec:
                invalid.append((order, f"Unknown instrument: {order.instrument_id}"))
                continue

            valid.append(order)

        return valid, invalid

    def _find_instrument_spec(self, instrument_id: str) -> Optional[Dict]:
        """Find instrument in config."""
        for category, instruments in self.instruments_config.items():
            if isinstance(instruments, dict):
                if instrument_id in instruments:
                    return instruments[instrument_id]
        return None

    def _build_execution_summary(
        self,
        reports: List[ExecutionReport],
        invalid_orders: List[Tuple[OrderSpec, str]]
    ) -> Dict[str, Any]:
        """Build execution summary statistics."""
        filled = [r for r in reports if r.status == OrderStatus.FILLED]
        partial = [r for r in reports if r.status == OrderStatus.PARTIAL]
        rejected = [r for r in reports if r.status in [OrderStatus.REJECTED, OrderStatus.ERROR]]

        total_commission = sum(r.commission for r in reports)
        total_value = sum(r.filled_qty * r.avg_fill_price for r in filled)

        return {
            "total_orders": len(reports) + len(invalid_orders),
            "filled": len(filled),
            "partial": len(partial),
            "rejected": len(rejected),
            "invalid": len(invalid_orders),
            "total_commission": total_commission,
            "total_value": total_value,
            "invalid_reasons": [(o.instrument_id, reason) for o, reason in invalid_orders]
        }
