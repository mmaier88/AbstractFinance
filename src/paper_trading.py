"""
Paper trading burn-in orchestrator for AbstractFinance.
Manages the 60-day paper trading validation period.
"""

import json
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, List
import pandas as pd
import numpy as np

from .scheduler import DailyScheduler
from .portfolio import PortfolioState, load_portfolio_state, save_portfolio_state
from .logging_utils import TradingLogger, get_trading_logger
from .backtest import BacktestResult


class PaperTradingOrchestrator:
    """
    Orchestrates the paper trading burn-in period.
    Validates strategy performance before going live.
    """

    DEFAULT_BURN_IN_DAYS = 60

    # Minimum thresholds for go-live approval
    MIN_SHARPE = 0.5
    MAX_DRAWDOWN = -0.15
    MIN_TRADES = 50
    MAX_REJECTION_RATE = 0.05

    def __init__(
        self,
        config_path: str = "config/settings.yaml",
        instruments_path: str = "config/instruments.yaml",
        state_dir: str = "state/paper",
        burn_in_days: Optional[int] = None
    ):
        """
        Initialize paper trading orchestrator.

        Args:
            config_path: Path to settings
            instruments_path: Path to instruments config
            state_dir: Directory for paper trading state
            burn_in_days: Number of days for burn-in period
        """
        self.config_path = config_path
        self.instruments_path = instruments_path
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.burn_in_days = burn_in_days or self.DEFAULT_BURN_IN_DAYS
        self.logger = get_trading_logger()

        # Initialize scheduler in paper mode
        self.scheduler = DailyScheduler(
            config_path=config_path,
            instruments_path=instruments_path,
            state_dir=str(self.state_dir)
        )

        # Tracking
        self.run_history: List[Dict[str, Any]] = []
        self.start_date: Optional[date] = None
        self.metrics_history: List[Dict[str, float]] = []

    def start(self) -> bool:
        """
        Start the paper trading burn-in period.

        Returns:
            True if started successfully
        """
        self.logger.logger.info("paper_trading_start", burn_in_days=self.burn_in_days)

        # Initialize scheduler
        if not self.scheduler.initialize():
            self.logger.log_alert(
                alert_type="paper_start_failed",
                severity="error",
                message="Failed to initialize paper trading scheduler"
            )
            return False

        self.start_date = date.today()

        # Save initial state
        self._save_burn_in_state()

        return True

    def run_day(self) -> Dict[str, Any]:
        """
        Run a single day of paper trading.

        Returns:
            Run summary
        """
        result = self.scheduler.run_daily()

        # Track run
        self.run_history.append(result)

        # Update metrics
        self._update_metrics()

        # Save state
        self._save_burn_in_state()

        return result

    def get_progress(self) -> Dict[str, Any]:
        """
        Get burn-in progress summary.

        Returns:
            Progress summary
        """
        days_completed = len(self.run_history)
        days_remaining = max(0, self.burn_in_days - days_completed)
        progress_pct = days_completed / self.burn_in_days

        return {
            "days_completed": days_completed,
            "days_remaining": days_remaining,
            "burn_in_days": self.burn_in_days,
            "progress_pct": progress_pct,
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "expected_end": (self.start_date + timedelta(days=self.burn_in_days)).isoformat() if self.start_date else None,
            "is_complete": days_completed >= self.burn_in_days
        }

    def _update_metrics(self) -> None:
        """Update rolling performance metrics."""
        if not self.scheduler.portfolio:
            return

        portfolio = self.scheduler.portfolio
        returns = self.scheduler.returns_history

        metrics = {
            "date": date.today().isoformat(),
            "nav": portfolio.nav,
            "daily_return": portfolio.daily_return,
            "realized_vol": portfolio.realized_vol_annual,
            "max_drawdown": portfolio.max_drawdown,
            "current_drawdown": portfolio.current_drawdown
        }

        # Compute Sharpe if enough data
        if len(returns) >= 20:
            annual_return = returns.mean() * 252
            annual_vol = returns.std() * np.sqrt(252)
            metrics["sharpe"] = (annual_return - 0.02) / annual_vol if annual_vol > 0 else 0
        else:
            metrics["sharpe"] = 0

        self.metrics_history.append(metrics)

    def generate_report(self) -> Dict[str, Any]:
        """
        Generate burn-in period report.

        Returns:
            Comprehensive report
        """
        if not self.scheduler.portfolio:
            return {"error": "No portfolio data"}

        portfolio = self.scheduler.portfolio
        returns = self.scheduler.returns_history

        # Basic metrics
        trading_days = len(returns)
        total_return = (portfolio.nav / portfolio.initial_capital) - 1 if portfolio.initial_capital > 0 else 0

        # Annualized metrics
        if trading_days > 0:
            annual_factor = 252 / trading_days
            annual_return = (1 + total_return) ** annual_factor - 1
            annual_vol = returns.std() * np.sqrt(252) if len(returns) > 0 else 0
            sharpe = (annual_return - 0.02) / annual_vol if annual_vol > 0 else 0
        else:
            annual_return = 0
            annual_vol = 0
            sharpe = 0

        # Sortino
        negative_returns = returns[returns < 0]
        downside_vol = negative_returns.std() * np.sqrt(252) if len(negative_returns) > 0 else annual_vol
        sortino = (annual_return - 0.02) / downside_vol if downside_vol > 0 else 0

        # Trade statistics
        total_runs = len(self.run_history)
        successful_runs = len([r for r in self.run_history if r.get('success', False)])
        total_orders = sum(r.get('orders_placed', 0) for r in self.run_history)
        filled_orders = sum(r.get('orders_filled', 0) for r in self.run_history)
        rejection_rate = 1 - (filled_orders / total_orders) if total_orders > 0 else 0

        report = {
            "summary": {
                "start_date": self.start_date.isoformat() if self.start_date else None,
                "end_date": date.today().isoformat(),
                "trading_days": trading_days,
                "burn_in_target": self.burn_in_days
            },
            "performance": {
                "initial_capital": portfolio.initial_capital,
                "final_nav": portfolio.nav,
                "total_return": total_return,
                "annual_return": annual_return,
                "annual_volatility": annual_vol,
                "sharpe_ratio": sharpe,
                "sortino_ratio": sortino,
                "max_drawdown": portfolio.max_drawdown,
                "current_drawdown": portfolio.current_drawdown
            },
            "trading": {
                "total_runs": total_runs,
                "successful_runs": successful_runs,
                "success_rate": successful_runs / total_runs if total_runs > 0 else 0,
                "total_orders": total_orders,
                "filled_orders": filled_orders,
                "rejection_rate": rejection_rate,
                "hedge_budget_used": portfolio.hedge_budget_used_ytd
            },
            "validation": self._validate_for_live(),
            "daily_metrics": self.metrics_history[-30:] if len(self.metrics_history) > 30 else self.metrics_history
        }

        return report

    def _validate_for_live(self) -> Dict[str, Any]:
        """
        Validate if strategy is ready to go live.

        Returns:
            Validation results
        """
        if len(self.metrics_history) < 20:
            return {
                "ready": False,
                "reason": "Insufficient data (need at least 20 days)",
                "checks": {}
            }

        latest = self.metrics_history[-1]
        returns = self.scheduler.returns_history

        # Calculate metrics
        annual_return = returns.mean() * 252 if len(returns) > 0 else 0
        annual_vol = returns.std() * np.sqrt(252) if len(returns) > 0 else 0
        sharpe = (annual_return - 0.02) / annual_vol if annual_vol > 0 else 0

        total_orders = sum(r.get('orders_placed', 0) for r in self.run_history)
        filled_orders = sum(r.get('orders_filled', 0) for r in self.run_history)
        rejection_rate = 1 - (filled_orders / total_orders) if total_orders > 0 else 0

        # Run checks
        checks = {
            "sharpe_ratio": {
                "value": sharpe,
                "threshold": self.MIN_SHARPE,
                "passed": sharpe >= self.MIN_SHARPE
            },
            "max_drawdown": {
                "value": latest.get("max_drawdown", 0),
                "threshold": self.MAX_DRAWDOWN,
                "passed": latest.get("max_drawdown", 0) >= self.MAX_DRAWDOWN
            },
            "total_trades": {
                "value": total_orders,
                "threshold": self.MIN_TRADES,
                "passed": total_orders >= self.MIN_TRADES
            },
            "rejection_rate": {
                "value": rejection_rate,
                "threshold": self.MAX_REJECTION_RATE,
                "passed": rejection_rate <= self.MAX_REJECTION_RATE
            },
            "burn_in_complete": {
                "value": len(self.run_history),
                "threshold": self.burn_in_days,
                "passed": len(self.run_history) >= self.burn_in_days
            }
        }

        all_passed = all(check["passed"] for check in checks.values())
        failed_checks = [name for name, check in checks.items() if not check["passed"]]

        return {
            "ready": all_passed,
            "reason": "All checks passed" if all_passed else f"Failed checks: {', '.join(failed_checks)}",
            "checks": checks
        }

    def _save_burn_in_state(self) -> None:
        """Save burn-in state to disk."""
        state = {
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "burn_in_days": self.burn_in_days,
            "run_history": self.run_history,
            "metrics_history": self.metrics_history
        }

        state_file = self.state_dir / "burn_in_state.json"
        with open(state_file, 'w') as f:
            json.dump(state, f, indent=2, default=str)

    def load_burn_in_state(self) -> bool:
        """
        Load previous burn-in state.

        Returns:
            True if state loaded
        """
        state_file = self.state_dir / "burn_in_state.json"

        if not state_file.exists():
            return False

        with open(state_file, 'r') as f:
            state = json.load(f)

        self.start_date = date.fromisoformat(state["start_date"]) if state.get("start_date") else None
        self.burn_in_days = state.get("burn_in_days", self.DEFAULT_BURN_IN_DAYS)
        self.run_history = state.get("run_history", [])
        self.metrics_history = state.get("metrics_history", [])

        return True

    def shutdown(self) -> None:
        """Clean shutdown."""
        self._save_burn_in_state()
        self.scheduler.shutdown()


def run_paper_trading(
    config_path: str = "config/settings.yaml",
    instruments_path: str = "config/instruments.yaml",
    burn_in_days: int = 60
) -> Dict[str, Any]:
    """
    Convenience function to run paper trading.

    Args:
        config_path: Settings path
        instruments_path: Instruments config path
        burn_in_days: Burn-in period length

    Returns:
        Final report
    """
    orchestrator = PaperTradingOrchestrator(
        config_path=config_path,
        instruments_path=instruments_path,
        burn_in_days=burn_in_days
    )

    try:
        # Load existing state if any
        orchestrator.load_burn_in_state()

        if not orchestrator.start():
            return {"error": "Failed to start paper trading"}

        # Run single day (in production, would run via cron)
        result = orchestrator.run_day()

        # Generate report
        report = orchestrator.generate_report()
        report["last_run"] = result

        return report

    finally:
        orchestrator.shutdown()


if __name__ == "__main__":
    report = run_paper_trading()
    print(json.dumps(report, indent=2, default=str))
