"""
Legacy Unwind Glidepath - Gradual transition from initial positions to target.

Prevents sudden de-risking when transitioning from legacy positions to
strategy-generated targets. Blends positions over N days.
"""

import json
import logging
from datetime import datetime, date
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)


class LegacyUnwindGlidepath:
    """
    Manages gradual transition from initial (legacy) positions to target positions.

    On first run: snapshots current positions and stores them.
    On subsequent runs: blends targets with initial snapshot based on elapsed days.

    alpha = min(1.0, days_elapsed / unwind_days)
    blended[i] = alpha * target[i] + (1 - alpha) * initial[i]
    """

    def __init__(self, settings: Dict[str, Any]):
        """
        Initialize LegacyUnwindGlidepath.

        Args:
            settings: Legacy unwind settings dict with:
                - enabled: Whether glidepath is active
                - unwind_days: Days to converge from initial to target
                - snapshot_file: Path to store initial positions
        """
        self.enabled = settings.get('enabled', False)
        self.unwind_days = settings.get('unwind_days', 10)
        self.snapshot_file = Path(settings.get('snapshot_file', 'state/portfolio_init.json'))

        # Ensure state directory exists
        self.snapshot_file.parent.mkdir(parents=True, exist_ok=True)

        # Cached snapshot
        self._snapshot: Optional[Dict[str, Any]] = None
        self._snapshot_loaded = False

    def has_snapshot(self) -> bool:
        """Check if initial snapshot exists."""
        return self.snapshot_file.exists()

    def load_snapshot(self) -> Optional[Dict[str, Any]]:
        """Load initial snapshot from file."""
        if self._snapshot_loaded:
            return self._snapshot

        if not self.snapshot_file.exists():
            return None

        try:
            with open(self.snapshot_file, 'r') as f:
                self._snapshot = json.load(f)
                self._snapshot_loaded = True
                logger.info(f"Loaded legacy snapshot from {self.snapshot_file}")
                return self._snapshot
        except Exception as e:
            logger.error(f"Failed to load snapshot: {e}")
            return None

    def save_snapshot(
        self,
        positions: Dict[str, float],
        snapshot_date: Optional[date] = None
    ) -> bool:
        """
        Save initial positions snapshot.

        Args:
            positions: Dict of instrument_id -> quantity
            snapshot_date: Date of snapshot (defaults to today)

        Returns:
            True if saved successfully
        """
        snapshot_date = snapshot_date or date.today()

        snapshot = {
            'snapshot_date': snapshot_date.isoformat(),
            'positions': positions,
            'created_at': datetime.now().isoformat(),
        }

        try:
            with open(self.snapshot_file, 'w') as f:
                json.dump(snapshot, f, indent=2)
            logger.info(
                f"Saved legacy snapshot: {len(positions)} positions to {self.snapshot_file}"
            )
            self._snapshot = snapshot
            self._snapshot_loaded = True
            return True
        except Exception as e:
            logger.error(f"Failed to save snapshot: {e}")
            return False

    def compute_alpha(self, today: Optional[date] = None) -> Tuple[float, int, Dict[str, Any]]:
        """
        Compute blending alpha based on days since snapshot.

        Args:
            today: Current date (defaults to today)

        Returns:
            Tuple of (alpha, days_elapsed, diagnostics)
            alpha = 1.0 means fully use target positions
            alpha = 0.0 means fully use initial positions
        """
        today = today or date.today()
        diagnostics = {
            'enabled': self.enabled,
            'unwind_days': self.unwind_days,
            'today': today.isoformat(),
        }

        if not self.enabled:
            diagnostics['reason'] = 'glidepath_disabled'
            return 1.0, 0, diagnostics

        snapshot = self.load_snapshot()
        if snapshot is None:
            diagnostics['reason'] = 'no_snapshot'
            return 1.0, 0, diagnostics

        try:
            snapshot_date = date.fromisoformat(snapshot['snapshot_date'])
        except (KeyError, ValueError) as e:
            logger.warning(f"Invalid snapshot date: {e}")
            diagnostics['reason'] = 'invalid_snapshot_date'
            return 1.0, 0, diagnostics

        days_elapsed = (today - snapshot_date).days
        diagnostics['snapshot_date'] = snapshot_date.isoformat()
        diagnostics['days_elapsed'] = days_elapsed

        if days_elapsed < 0:
            # Snapshot is in the future (shouldn't happen)
            logger.warning(f"Snapshot date {snapshot_date} is in the future")
            diagnostics['reason'] = 'future_snapshot'
            return 1.0, 0, diagnostics

        # Compute alpha: linear ramp from 0 to 1 over unwind_days
        alpha = min(1.0, days_elapsed / self.unwind_days)
        diagnostics['alpha'] = alpha
        diagnostics['fully_converged'] = alpha >= 1.0

        return alpha, days_elapsed, diagnostics

    def blend_positions(
        self,
        target_positions: Dict[str, float],
        today: Optional[date] = None,
    ) -> Tuple[Dict[str, float], Dict[str, Any]]:
        """
        Blend target positions with initial snapshot.

        Args:
            target_positions: Strategy-computed target positions
            today: Current date (defaults to today)

        Returns:
            Tuple of (blended_positions, diagnostics)
        """
        alpha, days_elapsed, diagnostics = self.compute_alpha(today)

        # If fully converged or not enabled, return targets unchanged
        if alpha >= 1.0:
            diagnostics['blending_applied'] = False
            return target_positions, diagnostics

        snapshot = self.load_snapshot()
        if snapshot is None:
            diagnostics['blending_applied'] = False
            return target_positions, diagnostics

        initial_positions = snapshot.get('positions', {})
        diagnostics['initial_position_count'] = len(initial_positions)
        diagnostics['target_position_count'] = len(target_positions)

        # Blend: blended[i] = alpha * target[i] + (1 - alpha) * initial[i]
        blended = {}
        all_instruments = set(target_positions.keys()) | set(initial_positions.keys())

        for inst_id in all_instruments:
            target_qty = target_positions.get(inst_id, 0)
            initial_qty = initial_positions.get(inst_id, 0)

            blended_qty = alpha * target_qty + (1 - alpha) * initial_qty

            # Round to nearest integer for share quantities
            blended[inst_id] = round(blended_qty)

        # Log significant blending
        if days_elapsed <= 3:
            logger.info(
                f"Legacy glidepath day {days_elapsed}/{self.unwind_days}: "
                f"alpha={alpha:.2f}, blending {len(initial_positions)} initial "
                f"with {len(target_positions)} target positions"
            )

        diagnostics['blending_applied'] = True
        diagnostics['blended_position_count'] = len(blended)

        return blended, diagnostics

    def is_first_run(self) -> bool:
        """Check if this is the first run (no snapshot exists)."""
        return not self.has_snapshot()

    def handle_first_run(
        self,
        current_positions: Dict[str, float],
        today: Optional[date] = None
    ) -> Dict[str, Any]:
        """
        Handle first run: save snapshot and return diagnostics.

        Args:
            current_positions: Current portfolio positions from broker
            today: Date of snapshot

        Returns:
            Diagnostics dict with first_run info
        """
        diagnostics = {
            'first_run': True,
            'positions_to_snapshot': len(current_positions),
        }

        if self.enabled:
            success = self.save_snapshot(current_positions, today)
            diagnostics['snapshot_saved'] = success
            if success:
                logger.info(
                    f"First run: saved {len(current_positions)} positions as initial snapshot. "
                    f"Glidepath will blend over {self.unwind_days} days."
                )
        else:
            diagnostics['snapshot_saved'] = False
            diagnostics['reason'] = 'glidepath_disabled'

        return diagnostics


def create_glidepath(settings: Dict[str, Any]) -> LegacyUnwindGlidepath:
    """
    Factory function to create LegacyUnwindGlidepath from settings.

    Args:
        settings: Application settings containing 'legacy_unwind' key

    Returns:
        Configured LegacyUnwindGlidepath instance
    """
    legacy_settings = settings.get('legacy_unwind', {})
    return LegacyUnwindGlidepath(legacy_settings)
