"""
Structured logging utilities for AbstractFinance.
Provides JSON-formatted logging for orders, fills, risk decisions, and system events.
"""

import logging
import sys
import json
from datetime import datetime
from typing import Any, Dict, Optional
from pathlib import Path
import structlog


def setup_logging(
    log_level: str = "INFO",
    log_file: Optional[str] = None,
    json_format: bool = True
) -> structlog.BoundLogger:
    """
    Configure structured logging for the application.

    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: Optional file path for log output
        json_format: If True, output logs in JSON format

    Returns:
        Configured structlog logger
    """
    # Configure standard library logging
    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, log_level.upper()),
        handlers=[logging.StreamHandler(sys.stdout)]
    )

    # Add file handler if specified
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(getattr(logging, log_level.upper()))
        logging.getLogger().addHandler(file_handler)

    # Configure structlog processors
    processors = [
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if json_format:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    return structlog.get_logger()


class TradingLogger:
    """
    Specialized logger for trading operations.
    Provides structured logging for orders, fills, positions, and risk events.
    """

    def __init__(self, logger: Optional[structlog.BoundLogger] = None):
        self.logger = logger or structlog.get_logger()

    def log_order(
        self,
        order_id: str,
        instrument_id: str,
        side: str,
        quantity: float,
        order_type: str,
        price: Optional[float] = None,
        status: str = "submitted",
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Log an order event."""
        self.logger.info(
            "order_event",
            event_type="order",
            order_id=order_id,
            instrument_id=instrument_id,
            side=side,
            quantity=quantity,
            order_type=order_type,
            price=price,
            status=status,
            timestamp=datetime.utcnow().isoformat(),
            **(metadata or {})
        )

    def log_fill(
        self,
        order_id: str,
        instrument_id: str,
        side: str,
        quantity: float,
        fill_price: float,
        commission: float = 0.0,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Log a fill event."""
        self.logger.info(
            "fill_event",
            event_type="fill",
            order_id=order_id,
            instrument_id=instrument_id,
            side=side,
            quantity=quantity,
            fill_price=fill_price,
            commission=commission,
            timestamp=datetime.utcnow().isoformat(),
            **(metadata or {})
        )

    def log_position(
        self,
        instrument_id: str,
        quantity: float,
        avg_cost: float,
        market_value: float,
        unrealized_pnl: float,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Log a position snapshot."""
        self.logger.info(
            "position_snapshot",
            event_type="position",
            instrument_id=instrument_id,
            quantity=quantity,
            avg_cost=avg_cost,
            market_value=market_value,
            unrealized_pnl=unrealized_pnl,
            timestamp=datetime.utcnow().isoformat(),
            **(metadata or {})
        )

    def log_risk_decision(
        self,
        decision_type: str,
        scaling_factor: float,
        realized_vol: float,
        target_vol: float,
        max_drawdown: float,
        current_drawdown: float,
        emergency_derisk: bool,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Log a risk engine decision."""
        self.logger.info(
            "risk_decision",
            event_type="risk",
            decision_type=decision_type,
            scaling_factor=scaling_factor,
            realized_vol=realized_vol,
            target_vol=target_vol,
            max_drawdown=max_drawdown,
            current_drawdown=current_drawdown,
            emergency_derisk=emergency_derisk,
            timestamp=datetime.utcnow().isoformat(),
            **(metadata or {})
        )

    def log_portfolio_snapshot(
        self,
        nav: float,
        gross_exposure: float,
        net_exposure: float,
        realized_vol: float,
        max_drawdown: float,
        sleeve_weights: Dict[str, float],
        hedge_budget_used: float,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Log a portfolio state snapshot."""
        self.logger.info(
            "portfolio_snapshot",
            event_type="portfolio",
            nav=nav,
            gross_exposure=gross_exposure,
            net_exposure=net_exposure,
            realized_vol=realized_vol,
            max_drawdown=max_drawdown,
            sleeve_weights=sleeve_weights,
            hedge_budget_used=hedge_budget_used,
            timestamp=datetime.utcnow().isoformat(),
            **(metadata or {})
        )

    def log_hedge_action(
        self,
        action_type: str,
        instrument_id: str,
        quantity: float,
        premium: float,
        budget_remaining: float,
        reason: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Log a tail hedge action."""
        self.logger.info(
            "hedge_action",
            event_type="hedge",
            action_type=action_type,
            instrument_id=instrument_id,
            quantity=quantity,
            premium=premium,
            budget_remaining=budget_remaining,
            reason=reason,
            timestamp=datetime.utcnow().isoformat(),
            **(metadata or {})
        )

    def log_connection_event(
        self,
        event_type: str,
        host: str,
        port: int,
        success: bool,
        error_message: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Log a connection event (connect, disconnect, reconnect)."""
        log_level = "info" if success else "error"
        getattr(self.logger, log_level)(
            "connection_event",
            event_type=event_type,
            host=host,
            port=port,
            success=success,
            error_message=error_message,
            timestamp=datetime.utcnow().isoformat(),
            **(metadata or {})
        )

    def log_alert(
        self,
        alert_type: str,
        severity: str,
        message: str,
        value: Optional[float] = None,
        threshold: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Log an alert event."""
        log_method = self.logger.warning if severity == "warning" else self.logger.error
        log_method(
            "alert",
            event_type="alert",
            alert_type=alert_type,
            severity=severity,
            message=message,
            value=value,
            threshold=threshold,
            timestamp=datetime.utcnow().isoformat(),
            **(metadata or {})
        )

    def log_backtest_result(
        self,
        start_date: str,
        end_date: str,
        initial_capital: float,
        final_nav: float,
        total_return: float,
        annual_return: float,
        annual_vol: float,
        sharpe_ratio: float,
        sortino_ratio: float,
        max_drawdown: float,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Log backtest results."""
        self.logger.info(
            "backtest_result",
            event_type="backtest",
            start_date=start_date,
            end_date=end_date,
            initial_capital=initial_capital,
            final_nav=final_nav,
            total_return=total_return,
            annual_return=annual_return,
            annual_vol=annual_vol,
            sharpe_ratio=sharpe_ratio,
            sortino_ratio=sortino_ratio,
            max_drawdown=max_drawdown,
            timestamp=datetime.utcnow().isoformat(),
            **(metadata or {})
        )


def get_trading_logger(name: str = "abstractfinance") -> TradingLogger:
    """Get a configured trading logger instance."""
    return TradingLogger(structlog.get_logger(name))
