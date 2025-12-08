"""
IB Gateway reconnection and watchdog layer for AbstractFinance.
Handles automatic reconnection, health checks, and connection management.
"""

import asyncio
import time
import threading
from datetime import datetime, timedelta
from typing import Optional, Callable, Any
from dataclasses import dataclass
from enum import Enum

try:
    from ib_insync import IB, util
    IB_AVAILABLE = True
except ImportError:
    IB_AVAILABLE = False

from .logging_utils import TradingLogger, get_trading_logger


class ConnectionState(Enum):
    """Connection state enumeration."""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    ERROR = "error"


@dataclass
class ConnectionStats:
    """Connection statistics."""
    total_connects: int = 0
    total_disconnects: int = 0
    total_reconnects: int = 0
    last_connect_time: Optional[datetime] = None
    last_disconnect_time: Optional[datetime] = None
    last_heartbeat_time: Optional[datetime] = None
    uptime_seconds: float = 0.0
    current_session_start: Optional[datetime] = None


class IBReconnectManager:
    """
    Manages IB Gateway connection with automatic reconnection.
    Implements heartbeat monitoring and graceful reconnection.
    """

    DEFAULT_HEARTBEAT_INTERVAL = 10  # seconds
    DEFAULT_RECONNECT_DELAY = 5  # seconds
    DEFAULT_MAX_RECONNECT_ATTEMPTS = 10
    DEFAULT_STALL_TIMEOUT = 30  # seconds

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 4002,
        client_id: int = 1,
        heartbeat_interval: int = DEFAULT_HEARTBEAT_INTERVAL,
        reconnect_delay: int = DEFAULT_RECONNECT_DELAY,
        max_reconnect_attempts: int = DEFAULT_MAX_RECONNECT_ATTEMPTS,
        stall_timeout: int = DEFAULT_STALL_TIMEOUT,
        logger: Optional[TradingLogger] = None,
        on_connect: Optional[Callable] = None,
        on_disconnect: Optional[Callable] = None,
        on_error: Optional[Callable[[Exception], None]] = None
    ):
        """
        Initialize reconnect manager.

        Args:
            host: IB Gateway host
            port: IB Gateway port
            client_id: Client ID
            heartbeat_interval: Seconds between heartbeats
            reconnect_delay: Seconds to wait before reconnect attempt
            max_reconnect_attempts: Maximum reconnection attempts
            stall_timeout: Seconds before considering connection stalled
            logger: Trading logger
            on_connect: Callback on successful connection
            on_disconnect: Callback on disconnection
            on_error: Callback on error
        """
        if not IB_AVAILABLE:
            raise ImportError("ib_insync is required")

        self.host = host
        self.port = port
        self.client_id = client_id
        self.heartbeat_interval = heartbeat_interval
        self.reconnect_delay = reconnect_delay
        self.max_reconnect_attempts = max_reconnect_attempts
        self.stall_timeout = stall_timeout
        self.logger = logger or get_trading_logger()

        # Callbacks
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self.on_error = on_error

        # IB instance
        self.ib = IB()

        # State
        self._state = ConnectionState.DISCONNECTED
        self._reconnect_attempts = 0
        self._stop_heartbeat = threading.Event()
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._stats = ConnectionStats()

        # Set up IB callbacks
        self.ib.disconnectedEvent += self._on_ib_disconnect
        self.ib.errorEvent += self._on_ib_error

    @property
    def state(self) -> ConnectionState:
        """Get current connection state."""
        return self._state

    @property
    def stats(self) -> ConnectionStats:
        """Get connection statistics."""
        return self._stats

    @property
    def is_connected(self) -> bool:
        """Check if currently connected."""
        return self.ib.isConnected()

    def connect(self) -> bool:
        """
        Connect to IB Gateway.

        Returns:
            True if connection successful
        """
        if self._state == ConnectionState.CONNECTED and self.is_connected:
            return True

        self._state = ConnectionState.CONNECTING
        self.logger.log_connection_event(
            event_type="connect_attempt",
            host=self.host,
            port=self.port,
            success=False
        )

        try:
            self.ib.connect(
                host=self.host,
                port=self.port,
                clientId=self.client_id,
                timeout=30,
                readonly=False
            )

            if self.ib.isConnected():
                self._state = ConnectionState.CONNECTED
                self._reconnect_attempts = 0
                self._stats.total_connects += 1
                self._stats.last_connect_time = datetime.now()
                self._stats.current_session_start = datetime.now()

                self.logger.log_connection_event(
                    event_type="connected",
                    host=self.host,
                    port=self.port,
                    success=True
                )

                # Start heartbeat monitoring
                self._start_heartbeat()

                # Call connect callback
                if self.on_connect:
                    try:
                        self.on_connect()
                    except Exception as e:
                        self.logger.log_alert(
                            alert_type="callback_error",
                            severity="warning",
                            message=f"on_connect callback error: {e}"
                        )

                return True

        except Exception as e:
            self._state = ConnectionState.ERROR
            self.logger.log_connection_event(
                event_type="connect_failed",
                host=self.host,
                port=self.port,
                success=False,
                error_message=str(e)
            )

            if self.on_error:
                self.on_error(e)

        return False

    def disconnect(self) -> None:
        """Disconnect from IB Gateway."""
        self._stop_heartbeat.set()

        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=5)

        if self.is_connected:
            self.ib.disconnect()

        self._state = ConnectionState.DISCONNECTED
        self._stats.total_disconnects += 1
        self._stats.last_disconnect_time = datetime.now()

        if self._stats.current_session_start:
            self._stats.uptime_seconds += (
                datetime.now() - self._stats.current_session_start
            ).total_seconds()
            self._stats.current_session_start = None

        self.logger.log_connection_event(
            event_type="disconnected",
            host=self.host,
            port=self.port,
            success=True
        )

    def ensure_connection(self) -> bool:
        """
        Ensure connection is active, reconnecting if needed.

        Returns:
            True if connected
        """
        if self.is_connected:
            return True

        self.logger.log_alert(
            alert_type="connection_lost",
            severity="warning",
            message="Connection lost, attempting reconnect"
        )

        return self._reconnect()

    def _reconnect(self) -> bool:
        """
        Attempt to reconnect to IB Gateway.

        Returns:
            True if reconnection successful
        """
        self._state = ConnectionState.RECONNECTING

        while self._reconnect_attempts < self.max_reconnect_attempts:
            self._reconnect_attempts += 1
            self._stats.total_reconnects += 1

            self.logger.log_connection_event(
                event_type="reconnect_attempt",
                host=self.host,
                port=self.port,
                success=False,
                metadata={"attempt": self._reconnect_attempts}
            )

            # Wait before attempting
            time.sleep(self.reconnect_delay)

            # Try to connect
            try:
                # Ensure disconnected first
                if self.ib.isConnected():
                    self.ib.disconnect()

                self.ib.connect(
                    host=self.host,
                    port=self.port,
                    clientId=self.client_id,
                    timeout=30
                )

                if self.ib.isConnected():
                    self._state = ConnectionState.CONNECTED
                    self._reconnect_attempts = 0
                    self._stats.last_connect_time = datetime.now()
                    self._stats.current_session_start = datetime.now()

                    self.logger.log_connection_event(
                        event_type="reconnected",
                        host=self.host,
                        port=self.port,
                        success=True
                    )

                    # Restart heartbeat
                    self._start_heartbeat()

                    if self.on_connect:
                        try:
                            self.on_connect()
                        except Exception:
                            pass

                    return True

            except Exception as e:
                self.logger.log_connection_event(
                    event_type="reconnect_failed",
                    host=self.host,
                    port=self.port,
                    success=False,
                    error_message=str(e)
                )

        # Max attempts reached
        self._state = ConnectionState.ERROR
        self.logger.log_alert(
            alert_type="reconnect_failed",
            severity="error",
            message=f"Failed to reconnect after {self.max_reconnect_attempts} attempts"
        )

        return False

    def _start_heartbeat(self) -> None:
        """Start heartbeat monitoring thread."""
        self._stop_heartbeat.clear()

        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            return

        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            daemon=True,
            name="IBHeartbeat"
        )
        self._heartbeat_thread.start()

    def _heartbeat_loop(self) -> None:
        """Heartbeat monitoring loop."""
        last_activity = datetime.now()

        # Create a new event loop for this thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        while not self._stop_heartbeat.is_set():
            try:
                if self.is_connected:
                    # Use isConnected() which is thread-safe, and check server time
                    # to verify the connection is truly alive
                    try:
                        # reqCurrentTime is synchronous in ib_insync and lightweight
                        server_time = self.ib.reqCurrentTime()
                        if server_time:
                            self._stats.last_heartbeat_time = datetime.now()
                            last_activity = datetime.now()
                    except Exception:
                        # If reqCurrentTime fails, fall back to just isConnected check
                        self._stats.last_heartbeat_time = datetime.now()
                        last_activity = datetime.now()

                else:
                    # Connection lost
                    self.logger.log_alert(
                        alert_type="heartbeat_failed",
                        severity="warning",
                        message="Heartbeat detected connection loss"
                    )
                    self.ensure_connection()

                # Check for stall
                if (datetime.now() - last_activity).total_seconds() > self.stall_timeout:
                    self.logger.log_alert(
                        alert_type="connection_stalled",
                        severity="warning",
                        message=f"Connection stalled for {self.stall_timeout}s"
                    )
                    self._force_reconnect()

            except Exception as e:
                self.logger.log_alert(
                    alert_type="heartbeat_error",
                    severity="warning",
                    message=f"Heartbeat error: {e}"
                )

            # Wait for next heartbeat
            self._stop_heartbeat.wait(self.heartbeat_interval)

        # Clean up event loop
        loop.close()

    def _force_reconnect(self) -> None:
        """Force a reconnection."""
        try:
            if self.ib.isConnected():
                self.ib.disconnect()
        except Exception:
            pass

        self._reconnect()

    def _on_ib_disconnect(self) -> None:
        """Handle IB disconnect event."""
        self._state = ConnectionState.DISCONNECTED
        self._stats.total_disconnects += 1
        self._stats.last_disconnect_time = datetime.now()

        self.logger.log_connection_event(
            event_type="ib_disconnect_event",
            host=self.host,
            port=self.port,
            success=True
        )

        if self.on_disconnect:
            try:
                self.on_disconnect()
            except Exception:
                pass

        # Attempt reconnection
        self._reconnect()

    def _on_ib_error(self, reqId: int, errorCode: int, errorString: str, contract: Any) -> None:
        """Handle IB error event."""
        # Log all errors
        self.logger.log_alert(
            alert_type="ib_error",
            severity="warning" if errorCode < 2000 else "error",
            message=f"IB Error {errorCode}: {errorString}",
            metadata={"reqId": reqId, "errorCode": errorCode}
        )

        # Handle specific error codes
        if errorCode in [1100, 1101, 1102]:
            # Connectivity issues
            self.logger.log_alert(
                alert_type="connectivity_error",
                severity="error",
                message=f"Connectivity issue: {errorString}"
            )

        if errorCode == 504:
            # Not connected
            self.ensure_connection()

    def get_connection_info(self) -> dict:
        """Get connection information."""
        return {
            "state": self._state.value,
            "is_connected": self.is_connected,
            "host": self.host,
            "port": self.port,
            "client_id": self.client_id,
            "stats": {
                "total_connects": self._stats.total_connects,
                "total_disconnects": self._stats.total_disconnects,
                "total_reconnects": self._stats.total_reconnects,
                "last_connect": self._stats.last_connect_time.isoformat() if self._stats.last_connect_time else None,
                "last_heartbeat": self._stats.last_heartbeat_time.isoformat() if self._stats.last_heartbeat_time else None,
                "uptime_seconds": self._stats.uptime_seconds
            }
        }


class HealthChecker:
    """
    Health check utilities for IB Gateway connection.
    """

    def __init__(self, reconnect_manager: IBReconnectManager):
        """Initialize health checker."""
        self.manager = reconnect_manager

    def check_connection_health(self) -> dict:
        """
        Perform comprehensive health check.

        Returns:
            Health check results
        """
        results = {
            "healthy": False,
            "checks": {},
            "timestamp": datetime.now().isoformat()
        }

        # Connection check
        results["checks"]["connected"] = self.manager.is_connected

        # Heartbeat check
        last_hb = self.manager.stats.last_heartbeat_time
        if last_hb:
            hb_age = (datetime.now() - last_hb).total_seconds()
            results["checks"]["heartbeat_fresh"] = hb_age < 60
            results["checks"]["heartbeat_age_seconds"] = hb_age
        else:
            results["checks"]["heartbeat_fresh"] = False

        # State check
        results["checks"]["state"] = self.manager.state.value
        results["checks"]["state_healthy"] = self.manager.state == ConnectionState.CONNECTED

        # Overall health
        results["healthy"] = all([
            results["checks"]["connected"],
            results["checks"].get("heartbeat_fresh", False),
            results["checks"]["state_healthy"]
        ])

        return results

    def wait_for_healthy(self, timeout: int = 60) -> bool:
        """
        Wait for connection to become healthy.

        Args:
            timeout: Maximum seconds to wait

        Returns:
            True if healthy within timeout
        """
        start = datetime.now()

        while (datetime.now() - start).total_seconds() < timeout:
            health = self.check_connection_health()
            if health["healthy"]:
                return True
            time.sleep(2)

        return False
