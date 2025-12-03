"""
Daily run scheduler and orchestrator for AbstractFinance.
Main entrypoint for automated trading execution.
"""

import os
import sys
import yaml
import json
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, List
import pandas as pd

from .data_feeds import DataFeed, load_instruments_config, load_settings
from .portfolio import PortfolioState, load_portfolio_state, save_portfolio_state, load_returns_history, save_returns_history
from .risk_engine import RiskEngine, RiskDecision
from .strategy_logic import Strategy, OrderSpec, generate_rebalance_orders
from .tail_hedge import TailHedgeManager
from .execution_ibkr import IBClient, ExecutionEngine
from .reconnect import IBReconnectManager, HealthChecker
from .logging_utils import setup_logging, TradingLogger, get_trading_logger

try:
    from .alerts import AlertManager
    ALERTS_AVAILABLE = True
except ImportError:
    ALERTS_AVAILABLE = False


class DailyScheduler:
    """
    Orchestrates daily trading operations.
    This is the main entrypoint for the automated trading system.
    """

    def __init__(
        self,
        config_path: str = "config/settings.yaml",
        instruments_path: str = "config/instruments.yaml",
        state_dir: str = "state"
    ):
        """
        Initialize the scheduler.

        Args:
            config_path: Path to settings.yaml
            instruments_path: Path to instruments.yaml
            state_dir: Directory for state files
        """
        # Load configuration
        self.settings = load_settings(config_path)
        self.instruments = load_instruments_config(instruments_path)
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

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

        # State
        self.portfolio: Optional[PortfolioState] = None
        self.returns_history: pd.Series = pd.Series(dtype=float)

    def initialize(self) -> bool:
        """
        Initialize all components.

        Returns:
            True if initialization successful
        """
        self.logger.logger.info("scheduler_init", mode=self.mode)

        try:
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
                return False

            # Initialize IB client
            self.ib_client = IBClient(
                host=self.ib_host,
                port=self.ib_port,
                client_id=self.ib_client_id + 1,  # Use different client ID
                logger=self.logger
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

            # Initialize execution engine
            self.execution_engine = ExecutionEngine(
                ib_client=self.ib_client,
                instruments_config=self.instruments,
                logger=self.logger
            )

            # Load portfolio state
            self.portfolio = load_portfolio_state(
                str(self.state_dir / "portfolio_state.json")
            )
            if self.portfolio is None:
                # Initialize new portfolio
                initial_capital = self.settings.get('backtest', {}).get('initial_capital', 1000000)
                self.portfolio = PortfolioState(
                    nav=initial_capital,
                    cash=initial_capital,
                    initial_capital=initial_capital,
                    inception_date=date.today()
                )

            # Load returns history
            self.returns_history = load_returns_history(
                str(self.state_dir / "returns_history.csv")
            )

            # Initialize alert manager if available
            if ALERTS_AVAILABLE:
                try:
                    self.alert_manager = AlertManager(self.settings)
                except Exception:
                    pass

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
            "success": False,
            "steps_completed": [],
            "errors": [],
            "orders_placed": 0,
            "orders_filled": 0
        }

        try:
            # Step 1: Initialize if not already done
            if self.portfolio is None:
                if not self.initialize():
                    run_summary["errors"].append("Initialization failed")
                    return run_summary
            run_summary["steps_completed"].append("initialize")

            # Step 2: Sync positions from IB
            self._sync_positions()
            run_summary["steps_completed"].append("sync_positions")

            # Step 3: Update NAV
            self.portfolio.compute_nav(self.data_feed)
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

            # Step 6: Manage tail hedges
            hedge_orders = self._manage_hedges()
            run_summary["hedge_orders"] = len(hedge_orders)
            run_summary["steps_completed"].append("manage_hedges")

            # Step 7: Check for crisis
            crisis_orders, crisis_action = self._check_crisis()
            run_summary["crisis_action"] = crisis_action.action_type
            run_summary["steps_completed"].append("check_crisis")

            # Step 8: Execute orders
            all_orders = strategy_output.orders + hedge_orders + crisis_orders
            if all_orders:
                execution_results = self._execute_orders(all_orders)
                run_summary["orders_placed"] = execution_results["total_orders"]
                run_summary["orders_filled"] = execution_results["filled"]
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
        """Sync positions from IB."""
        if not self.ib_client or not self.ib_client.is_connected():
            return

        ib_positions = self.ib_client.get_positions()
        # Convert to format expected by portfolio
        # This is a simplified sync - in production would be more robust
        for inst_id, position in ib_positions.items():
            self.portfolio.positions[inst_id] = position

        self.portfolio.compute_exposures()
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

        # Log risk decision
        self.logger.log_risk_decision(
            decision_type="daily_risk_eval",
            scaling_factor=risk_decision.scaling_factor,
            realized_vol=self.portfolio.realized_vol_annual,
            target_vol=self.risk_engine.vol_target_annual,
            max_drawdown=self.risk_engine.max_drawdown_pct,
            current_drawdown=self.portfolio.current_drawdown,
            emergency_derisk=risk_decision.emergency_derisk
        )

        return risk_decision

    def _compute_strategy_targets(self, risk_decision: RiskDecision):
        """Compute strategy target positions."""
        return self.strategy.compute_all_sleeve_targets(
            portfolio=self.portfolio,
            data_feed=self.data_feed,
            risk_decision=risk_decision
        )

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
        """Execute orders."""
        if self.is_paper:
            # Paper trading - simulate execution
            return self._simulate_execution(orders)
        else:
            # Live trading
            reports, summary = self.execution_engine.execute_strategy_orders(
                orders=orders,
                dry_run=False
            )
            return summary

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


def main():
    """Main entrypoint for scheduler."""
    scheduler = DailyScheduler()

    try:
        if scheduler.initialize():
            result = scheduler.run_daily()
            print(json.dumps(result, indent=2))
        else:
            print("Failed to initialize scheduler")
            sys.exit(1)
    finally:
        scheduler.shutdown()


if __name__ == "__main__":
    main()
