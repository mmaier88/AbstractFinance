"""
Daily run scheduler and orchestrator for AbstractFinance.
Main entrypoint for automated trading execution.
"""

import os
import sys
import yaml
import json
import time
import signal
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, List
import pandas as pd
import pytz

from .data_feeds import DataFeed, load_instruments_config, load_settings
from .portfolio import PortfolioState, load_portfolio_state, save_portfolio_state, load_returns_history, save_returns_history
from .risk_engine import RiskEngine, RiskDecision
from .strategy_logic import Strategy, OrderSpec, generate_rebalance_orders
from .tail_hedge import TailHedgeManager
from .execution_ibkr import IBClient, ExecutionEngine, check_execution_safety, IBKRTransport
from .reconnect import IBReconnectManager, HealthChecker
from .logging_utils import setup_logging, TradingLogger, get_trading_logger
from .healthcheck import start_health_server, get_health_server
from .futures_rollover import check_and_roll_futures
from .fx_rates import FXRates, get_fx_rates
from .legacy_unwind import LegacyUnwindGlidepath, create_glidepath
from .utils.invariants import (
    assert_position_id_valid,
    assert_no_conflicting_orders,
    assert_gbx_whitelist_valid,
    validate_instruments_config,
    InvariantError,
)

# ROADMAP Phase A: Run Ledger for exactly-once execution
try:
    from .state.run_ledger import (
        RunLedger, RunStatus, TradingRun, OrderRecord,
        compute_inputs_hash, compute_intents_hash
    )
    RUN_LEDGER_AVAILABLE = True
except ImportError:
    RUN_LEDGER_AVAILABLE = False

# EXECUTION_STACK_UPGRADE imports
try:
    from .execution import (
        ExecutionPolicy,
        OrderManager,
        BasketExecutor,
        PairExecutor,
        ExecutionAnalytics,
        MarketDataSnapshot,
        OrderIntent,
        is_market_open,
        should_avoid_trading,
    )
    from .execution.policy import load_execution_config, ExecutionConfig
    from .execution.liquidity import get_liquidity_estimator, LiquidityEstimator
    from .execution.calendars import get_market_calendar, get_session_phase
    from .execution.slippage import compute_slippage_bps
    EXECUTION_STACK_AVAILABLE = True
except ImportError:
    EXECUTION_STACK_AVAILABLE = False

# Metrics imports
try:
    from .metrics import record_netting_savings, record_execution_policy
    METRICS_AVAILABLE = True
except ImportError:
    METRICS_AVAILABLE = False


# =============================================================================
# IBKR Maintenance Window Configuration
# =============================================================================
# IBKR has regular maintenance windows when connections may be unstable:
# - Weekly: Sunday 23:45 - Monday 00:45 UTC (system restart)
# - Daily: 22:00 - 22:15 UTC (possible brief disconnects)
#
# During these windows, we should avoid placing orders.

IBKR_MAINTENANCE_WINDOWS = [
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


def is_maintenance_window(now: Optional[datetime] = None) -> tuple:
    """
    Check if the current time is within an IBKR maintenance window.

    Args:
        now: Optional datetime (UTC), defaults to current time

    Returns:
        Tuple of (is_maintenance: bool, window_name: str, minutes_remaining: int)
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

try:
    from .alerts import AlertManager
    ALERTS_AVAILABLE = True
except ImportError:
    ALERTS_AVAILABLE = False

try:
    from .metrics import (
        start_metrics_server,
        update_ib_connection_state,
        update_portfolio_metrics,
        update_risk_metrics,
        record_order_submitted,
        record_order_filled,
        record_order_rejected,
        record_scheduler_run,
    )
    METRICS_AVAILABLE = True
except ImportError:
    METRICS_AVAILABLE = False


class DailyScheduler:
    """
    Orchestrates daily trading operations.
    This is the main entrypoint for the automated trading system.
    """

    def __init__(
        self,
        config_path: str = "config/settings.yaml",
        instruments_path: str = "config/instruments.yaml",
        state_dir: str = "state",
        dry_run: bool = False
    ):
        """
        Initialize the scheduler.

        Args:
            config_path: Path to settings.yaml
            instruments_path: Path to instruments.yaml
            state_dir: Directory for state files
            dry_run: If True, compute everything but do not submit orders
        """
        self.dry_run = dry_run
        # Load configuration
        self.settings = load_settings(config_path)
        self.instruments = load_instruments_config(instruments_path)
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

        # INVARIANT CHECK: Validate instruments config at startup
        config_valid, config_errors = validate_instruments_config(self.instruments)
        if not config_valid:
            raise InvariantError(
                f"Invalid instruments config: {config_errors}. "
                f"Fix config/instruments.yaml before running."
            )

        # Set up logging
        log_file = self.state_dir / "logs" / f"trading_{date.today().isoformat()}.log"
        setup_logging(log_level="INFO", log_file=str(log_file))
        self.logger = get_trading_logger()

        # Initialize components
        # Environment variables take precedence over settings.yaml
        self.mode = os.environ.get('MODE', self.settings.get('mode', 'paper'))
        self.is_paper = self.mode == 'paper'

        # IB connection settings - prioritize environment variables for Docker
        ibkr_settings = self.settings.get('ibkr', {})
        self.ib_host = os.environ.get('IBKR_HOST', ibkr_settings.get('host', '127.0.0.1'))
        default_port = ibkr_settings.get('paper_port' if self.is_paper else 'live_port', 4002)
        self.ib_port = int(os.environ.get('IBKR_PORT', default_port))
        self.ib_client_id = ibkr_settings.get('client_id', 1)

        # Components (initialized on run)
        self.reconnect_manager: Optional[IBReconnectManager] = None
        self.ib_client: Optional[IBClient] = None
        self.data_feed: Optional[DataFeed] = None
        self.risk_engine: Optional[RiskEngine] = None
        self.strategy: Optional[Strategy] = None
        self.tail_hedge_manager: Optional[TailHedgeManager] = None
        self.execution_engine: Optional[ExecutionEngine] = None
        self.alert_manager: Optional[Any] = None

        # EXECUTION_STACK_UPGRADE: New execution components
        self.ibkr_transport: Optional[IBKRTransport] = None
        self.execution_policy: Optional[Any] = None  # ExecutionPolicy
        self.execution_config: Optional[Any] = None  # ExecutionConfig
        self.order_manager: Optional[Any] = None  # OrderManager
        self.basket_executor: Optional[Any] = None  # BasketExecutor
        self.pair_executor: Optional[Any] = None  # PairExecutor
        self.execution_analytics: Optional[Any] = None  # ExecutionAnalytics
        self.liquidity_estimator: Optional[Any] = None  # LiquidityEstimator
        self.market_calendar: Optional[Any] = None  # MarketCalendar

        # State
        self.portfolio: Optional[PortfolioState] = None
        self.returns_history: pd.Series = pd.Series(dtype=float)
        self.fx_rates: Optional[FXRates] = None
        self.fx_rates_valid: bool = False

        # ROADMAP Phase A: Run Ledger for exactly-once execution
        self.run_ledger: Optional[Any] = None
        self.current_run: Optional[Any] = None
        if RUN_LEDGER_AVAILABLE:
            run_ledger_config = self.settings.get('run_ledger', {})
            if run_ledger_config.get('enabled', True):
                db_path = run_ledger_config.get('db_path', 'state/run_ledger.db')
                self.run_ledger = RunLedger(db_path)

        # Legacy Unwind Glidepath for gradual position transitions
        self.legacy_glidepath: Optional[LegacyUnwindGlidepath] = None

    def initialize(self) -> bool:
        """
        Initialize all components.

        Returns:
            True if initialization successful
        """
        self.logger.logger.info("scheduler_init", mode=self.mode)

        try:
            # Initialize alert manager FIRST so it can be passed to other components
            if ALERTS_AVAILABLE:
                try:
                    self.alert_manager = AlertManager(self.settings)
                    self.logger.logger.info("alert_manager_initialized")
                except Exception as e:
                    self.logger.logger.warning(f"alert_manager_init_failed: {e}")

            # Initialize reconnect manager and connect
            self.reconnect_manager = IBReconnectManager(
                host=self.ib_host,
                port=self.ib_port,
                client_id=self.ib_client_id,
                logger=self.logger
            )

            if not self.reconnect_manager.connect():
                self.logger.log_alert(
                    alert_type="init_failed",
                    severity="error",
                    message="Failed to connect to IB Gateway"
                )
                if self.alert_manager:
                    self.alert_manager.send_connection_error("Failed to connect to IB Gateway during initialization")
                return False

            # Initialize IB client with alert manager for disconnect notifications
            self.ib_client = IBClient(
                host=self.ib_host,
                port=self.ib_port,
                client_id=self.ib_client_id + 1,  # Use different client ID
                logger=self.logger,
                alert_manager=self.alert_manager
            )
            self.ib_client.connect()

            # Initialize data feed
            self.data_feed = DataFeed(
                ib=self.reconnect_manager.ib,
                instruments_config=self.instruments,
                settings=self.settings
            )

            # Initialize risk engine
            self.risk_engine = RiskEngine(self.settings)

            # Initialize tail hedge manager
            self.tail_hedge_manager = TailHedgeManager(
                settings=self.settings,
                instruments_config=self.instruments
            )

            # Initialize strategy
            self.strategy = Strategy(
                settings=self.settings,
                instruments_config=self.instruments,
                risk_engine=self.risk_engine,
                tail_hedge_manager=self.tail_hedge_manager
            )

            # Initialize execution engine (legacy)
            self.execution_engine = ExecutionEngine(
                ib_client=self.ib_client,
                instruments_config=self.instruments,
                logger=self.logger
            )

            # EXECUTION_STACK_UPGRADE: Initialize new execution stack
            if EXECUTION_STACK_AVAILABLE:
                try:
                    # Load ExecutionConfig from settings using proper loader
                    self.execution_config = load_execution_config(self.settings)

                    # Initialize IBKRTransport
                    self.ibkr_transport = IBKRTransport(
                        ib_client=self.ib_client,
                        instruments_config=self.instruments,
                        logger=self.logger,
                    )

                    # Initialize ExecutionPolicy with proper config
                    self.execution_policy = ExecutionPolicy(self.execution_config)

                    # Initialize ExecutionAnalytics
                    self.execution_analytics = ExecutionAnalytics()

                    # Initialize LiquidityEstimator (singleton)
                    self.liquidity_estimator = get_liquidity_estimator()

                    # Initialize MarketCalendar (singleton)
                    self.market_calendar = get_market_calendar()

                    # Define callbacks for OrderManager
                    def on_fill(ticket):
                        """Log fill progress when order receives fills."""
                        if ticket.avg_fill_price:
                            self.logger.logger.info(
                                "order_fill",
                                extra={
                                    "instrument_id": ticket.intent.instrument_id,
                                    "filled_qty": ticket.filled_qty,
                                    "remaining_qty": ticket.remaining_qty,
                                    "avg_price": ticket.avg_fill_price,
                                }
                            )

                    def on_complete(result):
                        """Record completed order in analytics and log."""
                        # Record in ExecutionAnalytics for reporting
                        if self.execution_analytics:
                            asset_class = self._get_asset_class(result.ticket.intent.instrument_id)
                            self.execution_analytics.record_order_complete(
                                result=result,
                                asset_class=asset_class,
                            )

                        # Log completion
                        self.logger.logger.info(
                            "order_complete",
                            extra={
                                "instrument_id": result.ticket.intent.instrument_id,
                                "status": result.ticket.status.value,
                                "filled_qty": result.fill_qty,
                                "slippage_bps": result.slippage_bps,
                                "commission": result.commission,
                            }
                        )

                    # Initialize OrderManager with callbacks
                    self.order_manager = OrderManager(
                        transport=self.ibkr_transport,
                        policy=self.execution_policy,
                        on_fill=on_fill,
                        on_complete=on_complete,
                    )

                    # Initialize BasketExecutor
                    # BasketExecutor expects config and instruments
                    self.basket_executor = BasketExecutor(
                        config=self.execution_config,
                        instruments={},  # Instrument specs loaded dynamically
                    )

                    # Initialize PairExecutor for legging protection
                    self.pair_executor = PairExecutor(
                        order_manager=self.order_manager,
                        max_legging_seconds=self.execution_config.pair_max_legging_seconds,
                        hedge_trigger_fill_pct=self.execution_config.pair_min_hedge_trigger_fill_pct,
                    )

                    self.logger.logger.info(
                        "execution_stack_initialized",
                        extra={
                            "policy": self.execution_config.default_policy.value,
                            "ttl_seconds": self.execution_config.order_ttl_seconds,
                            "pair_executor_enabled": True,
                        }
                    )

                except Exception as e:
                    self.logger.logger.warning(f"execution_stack_init_failed: {e}")
                    import traceback
                    traceback.print_exc()

            # Load portfolio state
            self.portfolio = load_portfolio_state(
                str(self.state_dir / "portfolio_state.json")
            )
            if self.portfolio is None:
                # Initialize new portfolio FROM BROKER REALITY (not config)
                # This prevents phantom positions from config mismatches
                broker_nav = None
                if self.ib_client and self.ib_client.is_connected():
                    broker_nav = self.ib_client.get_account_nav()
                    self.logger.logger.info(
                        "portfolio_init_from_broker",
                        broker_nav=broker_nav
                    )

                if broker_nav and broker_nav > 0:
                    # Use broker NAV as source of truth
                    initial_capital = broker_nav
                else:
                    # Fallback only if broker unavailable (should not happen in production)
                    initial_capital = self.settings.get('backtest', {}).get('initial_capital', 1000000)
                    self.logger.logger.warning(
                        "portfolio_init_fallback_to_config",
                        reason="broker_nav_unavailable",
                        config_capital=initial_capital
                    )

                self.portfolio = PortfolioState(
                    nav=initial_capital,
                    cash_by_ccy={"USD": initial_capital},
                    initial_capital=initial_capital,
                    inception_date=date.today()
                )

                # Sync positions from broker on fresh init
                if self.ib_client and self.ib_client.is_connected():
                    ib_positions = self.ib_client.get_positions(self.instruments)
                    for inst_id, position in ib_positions.items():
                        self.portfolio.positions[inst_id] = position
                    self.logger.logger.info(
                        "portfolio_positions_synced_from_broker",
                        position_count=len(ib_positions)
                    )

            # Load returns history
            self.returns_history = load_returns_history(
                str(self.state_dir / "returns_history.csv")
            )

            # Initialize FX rates service
            self.fx_rates = get_fx_rates(refresh=True, ib=self.reconnect_manager.ib)
            self.fx_rates_valid = not self.fx_rates.is_stale()
            self.logger.logger.info(
                "fx_rates_initialized",
                valid=self.fx_rates_valid,
                timestamp=self.fx_rates.timestamp.isoformat() if self.fx_rates.timestamp else None
            )

            # Note: AlertManager is already initialized at the start of initialize()

            # Initialize Legacy Unwind Glidepath for gradual position transitions
            self.legacy_glidepath = create_glidepath(self.settings)
            if self.legacy_glidepath.enabled:
                self.logger.logger.info(
                    "legacy_glidepath_initialized",
                    enabled=True,
                    unwind_days=self.legacy_glidepath.unwind_days,
                    has_snapshot=self.legacy_glidepath.has_snapshot()
                )

            self.logger.logger.info("scheduler_init_complete")
            return True

        except Exception as e:
            self.logger.log_alert(
                alert_type="init_error",
                severity="error",
                message=f"Initialization error: {e}"
            )
            return False

    def run_daily(self) -> Dict[str, Any]:
        """
        Execute the daily trading routine.

        Returns:
            Summary of daily run
        """
        run_summary = {
            "date": date.today().isoformat(),
            "mode": self.mode,
            "dry_run": self.dry_run,
            "success": False,
            "steps_completed": [],
            "errors": [],
            "orders_placed": 0,
            "orders_filled": 0,
            "maintenance_window": False,
            "fx_rates_valid": False,
            "reconciliation_status": "NOT_CHECKED"
        }

        if self.dry_run:
            self.logger.logger.info("dry_run_mode_active")

        # Check for maintenance window
        in_maintenance, window_name, minutes_remaining = is_maintenance_window()
        if in_maintenance:
            run_summary["maintenance_window"] = True
            run_summary["maintenance_window_name"] = window_name
            run_summary["maintenance_minutes_remaining"] = minutes_remaining
            self.logger.logger.warning(
                f"IBKR maintenance window active: {window_name}, "
                f"~{minutes_remaining} minutes remaining. Skipping order execution."
            )

        try:
            # Step 1: Initialize if not already done
            if self.portfolio is None:
                if not self.initialize():
                    run_summary["errors"].append("Initialization failed")
                    return run_summary
            run_summary["steps_completed"].append("initialize")

            # ROADMAP Phase A: Check for duplicate run (exactly-once execution)
            if self.run_ledger and RUN_LEDGER_AVAILABLE:
                try:
                    existing_run = self.run_ledger.get_run_for_date(date.today())
                    if existing_run and existing_run.status in [RunStatus.SUBMITTED, RunStatus.FILLED, RunStatus.DONE]:
                        self.logger.logger.info(
                            "run_already_completed",
                            run_id=existing_run.run_id,
                            status=existing_run.status.value
                        )
                        run_summary["skipped_duplicate"] = True
                        run_summary["existing_run_id"] = existing_run.run_id
                        run_summary["success"] = True
                        return run_summary
                except Exception as e:
                    self.logger.logger.warning(f"run_ledger_check_failed: {e}")
            run_summary["steps_completed"].append("idempotency_check")

            # Step 1.5: Refresh FX rates (ENGINE_FIX_PLAN Phase 1)
            self._refresh_fx_rates()
            run_summary["fx_rates_valid"] = self.fx_rates_valid
            run_summary["steps_completed"].append("refresh_fx_rates")

            # Step 2: Sync positions from IB
            self._sync_positions()
            run_summary["steps_completed"].append("sync_positions")

            # Step 2.3: Reconcile with broker (ENGINE_FIX_PLAN Phase 3)
            recon_passed = self._reconcile_with_broker()
            run_summary["reconciliation_status"] = self.portfolio.reconciliation_status
            run_summary["steps_completed"].append("broker_reconciliation")

            # CRITICAL: If reconciliation fails, do not proceed with trading
            if not recon_passed and not in_maintenance:
                run_summary["errors"].append(
                    f"Broker reconciliation failed: {self.portfolio.reconciliation_status}"
                )
                self.logger.log_alert(
                    alert_type="reconciliation_failed",
                    severity="error",
                    message=f"NAV reconciliation failed: {self.portfolio.reconciliation_status}, "
                            f"diff={self.portfolio.reconciliation_diff_pct:.4%}"
                )
                if self.alert_manager:
                    self.alert_manager.send_alert(
                        alert_type="risk",
                        severity="error",
                        title="NAV Reconciliation Failed",
                        message=f"Trading halted. Status: {self.portfolio.reconciliation_status}, "
                                f"Diff: {self.portfolio.reconciliation_diff_pct:.4%}"
                    )
                # Still save state and send summary, but don't trade
                self._save_state()
                self._send_daily_summary()
                return run_summary

            # Step 2.5: Check for expiring futures and auto-roll
            if not in_maintenance:
                rollover_results = self._check_futures_rollover()
                run_summary["futures_rolled"] = len([r for r in rollover_results if r.success])
                run_summary["steps_completed"].append("futures_rollover")

            # Step 3: Update NAV
            # ENGINE_FIX_PLAN Phase 2: Pass FX rates for NAV calculation
            self.portfolio.compute_nav(self.data_feed, fx_rates=self.fx_rates)
            run_summary["nav"] = self.portfolio.nav
            run_summary["steps_completed"].append("compute_nav")

            # Step 4: Compute risk metrics
            risk_decision = self._compute_risk()
            run_summary["risk_regime"] = risk_decision.regime.value
            run_summary["scaling_factor"] = risk_decision.scaling_factor
            run_summary["steps_completed"].append("compute_risk")

            # Step 5: Compute strategy targets
            strategy_output = self._compute_strategy_targets(risk_decision)
            run_summary["strategy_orders"] = len(strategy_output.orders)
            run_summary["steps_completed"].append("compute_strategy")

            # Step 5.5: Legacy Unwind Glidepath - blend positions for gradual transition
            if self.legacy_glidepath and self.legacy_glidepath.enabled:
                glidepath_result = self._apply_legacy_glidepath(strategy_output)
                if glidepath_result:
                    strategy_output, glidepath_diagnostics = glidepath_result
                    run_summary["glidepath_applied"] = glidepath_diagnostics.get('blending_applied', False)
                    run_summary["glidepath_alpha"] = glidepath_diagnostics.get('alpha', 1.0)
                    run_summary["glidepath_days_elapsed"] = glidepath_diagnostics.get('days_elapsed', 0)
                run_summary["steps_completed"].append("legacy_glidepath")

            # Step 6: Manage tail hedges
            hedge_orders = self._manage_hedges()
            run_summary["hedge_orders"] = len(hedge_orders)
            run_summary["steps_completed"].append("manage_hedges")

            # Step 7: Check for crisis
            crisis_orders, crisis_action = self._check_crisis()
            run_summary["crisis_action"] = crisis_action.action_type
            run_summary["steps_completed"].append("check_crisis")

            # Step 8: Execute orders (skip during maintenance windows)
            all_orders = strategy_output.orders + hedge_orders + crisis_orders

            # ROADMAP Phase A: Begin run and record intents
            if self.run_ledger and RUN_LEDGER_AVAILABLE and all_orders:
                try:
                    # Compute inputs hash for idempotency
                    inputs_snapshot = {
                        'nav': self.portfolio.nav,
                        'positions': list(self.portfolio.positions.keys()),
                        'fx_valid': self.fx_rates_valid,
                    }
                    inputs_hash = compute_inputs_hash(inputs_snapshot, {}, {})

                    # Compute intents hash
                    intents_data = [
                        {'instrument': o.instrument_id, 'side': o.side, 'qty': o.quantity}
                        for o in all_orders
                    ]
                    intents_hash = compute_intents_hash(intents_data)

                    # Begin run
                    self.current_run = self.run_ledger.begin_run(
                        run_date=date.today(),
                        strategy_version="v1.0",
                        inputs_hash=inputs_hash,
                        metadata={'order_count': len(all_orders), 'regime': risk_decision.regime.value}
                    )
                    self.run_ledger.record_intents(self.current_run.run_id, intents_hash)
                    run_summary["run_id"] = self.current_run.run_id

                    # Pre-record orders with client order IDs
                    for order in all_orders:
                        client_order_id = TradingRun.generate_client_order_id(
                            run_id=self.current_run.run_id,
                            instrument_id=order.instrument_id,
                            side=order.side,
                            quantity=order.quantity,
                            sleeve=getattr(order, 'sleeve', 'unknown')
                        )
                        order_record = OrderRecord(
                            client_order_id=client_order_id,
                            instrument_id=order.instrument_id,
                            side=order.side,
                            quantity=order.quantity,
                            sleeve=getattr(order, 'sleeve', 'unknown'),
                            status='pending'
                        )
                        self.run_ledger.record_order(self.current_run.run_id, order_record)

                except Exception as e:
                    self.logger.logger.warning(f"run_ledger_begin_failed: {e}")

            if all_orders:
                if self.dry_run:
                    # Dry-run mode: log orders but do not execute
                    self.logger.logger.info(
                        "dry_run_orders",
                        extra={
                            "order_count": len(all_orders),
                            "orders": [
                                {"instrument": o.instrument_id, "side": o.side, "qty": o.quantity}
                                for o in all_orders
                            ]
                        }
                    )
                    run_summary["dry_run"] = True
                    run_summary["dry_run_orders"] = len(all_orders)
                    run_summary["orders_placed"] = 0
                    run_summary["orders_filled"] = 0
                    print("\n" + "="*60)
                    print("DRY RUN - Orders would be executed:")
                    print("="*60)
                    for order in all_orders:
                        print(f"  {order.side:4} {order.quantity:6} {order.instrument_id}")
                    print("="*60 + "\n")
                elif in_maintenance:
                    # Skip order execution during maintenance
                    self.logger.logger.warning(
                        f"Skipping {len(all_orders)} orders due to maintenance window"
                    )
                    run_summary["orders_skipped"] = len(all_orders)
                    run_summary["orders_placed"] = 0
                    run_summary["orders_filled"] = 0
                else:
                    # ROADMAP Phase A: Mark run as submitted before execution
                    if self.run_ledger and self.current_run:
                        try:
                            self.run_ledger.mark_submitted(self.current_run.run_id)
                        except Exception as e:
                            self.logger.logger.warning(f"run_ledger_mark_submitted_failed: {e}")

                    execution_results = self._execute_orders(all_orders)
                    run_summary["orders_placed"] = execution_results.get("total_orders", 0)
                    run_summary["orders_filled"] = execution_results.get("filled", 0)

                    # EXECUTION_STACK_UPGRADE: Add execution stack metrics
                    if execution_results.get("execution_stack"):
                        run_summary["execution_stack"] = True
                        run_summary["orders_netted"] = execution_results.get("netted_orders", 0)
                        run_summary["orders_cancelled"] = execution_results.get("cancelled", 0)
                        run_summary["avg_slippage_bps"] = execution_results.get("avg_slippage_bps", 0.0)
                        run_summary["total_commission"] = execution_results.get("total_commission", 0.0)

                    # ROADMAP Phase A: Mark run as done after execution
                    if self.run_ledger and self.current_run:
                        try:
                            if execution_results.get("filled", 0) > 0:
                                self.run_ledger.mark_done(self.current_run.run_id)
                            elif execution_results.get("rejected", 0) > 0 or execution_results.get("safety_blocked"):
                                self.run_ledger.mark_aborted(
                                    self.current_run.run_id,
                                    reason=execution_results.get("safety_reason", "execution_failed")
                                )
                        except Exception as e:
                            self.logger.logger.warning(f"run_ledger_mark_done_failed: {e}")

            run_summary["steps_completed"].append("execute_orders")

            # Step 9: Record daily P&L
            self._record_daily_pnl()
            run_summary["daily_return"] = self.portfolio.daily_return
            run_summary["steps_completed"].append("record_pnl")

            # Step 10: Save state
            self._save_state()
            run_summary["steps_completed"].append("save_state")

            # Step 11: Send alerts
            self._send_daily_summary()
            run_summary["steps_completed"].append("send_alerts")

            run_summary["success"] = True
            self.logger.logger.info("daily_run_complete", summary=run_summary)

        except Exception as e:
            run_summary["errors"].append(str(e))
            self.logger.log_alert(
                alert_type="daily_run_error",
                severity="error",
                message=f"Daily run failed: {e}"
            )

        return run_summary

    def _sync_positions(self) -> None:
        """Sync positions from IB.

        ENGINE_FIX_PLAN: Uses centralized FX rates for currency conversion.
        """
        if not self.ib_client or not self.ib_client.is_connected():
            return

        ib_positions = self.ib_client.get_positions(self.instruments)

        # CRITICAL: Replace internal positions with broker positions
        # This prevents phantom positions from persisting in internal state
        # when they no longer exist at the broker (e.g., closed trades, rollovers)
        old_position_count = len(self.portfolio.positions)
        self.portfolio.positions.clear()

        for inst_id, position in ib_positions.items():
            # INVARIANT CHECK: Position ID must be a config ID, not IBKR symbol
            try:
                assert_position_id_valid(
                    inst_id,
                    self.instruments,
                    context=f"position_sync: {position.quantity} @ {position.market_price}"
                )
            except InvariantError as e:
                self.logger.logger.error(
                    "position_id_invariant_violation",
                    error=str(e),
                    instrument_id=inst_id,
                )
                raise  # Fail fast - this is a critical bug

            self.portfolio.positions[inst_id] = position

        new_position_count = len(self.portfolio.positions)
        if old_position_count != new_position_count:
            self.logger.logger.info(
                "positions_synced_from_broker",
                old_count=old_position_count,
                new_count=new_position_count,
                synced_positions=list(ib_positions.keys())
            )

        # Sync cash balances from IB (critical for accurate NAV)
        self._sync_cash_from_ib()

        # Compute NAV before reconciliation (positions and cash are synced)
        # This ensures portfolio.nav is accurate for broker reconciliation
        self.portfolio.compute_nav(self.data_feed, fx_rates=self.fx_rates)

        # ENGINE_FIX_PLAN Phase 2: Pass FX rates for exposure calculation
        self.portfolio.compute_exposures(fx_rates=self.fx_rates)
        self.portfolio.compute_sleeve_exposures()

        self.logger.log_portfolio_snapshot(
            nav=self.portfolio.nav,
            gross_exposure=self.portfolio.gross_exposure,
            net_exposure=self.portfolio.net_exposure,
            realized_vol=self.portfolio.realized_vol_annual,
            max_drawdown=self.portfolio.max_drawdown,
            sleeve_weights=self.portfolio.get_sleeve_weights(),
            hedge_budget_used=self.portfolio.hedge_budget_used_ytd
        )

    def _sync_cash_from_ib(self) -> None:
        """Sync cash balances from IB account values.

        IMPORTANT: For margin accounts, we use TotalCashValue (net cash in account
        base currency) rather than CashBalance per currency. The per-currency
        CashBalance values don't properly account for margin borrowing.

        For example, if you buy USD stocks with EUR margin:
        - CashBalance (EUR): +452k (your collateral)
        - CashBalance (USD): -707k (your margin loan)
        - TotalCashValue: -149k (the true net cash after FX conversion)
        """
        if not self.ib_client or not self.ib_client.is_connected():
            return

        try:
            account_values = self.ib_client.ib.accountValues()
            total_cash_base = None

            for av in account_values:
                # TotalCashValue in BASE or account base currency (EUR)
                # This is the proper net cash after margin accounting
                if av.tag == "TotalCashValue" and av.currency in ["BASE", "EUR"]:
                    total_cash_base = float(av.value)
                    break

            if total_cash_base is not None:
                # Store in EUR (account base currency)
                self.portfolio.cash_by_ccy = {"EUR": total_cash_base}
                self.logger.logger.debug(
                    "cash_synced_from_ib",
                    total_cash_eur=total_cash_base
                )
        except Exception as e:
            self.logger.logger.warning(
                "cash_sync_failed",
                error=str(e)
            )

    def _refresh_fx_rates(self) -> None:
        """
        Refresh FX rates from IB or fallback sources.

        ENGINE_FIX_PLAN Phase 1: Centralized FX rate management.
        Must be called before any NAV/exposure calculations.
        """
        try:
            if self.fx_rates is None:
                self.fx_rates = get_fx_rates(refresh=True, ib=self.reconnect_manager.ib)
            else:
                self.fx_rates.refresh(ib=self.reconnect_manager.ib)

            self.fx_rates_valid = not self.fx_rates.is_stale()

            self.logger.logger.info(
                "fx_rates_refreshed",
                valid=self.fx_rates_valid,
                timestamp=self.fx_rates.timestamp.isoformat() if self.fx_rates.timestamp else None,
                rates_count=len(self.fx_rates.rates)
            )

            if not self.fx_rates_valid:
                self.logger.log_alert(
                    alert_type="fx_rates_stale",
                    severity="warning",
                    message=f"FX rates are stale (>{self.fx_rates.max_age_hours}h old), using cached values"
                )

        except Exception as e:
            self.fx_rates_valid = False
            self.logger.log_alert(
                alert_type="fx_rates_error",
                severity="warning",
                message=f"Failed to refresh FX rates: {e}"
            )

    def _reconcile_with_broker(self) -> bool:
        """
        Reconcile portfolio NAV with broker-reported values.

        ENGINE_FIX_PLAN Phase 3: Broker reconciliation circuit breaker.
        Returns False if trading should be halted due to large discrepancy.

        Returns:
            True if reconciliation passed or not critical, False if trading should halt
        """
        if not self.ib_client or not self.ib_client.is_connected():
            self.logger.logger.warning("reconciliation_skipped", reason="IB not connected")
            return True  # Don't block trading if we can't check

        try:
            # Compute broker NAV from positions + cash (more accurate than NetLiquidation)
            # NetLiquidation can differ due to accrued interest, pending commissions, etc.
            broker_nav = self.ib_client.get_computed_nav(self.fx_rates)

            if broker_nav is None or broker_nav <= 0:
                # Fallback to NetLiquidation if computed NAV fails
                broker_nav_base = self.ib_client.get_account_nav()
                if broker_nav_base is None or broker_nav_base <= 0:
                    self.logger.logger.warning("reconciliation_skipped", reason="Invalid broker NAV")
                    return True
                # Convert from EUR to USD
                broker_nav = self.fx_rates.to_base(broker_nav_base, "EUR")

            # Perform reconciliation with 0.5% threshold
            # (increased from 0.25% to account for timing differences)
            status = self.portfolio.reconcile_with_broker(
                broker_nav,
                halt_threshold_pct=0.005  # 0.5%
            )

            self.logger.logger.info(
                "broker_reconciliation",
                status=status,
                internal_nav=self.portfolio.nav,
                broker_nav=broker_nav,
                diff_pct=self.portfolio.reconciliation_diff_pct
            )

            # Check if we should halt trading
            if status in ["HALT", "EMERGENCY"]:
                return False

            return True

        except Exception as e:
            self.logger.log_alert(
                alert_type="reconciliation_error",
                severity="warning",
                message=f"Failed to reconcile with broker: {e}"
            )
            return True  # Don't block trading on reconciliation errors

    def _check_futures_rollover(self) -> List:
        """
        Check for expiring futures positions and auto-roll them.

        Returns:
            List of RolloverResult objects
        """
        if not self.ib_client or not self.ib_client.is_connected():
            self.logger.logger.warning("futures_rollover_skipped", reason="IB not connected")
            return []

        try:
            # Get settings for rollover
            settings = load_settings()
            rollover_config = settings.get('futures_rollover', {})
            days_before = rollover_config.get('days_before_expiry', 3)
            dry_run = rollover_config.get('dry_run', False)

            self.logger.logger.info(
                "futures_rollover_check",
                days_before=days_before,
                dry_run=dry_run
            )

            # Run the rollover check
            results = check_and_roll_futures(
                ib=self.ib_client.ib,
                days_before_expiry=days_before,
                logger=self.logger,
                alert_manager=self.alert_manager,
                dry_run=dry_run
            )

            return results

        except Exception as e:
            self.logger.logger.error("futures_rollover_error", error=str(e))
            if self.alert_manager:
                self.alert_manager.send_alert(
                    alert_type="system",
                    severity="error",
                    title="Futures Rollover Error",
                    message=f"Error during futures rollover check: {e}"
                )
            return []

    def _compute_risk(self) -> RiskDecision:
        """Compute risk decision."""
        # Get VIX level
        vix_level = self.data_feed.get_vix_level()

        # Get ratio series for momentum
        ratio_series = self.data_feed.get_ratio_series_spx_sx5e(lookback_days=252)

        # Evaluate risk
        risk_decision = self.risk_engine.evaluate_risk(
            portfolio_state=self.portfolio,
            returns_series=self.returns_history,
            vix_level=vix_level,
            ratio_series=ratio_series
        )

        # Log risk decision with vol burn-in and scaling clamp diagnostics
        diag = risk_decision.scaling_diagnostics or {}
        self.logger.log_risk_decision(
            decision_type="daily_risk_eval",
            scaling_factor=risk_decision.scaling_factor,
            realized_vol=self.portfolio.realized_vol_annual,
            target_vol=self.risk_engine.vol_target_annual,
            max_drawdown=self.risk_engine.max_drawdown_pct,
            current_drawdown=self.portfolio.current_drawdown,
            emergency_derisk=risk_decision.emergency_derisk
        )

        # Log vol burn-in and scaling clamp diagnostics
        self.logger.logger.info(
            "scaling_diagnostics",
            extra={
                "history_days": diag.get('history_days', 0),
                "raw_realized_vol": diag.get('raw_realized_vol'),
                "effective_vol": diag.get('effective_vol'),
                "burn_in_active": diag.get('burn_in_active', False),
                "raw_scaling": diag.get('raw_scaling'),
                "clamped_scaling": diag.get('clamped_scaling'),
                "clamp_applied": diag.get('clamp_applied', False),
                "final_scaling": diag.get('final_scaling'),
            }
        )

        return risk_decision

    def _compute_strategy_targets(self, risk_decision: RiskDecision):
        """Compute strategy target positions.

        ENGINE_FIX_PLAN Phase 4/5: Pass FX rates for currency-correct sizing
        and portfolio-level FX hedging.
        """
        return self.strategy.compute_all_sleeve_targets(
            portfolio=self.portfolio,
            data_feed=self.data_feed,
            risk_decision=risk_decision,
            fx_rates=self.fx_rates  # ENGINE_FIX_PLAN: Pass centralized FX rates
        )

    def _apply_legacy_glidepath(self, strategy_output) -> Optional[tuple]:
        """
        Apply legacy unwind glidepath to blend strategy targets with initial positions.

        On first run: saves current positions as snapshot, returns None (no blending).
        On subsequent runs: blends targets with initial snapshot based on elapsed days.

        Args:
            strategy_output: StrategyOutput from _compute_strategy_targets

        Returns:
            Tuple of (modified_strategy_output, diagnostics) or None if first run
        """
        if not self.legacy_glidepath:
            return None

        # Check if this is the first run (no snapshot exists)
        if self.legacy_glidepath.is_first_run():
            # Get current positions from portfolio as quantities
            current_positions = {
                inst_id: pos.quantity
                for inst_id, pos in self.portfolio.positions.items()
            }

            # Save snapshot for future blending
            first_run_diagnostics = self.legacy_glidepath.handle_first_run(current_positions)

            self.logger.logger.info(
                "legacy_glidepath_first_run",
                positions_saved=len(current_positions),
                unwind_days=self.legacy_glidepath.unwind_days
            )

            # On first run (day 0), use current positions (NO TRADES)
            # This protects legacy positions during burn-in period
            # Tomorrow (day 1) we'll blend 10% toward targets, etc.
            first_run_diagnostics['blending_applied'] = True
            first_run_diagnostics['alpha'] = 0.0  # Day 0 = 100% initial, 0% target
            first_run_diagnostics['days_elapsed'] = 0

            # Create modified output with current positions as targets (no orders)
            from .strategy_logic import StrategyOutput

            no_trade_output = StrategyOutput(
                sleeve_targets=strategy_output.sleeve_targets,
                total_target_positions=current_positions,  # Use current, not targets
                orders=[],  # No orders on day 0
                scaling_factor=strategy_output.scaling_factor,
                regime=strategy_output.regime,
                commentary=strategy_output.commentary + f"\n[Glidepath Day 0: No trades, preserving {len(current_positions)} positions]"
            )

            self.logger.logger.info(
                "legacy_glidepath_day0_protection",
                positions_preserved=len(current_positions),
                target_positions=len(strategy_output.total_target_positions),
                orders_blocked=len(strategy_output.orders)
            )

            return no_trade_output, first_run_diagnostics

        # Blend positions with initial snapshot
        blended_positions, diagnostics = self.legacy_glidepath.blend_positions(
            strategy_output.total_target_positions
        )

        # If no blending was applied (fully converged), return original output
        if not diagnostics.get('blending_applied', False):
            return strategy_output, diagnostics

        # Re-generate orders based on blended positions
        from .strategy_logic import StrategyOutput, generate_rebalance_orders

        # Get current positions as quantities
        current_positions = {
            inst_id: pos.quantity
            for inst_id, pos in self.portfolio.positions.items()
        }

        # Generate new orders from blended targets
        blended_orders = generate_rebalance_orders(
            current_positions=current_positions,
            target_positions=blended_positions,
            instruments_config=self.instruments,
            min_trade_value=self.settings.get('execution', {}).get('min_trade_notional_usd', 500.0)
        )

        # Create modified strategy output with blended positions and orders
        modified_output = StrategyOutput(
            sleeve_targets=strategy_output.sleeve_targets,
            total_target_positions=blended_positions,
            orders=blended_orders,
            scaling_factor=strategy_output.scaling_factor,
            regime=strategy_output.regime,
            commentary=strategy_output.commentary + f"\n[Glidepath: alpha={diagnostics.get('alpha', 1.0):.2f}, day {diagnostics.get('days_elapsed', 0)}/{self.legacy_glidepath.unwind_days}]"
        )

        self.logger.logger.info(
            "legacy_glidepath_blended",
            alpha=diagnostics.get('alpha', 1.0),
            days_elapsed=diagnostics.get('days_elapsed', 0),
            original_orders=len(strategy_output.orders),
            blended_orders=len(blended_orders)
        )

        return modified_output, diagnostics

    def _manage_hedges(self) -> List[OrderSpec]:
        """Manage tail hedge positions."""
        return self.tail_hedge_manager.ensure_tail_hedges(
            portfolio_state=self.portfolio,
            data_feed=self.data_feed,
            today=date.today()
        )

    def _check_crisis(self) -> tuple:
        """Check for crisis conditions."""
        vix_level = self.data_feed.get_vix_level()

        return self.tail_hedge_manager.handle_crisis_if_any(
            portfolio_state=self.portfolio,
            data_feed=self.data_feed,
            vix_level=vix_level,
            daily_pnl=self.portfolio.daily_return
        )

    def _execute_orders(self, orders: List[OrderSpec]) -> Dict[str, Any]:
        """Execute orders via IBKR.

        EXECUTION_STACK_UPGRADE: Uses new execution stack if available:
        - Trade netting across sleeves via BasketExecutor
        - Marketable limit orders via ExecutionPolicy
        - Order state machine via OrderManager
        - Execution metrics via ExecutionAnalytics

        Falls back to legacy ExecutionEngine if new stack unavailable.

        ENGINE_FIX_PLAN Phase 9: Includes pre-execution safety checks.
        """
        # INVARIANT CHECK: No conflicting BUY/SELL for same instrument
        try:
            assert_no_conflicting_orders(
                orders,
                context=f"execute_orders: {len(orders)} orders"
            )
        except InvariantError as e:
            self.logger.logger.error(
                "conflicting_orders_invariant_violation",
                error=str(e),
                order_count=len(orders),
            )
            raise  # Fail fast - this indicates an ID mapping bug

        # ENGINE_FIX_PLAN Phase 9: Pre-execution safety checks
        safety_passed, safety_reasons = check_execution_safety(
            portfolio_state=self.portfolio,
            fx_rates_valid=self.fx_rates_valid,
            vol_estimate_valid=True,  # TODO: Add proper vol validation
            exchange="EU"  # Primary focus is European markets
        )
        safety_reason = "; ".join(safety_reasons) if safety_reasons else ""

        if not safety_passed:
            self.logger.log_alert(
                alert_type="execution_safety_failed",
                severity="error",
                message=f"Execution blocked by safety check: {safety_reason}"
            )
            if self.alert_manager:
                self.alert_manager.send_alert(
                    alert_type="risk",
                    severity="error",
                    title="Execution Safety Check Failed",
                    message=f"Orders blocked: {safety_reason}"
                )
            return {
                "total_orders": len(orders),
                "filled": 0,
                "rejected": len(orders),
                "safety_blocked": True,
                "safety_reason": safety_reason
            }

        # EXECUTION_STACK_UPGRADE: Use new execution stack if available
        if EXECUTION_STACK_AVAILABLE and self.order_manager and self.execution_policy:
            return self._execute_orders_new_stack(orders)

        # Fallback to legacy execution engine
        reports, summary = self.execution_engine.execute_strategy_orders(
            orders=orders,
            dry_run=False
        )
        return summary

    def _execute_orders_new_stack(self, orders: List[OrderSpec]) -> Dict[str, Any]:
        """
        Execute orders using the new execution stack.

        EXECUTION_STACK_UPGRADE: Full execution flow:
        1. Check session timing - skip if too close to open/close
        2. Convert OrderSpec to OrderIntent
        3. Net trades via BasketExecutor
        4. Get ADV from LiquidityEstimator
        5. Get session phase from MarketCalendar
        6. Create OrderPlan via ExecutionPolicy.create_plan()
        7. Submit via OrderManager
        8. Polling loop for order lifecycle management
        9. Record metrics in ExecutionAnalytics
        """
        summary = {
            "total_orders": len(orders),
            "filled": 0,
            "partial": 0,
            "rejected": 0,
            "cancelled": 0,
            "netted_orders": 0,
            "skipped_timing": 0,
            "total_commission": 0.0,
            "avg_slippage_bps": 0.0,
            "execution_stack": True,
        }

        if not orders:
            return summary

        # Step 1: Check session timing
        exchange = "NYSE"  # Default exchange for timing check
        if self.market_calendar:
            avoid_result, avoid_reason = should_avoid_trading(
                exchange=exchange,
                avoid_first_minutes=self.execution_config.avoid_first_minutes_after_open,
                avoid_last_minutes=self.execution_config.avoid_last_minutes_before_close,
            )
            if avoid_result:
                self.logger.logger.warning(
                    "execution_postponed_timing",
                    extra={"reason": avoid_reason, "order_count": len(orders)}
                )
                summary["skipped_timing"] = len(orders)
                # Still return - orders will execute next run
                return summary

            # Get current session phase
            session_phase = get_session_phase(exchange)
        else:
            session_phase = "regular"

        # Step 2: Convert OrderSpec to OrderIntent
        intents = []
        prices = {}

        # Build fallback prices from portfolio positions (IBKR already provides market prices)
        # Portfolio positions use IBKR symbols (e.g., "CSPX") while orders may use config IDs (e.g., "us_index_etf")
        position_prices = {}
        if hasattr(self, 'portfolio') and self.portfolio:
            for pos in self.portfolio.positions.values():
                if pos.market_price and pos.market_price > 0:
                    position_prices[pos.instrument_id] = pos.market_price

        # Build reverse mapping: config instrument_id -> IBKR symbol
        config_to_symbol = {}
        for category, insts in self.instruments.items():
            if isinstance(insts, dict):
                for inst_id, spec in insts.items():
                    if isinstance(spec, dict) and "symbol" in spec:
                        config_to_symbol[inst_id] = spec["symbol"]

        for order in orders:
            # Get current price for netting calculations
            price = self.data_feed.get_last_price(order.instrument_id)
            if not price:
                # Fallback 1: Try portfolio position price directly
                price = position_prices.get(order.instrument_id)
            if not price:
                # Fallback 2: Map config ID to IBKR symbol and look up
                ibkr_symbol = config_to_symbol.get(order.instrument_id)
                if ibkr_symbol:
                    price = position_prices.get(ibkr_symbol)
            if price:
                prices[order.instrument_id] = price

            intent = OrderIntent(
                instrument_id=order.instrument_id,
                side=order.side,
                quantity=int(abs(order.quantity)),
                reason=getattr(order, 'reason', 'rebalance'),
                sleeve=getattr(order, 'sleeve', 'unknown'),
                urgency="normal",
            )
            intents.append(intent)

        # Step 3: Net trades via BasketExecutor
        original_count = len(intents)
        if self.basket_executor:
            net_positions = self.basket_executor.net_trades(intents, prices)
            # Convert back to intents for execution - only non-zero positions
            netted_intents = []
            for net_pos in net_positions:
                if net_pos.net_qty != 0:
                    netted_intents.append(OrderIntent(
                        instrument_id=net_pos.instrument_id,
                        side="BUY" if net_pos.net_qty > 0 else "SELL",
                        quantity=abs(net_pos.net_qty),
                        reason="rebalance",
                        sleeve="netted",
                        urgency="normal",
                    ))
            summary["netted_orders"] = original_count - len(netted_intents)
            intents = netted_intents
            self.logger.logger.info(
                "trade_netting_applied",
                extra={"original": original_count, "netted": len(intents), "eliminated": summary["netted_orders"]}
            )

            # Record netting savings in Prometheus
            if METRICS_AVAILABLE and summary["netted_orders"] > 0:
                record_netting_savings(summary["netted_orders"])

        if not intents:
            self.logger.logger.info("All orders netted to zero")
            return summary

        # Step 4-7: Create OrderPlan and submit for each intent
        tickets = []
        for intent in intents:
            try:
                # Get market data for the instrument
                md = None
                if self.ibkr_transport:
                    md = self.ibkr_transport.get_market_data(intent.instrument_id)

                if md is None:
                    # Create minimal market data from last price
                    price = prices.get(intent.instrument_id)
                    if price:
                        md = MarketDataSnapshot(
                            symbol=intent.instrument_id,
                            ts=datetime.now(),
                            last=price,
                            close=price,
                        )
                    else:
                        self.logger.logger.error(f"No market data for {intent.instrument_id}")
                        summary["rejected"] += 1
                        continue

                # Step 4: Get ADV from LiquidityEstimator
                adv = None
                if self.liquidity_estimator:
                    adv = self.liquidity_estimator.get_adv(intent.instrument_id)

                # Determine asset class for slippage limits
                asset_class = self._get_asset_class(intent.instrument_id)

                # Step 6: Create order plan using correct method name
                plan, warning = self.execution_policy.create_plan(
                    intent=intent,
                    md=md,
                    asset_class=asset_class,
                    session_phase=session_phase,
                    adv=adv,
                )

                if warning:
                    self.logger.logger.info(f"Execution warning for {intent.instrument_id}: {warning}")

                # Step 7: Submit via OrderManager
                ticket = self.order_manager.submit(intent, plan, md)
                tickets.append(ticket)

            except ValueError as e:
                # Validation errors from ExecutionPolicy
                self.logger.logger.warning(f"Order validation failed: {intent.instrument_id} - {e}")
                summary["rejected"] += 1
            except Exception as e:
                self.logger.logger.error(f"Order submission failed: {intent.instrument_id} - {e}")
                summary["rejected"] += 1

        # Step 8: Polling loop for order lifecycle management
        if tickets:
            timeout_seconds = self.execution_config.order_ttl_seconds
            poll_interval_seconds = 5  # Check orders every 5 seconds
            start_time = time.time()

            self.logger.logger.info(
                "order_polling_started",
                extra={"ticket_count": len(tickets), "timeout_seconds": timeout_seconds}
            )

            while time.time() - start_time < timeout_seconds:
                # Process all active orders (handles TTL, replaces, etc.)
                updated = self.order_manager.process_all()

                # Check if all done
                active = self.order_manager.get_active_tickets()
                if not active:
                    break

                # Log progress
                filled_count = len([t for t in updated if t.status.value == "filled"])
                if filled_count > 0:
                    self.logger.logger.info(f"Polling: {filled_count} filled, {len(active)} active")

                time.sleep(poll_interval_seconds)

            # Cancel any remaining active orders after timeout
            remaining = self.order_manager.cancel_all("timeout")
            if remaining > 0:
                self.logger.logger.warning(f"Cancelled {remaining} orders due to timeout")
                # Final process to capture cancellations
                time.sleep(1)
                self.order_manager.process_all()

        # Step 9: Collect results and finalize analytics
        exec_summary = self.order_manager.get_execution_summary()
        summary["filled"] = exec_summary.get("filled", 0)
        summary["partial"] = exec_summary.get("partial_fills", 0)
        summary["cancelled"] = exec_summary.get("cancelled", 0)
        summary["rejected"] += exec_summary.get("rejected", 0)
        summary["total_commission"] = exec_summary.get("total_commission", 0.0)
        summary["avg_slippage_bps"] = exec_summary.get("avg_slippage_bps", 0.0)

        # Finalize daily analytics
        if self.execution_analytics:
            self.execution_analytics.finalize_day()

        # Clear completed tickets for next run
        self.order_manager.clear_completed()

        self.logger.logger.info(
            "execution_stack_complete",
            extra={
                "filled": summary["filled"],
                "partial": summary["partial"],
                "cancelled": summary["cancelled"],
                "rejected": summary["rejected"],
                "slippage_bps": summary["avg_slippage_bps"],
                "commission": summary["total_commission"],
            }
        )

        return summary

    def _get_asset_class(self, instrument_id: str) -> str:
        """Determine asset class for an instrument."""
        # Check instruments config for hints
        for category, instruments in self.instruments.items():
            if isinstance(instruments, dict):
                if instrument_id in instruments:
                    spec = instruments[instrument_id]
                    sec_type = spec.get('sec_type', 'STK')
                    if sec_type == 'FUT':
                        # Check if FX future
                        if 'M6' in instrument_id or 'EUR' in instrument_id or 'GBP' in instrument_id:
                            return 'FX_FUT'
                        return 'FUT'
                    elif sec_type == 'STK':
                        # Check if ETF (common patterns)
                        if any(etf in instrument_id for etf in ['SPY', 'QQQ', 'IWM', 'CSPX', 'IUKD', 'CS51']):
                            return 'ETF'
                        return 'STK'
        # Default
        return 'STK'

    def _simulate_execution(self, orders: List[OrderSpec]) -> Dict[str, Any]:
        """Simulate order execution for paper trading."""
        summary = {
            "total_orders": len(orders),
            "filled": 0,
            "rejected": 0
        }

        for order in orders:
            try:
                # Get current price
                price = self.data_feed.get_last_price(order.instrument_id)

                # Update portfolio position
                current_qty = self.portfolio.positions.get(order.instrument_id)
                if current_qty:
                    current_qty = current_qty.quantity
                else:
                    current_qty = 0

                if order.side == "BUY":
                    new_qty = current_qty + order.quantity
                else:
                    new_qty = current_qty - order.quantity

                # Log the simulated fill
                self.logger.log_fill(
                    order_id=str(id(order)),
                    instrument_id=order.instrument_id,
                    side=order.side,
                    quantity=order.quantity,
                    fill_price=price,
                    commission=0.0,
                    metadata={"simulated": True}
                )

                summary["filled"] += 1

            except Exception as e:
                summary["rejected"] += 1
                self.logger.log_alert(
                    alert_type="simulated_order_failed",
                    severity="warning",
                    message=f"Failed to simulate order for {order.instrument_id}: {e}"
                )

        return summary

    def _record_daily_pnl(self) -> None:
        """Record daily P&L."""
        # Compute daily return
        if len(self.portfolio.nav_history) > 0:
            prev_nav = self.portfolio.nav_history.iloc[-1]
            daily_return = (self.portfolio.nav - prev_nav) / prev_nav
        else:
            daily_return = 0.0

        # Record in portfolio
        self.portfolio.record_daily_pnl(daily_return, date.today())

        # Update history
        self.returns_history[pd.Timestamp(date.today())] = daily_return

        # Update volatility
        if len(self.returns_history) >= 20:
            self.portfolio.realized_vol_annual = self.risk_engine.compute_realized_vol_annual(
                self.returns_history, window=20
            )

    def _save_state(self) -> None:
        """Save portfolio state and history."""
        save_portfolio_state(
            self.portfolio,
            str(self.state_dir / "portfolio_state.json")
        )
        save_returns_history(
            self.returns_history,
            str(self.state_dir / "returns_history.csv")
        )

    def _send_daily_summary(self) -> None:
        """Send daily summary alert."""
        if self.alert_manager:
            try:
                self.alert_manager.send_daily_summary(
                    nav=self.portfolio.nav,
                    daily_pnl=self.portfolio.daily_pnl,
                    daily_return=self.portfolio.daily_return,
                    gross_exposure=self.portfolio.gross_exposure,
                    net_exposure=self.portfolio.net_exposure,
                    realized_vol=self.portfolio.realized_vol_annual,
                    drawdown=self.portfolio.current_drawdown,
                    hedge_budget=self.portfolio.hedge_budget_used_ytd
                )
            except Exception:
                pass

        # EXECUTION_STACK_UPGRADE: Send execution analytics summary
        if self.execution_analytics and self.alert_manager:
            try:
                exec_summary = self.execution_analytics.get_telegram_summary()
                if exec_summary:
                    self.alert_manager.send_alert(
                        alert_type="daily",
                        severity="info",
                        title="Execution Summary",
                        message=exec_summary
                    )
            except Exception:
                pass

    def shutdown(self) -> None:
        """Clean shutdown of all components."""
        self.logger.logger.info("scheduler_shutdown")

        # Save state
        if self.portfolio:
            self._save_state()

        # Disconnect
        if self.reconnect_manager:
            self.reconnect_manager.disconnect()

        if self.ib_client and self.ib_client.is_connected():
            self.ib_client.disconnect()


class ContinuousScheduler:
    """
    Runs the DailyScheduler on a continuous schedule.
    Executes the daily run at the configured time each day.
    """

    # Startup delay to wait for IB Gateway to be ready
    STARTUP_DELAY_SECONDS = 120  # 2 minutes
    # Max retries for initialization failures
    MAX_INIT_RETRIES = 10  # Increased from 5 to handle gateway restart cycles
    # Delay between init retries
    INIT_RETRY_DELAY_SECONDS = 90  # Increased from 60 to give gateway more time
    # Max time to wait for gateway to be API-ready
    GATEWAY_READY_TIMEOUT_SECONDS = 600  # 10 minutes total budget

    def __init__(self):
        self.running = True
        self.scheduler: Optional[DailyScheduler] = None
        self.last_run_date: Optional[date] = None

        # Load settings for schedule config
        settings = load_settings("config/settings.yaml")
        schedule_config = settings.get('schedule', {})
        self.run_hour = schedule_config.get('run_hour_utc', 6)
        self.run_minute = schedule_config.get('run_minute_utc', 0)
        self.timezone = pytz.timezone(schedule_config.get('timezone', 'UTC'))

        # Set up signal handlers for graceful shutdown
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully."""
        print(f"Received signal {signum}, shutting down...")
        self.running = False

    def _should_run_now(self) -> bool:
        """Check if it's time to run the daily job."""
        now = datetime.now(pytz.UTC)
        today = now.date()

        # Don't run if we already ran today
        if self.last_run_date == today:
            return False

        # Check if we're past the scheduled time
        scheduled_time = now.replace(
            hour=self.run_hour,
            minute=self.run_minute,
            second=0,
            microsecond=0
        )

        return now >= scheduled_time

    def _seconds_until_next_run(self) -> int:
        """Calculate seconds until next scheduled run."""
        now = datetime.now(pytz.UTC)

        # Next run is today if we haven't run yet and it's before scheduled time
        next_run = now.replace(
            hour=self.run_hour,
            minute=self.run_minute,
            second=0,
            microsecond=0
        )

        # If we're past today's run time or already ran today, schedule for tomorrow
        if now >= next_run or self.last_run_date == now.date():
            next_run += timedelta(days=1)

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

    def _run_daily_with_retries(self) -> bool:
        """
        Run the daily job with retries on initialization failure.

        First waits for IB Gateway API to be truly ready (not just accepting
        connections), then attempts initialization with retries.

        Returns:
            True if successful, False otherwise
        """
        # First, wait for gateway API to be ready before any init attempts
        # This prevents wasting retries on a gateway that's still restarting
        if not self._wait_for_gateway_api_ready():
            print("Gateway API not ready after timeout, skipping today's run")
            get_health_server().update_ib_status(False)
            self.last_run_date = date.today()  # Mark as attempted to prevent retry loop
            return False

        for attempt in range(1, self.MAX_INIT_RETRIES + 1):
            print(f"Initialization attempt {attempt}/{self.MAX_INIT_RETRIES}")

            # Create new scheduler instance for each attempt
            self.scheduler = DailyScheduler()

            try:
                if self.scheduler.initialize():
                    # Update health server with IB connected status
                    get_health_server().update_ib_status(True)
                    result = self.scheduler.run_daily()
                    print(f"Daily run completed: {json.dumps(result, indent=2)}")
                    self.last_run_date = date.today()
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
        """Main loop - runs continuously, executing daily job at scheduled time."""
        print(f"ContinuousScheduler started")
        print(f"Scheduled run time: {self.run_hour:02d}:{self.run_minute:02d} UTC")
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
                if self._should_run_now():
                    print(f"\n{'='*60}")
                    print(f"Starting daily run at {datetime.now(pytz.UTC).isoformat()}")
                    print(f"{'='*60}\n")

                    success = self._run_daily_with_retries()

                    # Update health server with run result
                    health_server.update_daily_run({
                        "timestamp": datetime.now(pytz.UTC).isoformat(),
                        "success": success,
                        "date": date.today().isoformat()
                    })

                    print(f"\n{'='*60}")
                    print(f"Daily run finished at {datetime.now(pytz.UTC).isoformat()}")
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


def main():
    """Main entrypoint for scheduler."""
    import argparse

    parser = argparse.ArgumentParser(description='AbstractFinance Trading Scheduler')
    parser.add_argument('--once', action='store_true', help='Run once and exit (for testing)')
    parser.add_argument('--dry-run', action='store_true',
                       help='Compute everything but do not submit orders (for testing)')
    args = parser.parse_args()

    if args.once or args.dry_run:
        # Single run mode (for testing/cron) or dry-run mode
        scheduler = DailyScheduler(dry_run=args.dry_run)
        try:
            if scheduler.initialize():
                result = scheduler.run_daily()
                print(json.dumps(result, indent=2))
            else:
                print("Failed to initialize scheduler")
                sys.exit(1)
        finally:
            scheduler.shutdown()
    else:
        # Continuous mode (for Docker)
        continuous = ContinuousScheduler()
        continuous.run()


if __name__ == "__main__":
    main()
