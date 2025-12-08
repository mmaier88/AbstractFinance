"""
Prometheus metrics for AbstractFinance trading engine.
Exposes metrics for monitoring via /metrics endpoint.
"""

import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional

from prometheus_client import (
    Counter, Gauge, Histogram, Info,
    generate_latest, CONTENT_TYPE_LATEST,
    REGISTRY, CollectorRegistry
)


# =============================================================================
# IB Gateway Connection Metrics
# =============================================================================

ib_connection_state = Gauge(
    'abstractfinance_ib_connection_state',
    'IB Gateway connection state (1=connected, 0=disconnected)'
)

ib_reconnect_total = Counter(
    'abstractfinance_ib_reconnect_total',
    'Total IB Gateway reconnection attempts'
)

ib_reconnect_success_total = Counter(
    'abstractfinance_ib_reconnect_success_total',
    'Successful IB Gateway reconnections'
)

ib_last_heartbeat_timestamp = Gauge(
    'abstractfinance_ib_last_heartbeat_timestamp',
    'Unix timestamp of last IB Gateway heartbeat'
)

ib_pacing_violations_total = Counter(
    'abstractfinance_ib_pacing_violations_total',
    'IB API pacing/rate limit violations'
)


# =============================================================================
# Order Metrics
# =============================================================================

orders_submitted_total = Counter(
    'abstractfinance_orders_submitted_total',
    'Total orders submitted',
    ['instrument', 'side', 'sleeve']
)

orders_filled_total = Counter(
    'abstractfinance_orders_filled_total',
    'Total orders filled',
    ['instrument', 'side', 'sleeve']
)

orders_rejected_total = Counter(
    'abstractfinance_orders_rejected_total',
    'Total orders rejected',
    ['instrument', 'reason']
)

orders_cancelled_total = Counter(
    'abstractfinance_orders_cancelled_total',
    'Total orders cancelled',
    ['instrument']
)

order_fill_latency_seconds = Histogram(
    'abstractfinance_order_fill_latency_seconds',
    'Order fill latency in seconds',
    ['instrument'],
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)
)


# =============================================================================
# Portfolio Metrics
# =============================================================================

portfolio_nav_usd = Gauge(
    'abstractfinance_portfolio_nav_usd',
    'Portfolio Net Asset Value in USD'
)

portfolio_cash_usd = Gauge(
    'abstractfinance_portfolio_cash_usd',
    'Portfolio cash balance in USD'
)

portfolio_gross_exposure_usd = Gauge(
    'abstractfinance_portfolio_gross_exposure_usd',
    'Portfolio gross exposure in USD'
)

portfolio_net_exposure_usd = Gauge(
    'abstractfinance_portfolio_net_exposure_usd',
    'Portfolio net exposure in USD'
)

portfolio_daily_pnl_usd = Gauge(
    'abstractfinance_portfolio_daily_pnl_usd',
    'Portfolio daily P&L in USD'
)

portfolio_daily_return_pct = Gauge(
    'abstractfinance_portfolio_daily_return_pct',
    'Portfolio daily return percentage'
)

portfolio_drawdown_pct = Gauge(
    'abstractfinance_portfolio_drawdown_pct',
    'Current portfolio drawdown percentage (negative)'
)

portfolio_max_drawdown_pct = Gauge(
    'abstractfinance_portfolio_max_drawdown_pct',
    'Maximum historical drawdown percentage'
)


# =============================================================================
# Risk Metrics
# =============================================================================

risk_realized_vol_annual = Gauge(
    'abstractfinance_risk_realized_vol_annual',
    'Realized annual volatility (20-day)'
)

risk_target_vol_annual = Gauge(
    'abstractfinance_risk_target_vol_annual',
    'Target annual volatility'
)

risk_scaling_factor = Gauge(
    'abstractfinance_risk_scaling_factor',
    'Current risk scaling factor (0-1)'
)

risk_regime = Gauge(
    'abstractfinance_risk_regime',
    'Current risk regime (0=NORMAL, 1=ELEVATED, 2=CRISIS)'
)

risk_vix_level = Gauge(
    'abstractfinance_risk_vix_level',
    'Current VIX level'
)

risk_emergency_derisk = Gauge(
    'abstractfinance_risk_emergency_derisk',
    'Emergency de-risk flag (1=active, 0=inactive)'
)


# =============================================================================
# Sleeve Metrics
# =============================================================================

sleeve_weight_pct = Gauge(
    'abstractfinance_sleeve_weight_pct',
    'Sleeve weight as percentage of portfolio',
    ['sleeve']
)

sleeve_target_weight_pct = Gauge(
    'abstractfinance_sleeve_target_weight_pct',
    'Target sleeve weight as percentage of portfolio',
    ['sleeve']
)

sleeve_pnl_usd = Gauge(
    'abstractfinance_sleeve_pnl_usd',
    'Sleeve P&L in USD',
    ['sleeve']
)


# =============================================================================
# Hedge Budget Metrics
# =============================================================================

hedge_budget_annual_usd = Gauge(
    'abstractfinance_hedge_budget_annual_usd',
    'Annual hedge budget in USD'
)

hedge_budget_used_ytd_usd = Gauge(
    'abstractfinance_hedge_budget_used_ytd_usd',
    'Hedge budget used year-to-date in USD'
)

hedge_budget_usage_pct = Gauge(
    'abstractfinance_hedge_budget_usage_pct',
    'Hedge budget usage percentage'
)


# =============================================================================
# Scheduler Metrics
# =============================================================================

scheduler_last_run_timestamp = Gauge(
    'abstractfinance_scheduler_last_run_timestamp',
    'Unix timestamp of last daily run'
)

scheduler_last_run_success = Gauge(
    'abstractfinance_scheduler_last_run_success',
    'Last daily run success (1=success, 0=failure)'
)

scheduler_run_duration_seconds = Histogram(
    'abstractfinance_scheduler_run_duration_seconds',
    'Daily run duration in seconds',
    buckets=(10, 30, 60, 120, 300, 600, 1200)
)

scheduler_total_runs = Counter(
    'abstractfinance_scheduler_total_runs',
    'Total daily runs executed',
    ['status']
)


# =============================================================================
# System Info
# =============================================================================

system_info = Info(
    'abstractfinance_system',
    'System information'
)


# =============================================================================
# Metrics Server
# =============================================================================

class MetricsHandler(BaseHTTPRequestHandler):
    """HTTP handler for Prometheus metrics endpoint."""

    def log_message(self, format, *args):
        """Suppress default logging."""
        pass

    def do_GET(self):
        if self.path == '/metrics':
            self.send_response(200)
            self.send_header('Content-Type', CONTENT_TYPE_LATEST)
            self.end_headers()
            self.wfile.write(generate_latest(REGISTRY))
        else:
            self.send_error(404, 'Not Found')


class MetricsServer:
    """Prometheus metrics HTTP server."""

    def __init__(self, host: str = '0.0.0.0', port: int = 8000):
        self.host = host
        self.port = port
        self.server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """Start metrics server in background thread."""
        self.server = HTTPServer((self.host, self.port), MetricsHandler)
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()
        print(f"Metrics server started on http://{self.host}:{self.port}/metrics")

    def stop(self):
        """Stop the metrics server."""
        if self.server:
            self.server.shutdown()
            self.server = None


# Singleton
_metrics_server: Optional[MetricsServer] = None


def start_metrics_server(port: int = 8000) -> MetricsServer:
    """Start the metrics server singleton."""
    global _metrics_server
    if _metrics_server is None:
        _metrics_server = MetricsServer(port=port)
        _metrics_server.start()
    return _metrics_server


# =============================================================================
# Helper Functions
# =============================================================================

def update_portfolio_metrics(
    nav: float,
    cash: float,
    gross_exposure: float,
    net_exposure: float,
    daily_pnl: float,
    daily_return: float,
    drawdown: float,
    max_drawdown: float
):
    """Update all portfolio metrics."""
    portfolio_nav_usd.set(nav)
    portfolio_cash_usd.set(cash)
    portfolio_gross_exposure_usd.set(gross_exposure)
    portfolio_net_exposure_usd.set(net_exposure)
    portfolio_daily_pnl_usd.set(daily_pnl)
    portfolio_daily_return_pct.set(daily_return * 100)
    portfolio_drawdown_pct.set(drawdown * 100)
    portfolio_max_drawdown_pct.set(max_drawdown * 100)


def update_risk_metrics(
    realized_vol: float,
    target_vol: float,
    scaling_factor: float,
    regime: int,
    vix_level: float,
    emergency_derisk: bool
):
    """Update all risk metrics."""
    risk_realized_vol_annual.set(realized_vol)
    risk_target_vol_annual.set(target_vol)
    risk_scaling_factor.set(scaling_factor)
    risk_regime.set(regime)
    risk_vix_level.set(vix_level)
    risk_emergency_derisk.set(1 if emergency_derisk else 0)


def update_sleeve_metrics(sleeve_weights: dict, target_weights: dict, sleeve_pnls: dict):
    """Update sleeve metrics."""
    for sleeve, weight in sleeve_weights.items():
        sleeve_weight_pct.labels(sleeve=sleeve).set(weight * 100)
    for sleeve, target in target_weights.items():
        sleeve_target_weight_pct.labels(sleeve=sleeve).set(target * 100)
    for sleeve, pnl in sleeve_pnls.items():
        sleeve_pnl_usd.labels(sleeve=sleeve).set(pnl)


def update_hedge_budget_metrics(budget_annual: float, used_ytd: float):
    """Update hedge budget metrics."""
    hedge_budget_annual_usd.set(budget_annual)
    hedge_budget_used_ytd_usd.set(used_ytd)
    if budget_annual > 0:
        hedge_budget_usage_pct.set((used_ytd / budget_annual) * 100)


def record_order_submitted(instrument: str, side: str, sleeve: str = 'unknown'):
    """Record an order submission."""
    orders_submitted_total.labels(instrument=instrument, side=side, sleeve=sleeve).inc()


def record_order_filled(instrument: str, side: str, sleeve: str = 'unknown', latency_seconds: float = 0):
    """Record an order fill."""
    orders_filled_total.labels(instrument=instrument, side=side, sleeve=sleeve).inc()
    if latency_seconds > 0:
        order_fill_latency_seconds.labels(instrument=instrument).observe(latency_seconds)


def record_order_rejected(instrument: str, reason: str):
    """Record an order rejection."""
    orders_rejected_total.labels(instrument=instrument, reason=reason).inc()


def update_ib_connection(connected: bool):
    """Update IB connection state."""
    ib_connection_state.set(1 if connected else 0)
    if connected:
        ib_last_heartbeat_timestamp.set(time.time())


def record_ib_reconnect(success: bool):
    """Record an IB reconnection attempt."""
    ib_reconnect_total.inc()
    if success:
        ib_reconnect_success_total.inc()


def record_scheduler_run(success: bool, duration_seconds: float):
    """Record a scheduler run."""
    scheduler_last_run_timestamp.set(time.time())
    scheduler_last_run_success.set(1 if success else 0)
    scheduler_run_duration_seconds.observe(duration_seconds)
    scheduler_total_runs.labels(status='success' if success else 'failure').inc()


def set_system_info(version: str, mode: str, environment: str):
    """Set system information."""
    system_info.info({
        'version': version,
        'mode': mode,
        'environment': environment
    })
