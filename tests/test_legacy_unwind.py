"""Tests for legacy unwind glidepath."""

import pytest
import json
import tempfile
from datetime import date, timedelta
from pathlib import Path

from src.legacy_unwind import LegacyUnwindGlidepath, create_glidepath


@pytest.fixture
def temp_state_dir():
    """Create a temporary directory for test state files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def glidepath_settings(temp_state_dir):
    """Default glidepath settings."""
    return {
        'enabled': True,
        'unwind_days': 10,
        'snapshot_file': str(temp_state_dir / 'portfolio_init.json'),
    }


@pytest.fixture
def glidepath(glidepath_settings):
    """Create a glidepath instance."""
    return LegacyUnwindGlidepath(glidepath_settings)


class TestGlidepathInitialization:
    """Tests for glidepath initialization."""

    def test_creates_with_settings(self, glidepath_settings):
        """Test glidepath creates with settings."""
        gp = LegacyUnwindGlidepath(glidepath_settings)
        assert gp.enabled is True
        assert gp.unwind_days == 10

    def test_disabled_by_default_when_not_enabled(self):
        """Test glidepath is disabled when enabled=False."""
        gp = LegacyUnwindGlidepath({'enabled': False})
        assert gp.enabled is False

    def test_create_glidepath_factory(self):
        """Test create_glidepath factory function."""
        settings = {
            'legacy_unwind': {
                'enabled': True,
                'unwind_days': 5,
            }
        }
        gp = create_glidepath(settings)
        assert gp.enabled is True
        assert gp.unwind_days == 5


class TestSnapshotPersistence:
    """Tests for snapshot save/load."""

    def test_has_snapshot_false_initially(self, glidepath):
        """Test has_snapshot returns False initially."""
        assert glidepath.has_snapshot() is False
        assert glidepath.is_first_run() is True

    def test_save_and_load_snapshot(self, glidepath):
        """Test saving and loading a snapshot."""
        positions = {'CSPX': 100, 'CS51': -50, 'IUIT': 200}
        today = date.today()

        success = glidepath.save_snapshot(positions, today)
        assert success is True
        assert glidepath.has_snapshot() is True
        assert glidepath.is_first_run() is False

        loaded = glidepath.load_snapshot()
        assert loaded is not None
        assert loaded['positions'] == positions
        assert loaded['snapshot_date'] == today.isoformat()

    def test_handle_first_run_saves_snapshot(self, glidepath):
        """Test handle_first_run saves snapshot when enabled."""
        positions = {'SPY': 50, 'QQQ': 25}

        diag = glidepath.handle_first_run(positions)
        assert diag['first_run'] is True
        assert diag['snapshot_saved'] is True
        assert glidepath.has_snapshot() is True


class TestAlphaComputation:
    """Tests for alpha (blending factor) computation."""

    def test_alpha_zero_on_snapshot_date(self, glidepath):
        """Test alpha is 0 on the snapshot date."""
        positions = {'CSPX': 100}
        today = date.today()
        glidepath.save_snapshot(positions, today)

        alpha, days, diag = glidepath.compute_alpha(today)
        assert days == 0
        assert alpha == 0.0

    def test_alpha_increases_linearly(self, glidepath):
        """Test alpha increases linearly over unwind_days."""
        positions = {'CSPX': 100}
        snapshot_date = date.today() - timedelta(days=5)
        glidepath.save_snapshot(positions, snapshot_date)

        alpha, days, diag = glidepath.compute_alpha(date.today())
        assert days == 5
        assert alpha == 0.5  # 5/10 unwind days

    def test_alpha_capped_at_one(self, glidepath):
        """Test alpha is capped at 1.0 after unwind_days."""
        positions = {'CSPX': 100}
        snapshot_date = date.today() - timedelta(days=15)
        glidepath.save_snapshot(positions, snapshot_date)

        alpha, days, diag = glidepath.compute_alpha(date.today())
        assert days == 15
        assert alpha == 1.0
        assert diag['fully_converged'] is True

    def test_alpha_one_when_disabled(self, glidepath_settings, temp_state_dir):
        """Test alpha is 1.0 when glidepath disabled."""
        glidepath_settings['enabled'] = False
        gp = LegacyUnwindGlidepath(glidepath_settings)

        alpha, days, diag = gp.compute_alpha()
        assert alpha == 1.0
        assert diag['reason'] == 'glidepath_disabled'

    def test_alpha_one_when_no_snapshot(self, glidepath):
        """Test alpha is 1.0 when no snapshot exists."""
        alpha, days, diag = glidepath.compute_alpha()
        assert alpha == 1.0
        assert diag['reason'] == 'no_snapshot'


class TestPositionBlending:
    """Tests for position blending."""

    def test_blend_positions_day_zero(self, glidepath):
        """Test blending on day 0 uses initial positions."""
        initial = {'CSPX': 100, 'CS51': -50}
        target = {'CSPX': 200, 'CS51': -100}
        today = date.today()

        glidepath.save_snapshot(initial, today)
        blended, diag = glidepath.blend_positions(target, today)

        # alpha=0 -> blended = initial
        assert blended['CSPX'] == 100
        assert blended['CS51'] == -50
        assert diag['blending_applied'] is True

    def test_blend_positions_midway(self, glidepath):
        """Test blending at midpoint."""
        initial = {'CSPX': 100, 'CS51': -50}
        target = {'CSPX': 200, 'CS51': -100}
        snapshot_date = date.today() - timedelta(days=5)

        glidepath.save_snapshot(initial, snapshot_date)
        blended, diag = glidepath.blend_positions(target, date.today())

        # alpha=0.5 -> blended = 0.5 * target + 0.5 * initial
        assert blended['CSPX'] == 150  # 0.5*200 + 0.5*100
        assert blended['CS51'] == -75  # 0.5*-100 + 0.5*-50

    def test_blend_positions_fully_converged(self, glidepath):
        """Test blending after full convergence returns targets."""
        initial = {'CSPX': 100}
        target = {'CSPX': 200, 'NEW_POS': 50}
        snapshot_date = date.today() - timedelta(days=15)

        glidepath.save_snapshot(initial, snapshot_date)
        blended, diag = glidepath.blend_positions(target, date.today())

        # alpha=1.0 -> no blending applied
        assert diag['blending_applied'] is False

    def test_blend_handles_new_positions(self, glidepath):
        """Test blending handles positions not in initial snapshot."""
        initial = {'CSPX': 100}
        target = {'CSPX': 200, 'NEW_POS': 100}
        snapshot_date = date.today() - timedelta(days=5)

        glidepath.save_snapshot(initial, snapshot_date)
        blended, diag = glidepath.blend_positions(target, date.today())

        # NEW_POS: 0.5 * 100 + 0.5 * 0 = 50
        assert blended['CSPX'] == 150
        assert blended['NEW_POS'] == 50

    def test_blend_handles_removed_positions(self, glidepath):
        """Test blending handles positions removed from targets."""
        initial = {'CSPX': 100, 'OLD_POS': 50}
        target = {'CSPX': 200}  # OLD_POS removed
        snapshot_date = date.today() - timedelta(days=5)

        glidepath.save_snapshot(initial, snapshot_date)
        blended, diag = glidepath.blend_positions(target, date.today())

        # OLD_POS: 0.5 * 0 + 0.5 * 50 = 25
        assert blended['CSPX'] == 150
        assert blended['OLD_POS'] == 25


class TestGlidepathDiagnostics:
    """Tests for glidepath diagnostics."""

    def test_diagnostics_contain_expected_fields(self, glidepath):
        """Test diagnostics contain all expected fields."""
        initial = {'CSPX': 100}
        target = {'CSPX': 200}
        snapshot_date = date.today() - timedelta(days=3)

        glidepath.save_snapshot(initial, snapshot_date)
        blended, diag = glidepath.blend_positions(target, date.today())

        assert 'alpha' in diag
        assert 'days_elapsed' in diag
        assert 'blending_applied' in diag
        assert 'initial_position_count' in diag
        assert 'target_position_count' in diag

    def test_first_run_diagnostics(self, glidepath):
        """Test first run diagnostics are correct."""
        positions = {'CSPX': 100}
        diag = glidepath.handle_first_run(positions)

        assert diag['first_run'] is True
        assert diag['snapshot_saved'] is True
        assert diag['positions_to_snapshot'] == 1
