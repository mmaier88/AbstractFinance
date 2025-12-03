"""
Historical and Monte Carlo backtesting for AbstractFinance.
Validates strategy performance using historical data and simulations.
"""

import pandas as pd
import numpy as np
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from pathlib import Path
import json

from .data_feeds import DataFeed
from .portfolio import PortfolioState, Sleeve
from .risk_engine import RiskEngine, RiskDecision, RiskRegime
from .strategy_logic import Strategy
from .tail_hedge import TailHedgeManager
from .logging_utils import TradingLogger, get_trading_logger


@dataclass
class BacktestResult:
    """Results from a backtest run."""
    # Period
    start_date: date
    end_date: date
    trading_days: int

    # Returns
    total_return: float
    annual_return: float
    annual_volatility: float
    sharpe_ratio: float
    sortino_ratio: float

    # Drawdown
    max_drawdown: float
    max_drawdown_date: Optional[date] = None
    avg_drawdown: float = 0.0
    drawdown_duration_days: int = 0

    # Risk metrics
    skewness: float = 0.0
    kurtosis: float = 0.0
    var_95: float = 0.0
    var_99: float = 0.0
    expected_shortfall: float = 0.0

    # Performance
    win_rate: float = 0.0
    profit_factor: float = 0.0
    best_day: float = 0.0
    worst_day: float = 0.0
    best_month: float = 0.0
    worst_month: float = 0.0

    # Sleeve attribution
    sleeve_returns: Dict[str, float] = field(default_factory=dict)
    sleeve_sharpe: Dict[str, float] = field(default_factory=dict)

    # Time series
    equity_curve: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    returns_series: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    drawdown_series: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "trading_days": self.trading_days,
            "total_return": self.total_return,
            "annual_return": self.annual_return,
            "annual_volatility": self.annual_volatility,
            "sharpe_ratio": self.sharpe_ratio,
            "sortino_ratio": self.sortino_ratio,
            "max_drawdown": self.max_drawdown,
            "max_drawdown_date": self.max_drawdown_date.isoformat() if self.max_drawdown_date else None,
            "skewness": self.skewness,
            "kurtosis": self.kurtosis,
            "var_95": self.var_95,
            "var_99": self.var_99,
            "win_rate": self.win_rate,
            "best_day": self.best_day,
            "worst_day": self.worst_day,
            "sleeve_returns": self.sleeve_returns,
            "sleeve_sharpe": self.sleeve_sharpe
        }


@dataclass
class MonteCarloResult:
    """Results from Monte Carlo simulation."""
    n_paths: int
    years: int

    # Distribution statistics
    median_return: float
    mean_return: float
    std_return: float
    percentile_10: float
    percentile_25: float
    percentile_75: float
    percentile_90: float

    # Risk
    prob_loss: float
    prob_significant_loss: float  # >20% loss
    median_max_drawdown: float
    worst_max_drawdown: float

    # All paths for analysis
    final_returns: np.ndarray = field(default_factory=lambda: np.array([]))
    max_drawdowns: np.ndarray = field(default_factory=lambda: np.array([]))


class Backtester:
    """
    Historical backtesting engine.
    Simulates strategy execution on historical data.
    """

    def __init__(
        self,
        settings: Dict[str, Any],
        instruments_config: Dict[str, Any],
        data_feed: Optional[DataFeed] = None,
        logger: Optional[TradingLogger] = None
    ):
        """
        Initialize backtester.

        Args:
            settings: Application settings
            instruments_config: Instrument configuration
            data_feed: Data feed for historical data
            logger: Trading logger
        """
        self.settings = settings
        self.instruments = instruments_config
        self.data_feed = data_feed or DataFeed(
            instruments_config=instruments_config,
            settings=settings
        )
        self.logger = logger or get_trading_logger()

        # Initialize components
        self.risk_engine = RiskEngine(settings)
        self.tail_hedge_manager = TailHedgeManager(settings, instruments_config)
        self.strategy = Strategy(
            settings=settings,
            instruments_config=instruments_config,
            risk_engine=self.risk_engine,
            tail_hedge_manager=self.tail_hedge_manager
        )

        # Backtest settings
        backtest_settings = settings.get('backtest', {})
        self.initial_capital = backtest_settings.get('initial_capital', 10_000_000)

    def run(
        self,
        start_date: str,
        end_date: Optional[str] = None,
        initial_capital: Optional[float] = None
    ) -> BacktestResult:
        """
        Run historical backtest.

        Args:
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (defaults to today)
            initial_capital: Starting capital

        Returns:
            BacktestResult with performance metrics
        """
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else date.today()
        capital = initial_capital or self.initial_capital

        self.logger.logger.info("backtest_start", start=start_date, end=end_date, capital=capital)

        # Initialize portfolio
        portfolio = PortfolioState(
            nav=capital,
            cash=capital,
            initial_capital=capital,
            inception_date=start
        )

        # Load historical data
        spy_hist = self.data_feed.get_history("SPY", lookback_days=2520)  # ~10 years
        fez_hist = self.data_feed.get_history("FEZ", lookback_days=2520)

        # Get trading dates
        trading_dates = spy_hist.loc[start:end].index

        # Initialize tracking
        equity_curve = []
        returns_list = []
        dates_list = []

        # Sleeve tracking
        sleeve_returns = {s.value: [] for s in Sleeve}

        prev_nav = capital

        for i, dt in enumerate(trading_dates):
            try:
                current_date = dt.date() if hasattr(dt, 'date') else dt

                # Skip if no data
                if dt not in spy_hist.index or dt not in fez_hist.index:
                    continue

                # Get prices for the day
                spy_price = spy_hist.loc[dt, 'Close']
                fez_price = fez_hist.loc[dt, 'Close']

                # Compute daily return (simplified - use spread return)
                if i > 0:
                    prev_dt = trading_dates[i - 1]
                    spy_ret = (spy_price / spy_hist.loc[prev_dt, 'Close']) - 1
                    fez_ret = (fez_price / fez_hist.loc[prev_dt, 'Close']) - 1

                    # Spread return (long US, short EU)
                    # Weight based on strategy allocation
                    core_weight = self.strategy.sleeve_weights[Sleeve.CORE_INDEX_RV]
                    spread_ret = core_weight * (spy_ret - fez_ret)

                    # Add sector contribution (simplified)
                    sector_weight = self.strategy.sleeve_weights[Sleeve.SECTOR_RV]
                    sector_ret = sector_weight * spy_ret * 0.5  # Simplified

                    # Total daily return
                    daily_ret = spread_ret + sector_ret

                    # Apply volatility scaling
                    if len(returns_list) >= 20:
                        recent_vol = np.std(returns_list[-20:]) * np.sqrt(252)
                        if recent_vol > 0:
                            scale = min(
                                self.risk_engine.vol_target_annual / recent_vol,
                                self.risk_engine.gross_leverage_max
                            )
                            daily_ret *= scale

                    # Apply hedge drag (simplified)
                    hedge_budget_pct = self.settings.get('hedge_budget_annual_pct', 0.025)
                    daily_hedge_cost = hedge_budget_pct / 252
                    daily_ret -= daily_hedge_cost

                    # Update NAV
                    portfolio.nav = prev_nav * (1 + daily_ret)

                    # Track
                    returns_list.append(daily_ret)
                    sleeve_returns[Sleeve.CORE_INDEX_RV.value].append(spread_ret)
                    sleeve_returns[Sleeve.SECTOR_RV.value].append(sector_ret)

                else:
                    daily_ret = 0.0
                    returns_list.append(0.0)

                equity_curve.append(portfolio.nav)
                dates_list.append(dt)
                prev_nav = portfolio.nav

            except Exception as e:
                continue

        # Convert to series
        returns_series = pd.Series(returns_list, index=dates_list[:len(returns_list)])
        equity_series = pd.Series(equity_curve, index=dates_list[:len(equity_curve)])

        # Compute metrics
        result = self._compute_metrics(
            returns_series=returns_series,
            equity_series=equity_series,
            sleeve_returns=sleeve_returns,
            start_date=start,
            end_date=end,
            initial_capital=capital
        )

        self.logger.log_backtest_result(
            start_date=start_date,
            end_date=end_date or date.today().isoformat(),
            initial_capital=capital,
            final_nav=result.equity_curve.iloc[-1] if len(result.equity_curve) > 0 else capital,
            total_return=result.total_return,
            annual_return=result.annual_return,
            annual_vol=result.annual_volatility,
            sharpe_ratio=result.sharpe_ratio,
            sortino_ratio=result.sortino_ratio,
            max_drawdown=result.max_drawdown
        )

        return result

    def _compute_metrics(
        self,
        returns_series: pd.Series,
        equity_series: pd.Series,
        sleeve_returns: Dict[str, List[float]],
        start_date: date,
        end_date: date,
        initial_capital: float
    ) -> BacktestResult:
        """Compute all backtest metrics."""

        # Basic stats
        total_return = (equity_series.iloc[-1] / initial_capital) - 1 if len(equity_series) > 0 else 0
        trading_days = len(returns_series)
        years = trading_days / 252

        # Annualized metrics
        annual_return = (1 + total_return) ** (1 / years) - 1 if years > 0 else 0
        annual_vol = returns_series.std() * np.sqrt(252) if len(returns_series) > 0 else 0

        # Risk-adjusted returns
        rf_rate = 0.02  # Assume 2% risk-free rate
        excess_return = annual_return - rf_rate
        sharpe = excess_return / annual_vol if annual_vol > 0 else 0

        # Sortino (downside deviation)
        negative_returns = returns_series[returns_series < 0]
        downside_vol = negative_returns.std() * np.sqrt(252) if len(negative_returns) > 0 else annual_vol
        sortino = excess_return / downside_vol if downside_vol > 0 else 0

        # Drawdown
        rolling_max = equity_series.cummax()
        drawdown_series = (equity_series - rolling_max) / rolling_max
        max_dd = drawdown_series.min() if len(drawdown_series) > 0 else 0
        max_dd_idx = drawdown_series.idxmin() if len(drawdown_series) > 0 else None

        # Higher moments
        skew = returns_series.skew() if len(returns_series) > 10 else 0
        kurt = returns_series.kurtosis() if len(returns_series) > 10 else 0

        # VaR
        var_95 = np.percentile(returns_series, 5) if len(returns_series) > 0 else 0
        var_99 = np.percentile(returns_series, 1) if len(returns_series) > 0 else 0

        # Win rate
        wins = len(returns_series[returns_series > 0])
        win_rate = wins / len(returns_series) if len(returns_series) > 0 else 0

        # Best/worst
        best_day = returns_series.max() if len(returns_series) > 0 else 0
        worst_day = returns_series.min() if len(returns_series) > 0 else 0

        # Sleeve analysis
        sleeve_annual_returns = {}
        sleeve_sharpe_ratios = {}
        for sleeve_name, rets in sleeve_returns.items():
            if len(rets) > 0:
                sleeve_series = pd.Series(rets)
                sleeve_total = (1 + sleeve_series).prod() - 1
                sleeve_annual = (1 + sleeve_total) ** (252 / len(rets)) - 1 if len(rets) > 0 else 0
                sleeve_vol = sleeve_series.std() * np.sqrt(252)
                sleeve_annual_returns[sleeve_name] = sleeve_annual
                sleeve_sharpe_ratios[sleeve_name] = (sleeve_annual - rf_rate) / sleeve_vol if sleeve_vol > 0 else 0

        return BacktestResult(
            start_date=start_date,
            end_date=end_date,
            trading_days=trading_days,
            total_return=total_return,
            annual_return=annual_return,
            annual_volatility=annual_vol,
            sharpe_ratio=sharpe,
            sortino_ratio=sortino,
            max_drawdown=max_dd,
            max_drawdown_date=max_dd_idx.date() if max_dd_idx and hasattr(max_dd_idx, 'date') else None,
            skewness=skew,
            kurtosis=kurt,
            var_95=var_95,
            var_99=var_99,
            win_rate=win_rate,
            best_day=best_day,
            worst_day=worst_day,
            sleeve_returns=sleeve_annual_returns,
            sleeve_sharpe=sleeve_sharpe_ratios,
            equity_curve=equity_series,
            returns_series=returns_series,
            drawdown_series=drawdown_series
        )


class MonteCarloSimulator:
    """
    Monte Carlo simulation engine.
    Generates synthetic return paths based on calibrated parameters.
    """

    def __init__(
        self,
        settings: Dict[str, Any],
        logger: Optional[TradingLogger] = None
    ):
        """
        Initialize simulator.

        Args:
            settings: Application settings
            logger: Trading logger
        """
        self.settings = settings
        self.logger = logger or get_trading_logger()

        # Calibration parameters from settings
        quant = settings.get('quant_assumptions', {})
        self.us_return = quant.get('sp500_annual_return', 0.10)
        self.eu_return = quant.get('eurostoxx_annual_return', 0.03)
        self.volatility = quant.get('index_volatility', 0.15)
        self.correlation = quant.get('correlation_us_eu', 0.82)

        # Strategy parameters
        self.hedge_cost = settings.get('hedge_budget_annual_pct', 0.025)

    def run(
        self,
        n_paths: int = 1000,
        years: int = 10,
        initial_capital: float = 10_000_000
    ) -> MonteCarloResult:
        """
        Run Monte Carlo simulation.

        Args:
            n_paths: Number of simulation paths
            years: Years to simulate
            initial_capital: Starting capital

        Returns:
            MonteCarloResult with simulation statistics
        """
        self.logger.logger.info("monte_carlo_start", n_paths=n_paths, years=years)

        trading_days = years * 252

        # Daily parameters
        daily_us_return = self.us_return / 252
        daily_eu_return = self.eu_return / 252
        daily_vol = self.volatility / np.sqrt(252)

        # Spread parameters (long US, short EU)
        spread_return = daily_us_return - daily_eu_return
        # Spread vol is lower due to correlation
        spread_vol = daily_vol * np.sqrt(2 * (1 - self.correlation))

        # Generate paths
        np.random.seed(42)  # For reproducibility

        # Generate daily returns for each path
        daily_returns = np.random.normal(
            loc=spread_return - (spread_vol ** 2) / 2,  # Adjust for log-normal
            scale=spread_vol,
            size=(n_paths, trading_days)
        )

        # Apply hedge cost
        daily_returns -= self.hedge_cost / 252

        # Compute equity paths
        cumulative_returns = np.cumprod(1 + daily_returns, axis=1)
        final_values = cumulative_returns[:, -1]
        final_returns = final_values - 1

        # Compute max drawdowns for each path
        max_drawdowns = np.zeros(n_paths)
        for i in range(n_paths):
            equity = cumulative_returns[i, :]
            running_max = np.maximum.accumulate(equity)
            drawdown = (equity - running_max) / running_max
            max_drawdowns[i] = drawdown.min()

        # Compute statistics
        result = MonteCarloResult(
            n_paths=n_paths,
            years=years,
            median_return=np.median(final_returns),
            mean_return=np.mean(final_returns),
            std_return=np.std(final_returns),
            percentile_10=np.percentile(final_returns, 10),
            percentile_25=np.percentile(final_returns, 25),
            percentile_75=np.percentile(final_returns, 75),
            percentile_90=np.percentile(final_returns, 90),
            prob_loss=np.mean(final_returns < 0),
            prob_significant_loss=np.mean(final_returns < -0.20),
            median_max_drawdown=np.median(max_drawdowns),
            worst_max_drawdown=np.min(max_drawdowns),
            final_returns=final_returns,
            max_drawdowns=max_drawdowns
        )

        self.logger.logger.info(
            "monte_carlo_complete",
            median_return=result.median_return,
            prob_loss=result.prob_loss
        )

        return result


def run_backtest(
    start_date: str = "2015-01-01",
    end_date: Optional[str] = None,
    config_path: str = "config/settings.yaml",
    instruments_path: str = "config/instruments.yaml"
) -> BacktestResult:
    """
    Convenience function to run a backtest.

    Args:
        start_date: Start date
        end_date: End date
        config_path: Path to settings
        instruments_path: Path to instruments config

    Returns:
        BacktestResult
    """
    from .data_feeds import load_settings, load_instruments_config

    settings = load_settings(config_path)
    instruments = load_instruments_config(instruments_path)

    backtester = Backtester(settings, instruments)
    return backtester.run(start_date, end_date)


def run_monte_carlo(
    n_paths: int = 1000,
    years: int = 10,
    config_path: str = "config/settings.yaml"
) -> MonteCarloResult:
    """
    Convenience function to run Monte Carlo simulation.

    Args:
        n_paths: Number of paths
        years: Years to simulate
        config_path: Path to settings

    Returns:
        MonteCarloResult
    """
    from .data_feeds import load_settings

    settings = load_settings(config_path)
    simulator = MonteCarloSimulator(settings)
    return simulator.run(n_paths, years)


if __name__ == "__main__":
    # Run sample backtest
    result = run_backtest("2015-01-01")
    print(f"Total Return: {result.total_return:.2%}")
    print(f"Annual Return: {result.annual_return:.2%}")
    print(f"Sharpe Ratio: {result.sharpe_ratio:.2f}")
    print(f"Max Drawdown: {result.max_drawdown:.2%}")

    # Run Monte Carlo
    mc_result = run_monte_carlo(1000, 10)
    print(f"\nMonte Carlo (10 years, 1000 paths):")
    print(f"Median Return: {mc_result.median_return:.2%}")
    print(f"10th Percentile: {mc_result.percentile_10:.2%}")
    print(f"90th Percentile: {mc_result.percentile_90:.2%}")
    print(f"Probability of Loss: {mc_result.prob_loss:.1%}")
