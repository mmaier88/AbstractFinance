"""
Health check HTTP server for AbstractFinance.
Provides endpoints for external uptime monitoring services.
"""

import json
import os
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Dict, Any, Optional


class HealthCheckHandler(BaseHTTPRequestHandler):
    """HTTP request handler for health checks."""

    # Shared state across requests
    _last_daily_run: Optional[Dict[str, Any]] = None
    _ib_connected: bool = False
    _startup_time: datetime = datetime.utcnow()

    def log_message(self, format, *args):
        """Suppress default logging."""
        pass

    def _send_json_response(self, data: Dict[str, Any], status: int = 200):
        """Send JSON response."""
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data, default=str).encode())

    def do_GET(self):
        """Handle GET requests."""
        if self.path == '/health' or self.path == '/':
            self._handle_health()
        elif self.path == '/health/detailed':
            self._handle_detailed_health()
        elif self.path == '/health/ib':
            self._handle_ib_health()
        else:
            self.send_error(404, 'Not Found')

    def _handle_health(self):
        """Simple health check - returns 200 if service is running."""
        response = {
            'status': 'healthy',
            'service': 'abstractfinance-trading-engine',
            'timestamp': datetime.utcnow().isoformat(),
            'uptime_seconds': (datetime.utcnow() - self._startup_time).total_seconds()
        }
        self._send_json_response(response)

    def _handle_detailed_health(self):
        """Detailed health check with component status."""
        # Check portfolio state file
        portfolio_ok = False
        portfolio_age_seconds = None
        try:
            state_file = Path('state/portfolio_state.json')
            if state_file.exists():
                portfolio_ok = True
                portfolio_age_seconds = (datetime.utcnow() - datetime.fromtimestamp(state_file.stat().st_mtime)).total_seconds()
        except Exception:
            pass

        # Overall status
        status = 'healthy'
        if not self._ib_connected:
            status = 'degraded'

        response = {
            'status': status,
            'service': 'abstractfinance-trading-engine',
            'timestamp': datetime.utcnow().isoformat(),
            'uptime_seconds': (datetime.utcnow() - self._startup_time).total_seconds(),
            'environment': os.environ.get('ENVIRONMENT', 'unknown'),
            'mode': os.environ.get('MODE', 'unknown'),
            'components': {
                'ib_gateway': {
                    'connected': self._ib_connected,
                    'status': 'healthy' if self._ib_connected else 'disconnected'
                },
                'portfolio_state': {
                    'exists': portfolio_ok,
                    'age_seconds': portfolio_age_seconds,
                    'status': 'healthy' if portfolio_ok else 'missing'
                }
            },
            'last_daily_run': self._last_daily_run
        }

        status_code = 200 if status == 'healthy' else 503
        self._send_json_response(response, status_code)

    def _handle_ib_health(self):
        """IB Gateway connection health check."""
        response = {
            'ib_connected': self._ib_connected,
            'status': 'healthy' if self._ib_connected else 'disconnected',
            'timestamp': datetime.utcnow().isoformat()
        }
        status_code = 200 if self._ib_connected else 503
        self._send_json_response(response, status_code)

    @classmethod
    def update_ib_status(cls, connected: bool):
        """Update IB connection status."""
        cls._ib_connected = connected

    @classmethod
    def update_daily_run(cls, run_result: Dict[str, Any]):
        """Update last daily run result."""
        cls._last_daily_run = run_result


class HealthCheckServer:
    """Health check HTTP server."""

    def __init__(self, host: str = '0.0.0.0', port: int = 8080):
        self.host = host
        self.port = port
        self.server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """Start the health check server in a background thread."""
        self.server = HTTPServer((self.host, self.port), HealthCheckHandler)
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()
        print(f"Health check server started on http://{self.host}:{self.port}")

    def stop(self):
        """Stop the health check server."""
        if self.server:
            self.server.shutdown()
            self.server = None

    def update_ib_status(self, connected: bool):
        """Update IB connection status."""
        HealthCheckHandler.update_ib_status(connected)

    def update_daily_run(self, run_result: Dict[str, Any]):
        """Update last daily run result."""
        HealthCheckHandler.update_daily_run(run_result)


# Singleton instance
_health_server: Optional[HealthCheckServer] = None


def get_health_server(port: int = 8080) -> HealthCheckServer:
    """Get or create the health check server singleton."""
    global _health_server
    if _health_server is None:
        _health_server = HealthCheckServer(port=port)
    return _health_server


def start_health_server(port: int = 8080) -> HealthCheckServer:
    """Start the health check server."""
    server = get_health_server(port)
    server.start()
    return server
