"""
Continuous Scheduler for AbstractFinance.

Runs the DailyScheduler on a continuous schedule, executing at configured times each day.
"""

import json
import signal
import time
from datetime import datetime, date, timedelta
from typing import Optional, Dict, Set, List, Tuple

import pytz

from ..data_feeds import load_settings
from ..healthcheck import start_health_server, get_health_server


class ContinuousScheduler:
    """
    Runs the DailyScheduler on a continuous schedule.
    Executes the daily run at the configured times each day.

    Supports multiple run times per day, each targeting specific exchanges:
    - EU_open: 9:15 UTC for LSE, XETRA
    - US_open: 15:00 UTC for NYSE, NASDAQ, CME

    Orders deferred due to market closure are included in subsequent runs.
    """

    # Startup delay to wait for IB Gateway to be ready
    STARTUP_DELAY_SECONDS = 120  # 2 minutes
    # Max retries for initialization failures
    MAX_INIT_RETRIES = 10  # Handle gateway restart cycles
    # Delay between init retries
    INIT_RETRY_DELAY_SECONDS = 90  # Give gateway time
    # Max time to wait for gateway to be API-ready
    GATEWAY_READY_TIMEOUT_SECONDS = 600  # 10 minutes total budget

    def __init__(self):
        self.running = True
        self.scheduler = None  # Will be DailyScheduler instance
        self.last_run_date: Optional[date] = None
        self.completed_runs_today: Set[str] = set()

        # Load settings for schedule config
        settings = load_settings("config/settings.yaml")
        schedule_config = settings.get('schedule', {})
        self.timezone = pytz.timezone(schedule_config.get('timezone', 'UTC'))

        # Load multiple run times if configured, otherwise use legacy single time
        self.run_times: List[Dict] = schedule_config.get('run_times', [])
        if not self.run_times:
            # Legacy fallback: single run time
            self.run_times = [{
                'name': 'daily',
                'hour_utc': schedule_config.get('run_hour_utc', 6),
                'minute_utc': schedule_config.get('run_minute_utc', 0),
                'exchanges': []  # Empty = all exchanges
            }]

        print(f"Configured run times: {[rt['name'] for rt in self.run_times]}")

        # Set up signal handlers for graceful shutdown
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully."""
        print(f"Received signal {signum}, shutting down...")
        self.running = False

    def _should_run_now(self) -> Tuple[bool, Optional[Dict]]:
        """
        Check if it's time to run a scheduled job.

        Returns:
            Tuple of (should_run: bool, run_config: dict or None)
            run_config contains 'name', 'hour_utc', 'minute_utc', 'exchanges'
        """
        now = datetime.now(pytz.UTC)
        today = now.date()

        # Reset completed runs on new day
        if self.last_run_date != today:
            self.completed_runs_today = set()
            self.last_run_date = today

        # Check each configured run time
        for run_config in self.run_times:
            run_name = run_config.get('name', 'default')

            # Skip if already completed this run today
            if run_name in self.completed_runs_today:
                continue

            scheduled_time = now.replace(
                hour=run_config['hour_utc'],
                minute=run_config['minute_utc'],
                second=0,
                microsecond=0
            )

            if now >= scheduled_time:
                return (True, run_config)

        return (False, None)

    def _seconds_until_next_run(self) -> int:
        """
        Calculate seconds until next scheduled run.

        Considers all configured run times and finds the soonest one
        that hasn't been completed today.
        """
        now = datetime.now(pytz.UTC)

        candidates = []

        for run_config in self.run_times:
            run_name = run_config.get('name', 'default')

            # Create scheduled time for today
            scheduled_time = now.replace(
                hour=run_config['hour_utc'],
                minute=run_config['minute_utc'],
                second=0,
                microsecond=0
            )

            # If this run is already completed today or past, schedule for tomorrow
            if run_name in self.completed_runs_today or now >= scheduled_time:
                scheduled_time += timedelta(days=1)

            candidates.append(scheduled_time)

        # Find the soonest scheduled time
        if candidates:
            next_run = min(candidates)
        else:
            # Fallback: tomorrow at 6:00 UTC
            next_run = now.replace(hour=6, minute=0, second=0, microsecond=0) + timedelta(days=1)

        delta = next_run - now
        return max(int(delta.total_seconds()), 60)  # Minimum 60 seconds

    def _wait_for_ib_gateway(self) -> bool:
        """
        Wait for IB Gateway to be ready before proceeding.

        Returns:
            True if gateway appears ready, False if interrupted
        """
        print(f"Waiting {self.STARTUP_DELAY_SECONDS}s for IB Gateway to be ready...")

        # Wait in small increments to allow for graceful shutdown
        remaining = self.STARTUP_DELAY_SECONDS
        while remaining > 0 and self.running:
            time.sleep(min(10, remaining))
            remaining -= 10
            if remaining > 0:
                print(f"  ...{remaining}s remaining")

        return self.running

    def _check_gateway_api_ready(self, host: str = "ibgateway", port: int = 4000) -> bool:
        """
        Check if IB Gateway is truly API-ready (not just accepting connections).

        This is more reliable than just checking if the port is open, because
        the socat proxy in IBGA accepts connections even when the IB Gateway
        backend isn't authenticated yet.

        Returns:
            True if gateway API is responding, False otherwise
        """
        try:
            from ib_insync import IB
            ib = IB()
            # Use a short timeout and test client ID
            ib.connect(host=host, port=port, clientId=99, timeout=15, readonly=True)
            if ib.isConnected():
                # Try to get account info to verify API is truly ready
                accounts = ib.managedAccounts()
                ib.disconnect()
                if accounts:
                    print(f"  Gateway API ready - accounts: {accounts}")
                    return True
            ib.disconnect()
            return False
        except Exception as e:
            print(f"  Gateway API not ready: {e}")
            return False

    def _wait_for_gateway_api_ready(self, host: str = "ibgateway", port: int = 4000) -> bool:
        """
        Wait for IB Gateway API to be truly ready with timeout.

        Returns:
            True if gateway becomes API-ready, False if timeout or interrupted
        """
        print(f"Checking if IB Gateway API is ready (timeout: {self.GATEWAY_READY_TIMEOUT_SECONDS}s)...")

        start_time = time.time()
        check_interval = 30  # Check every 30 seconds

        while self.running:
            elapsed = time.time() - start_time
            if elapsed >= self.GATEWAY_READY_TIMEOUT_SECONDS:
                print(f"  Timeout waiting for gateway API after {elapsed:.0f}s")
                return False

            if self._check_gateway_api_ready(host, port):
                print(f"  Gateway API ready after {elapsed:.0f}s")
                return True

            remaining = self.GATEWAY_READY_TIMEOUT_SECONDS - elapsed
            print(f"  Gateway not ready, retrying in {check_interval}s ({remaining:.0f}s remaining)...")

            # Wait before next check
            wait_remaining = check_interval
            while wait_remaining > 0 and self.running:
                time.sleep(min(5, wait_remaining))
                wait_remaining -= 5

        return False

    def _run_daily_with_retries(self, run_config: Optional[Dict] = None) -> bool:
        """
        Run the daily job with retries on initialization failure.

        First waits for IB Gateway API to be truly ready (not just accepting
        connections), then attempts initialization with retries.

        Args:
            run_config: Optional config for this specific run, containing:
                - name: Run name (e.g., "EU_open", "US_open")
                - exchanges: List of target exchanges for this run

        Returns:
            True if successful, False otherwise
        """
        # Import here to avoid circular imports
        from ..scheduler_main import DailyScheduler

        # First, wait for gateway API to be ready before any init attempts
        if not self._wait_for_gateway_api_ready():
            print("Gateway API not ready after timeout, skipping this run")
            get_health_server().update_ib_status(False)
            return False

        for attempt in range(1, self.MAX_INIT_RETRIES + 1):
            print(f"Initialization attempt {attempt}/{self.MAX_INIT_RETRIES}")

            # Create new scheduler instance for each attempt
            self.scheduler = DailyScheduler()

            try:
                if self.scheduler.initialize():
                    # Update health server with IB connected status
                    get_health_server().update_ib_status(True)

                    # Pass run config to run_daily for exchange filtering
                    result = self.scheduler.run_daily(run_config=run_config)

                    run_name = run_config.get('name', 'daily') if run_config else 'daily'
                    print(f"Run '{run_name}' completed: {json.dumps(result, indent=2)}")
                    return True
                else:
                    get_health_server().update_ib_status(False)
                    print(f"Initialization failed on attempt {attempt}")
                    if attempt < self.MAX_INIT_RETRIES:
                        print(f"Retrying in {self.INIT_RETRY_DELAY_SECONDS}s...")
                        # Wait before retry
                        remaining = self.INIT_RETRY_DELAY_SECONDS
                        while remaining > 0 and self.running:
                            time.sleep(min(10, remaining))
                            remaining -= 10
                        if not self.running:
                            return False
            finally:
                self.scheduler.shutdown()
                self.scheduler = None

        print(f"Failed to initialize after {self.MAX_INIT_RETRIES} attempts")
        return False

    def run(self):
        """Main loop - runs continuously, executing jobs at scheduled times."""
        # Import metrics here to avoid circular imports
        try:
            from ..metrics import start_metrics_server
            METRICS_AVAILABLE = True
        except ImportError:
            METRICS_AVAILABLE = False

        print(f"ContinuousScheduler started")
        print(f"Configured run times:")
        for rt in self.run_times:
            print(f"  - {rt['name']}: {rt['hour_utc']:02d}:{rt['minute_utc']:02d} UTC "
                  f"(exchanges: {rt.get('exchanges', 'all')})")
        print(f"Current time: {datetime.now(pytz.UTC).strftime('%Y-%m-%d %H:%M:%S')} UTC")

        # Start health check server for external monitoring
        health_server = start_health_server(port=8080)
        print("Health check server running on port 8080")

        # Start Prometheus metrics server
        if METRICS_AVAILABLE:
            start_metrics_server(port=8000)
            print("Prometheus metrics server running on port 8000")

        # Wait for IB Gateway to be ready on startup
        if not self._wait_for_ib_gateway():
            print("Startup interrupted")
            health_server.stop()
            return

        while self.running:
            try:
                should_run, run_config = self._should_run_now()
                if should_run and run_config:
                    run_name = run_config.get('name', 'default')
                    print(f"\n{'='*60}")
                    print(f"Starting run '{run_name}' at {datetime.now(pytz.UTC).isoformat()}")
                    print(f"Target exchanges: {run_config.get('exchanges', 'all')}")
                    print(f"{'='*60}\n")

                    success = self._run_daily_with_retries(run_config)

                    # Mark this run as completed
                    self.completed_runs_today.add(run_name)

                    # Update health server with run result
                    health_server.update_daily_run({
                        "timestamp": datetime.now(pytz.UTC).isoformat(),
                        "success": success,
                        "date": date.today().isoformat(),
                        "run_name": run_name
                    })

                    print(f"\n{'='*60}")
                    print(f"Run '{run_name}' finished at {datetime.now(pytz.UTC).isoformat()}")
                    print(f"{'='*60}\n")

                # Calculate sleep time
                sleep_seconds = self._seconds_until_next_run()
                next_run_time = datetime.now(pytz.UTC) + timedelta(seconds=sleep_seconds)
                print(f"Next run scheduled for: {next_run_time.strftime('%Y-%m-%d %H:%M:%S')} UTC")
                print(f"Sleeping for {sleep_seconds} seconds ({sleep_seconds/3600:.1f} hours)...")

                # Sleep in small increments to allow for graceful shutdown
                sleep_increment = 60  # Check every minute
                while sleep_seconds > 0 and self.running:
                    time.sleep(min(sleep_increment, sleep_seconds))
                    sleep_seconds -= sleep_increment

            except Exception as e:
                print(f"Error in scheduler loop: {e}")
                import traceback
                traceback.print_exc()
                # Wait a bit before retrying
                time.sleep(300)  # 5 minutes

        print("ContinuousScheduler stopped")
