"""
IBKR Maintenance Window Detection.

Provides utilities to detect when IBKR may be unstable due to scheduled maintenance.
"""

from datetime import datetime
from typing import Optional, Tuple, List, Dict
import pytz


# =============================================================================
# IBKR Maintenance Window Configuration
# =============================================================================
# IBKR has regular maintenance windows when connections may be unstable:
# - Weekly: Sunday 23:45 - Monday 00:45 UTC (system restart)
# - Daily: 22:00 - 22:15 UTC (possible brief disconnects)
#
# During these windows, we should avoid placing orders.

IBKR_MAINTENANCE_WINDOWS: List[Dict] = [
    # Weekly maintenance (Sunday night UTC)
    {
        "name": "weekly_restart",
        "days": [6],  # Sunday (0=Monday, 6=Sunday in Python weekday())
        "start_hour": 23,
        "start_minute": 45,
        "end_hour": 24,  # Wraps to next day
        "end_minute": 45,  # Actually 00:45 next day
    },
    # Daily maintenance window
    {
        "name": "daily_disconnect",
        "days": [0, 1, 2, 3, 4],  # Monday-Friday
        "start_hour": 22,
        "start_minute": 0,
        "end_hour": 22,
        "end_minute": 15,
    },
]


def is_maintenance_window(now: Optional[datetime] = None) -> Tuple[bool, Optional[str], int]:
    """
    Check if the current time is within an IBKR maintenance window.

    Args:
        now: Optional datetime (UTC), defaults to current time

    Returns:
        Tuple of (is_maintenance: bool, window_name: str or None, minutes_remaining: int)
    """
    if now is None:
        now = datetime.now(pytz.UTC)

    current_day = now.weekday()
    current_minutes = now.hour * 60 + now.minute

    for window in IBKR_MAINTENANCE_WINDOWS:
        if current_day not in window["days"]:
            continue

        start_minutes = window["start_hour"] * 60 + window["start_minute"]

        # Handle overnight windows (end_hour >= 24)
        if window["end_hour"] >= 24:
            end_minutes = (window["end_hour"] - 24) * 60 + window["end_minute"]
            # Check if we're in the first part (before midnight) or second part (after midnight)
            if current_minutes >= start_minutes:
                # We're before midnight, in maintenance
                remaining = (24 * 60 - current_minutes) + end_minutes
                return (True, window["name"], remaining)
            elif current_day == 0 and current_minutes < end_minutes:
                # Monday morning, check if we're still in Sunday's window
                return (True, window["name"], end_minutes - current_minutes)
        else:
            end_minutes = window["end_hour"] * 60 + window["end_minute"]
            if start_minutes <= current_minutes < end_minutes:
                remaining = end_minutes - current_minutes
                return (True, window["name"], remaining)

    return (False, None, 0)


def get_next_maintenance_window(now: Optional[datetime] = None) -> Optional[Dict]:
    """
    Get information about the next scheduled maintenance window.

    Args:
        now: Optional datetime (UTC), defaults to current time

    Returns:
        Dict with 'name', 'starts_in_minutes', 'duration_minutes' or None if none within 24h
    """
    if now is None:
        now = datetime.now(pytz.UTC)

    current_day = now.weekday()
    current_minutes = now.hour * 60 + now.minute

    candidates = []

    for window in IBKR_MAINTENANCE_WINDOWS:
        start_minutes = window["start_hour"] * 60 + window["start_minute"]

        # Check today's windows
        if current_day in window["days"]:
            if current_minutes < start_minutes:
                # Window is later today
                candidates.append({
                    "name": window["name"],
                    "starts_in_minutes": start_minutes - current_minutes,
                    "duration_minutes": _window_duration(window),
                })

        # Check tomorrow's windows
        tomorrow = (current_day + 1) % 7
        if tomorrow in window["days"]:
            minutes_until = (24 * 60 - current_minutes) + start_minutes
            if minutes_until <= 24 * 60:  # Within 24 hours
                candidates.append({
                    "name": window["name"],
                    "starts_in_minutes": minutes_until,
                    "duration_minutes": _window_duration(window),
                })

    if candidates:
        return min(candidates, key=lambda x: x["starts_in_minutes"])
    return None


def _window_duration(window: Dict) -> int:
    """Calculate duration of a maintenance window in minutes."""
    start = window["start_hour"] * 60 + window["start_minute"]
    end = window["end_hour"] * 60 + window["end_minute"]
    if window["end_hour"] >= 24:
        end = (window["end_hour"] - 24) * 60 + window["end_minute"] + 24 * 60
    return end - start
