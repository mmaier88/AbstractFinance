"""
Execution Jobs - Session-aware job scheduling and persistence.

Provides:
- ExecutionJob dataclass for scheduled execution windows
- ExecutionJobStore for persistence and state management
- Job lifecycle management with idempotency

Phase 2 Enhancement: Jobs are created at precompute time (06:00 UTC)
and executed later in venue-specific liquidity windows.
"""

import json
import hashlib
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Any
from threading import Lock

from .types import OrderIntent, Urgency


logger = logging.getLogger(__name__)


class Venue(str, Enum):
    """Trading venue categories."""
    EU = "EU"       # European equities (XETRA, LSE, Euronext)
    US = "US"       # US equities (NYSE, NASDAQ)
    FX = "FX"       # Currency hedges
    FUT = "FUT"     # Futures (ES, FESX, etc.)


class ExecutionStyle(str, Enum):
    """When within the session to execute."""
    MIDDAY = "MIDDAY"               # Core session, avoid open/close
    CLOSE_AUCTION = "CLOSE_AUCTION"  # MOC/LOC orders
    OPEN_AUCTION = "OPEN_AUCTION"    # MOO/LOO orders
    ANY = "ANY"                      # No timing preference


class JobStatus(str, Enum):
    """Job lifecycle states."""
    PENDING = "PENDING"     # Created, waiting for window
    RUNNING = "RUNNING"     # Currently executing
    DONE = "DONE"           # Successfully completed
    FAILED = "FAILED"       # Failed with error
    CANCELED = "CANCELED"   # Canceled before execution
    SKIPPED = "SKIPPED"     # Skipped (e.g., gated out)


@dataclass
class OverlayPosition:
    """Tracks a temporary overlay hedge position."""
    instrument_id: str
    side: str
    quantity: int
    notional_usd: float
    opened_at: datetime
    reason: str  # "legging_protection", "fx_hedge", etc.
    parent_job_id: str
    broker_order_id: Optional[int] = None
    status: str = "OPEN"  # OPEN, CLOSING, CLOSED


@dataclass
class ExecutionJob:
    """
    A scheduled execution job for a venue-specific window.

    Jobs are created at precompute time and executed later when
    the appropriate liquidity window opens.
    """
    job_id: str
    trade_date: str               # YYYY-MM-DD
    venue: Venue
    style: ExecutionStyle
    created_at_utc: datetime
    earliest_start_utc: datetime
    latest_end_utc: datetime
    intents: List[OrderIntent]
    status: JobStatus = JobStatus.PENDING
    last_error: Optional[str] = None

    # Execution tracking
    started_at_utc: Optional[datetime] = None
    completed_at_utc: Optional[datetime] = None
    filled_count: int = 0
    total_notional_usd: float = 0.0
    total_slippage_bps: float = 0.0

    # Overlay state (tracks temporary hedges opened for legging protection)
    overlay_state: Dict[str, Any] = field(default_factory=dict)

    # Gating results (tracks what was skipped)
    gated_intents: List[str] = field(default_factory=list)  # instrument_ids
    gated_notional_usd: float = 0.0

    def __post_init__(self):
        if isinstance(self.venue, str):
            self.venue = Venue(self.venue)
        if isinstance(self.style, str):
            self.style = ExecutionStyle(self.style)
        if isinstance(self.status, str):
            self.status = JobStatus(self.status)
        if isinstance(self.created_at_utc, str):
            self.created_at_utc = datetime.fromisoformat(self.created_at_utc)
        if isinstance(self.earliest_start_utc, str):
            self.earliest_start_utc = datetime.fromisoformat(self.earliest_start_utc)
        if isinstance(self.latest_end_utc, str):
            self.latest_end_utc = datetime.fromisoformat(self.latest_end_utc)
        if self.started_at_utc and isinstance(self.started_at_utc, str):
            self.started_at_utc = datetime.fromisoformat(self.started_at_utc)
        if self.completed_at_utc and isinstance(self.completed_at_utc, str):
            self.completed_at_utc = datetime.fromisoformat(self.completed_at_utc)

    @property
    def is_terminal(self) -> bool:
        """Check if job is in a terminal state."""
        return self.status in (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELED, JobStatus.SKIPPED)

    @property
    def is_executable(self) -> bool:
        """Check if job can be executed."""
        return self.status == JobStatus.PENDING

    def is_within_window(self, now_utc: datetime) -> bool:
        """Check if current time is within execution window."""
        return self.earliest_start_utc <= now_utc <= self.latest_end_utc

    def to_dict(self) -> Dict[str, Any]:
        """Serialize job to dictionary for persistence."""
        d = {
            "job_id": self.job_id,
            "trade_date": self.trade_date,
            "venue": self.venue.value,
            "style": self.style.value,
            "created_at_utc": self.created_at_utc.isoformat(),
            "earliest_start_utc": self.earliest_start_utc.isoformat(),
            "latest_end_utc": self.latest_end_utc.isoformat(),
            "status": self.status.value,
            "last_error": self.last_error,
            "started_at_utc": self.started_at_utc.isoformat() if self.started_at_utc else None,
            "completed_at_utc": self.completed_at_utc.isoformat() if self.completed_at_utc else None,
            "filled_count": self.filled_count,
            "total_notional_usd": self.total_notional_usd,
            "total_slippage_bps": self.total_slippage_bps,
            "overlay_state": self.overlay_state,
            "gated_intents": self.gated_intents,
            "gated_notional_usd": self.gated_notional_usd,
            "intents": [_intent_to_dict(i) for i in self.intents],
        }
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ExecutionJob":
        """Deserialize job from dictionary."""
        intents = [_intent_from_dict(i) for i in d.get("intents", [])]
        return cls(
            job_id=d["job_id"],
            trade_date=d["trade_date"],
            venue=Venue(d["venue"]),
            style=ExecutionStyle(d["style"]),
            created_at_utc=datetime.fromisoformat(d["created_at_utc"]),
            earliest_start_utc=datetime.fromisoformat(d["earliest_start_utc"]),
            latest_end_utc=datetime.fromisoformat(d["latest_end_utc"]),
            intents=intents,
            status=JobStatus(d.get("status", "PENDING")),
            last_error=d.get("last_error"),
            started_at_utc=datetime.fromisoformat(d["started_at_utc"]) if d.get("started_at_utc") else None,
            completed_at_utc=datetime.fromisoformat(d["completed_at_utc"]) if d.get("completed_at_utc") else None,
            filled_count=d.get("filled_count", 0),
            total_notional_usd=d.get("total_notional_usd", 0.0),
            total_slippage_bps=d.get("total_slippage_bps", 0.0),
            overlay_state=d.get("overlay_state", {}),
            gated_intents=d.get("gated_intents", []),
            gated_notional_usd=d.get("gated_notional_usd", 0.0),
        )


def _intent_to_dict(intent: OrderIntent) -> Dict[str, Any]:
    """Serialize OrderIntent to dictionary."""
    return {
        "instrument_id": intent.instrument_id,
        "side": intent.side,
        "quantity": intent.quantity,
        "reason": intent.reason,
        "sleeve": intent.sleeve,
        "urgency": intent.urgency.value if isinstance(intent.urgency, Enum) else intent.urgency,
        "limit_hint": intent.limit_hint,
        "notional_usd": intent.notional_usd,
        "pair_group": intent.pair_group,
    }


def _intent_from_dict(d: Dict[str, Any]) -> OrderIntent:
    """Deserialize OrderIntent from dictionary."""
    return OrderIntent(
        instrument_id=d["instrument_id"],
        side=d["side"],
        quantity=d["quantity"],
        reason=d["reason"],
        sleeve=d["sleeve"],
        urgency=Urgency(d.get("urgency", "normal")),
        limit_hint=d.get("limit_hint"),
        notional_usd=d.get("notional_usd"),
        pair_group=d.get("pair_group"),
    )


def generate_job_id(trade_date: str, venue: Venue, style: ExecutionStyle, intents: List[OrderIntent]) -> str:
    """
    Generate a unique, deterministic job ID.

    Same inputs will generate same ID, providing idempotency.
    """
    # Create a hash of the intents for uniqueness
    intent_str = "|".join(
        f"{i.instrument_id}:{i.side}:{i.quantity}:{i.sleeve}"
        for i in sorted(intents, key=lambda x: x.instrument_id)
    )
    content = f"{trade_date}|{venue.value}|{style.value}|{intent_str}"
    hash_suffix = hashlib.sha256(content.encode()).hexdigest()[:8]
    return f"{trade_date}_{venue.value}_{style.value}_{hash_suffix}"


class ExecutionJobStore:
    """
    Persistent storage for execution jobs.

    Provides:
    - Job persistence to JSON file
    - Idempotent job creation (same inputs = same job ID)
    - Thread-safe operations
    - Restart recovery
    """

    def __init__(self, persist_path: str = "state/execution_jobs.json"):
        self.persist_path = Path(persist_path)
        self._jobs: Dict[str, ExecutionJob] = {}
        self._lock = Lock()

        # Ensure state directory exists
        self.persist_path.parent.mkdir(parents=True, exist_ok=True)

        # Load existing jobs
        self._load()

    def _load(self) -> None:
        """Load jobs from disk."""
        if not self.persist_path.exists():
            return

        try:
            with open(self.persist_path, "r") as f:
                data = json.load(f)

            for job_data in data.get("jobs", []):
                try:
                    job = ExecutionJob.from_dict(job_data)
                    self._jobs[job.job_id] = job
                except Exception as e:
                    logger.warning(f"Failed to load job: {e}")

            logger.info(f"Loaded {len(self._jobs)} jobs from {self.persist_path}")

        except Exception as e:
            logger.error(f"Failed to load job store: {e}")

    def _save(self) -> None:
        """Save jobs to disk."""
        try:
            data = {
                "updated_at": datetime.utcnow().isoformat(),
                "jobs": [job.to_dict() for job in self._jobs.values()]
            }
            with open(self.persist_path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save job store: {e}")

    def save_job(self, job: ExecutionJob) -> None:
        """Save or update a job."""
        with self._lock:
            self._jobs[job.job_id] = job
            self._save()

    def save_jobs(self, jobs: List[ExecutionJob]) -> None:
        """Save multiple jobs at once."""
        with self._lock:
            for job in jobs:
                self._jobs[job.job_id] = job
            self._save()

    def get_job(self, job_id: str) -> Optional[ExecutionJob]:
        """Get a specific job by ID."""
        with self._lock:
            return self._jobs.get(job_id)

    def get_pending_jobs(self, trade_date: Optional[str] = None) -> List[ExecutionJob]:
        """Get all pending jobs, optionally filtered by date."""
        with self._lock:
            jobs = [j for j in self._jobs.values() if j.status == JobStatus.PENDING]
            if trade_date:
                jobs = [j for j in jobs if j.trade_date == trade_date]
            return sorted(jobs, key=lambda j: j.earliest_start_utc)

    def get_executable_jobs(self, now_utc: datetime) -> List[ExecutionJob]:
        """Get jobs that can be executed right now."""
        with self._lock:
            return [
                j for j in self._jobs.values()
                if j.is_executable and j.is_within_window(now_utc)
            ]

    def get_jobs_for_date(self, trade_date: str) -> List[ExecutionJob]:
        """Get all jobs for a specific date."""
        with self._lock:
            return [j for j in self._jobs.values() if j.trade_date == trade_date]

    def get_open_overlays(self) -> List[ExecutionJob]:
        """Get jobs with open overlay positions that need unwinding."""
        with self._lock:
            return [
                j for j in self._jobs.values()
                if j.overlay_state and j.overlay_state.get("positions")
            ]

    def mark_job_status(
        self,
        job_id: str,
        status: JobStatus,
        error: Optional[str] = None
    ) -> bool:
        """Update job status."""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False

            job.status = status
            job.last_error = error

            if status == JobStatus.RUNNING:
                job.started_at_utc = datetime.utcnow()
            elif status in (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELED):
                job.completed_at_utc = datetime.utcnow()

            self._save()
            return True

    def update_job_results(
        self,
        job_id: str,
        filled_count: int,
        total_notional_usd: float,
        total_slippage_bps: float,
        overlay_state: Optional[Dict[str, Any]] = None,
        gated_intents: Optional[List[str]] = None,
        gated_notional_usd: float = 0.0,
    ) -> bool:
        """Update job execution results."""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False

            job.filled_count = filled_count
            job.total_notional_usd = total_notional_usd
            job.total_slippage_bps = total_slippage_bps

            if overlay_state is not None:
                job.overlay_state = overlay_state
            if gated_intents is not None:
                job.gated_intents = gated_intents
                job.gated_notional_usd = gated_notional_usd

            self._save()
            return True

    def job_exists(self, job_id: str) -> bool:
        """Check if a job with this ID already exists."""
        with self._lock:
            return job_id in self._jobs

    def create_job_if_not_exists(
        self,
        trade_date: str,
        venue: Venue,
        style: ExecutionStyle,
        intents: List[OrderIntent],
        earliest_start_utc: datetime,
        latest_end_utc: datetime,
    ) -> Optional[ExecutionJob]:
        """
        Create a job only if it doesn't already exist.

        Returns the existing job if one exists with the same ID,
        or the newly created job.
        """
        job_id = generate_job_id(trade_date, venue, style, intents)

        with self._lock:
            # Check for existing job
            if job_id in self._jobs:
                logger.info(f"Job {job_id} already exists, skipping creation")
                return self._jobs[job_id]

            # Create new job
            job = ExecutionJob(
                job_id=job_id,
                trade_date=trade_date,
                venue=venue,
                style=style,
                created_at_utc=datetime.utcnow(),
                earliest_start_utc=earliest_start_utc,
                latest_end_utc=latest_end_utc,
                intents=intents,
            )

            self._jobs[job_id] = job
            self._save()

            logger.info(f"Created job {job_id} with {len(intents)} intents")
            return job

    def cleanup_old_jobs(self, days_to_keep: int = 7) -> int:
        """Remove jobs older than specified days."""
        cutoff = date.today().isoformat()
        # Simple cutoff - remove jobs with trade_date older than cutoff
        # In production, you'd want a more sophisticated date comparison

        with self._lock:
            old_ids = [
                job_id for job_id, job in self._jobs.items()
                if job.is_terminal and job.trade_date < cutoff
            ]

            for job_id in old_ids:
                del self._jobs[job_id]

            if old_ids:
                self._save()
                logger.info(f"Cleaned up {len(old_ids)} old jobs")

            return len(old_ids)

    def get_daily_summary(self, trade_date: str) -> Dict[str, Any]:
        """Get summary statistics for a trading day."""
        jobs = self.get_jobs_for_date(trade_date)

        total_jobs = len(jobs)
        pending = sum(1 for j in jobs if j.status == JobStatus.PENDING)
        running = sum(1 for j in jobs if j.status == JobStatus.RUNNING)
        done = sum(1 for j in jobs if j.status == JobStatus.DONE)
        failed = sum(1 for j in jobs if j.status == JobStatus.FAILED)
        canceled = sum(1 for j in jobs if j.status == JobStatus.CANCELED)
        skipped = sum(1 for j in jobs if j.status == JobStatus.SKIPPED)

        total_notional = sum(j.total_notional_usd for j in jobs)
        total_fills = sum(j.filled_count for j in jobs)
        total_gated_notional = sum(j.gated_notional_usd for j in jobs)

        # Weighted average slippage
        weighted_slip = 0.0
        for j in jobs:
            if j.total_notional_usd > 0 and j.total_slippage_bps != 0:
                weighted_slip += j.total_slippage_bps * j.total_notional_usd
        avg_slippage_bps = weighted_slip / total_notional if total_notional > 0 else 0.0

        by_venue = {}
        for j in jobs:
            v = j.venue.value
            if v not in by_venue:
                by_venue[v] = {"count": 0, "notional": 0.0, "fills": 0}
            by_venue[v]["count"] += 1
            by_venue[v]["notional"] += j.total_notional_usd
            by_venue[v]["fills"] += j.filled_count

        return {
            "trade_date": trade_date,
            "total_jobs": total_jobs,
            "pending": pending,
            "running": running,
            "done": done,
            "failed": failed,
            "canceled": canceled,
            "skipped": skipped,
            "total_fills": total_fills,
            "total_notional_usd": total_notional,
            "avg_slippage_bps": avg_slippage_bps,
            "gated_notional_usd": total_gated_notional,
            "by_venue": by_venue,
        }


# Singleton instance
_job_store: Optional[ExecutionJobStore] = None


def get_job_store(persist_path: Optional[str] = None) -> ExecutionJobStore:
    """Get singleton ExecutionJobStore instance."""
    global _job_store
    if _job_store is None:
        path = persist_path or "state/execution_jobs.json"
        _job_store = ExecutionJobStore(persist_path=path)
    return _job_store
