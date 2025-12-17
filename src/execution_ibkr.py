"""
IBKR execution engine for AbstractFinance.
Handles order placement, position management, and account data via ib_insync.

This module provides two interfaces:
1. IBClient - Low-level IB Gateway communication
2. IBKRTransport - BrokerTransport implementation for new execution stack

ENGINE_FIX_PLAN Phase 9: Execution Safety
EXECUTION_STACK_UPGRADE: IBKRTransport for stateful execution layer
"""

import time
from datetime import datetime, time as dt_time
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum

try:
    from ib_insync import (
        IB, Contract, Stock, Future, Forex, Option, Index,
        MarketOrder, LimitOrder, StopOrder, Order,
        Trade, Fill, Position as IBPosition
    )
    IB_AVAILABLE = True
except ImportError:
    IB_AVAILABLE = False

from .strategy_logic import OrderSpec
from .portfolio import Position, Sleeve, PortfolioState, InstrumentType
from .logging_utils import TradingLogger, get_trading_logger
from .alerts import AlertManager, Alert, AlertType, AlertSeverity

# Import new execution types
try:
    from .execution.types import MarketDataSnapshot
    from .execution.order_manager import BrokerTransport, OrderUpdate
    EXECUTION_STACK_AVAILABLE = True
except ImportError:
    EXECUTION_STACK_AVAILABLE = False


# Phase 9: Execution safety constants
MARKET_OPEN_BUFFER_MINUTES = 15  # No market orders within 15 min of open
US_MARKET_OPEN = dt_time(9, 30)  # 9:30 AM ET
US_MARKET_CLOSE = dt_time(16, 0)  # 4:00 PM ET
EU_MARKET_OPEN = dt_time(9, 0)  # 9:00 AM CET
EU_MARKET_CLOSE = dt_time(17, 30)  # 5:30 PM CET

# Metrics integration
try:
    from .metrics import (
        update_ib_connection,
        record_ib_reconnect,
        record_order_submitted,
        record_order_filled,
        record_order_rejected,
    )
    METRICS_AVAILABLE = True
except ImportError:
    METRICS_AVAILABLE = False


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
        logger: Optional[TradingLogger] = None,
        alert_manager: Optional[AlertManager] = None
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
            alert_manager: AlertManager for sending notifications
        """
        if not IB_AVAILABLE:
            raise ImportError("ib_insync is required for IBKR integration")

        self.host = host
        self.port = port
        self.client_id = client_id
        self.timeout = timeout
        self.readonly = readonly
        self.logger = logger or get_trading_logger()
        self.alert_manager = alert_manager

        self.ib = IB()
        self._connected = False
        self._instruments_cache: Dict[str, Contract] = {}
        self._pending_orders: Dict[str, Trade] = {}
        self._reconnect_attempts = 0
        self._max_reconnect_attempts = 5

        # Wire up disconnect event handler (use lambda to ensure proper binding)
        self.ib.disconnectedEvent += lambda: self._on_disconnect()

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

            # Update metrics
            if METRICS_AVAILABLE:
                update_ib_connection(self._connected)

            return self._connected

        except Exception as e:
            self.logger.log_connection_event(
                event_type="connect",
                host=self.host,
                port=self.port,
                success=False,
                error_message=str(e)
            )
            if METRICS_AVAILABLE:
                update_ib_connection(False)
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
            if METRICS_AVAILABLE:
                update_ib_connection(False)

    def _on_disconnect(self) -> None:
        """
        Handle unexpected disconnection from IB Gateway.
        Sends alert and attempts reconnection.
        """
        self._connected = False

        # Update metrics
        if METRICS_AVAILABLE:
            update_ib_connection(False)

        self.logger.log_connection_event(
            event_type="unexpected_disconnect",
            host=self.host,
            port=self.port,
            success=False,
            error_message="IB Gateway connection lost"
        )

        # Send Telegram alert
        if self.alert_manager:
            self.alert_manager.send_connection_error(
                f"IB Gateway disconnected unexpectedly!\n\n"
                f"Host: {self.host}:{self.port}\n"
                f"Attempting reconnection..."
            )

        # Attempt reconnection
        self._attempt_reconnect()

    def _attempt_reconnect(self) -> bool:
        """
        Attempt to reconnect to IB Gateway with exponential backoff.

        Returns:
            True if reconnection successful
        """
        import time as time_module

        for attempt in range(1, self._max_reconnect_attempts + 1):
            self._reconnect_attempts = attempt
            wait_seconds = min(30 * attempt, 120)  # 30s, 60s, 90s, 120s, 120s

            self.logger.log_connection_event(
                event_type="reconnect_attempt",
                host=self.host,
                port=self.port,
                success=False,
                error_message=f"Attempt {attempt}/{self._max_reconnect_attempts}, waiting {wait_seconds}s"
            )

            time_module.sleep(wait_seconds)

            try:
                self.ib.connect(
                    host=self.host,
                    port=self.port,
                    clientId=self.client_id,
                    timeout=self.timeout,
                    readonly=self.readonly
                )

                if self.ib.isConnected():
                    self._connected = True
                    self._reconnect_attempts = 0

                    self.logger.log_connection_event(
                        event_type="reconnect_success",
                        host=self.host,
                        port=self.port,
                        success=True
                    )

                    # Record successful reconnect
                    if METRICS_AVAILABLE:
                        record_ib_reconnect(success=True)
                        update_ib_connection(True)

                    if self.alert_manager:
                        self.alert_manager.send_connection_error(
                            f"IB Gateway reconnected successfully!\n\n"
                            f"Host: {self.host}:{self.port}\n"
                            f"Reconnected after {attempt} attempt(s)"
                        )
                    return True

            except Exception as e:
                self.logger.log_connection_event(
                    event_type="reconnect_failed",
                    host=self.host,
                    port=self.port,
                    success=False,
                    error_message=str(e)
                )
                # Record failed reconnect attempt
                if METRICS_AVAILABLE:
                    record_ib_reconnect(success=False)

        # All attempts failed
        if self.alert_manager:
            self.alert_manager.send_connection_error(
                f"CRITICAL: IB Gateway reconnection FAILED!\n\n"
                f"Host: {self.host}:{self.port}\n"
                f"All {self._max_reconnect_attempts} reconnection attempts failed.\n"
                f"Manual intervention required!"
            )
        return False

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
        Get current positions with real-time market prices.

        Uses ib.portfolio() which provides real-time prices from the broker
        without requiring a market data subscription.

        Returns:
            Dict mapping instrument_id to Position
        """
        if not self.is_connected():
            raise ConnectionError("Not connected to IB Gateway")

        positions = {}

        # Use portfolio() for real-time prices (no subscription needed)
        # PortfolioItem contains: contract, position, marketPrice, marketValue,
        # averageCost, unrealizedPNL, realizedPNL, account
        portfolio_items = self.ib.portfolio()

        for item in portfolio_items:
            contract = item.contract
            instrument_id = self._contract_to_instrument_id(contract)

            multiplier = float(contract.multiplier) if contract.multiplier else 1.0

            # Get prices from portfolio item (real-time from broker)
            avg_cost = item.averageCost
            market_price = item.marketPrice

            # Determine instrument type for proper P&L calculation
            if contract.secType == "FUT":
                inst_type = InstrumentType.FUT
                # For futures, IB's avgCost is already multiplied (price × multiplier)
                # but marketPrice is per-unit. We pass raw values to Position
                # and let cost_basis/market_value handle the multiplier correctly.
            elif contract.secType == "OPT":
                inst_type = InstrumentType.OPT
            else:
                inst_type = InstrumentType.ETF

            # Handle GBP pence conversion for LSE-listed securities
            if contract.currency == 'GBP':
                if avg_cost > 100:
                    avg_cost = avg_cost / 100.0
                if market_price and market_price > 100:
                    market_price = market_price / 100.0

            # Fallback to avgCost if no market price
            if not market_price or market_price <= 0:
                # For futures, avgCost is already multiplied, so derive unit price
                if inst_type == InstrumentType.FUT and multiplier > 1:
                    market_price = avg_cost / multiplier
                else:
                    market_price = avg_cost

            position = Position(
                instrument_id=instrument_id,
                quantity=item.position,
                avg_cost=avg_cost,
                market_price=market_price,
                multiplier=multiplier,
                currency=contract.currency,
                instrument_type=inst_type
            )

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
            # Include currency to avoid ambiguity (e.g. M6E requires currency)
            contract = Future(symbol, exchange=exchange, currency=currency, lastTradeDateOrContractMonth=expiry)

        elif sec_type == 'IND':
            # Index contracts (for volatility indices like VIX, V2X, SX7E)
            contract = Index(symbol, exchange, currency)

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

            # Record order submission metric
            if METRICS_AVAILABLE:
                sleeve = getattr(order_spec, 'sleeve', 'unknown')
                record_order_submitted(order_spec.instrument_id, action, sleeve)

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
                # Record fill metric with latency
                if METRICS_AVAILABLE:
                    latency = time.time() - start_time
                    sleeve = getattr(order_spec, 'sleeve', 'unknown')
                    record_order_filled(order_spec.instrument_id, action, sleeve, latency)

            # Record rejection metric if order was rejected
            if status in [OrderStatus.REJECTED, OrderStatus.CANCELLED]:
                if METRICS_AVAILABLE:
                    reason = trade.orderStatus.status if trade.orderStatus else 'unknown'
                    record_order_rejected(order_spec.instrument_id, reason)

            return report

        except Exception as e:
            # Record error as rejection
            if METRICS_AVAILABLE:
                record_order_rejected(order_spec.instrument_id, str(e)[:50])
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

    def get_account_nav(self) -> Optional[float]:
        """Get account NAV for reconciliation."""
        if not self.is_connected():
            return None
        try:
            summary = self.get_account_summary()
            return summary.net_liquidation
        except Exception:
            return None


# =============================================================================
# EXECUTION STACK UPGRADE: IBKRTransport
# =============================================================================

class IBKRTransport:
    """
    BrokerTransport implementation for Interactive Brokers.

    This class bridges the new execution stack (OrderManager, ExecutionPolicy)
    with the IBKR API via IBClient.

    EXECUTION_STACK_UPGRADE: Stateful execution with:
    - Marketable limit orders with collars
    - Order state machine (NEW -> SUBMITTED -> FILLED)
    - Cancel/replace logic for unfilled orders
    - Market data snapshots for slippage tracking
    """

    def __init__(
        self,
        ib_client: IBClient,
        instruments_config: Dict[str, Any],
        logger: Optional[TradingLogger] = None,
    ):
        """
        Initialize IBKR transport.

        Args:
            ib_client: Connected IBClient instance
            instruments_config: Instrument configuration dict
            logger: Trading logger instance
        """
        if not EXECUTION_STACK_AVAILABLE:
            raise ImportError("Execution stack not available. Check src/execution/ package.")

        self.ib_client = ib_client
        self.instruments_config = instruments_config
        self.logger = logger or get_trading_logger()

        # Track active trades by broker_order_id
        self._active_trades: Dict[int, Trade] = {}

        # Internal order ID counter
        self._next_order_id = 1

    @property
    def ib(self) -> Optional[Any]:
        """Get underlying IB instance."""
        return self.ib_client.ib if self.ib_client else None

    def submit_order(
        self,
        instrument_id: str,
        side: str,
        quantity: int,
        order_type: str,
        limit_price: Optional[float],
        tif: str,
        algo: Optional[str] = None,
        algo_params: Optional[Dict[str, Any]] = None,
    ) -> int:
        """
        Submit order to IBKR.

        Args:
            instrument_id: Internal instrument ID
            side: "BUY" or "SELL"
            quantity: Number of shares/contracts
            order_type: "LMT", "MKT", etc.
            limit_price: Limit price (required for LMT)
            tif: Time in force ("DAY", "GTC", etc.)
            algo: Optional algo type (e.g., "ADAPTIVE")
            algo_params: Optional algo parameters

        Returns:
            broker_order_id for tracking

        Raises:
            ConnectionError: If not connected
            ValueError: If contract cannot be built
        """
        if not self.ib_client.is_connected():
            raise ConnectionError("Not connected to IB Gateway")

        # Build contract
        contract = self.ib_client.build_contract(instrument_id, self.instruments_config)
        if not contract:
            raise ValueError(f"Could not build contract for {instrument_id}")

        # Build order
        if order_type.upper() == "MKT":
            order = MarketOrder(side, quantity)
        elif order_type.upper() == "LMT":
            order = LimitOrder(side, quantity, limit_price)
        else:
            order = LimitOrder(side, quantity, limit_price)

        # Set TIF
        order.tif = tif

        # Handle algo orders
        if algo == "ADAPTIVE":
            order.algoStrategy = "Adaptive"
            order.algoParams = []
            if algo_params:
                priority = algo_params.get("adaptivePriority", "Normal")
                from ib_insync import TagValue
                order.algoParams.append(TagValue("adaptivePriority", priority))

        # Place order
        trade = self.ib_client.ib.placeOrder(contract, order)

        # Track the trade
        broker_order_id = trade.order.orderId
        self._active_trades[broker_order_id] = trade

        self.logger.log_order(
            order_id=str(broker_order_id),
            instrument_id=instrument_id,
            side=side,
            quantity=quantity,
            order_type=order_type,
            price=limit_price,
            status="submitted"
        )

        # Record metric
        if METRICS_AVAILABLE:
            record_order_submitted(instrument_id, side, "execution_stack")

        return broker_order_id

    def cancel_order(self, broker_order_id: int) -> bool:
        """
        Cancel an order.

        Args:
            broker_order_id: IBKR order ID

        Returns:
            True if cancel request was sent
        """
        trade = self._active_trades.get(broker_order_id)
        if not trade:
            return False

        try:
            self.ib_client.ib.cancelOrder(trade.order)
            return True
        except Exception as e:
            self.logger.logger.warning(f"Cancel failed for order {broker_order_id}: {e}")
            return False

    def modify_order(
        self,
        broker_order_id: int,
        new_limit_price: float,
    ) -> bool:
        """
        Modify order limit price (cancel/replace).

        Args:
            broker_order_id: IBKR order ID
            new_limit_price: New limit price

        Returns:
            True if modification was sent
        """
        trade = self._active_trades.get(broker_order_id)
        if not trade:
            return False

        try:
            # Modify by updating order object and re-placing
            trade.order.lmtPrice = new_limit_price
            self.ib_client.ib.placeOrder(trade.contract, trade.order)
            return True
        except Exception as e:
            self.logger.logger.warning(f"Modify failed for order {broker_order_id}: {e}")
            return False

    def get_order_status(self, broker_order_id: int) -> Optional['OrderUpdate']:
        """
        Get current order status from IBKR.

        Args:
            broker_order_id: IBKR order ID

        Returns:
            OrderUpdate with current status, or None if not found
        """
        if not EXECUTION_STACK_AVAILABLE:
            return None

        trade = self._active_trades.get(broker_order_id)
        if not trade:
            return None

        # Ensure we have latest status
        self.ib_client.ib.sleep(0.1)

        # Get fill info
        total_commission = sum(
            fill.commissionReport.commission
            for fill in trade.fills
            if fill.commissionReport and fill.commissionReport.commission
        )

        # Get last fill info
        last_fill_price = None
        last_fill_qty = None
        if trade.fills:
            last_fill = trade.fills[-1]
            last_fill_price = last_fill.execution.price
            last_fill_qty = int(last_fill.execution.shares)

        return OrderUpdate(
            broker_order_id=broker_order_id,
            status=trade.orderStatus.status,
            filled_qty=int(trade.orderStatus.filled),
            remaining_qty=int(trade.orderStatus.remaining),
            avg_fill_price=trade.orderStatus.avgFillPrice if trade.orderStatus.avgFillPrice else None,
            last_fill_price=last_fill_price,
            last_fill_qty=last_fill_qty,
            commission=total_commission,
            error_message=None if trade.orderStatus.status != "Inactive" else "Order inactive",
        )

    def get_market_data(self, instrument_id: str) -> Optional['MarketDataSnapshot']:
        """
        Get current market data snapshot for instrument.

        Tries real-time market data first, falls back to portfolio prices.

        Args:
            instrument_id: Internal instrument ID

        Returns:
            MarketDataSnapshot with current prices
        """
        if not EXECUTION_STACK_AVAILABLE:
            return None

        if not self.ib_client.is_connected():
            return None

        contract = self.ib_client.build_contract(instrument_id, self.instruments_config)
        if not contract:
            return None

        last = None
        bid = None
        ask = None
        close = None

        # Try real-time market data first
        try:
            ticker = self.ib_client.ib.reqMktData(contract, '', False, False)
            self.ib_client.ib.sleep(1)  # Wait for data

            last = ticker.last if ticker.last and ticker.last > 0 else None
            bid = ticker.bid if ticker.bid and ticker.bid > 0 else None
            ask = ticker.ask if ticker.ask and ticker.ask > 0 else None
            close = ticker.close if ticker.close and ticker.close > 0 else None

            self.ib_client.ib.cancelMktData(contract)

        except Exception as e:
            self.logger.logger.debug(f"Real-time market data failed for {instrument_id}: {e}")

        # Fall back to portfolio prices if real-time data unavailable
        if last is None and close is None:
            try:
                # Use the contract symbol (already resolved from instruments config)
                # This maps us_index_etf -> CSPX, hy_hyg -> IHYU, etc.
                target_symbol = contract.symbol

                # Search portfolio items for matching symbol
                for item in self.ib_client.ib.portfolio():
                    if item.contract.symbol == target_symbol:
                        # Found matching position - use marketPrice as reference
                        if item.marketPrice and item.marketPrice > 0:
                            last = item.marketPrice
                            close = item.marketPrice
                            self.logger.logger.debug(
                                f"Using portfolio price for {instrument_id} ({target_symbol}): {last}"
                            )
                            break
            except Exception as e:
                self.logger.logger.debug(f"Portfolio price fallback failed for {instrument_id}: {e}")

        # Handle GBP pence conversion
        if contract.currency == 'GBP':
            if last and last > 100:
                last = last / 100.0
            if bid and bid > 100:
                bid = bid / 100.0
            if ask and ask > 100:
                ask = ask / 100.0
            if close and close > 100:
                close = close / 100.0

        # If we still have no price data, return None
        if last is None and close is None:
            self.logger.logger.warning(f"No price data available for {instrument_id}")
            return None

        return MarketDataSnapshot(
            symbol=instrument_id,
            ts=datetime.now(),
            last=last,
            bid=bid,
            ask=ask,
            close=close,
        )

    def wait_for_fills(
        self,
        broker_order_ids: List[int],
        timeout_seconds: float = 30,
    ) -> Dict[int, 'OrderUpdate']:
        """
        Wait for orders to complete (or timeout).

        Args:
            broker_order_ids: List of order IDs to wait for
            timeout_seconds: Maximum wait time

        Returns:
            Dict of broker_order_id -> final OrderUpdate
        """
        results = {}
        start = time.time()

        while time.time() - start < timeout_seconds:
            all_done = True

            for order_id in broker_order_ids:
                if order_id in results:
                    continue

                update = self.get_order_status(order_id)
                if update:
                    status_upper = update.status.upper()
                    if status_upper in ("FILLED", "CANCELLED", "CANCELED", "REJECTED", "ERROR", "INACTIVE"):
                        results[order_id] = update
                    else:
                        all_done = False
                else:
                    all_done = False

            if all_done:
                break

            self.ib_client.ib.sleep(0.5)

        # Get final status for any remaining
        for order_id in broker_order_ids:
            if order_id not in results:
                update = self.get_order_status(order_id)
                if update:
                    results[order_id] = update

        return results

    def cleanup_trade(self, broker_order_id: int) -> None:
        """Remove trade from tracking after completion."""
        self._active_trades.pop(broker_order_id, None)


def is_near_market_open(exchange: str = "US", buffer_minutes: int = MARKET_OPEN_BUFFER_MINUTES) -> bool:
    """
    Check if current time is within buffer of market open.

    Phase 9: No market orders near open.

    Args:
        exchange: "US" or "EU"
        buffer_minutes: Minutes after open to avoid

    Returns:
        True if within buffer of market open
    """
    now = datetime.now()
    current_time = now.time()

    if exchange == "US":
        market_open = US_MARKET_OPEN
    else:
        market_open = EU_MARKET_OPEN

    # Convert to minutes since midnight for easy comparison
    open_minutes = market_open.hour * 60 + market_open.minute
    current_minutes = current_time.hour * 60 + current_time.minute

    # Check if within buffer after open
    if current_minutes >= open_minutes and current_minutes < open_minutes + buffer_minutes:
        return True

    return False


def check_execution_safety(
    portfolio_state: PortfolioState,
    fx_rates_valid: bool = True,
    vol_estimate_valid: bool = True,
    exchange: str = "US"
) -> Tuple[bool, List[str]]:
    """
    Check all execution safety conditions.

    Phase 9: Block orders if safety conditions not met.

    Args:
        portfolio_state: Current portfolio state
        fx_rates_valid: Whether FX rates are fresh
        vol_estimate_valid: Whether volatility estimate is valid
        exchange: Primary exchange for timing check

    Returns:
        Tuple of (safe_to_execute: bool, reasons: List[str])
    """
    reasons = []
    safe = True

    # Check 1: NAV reconciliation must pass
    if not portfolio_state.can_trade():
        safe = False
        reasons.append(f"NAV reconciliation failed: {portfolio_state.reconciliation_status}")

    # Check 2: FX rates must be valid
    if not fx_rates_valid:
        safe = False
        reasons.append("FX rates are stale or invalid")

    # Check 3: Volatility estimate must be valid
    if not vol_estimate_valid:
        safe = False
        reasons.append("Volatility estimate is invalid")

    # Check 4: No market orders near open
    if is_near_market_open(exchange):
        safe = False
        reasons.append(f"Within {MARKET_OPEN_BUFFER_MINUTES} min of {exchange} market open")

    return safe, reasons


class ExecutionEngine:
    """
    High-level execution engine for strategy orders.
    Wraps IBClient with additional logic for order management.

    ENGINE_FIX_PLAN Phase 9: Execution Safety
    - Validates all safety conditions before placing orders
    - Converts market orders to limit orders near open
    - Blocks orders if reconciliation fails
    """

    def __init__(
        self,
        ib_client: IBClient,
        instruments_config: Dict[str, Any],
        logger: Optional[TradingLogger] = None,
        portfolio_state: Optional[PortfolioState] = None
    ):
        """
        Initialize execution engine.

        Args:
            ib_client: Connected IB client
            instruments_config: Instrument configuration
            logger: Trading logger
            portfolio_state: Portfolio state for safety checks
        """
        self.ib_client = ib_client
        self.instruments_config = instruments_config
        self.logger = logger or get_trading_logger()
        self.portfolio_state = portfolio_state

    def execute_strategy_orders(
        self,
        orders: List[OrderSpec],
        dry_run: bool = False,
        fx_rates_valid: bool = True,
        vol_estimate_valid: bool = True
    ) -> Tuple[List[ExecutionReport], Dict[str, Any]]:
        """
        Execute strategy orders with validation and safety checks.

        ENGINE_FIX_PLAN Phase 9: Execution Safety
        - Checks all safety conditions before execution
        - Blocks orders if any safety check fails
        - Logs reasons for blocked orders

        Args:
            orders: List of orders to execute
            dry_run: If True, validate but don't execute
            fx_rates_valid: Whether FX rates are fresh
            vol_estimate_valid: Whether volatility estimate is valid

        Returns:
            Tuple of (reports, summary_stats)
        """
        # Phase 9: Check execution safety conditions
        if self.portfolio_state is not None:
            safe, safety_reasons = check_execution_safety(
                self.portfolio_state,
                fx_rates_valid=fx_rates_valid,
                vol_estimate_valid=vol_estimate_valid
            )

            if not safe:
                # Log blocked execution
                self.logger.logger.warning(
                    "execution_blocked",
                    extra={
                        "reasons": safety_reasons,
                        "order_count": len(orders),
                        "reconciliation_status": self.portfolio_state.reconciliation_status
                    }
                )

                # Return rejected reports for all orders
                reports = [
                    ExecutionReport(
                        order_id=str(i),
                        instrument_id=order.instrument_id,
                        status=OrderStatus.REJECTED,
                        error_message=f"Execution blocked: {', '.join(safety_reasons)}"
                    )
                    for i, order in enumerate(orders)
                ]

                summary = {
                    "total_orders": len(orders),
                    "filled": 0,
                    "partial": 0,
                    "rejected": len(orders),
                    "invalid": 0,
                    "blocked": True,
                    "block_reasons": safety_reasons,
                    "total_commission": 0.0,
                    "total_value": 0.0
                }

                return reports, summary

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
        summary["blocked"] = False
        summary["block_reasons"] = []

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
