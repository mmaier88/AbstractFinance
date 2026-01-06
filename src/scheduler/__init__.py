"""
Scheduler package for AbstractFinance.

This package contains the trading schedule orchestration components:
- DailyScheduler: Main daily trading orchestrator
- ContinuousScheduler: Continuous loop runner for Docker deployment
- Maintenance utilities: IBKR maintenance window detection

For backward compatibility, all public interfaces are re-exported from here.
"""

# Re-export from maintenance module
from .maintenance import (
    is_maintenance_window,
    get_next_maintenance_window,
    IBKR_MAINTENANCE_WINDOWS,
)

# Re-export from continuous module
from .continuous import ContinuousScheduler

# DailyScheduler stays in the parent module for now due to tight coupling
# It's imported here to provide a clean interface
# Import will be added when ready: from .daily import DailyScheduler

__all__ = [
    'is_maintenance_window',
    'get_next_maintenance_window',
    'IBKR_MAINTENANCE_WINDOWS',
    'ContinuousScheduler',
]
