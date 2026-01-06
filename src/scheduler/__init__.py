"""
Scheduler package for AbstractFinance.

This package contains the trading schedule orchestration components:
- DailyScheduler: Main daily trading orchestrator
- ContinuousScheduler: Continuous loop runner for Docker deployment
- Maintenance utilities: IBKR maintenance window detection

For backward compatibility, all public interfaces are re-exported from here.
"""

# Re-export from maintenance module (lightweight, no heavy dependencies)
from .maintenance import (
    is_maintenance_window,
    get_next_maintenance_window,
    IBKR_MAINTENANCE_WINDOWS,
)

# Lazy imports for heavy modules to avoid import errors when ib_insync not installed
def __getattr__(name):
    """Lazy loading for heavy imports."""
    if name == 'ContinuousScheduler':
        from .continuous import ContinuousScheduler
        return ContinuousScheduler
    elif name == 'DailyScheduler':
        from src.scheduler_main import DailyScheduler
        return DailyScheduler
    elif name == 'main':
        from src.scheduler_main import main
        return main
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    'is_maintenance_window',
    'get_next_maintenance_window',
    'IBKR_MAINTENANCE_WINDOWS',
    'ContinuousScheduler',
    'DailyScheduler',
    'main',
]
