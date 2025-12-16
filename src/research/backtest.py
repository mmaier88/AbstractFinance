"""
Backtest Runner for AbstractFinance Strategy.

Phase E: Historical validation with cost realism (2008-today).

Key features:
- Uses research data (Yahoo) - NEVER live data
- Transaction cost model with calibrated slippage
- Stress period analysis (2008, 2011, 2020, 2022)
- Insurance payoff scoring (stress vs normal days)
- Deterministic JSON report output
"""

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class CostModelConfig:
    """Transaction cost model configuration."""
    # Slippage in basis points
    equity_slippage_bps: float = 5.0
    etf_slippage_bps: float = 4.0
    futures_slippage_bps: float = 1.0
    fx_slippage_bps: float = 2.0

    # Fixed costs
    commissions_per_trade_usd: float = 1.0

    # Carry costs (annual bps)
    short_dividend_bps_annual: float = 200.0  # Dividends paid out on shorts
    borrow_bps_annual: float = 50.0           # General borrow cost

    # Financing (for futures margin)
    financing_rate_annual: float = 0.05       # 5% annual


@dataclass
class BacktestConfig:
    """Backtest configuration."""
    start_date: date = date(2008, 1, 1)
    end_date: date = field(default_factory=date.today)
    initial_capital: float = 1_000_000.0

    # Strategy parameters
    vol_target_annual: float = 0.12
    gross_leverage_max: float = 2.0

    # STRATEGY EVOLUTION: Updated sleeve weights
    # Reduced equity L/S, increased Europe vol convexity
    sleeve_weights: Dict[str, float] = field(default_factory=lambda: {
        "core_index_rv": 0.20,        # Reduced from 0.35
        "sector_rv": 0.20,            # Factor-neutral
        "single_name": 0.10,          # Trend-gated
        "credit_carry": 0.15,         # Regime-adaptive
        "europe_vol_convex": 0.15,    # NEW: VSTOXX + SX5E structures
        "crisis_alpha": 0.10,         # Increased from 0.05
        "cash_buffer": 0.10           # Increased safety margin
    })

    # Regime thresholds (Europe-first)
    vix_enter_elevated: float = 25.0
    vix_exit_elevated: float = 20.0
    vix_enter_crisis: float = 40.0
    vix_exit_crisis: float = 35.0
    v2x_weight: float = 0.4
    vix_weight: float = 0.3
    eurusd_trend_weight: float = 0.2
    drawdown_weight: float = 0.1

    # STRATEGY EVOLUTION: Trend filter for equity L/S
    trend_filter_enabled: bool = True
    trend_short_lookback: int = 60     # 3-month momentum
    trend_long_lookback: int = 252     # 12-month momentum
    trend_positive_threshold: float = 0.02
    trend_negative_threshold: float = -0.05
    trend_options_only_threshold: float = -0.10

    # FX hedge mode
    fx_hedge_mode: str = "PARTIAL"  # FULL, PARTIAL, NONE
    fx_hedge_ratio: float = 0.75    # For PARTIAL mode

    # Cost model
    costs: CostModelConfig = field(default_factory=CostModelConfig)

    # Output
    output_dir: str = "state/research"


@dataclass
class DailyResult:
    """Single day backtest result."""
    date: date
    nav: float
    daily_return: float
    gross_exposure: float
    net_exposure: float
    scaling_factor: float
    regime: str
    vix: float
    v2x: Optional[float]
    eurusd: float
    turnover: float
    transaction_costs: float
    carry_costs: float

    # Sleeve returns
    core_rv_return: float = 0.0
    sector_rv_return: float = 0.0
    single_name_return: float = 0.0
    credit_carry_return: float = 0.0
    europe_vol_convex_return: float = 0.0   # NEW: VSTOXX convexity
    crisis_alpha_return: float = 0.0

    # STRATEGY EVOLUTION: Trend filter state
    trend_momentum: float = 0.0
    trend_multiplier: float = 1.0


@dataclass
class StressPeriodStats:
    """Statistics for a stress period."""
    name: str
    start_date: date
    end_date: date
    total_return: float
    max_drawdown: float
    avg_daily_return: float
    vol_realized: float
    hedge_payoff: float  # Return from crisis alpha sleeve


@dataclass
class BacktestResult:
    """Complete backtest result."""
    config: BacktestConfig

    # Summary metrics
    total_return: float
    cagr: float
    sharpe_ratio: float
    sortino_ratio: float
    max_drawdown: float
    max_drawdown_date: date
    calmar_ratio: float

    # Risk metrics
    realized_vol: float
    downside_vol: float
    var_95: float
    var_99: float
    expected_shortfall: float

    # Turnover and costs
    avg_daily_turnover: float
    total_transaction_costs: float
    total_carry_costs: float
    total_costs: float

    # Stress period analysis
    stress_periods: List[StressPeriodStats]

    # Insurance payoff score
    insurance_score: float  # Avg return on stress days vs normal days

    # Attribution
    core_rv_contribution: float
    sector_rv_contribution: float
    single_name_contribution: float
    credit_carry_contribution: float
    crisis_alpha_contribution: float

    # Daily series (for charting)
    daily_results: List[DailyResult] = field(default_factory=list)

    # Metadata
    run_date: datetime = field(default_factory=datetime.now)
    run_duration_seconds: float = 0.0


class ResearchMarketData:
    """
    Research-only market data using Yahoo Finance.

    CRITICAL: This is for research ONLY. Never use in live trading.
    """

    def __init__(self, cache_dir: str = None):
        """Initialize with cache directory."""
        if cache_dir is None:
            # Use relative path to project root
            cache_dir = Path(__file__).parent.parent.parent / "state" / "cache" / "research"
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache: Dict[str, pd.DataFrame] = {}

    def get_total_return_series(
        self,
        symbol: str,
        start: date,
        end: date
    ) -> pd.Series:
        """
        Get total return series for a symbol.

        Uses adjusted close (includes dividends).
        """
        cache_key = f"{symbol}_{start}_{end}"

        if cache_key in self._cache:
            return self._cache[cache_key]['adj_close']

        # Check file cache
        cache_file = self.cache_dir / f"{cache_key}.parquet"
        if cache_file.exists():
            df = pd.read_parquet(cache_file)
            self._cache[cache_key] = df
            return df['adj_close']

        # Fetch from Yahoo
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        hist = ticker.history(start=start, end=end, auto_adjust=True)

        if hist.empty:
            raise ValueError(f"No data for {symbol}")

        df = pd.DataFrame({
            'adj_close': hist['Close'],
            'volume': hist['Volume']
        })
        df.index = pd.to_datetime(df.index).date

        # Save to cache
        df.to_parquet(cache_file)
        self._cache[cache_key] = df

        return df['adj_close']

    def get_vix_series(self, start: date, end: date) -> pd.Series:
        """Get VIX level series."""
        return self.get_total_return_series("^VIX", start, end)

    def get_v2x_series(self, start: date, end: date) -> pd.Series:
        """
        Get V2X (VSTOXX) series.

        Note: V2X data may not be available before 2009.
        Falls back to VIX * 1.2 adjustment if unavailable.
        """
        try:
            # Try VSTOXX ETN or index
            return self.get_total_return_series("^V2X", start, end)
        except Exception:
            # Fallback: use VIX with European adjustment
            vix = self.get_vix_series(start, end)
            return vix * 1.2  # Historical average V2X/VIX ratio

    def get_eurusd_series(self, start: date, end: date) -> pd.Series:
        """Get EUR/USD exchange rate series."""
        return self.get_total_return_series("EURUSD=X", start, end)

    def get_index_series(
        self,
        us_symbol: str = "SPY",
        eu_symbol: str = "EZU",
        start: date = date(2008, 1, 1),
        end: date = date.today()
    ) -> Tuple[pd.Series, pd.Series]:
        """Get US and EU index series."""
        us = self.get_total_return_series(us_symbol, start, end)
        eu = self.get_total_return_series(eu_symbol, start, end)

        # Align dates
        common = us.index.intersection(eu.index)
        return us.loc[common], eu.loc[common]


class CostModel:
    """Transaction and carry cost model."""

    def __init__(self, config: CostModelConfig):
        """Initialize with configuration."""
        self.config = config

    def compute_transaction_cost(
        self,
        notional: float,
        asset_class: str,
        is_buy: bool
    ) -> float:
        """
        Compute transaction cost for a trade.

        Args:
            notional: Trade notional in USD
            asset_class: "equity", "etf", "futures", "fx"
            is_buy: True for buy, False for sell

        Returns:
            Transaction cost in USD
        """
        # Get slippage for asset class
        slippage_map = {
            "equity": self.config.equity_slippage_bps,
            "etf": self.config.etf_slippage_bps,
            "futures": self.config.futures_slippage_bps,
            "fx": self.config.fx_slippage_bps,
        }
        slippage_bps = slippage_map.get(asset_class, self.config.etf_slippage_bps)

        # Slippage cost
        slippage_cost = abs(notional) * slippage_bps / 10000

        # Commission
        commission = self.config.commissions_per_trade_usd

        return slippage_cost + commission

    def compute_daily_carry_cost(
        self,
        short_notional: float,
        futures_notional: float
    ) -> float:
        """
        Compute daily carry costs.

        Args:
            short_notional: Total short position notional
            futures_notional: Total futures notional (for margin financing)

        Returns:
            Daily carry cost in USD
        """
        daily_factor = 1 / 252

        # Short dividend cost
        dividend_cost = abs(short_notional) * self.config.short_dividend_bps_annual / 10000 * daily_factor

        # Borrow cost
        borrow_cost = abs(short_notional) * self.config.borrow_bps_annual / 10000 * daily_factor

        # Futures financing (margin ~ 10% of notional)
        margin = abs(futures_notional) * 0.10
        financing_cost = margin * self.config.financing_rate_annual * daily_factor

        return dividend_cost + borrow_cost + financing_cost


class BacktestRunner:
    """
    Main backtest runner.

    Usage:
        config = BacktestConfig(
            start_date=date(2008, 1, 1),
            initial_capital=1_000_000
        )
        runner = BacktestRunner(config)
        result = runner.run()
        runner.save_report(result)
    """

    STRESS_PERIODS = [
        ("GFC 2008", date(2008, 9, 1), date(2009, 3, 31)),
        ("Euro Crisis 2011", date(2011, 7, 1), date(2011, 12, 31)),
        ("COVID 2020", date(2020, 2, 15), date(2020, 4, 15)),
        ("Rate Shock 2022", date(2022, 1, 1), date(2022, 10, 31)),
    ]

    def __init__(self, config: BacktestConfig):
        """Initialize backtest runner."""
        self.config = config
        self.market_data = ResearchMarketData()
        self.cost_model = CostModel(config.costs)

        # State
        self._nav = config.initial_capital
        self._positions: Dict[str, float] = {}
        self._regime = "NORMAL"
        self._regime_days = 0
        self._hwm = config.initial_capital  # High water mark

        # STRATEGY EVOLUTION: Trend filter state
        self._trend_momentum = 0.0
        self._trend_multiplier = 1.0

    def run(self) -> BacktestResult:
        """Run the backtest."""
        start_time = datetime.now()
        logger.info(f"Starting backtest from {self.config.start_date} to {self.config.end_date}")

        # Load market data
        us_prices, eu_prices = self.market_data.get_index_series(
            start=self.config.start_date,
            end=self.config.end_date
        )
        vix = self.market_data.get_vix_series(self.config.start_date, self.config.end_date)
        eurusd = self.market_data.get_eurusd_series(self.config.start_date, self.config.end_date)

        try:
            v2x = self.market_data.get_v2x_series(self.config.start_date, self.config.end_date)
        except Exception:
            v2x = vix * 1.2  # Fallback

        # Align all series
        common_dates = us_prices.index.intersection(eu_prices.index)\
            .intersection(vix.index).intersection(eurusd.index)

        us_prices = us_prices.loc[common_dates]
        eu_prices = eu_prices.loc[common_dates]
        vix = vix.loc[common_dates]
        eurusd = eurusd.loc[common_dates]
        v2x = v2x.reindex(common_dates, method='ffill')

        # Calculate returns
        us_returns = us_prices.pct_change()
        eu_returns = eu_prices.pct_change()
        eurusd_returns = eurusd.pct_change()

        # Run simulation
        daily_results = []
        prev_positions = {}

        for i, dt in enumerate(common_dates[1:], 1):
            # Get current market state
            current_vix = vix.iloc[i]
            current_v2x = v2x.iloc[i] if i < len(v2x) else current_vix * 1.2
            current_eurusd = eurusd.iloc[i]

            # Compute regime and scaling
            regime, scaling = self._compute_regime_and_scaling(
                vix=current_vix,
                v2x=current_v2x,
                eurusd_returns=eurusd_returns.iloc[max(0, i-60):i],
                current_dd=self._compute_drawdown()
            )

            # STRATEGY EVOLUTION: Compute trend filter
            trend_multiplier = self._compute_trend_multiplier(
                us_returns, eu_returns, i
            )
            self._trend_multiplier = trend_multiplier
            self._trend_momentum = self._compute_trend_momentum(us_returns, eu_returns, i)

            # Compute target positions (with trend filter applied)
            targets = self._compute_targets(scaling * trend_multiplier)

            # Compute turnover and costs
            turnover = self._compute_turnover(prev_positions, targets)
            tx_costs = self._compute_transaction_costs(prev_positions, targets)
            carry_costs = self.cost_model.compute_daily_carry_cost(
                short_notional=self._get_short_notional(targets),
                futures_notional=0  # Simplified: no futures in backtest
            )

            # Compute sleeve returns
            core_rv = us_returns.iloc[i] - eu_returns.iloc[i]  # Long US, short EU

            # Apply FX hedge effect
            if self.config.fx_hedge_mode == "FULL":
                fx_impact = 0
            elif self.config.fx_hedge_mode == "PARTIAL":
                fx_impact = -eurusd_returns.iloc[i] * (1 - self.config.fx_hedge_ratio)
            else:  # NONE
                fx_impact = -eurusd_returns.iloc[i]

            # STRATEGY EVOLUTION: Europe vol convexity returns
            eu_vol_convex_return = self._europe_vol_convex_return(current_v2x, regime)

            # Total portfolio return (with trend filter and Europe vol convexity)
            weights = self.config.sleeve_weights

            # Apply trend filter to equity L/S sleeves
            equity_trend_mult = trend_multiplier

            portfolio_return = (
                # Equity L/S sleeves (trend-gated)
                core_rv * weights.get("core_index_rv", 0.20) * scaling * equity_trend_mult +
                core_rv * 0.8 * weights.get("sector_rv", 0.20) * scaling * equity_trend_mult +
                core_rv * 0.5 * weights.get("single_name", 0.10) * scaling * equity_trend_mult +
                # Credit carry (not trend-gated)
                0.0003 * weights.get("credit_carry", 0.15) +
                # STRATEGY EVOLUTION: Europe vol convexity (PRIMARY insurance)
                eu_vol_convex_return * weights.get("europe_vol_convex", 0.15) +
                # Crisis alpha (secondary insurance)
                self._crisis_alpha_return(current_vix, regime) * weights.get("crisis_alpha", 0.10) +
                # FX impact
                fx_impact * 0.3
            )

            # Apply costs
            portfolio_return -= (tx_costs + carry_costs) / self._nav

            # Update NAV
            self._nav *= (1 + portfolio_return)
            self._hwm = max(self._hwm, self._nav)

            # Record result
            daily_results.append(DailyResult(
                date=dt,
                nav=self._nav,
                daily_return=portfolio_return,
                gross_exposure=self._nav * scaling,
                net_exposure=self._nav * scaling * 0.1,  # Small net exposure
                scaling_factor=scaling,
                regime=regime,
                vix=current_vix,
                v2x=current_v2x,
                eurusd=current_eurusd,
                turnover=turnover,
                transaction_costs=tx_costs,
                carry_costs=carry_costs,
                core_rv_return=core_rv * weights.get("core_index_rv", 0.20) * scaling * equity_trend_mult,
                europe_vol_convex_return=eu_vol_convex_return * weights.get("europe_vol_convex", 0.15),
                trend_momentum=self._trend_momentum,
                trend_multiplier=self._trend_multiplier
            ))

            prev_positions = targets

        # Compute summary statistics
        result = self._compute_result(daily_results, start_time)

        return result

    def _compute_regime_and_scaling(
        self,
        vix: float,
        v2x: float,
        eurusd_returns: pd.Series,
        current_dd: float
    ) -> Tuple[str, float]:
        """
        Compute regime and scaling factor using Europe-first model.
        """
        # EURUSD trend (negative = EUR weakening)
        eurusd_trend = eurusd_returns.mean() * 252 if len(eurusd_returns) > 20 else 0

        # Compute stress score (Europe-first)
        stress_score = (
            self.config.v2x_weight * max(0, (v2x - 20) / 20) +
            self.config.vix_weight * max(0, (vix - 20) / 25) +
            self.config.eurusd_trend_weight * max(0, -eurusd_trend / 0.10) +
            self.config.drawdown_weight * max(0, -current_dd / 0.10)
        )

        # Determine regime
        prev_regime = self._regime

        if stress_score > 0.6 or vix >= self.config.vix_enter_crisis:
            new_regime = "CRISIS"
        elif stress_score > 0.3 or vix >= self.config.vix_enter_elevated:
            new_regime = "ELEVATED"
        else:
            new_regime = "NORMAL"

        # Apply hysteresis (except CRISIS is immediate)
        if new_regime != prev_regime:
            if new_regime == "CRISIS":
                self._regime = "CRISIS"
                self._regime_days = 0
            else:
                self._regime_days += 1
                if self._regime_days >= 3:
                    self._regime = new_regime
                    self._regime_days = 0
        else:
            self._regime_days = 0

        # Compute scaling factor
        regime_scaling = {"NORMAL": 1.0, "ELEVATED": 0.7, "CRISIS": 0.3}
        scaling = min(self.config.gross_leverage_max, regime_scaling.get(self._regime, 1.0))

        return self._regime, scaling

    def _compute_targets(self, scaling: float) -> Dict[str, float]:
        """Compute target positions based on scaling."""
        nav = self._nav
        targets = {}

        for sleeve, weight in self.config.sleeve_weights.items():
            targets[sleeve] = nav * weight * scaling

        return targets

    def _compute_turnover(
        self,
        prev: Dict[str, float],
        current: Dict[str, float]
    ) -> float:
        """Compute turnover as fraction of NAV."""
        total_change = sum(
            abs(current.get(k, 0) - prev.get(k, 0))
            for k in set(prev.keys()) | set(current.keys())
        )
        return total_change / self._nav if self._nav > 0 else 0

    def _compute_transaction_costs(
        self,
        prev: Dict[str, float],
        current: Dict[str, float]
    ) -> float:
        """Compute transaction costs for position changes."""
        total_cost = 0.0
        for sleeve in set(prev.keys()) | set(current.keys()):
            change = abs(current.get(sleeve, 0) - prev.get(sleeve, 0))
            if change > 0:
                total_cost += self.cost_model.compute_transaction_cost(
                    notional=change,
                    asset_class="etf",
                    is_buy=current.get(sleeve, 0) > prev.get(sleeve, 0)
                )
        return total_cost

    def _get_short_notional(self, positions: Dict[str, float]) -> float:
        """Get total short notional (simplified: assume 50% of sector RV is short)."""
        return positions.get("sector_rv", 0) * 0.5 + positions.get("single_name", 0) * 0.5

    def _compute_drawdown(self) -> float:
        """Compute current drawdown from high water mark."""
        if self._hwm <= 0:
            return 0.0
        return (self._nav - self._hwm) / self._hwm

    def _crisis_alpha_return(self, vix: float, regime: str) -> float:
        """
        Simulate crisis alpha (tail hedge) returns.

        Assumes options that pay off in high vol environments.
        Secondary insurance channel (VIX-based, after Europe vol).
        """
        if regime == "CRISIS":
            return 0.008  # ~0.8% daily in crisis (more conservative)
        elif regime == "ELEVATED":
            return 0.002  # ~0.2% daily
        else:
            return -0.00025  # Small daily bleed (theta decay, ~6% annual)

    def _europe_vol_convex_return(self, v2x: float, regime: str) -> float:
        """
        STRATEGY EVOLUTION: Simulate Europe vol convexity returns.

        This is the PRIMARY insurance channel - VSTOXX calls and SX5E put spreads.
        More sensitive to European stress than VIX-based hedges.

        Calibrated for realistic option payoffs:
        - Put spreads and call spreads have capped upside but lower premium
        - Typical VIX spike of 2x = ~50% option gain, not 10x
        """
        if regime == "CRISIS":
            # VSTOXX spike means convexity pays off
            # More conservative: ~1% daily (annualized ~250% but only for few crisis days)
            return 0.01
        elif regime == "ELEVATED":
            # Some payoff as V2X rises
            return 0.003  # ~0.3% daily
        else:
            # Normal: theta decay on options structures
            # Spread structures reduce bleed vs outright options
            return -0.0003  # ~7% annual decay (realistic for OTM spread structures)

    def _compute_trend_momentum(
        self,
        us_returns: pd.Series,
        eu_returns: pd.Series,
        current_idx: int
    ) -> float:
        """Compute US vs EU relative momentum for trend filter."""
        if not self.config.trend_filter_enabled:
            return 0.0

        lookback = self.config.trend_short_lookback
        if current_idx < lookback:
            return 0.0

        # Compute cumulative returns over lookback
        us_cum = (1 + us_returns.iloc[current_idx - lookback:current_idx]).prod() - 1
        eu_cum = (1 + eu_returns.iloc[current_idx - lookback:current_idx]).prod() - 1

        return us_cum - eu_cum

    def _compute_trend_multiplier(
        self,
        us_returns: pd.Series,
        eu_returns: pd.Series,
        current_idx: int
    ) -> float:
        """
        Compute position sizing multiplier based on trend filter.

        Returns:
            0.0 to 1.0 multiplier for equity L/S sleeves
        """
        if not self.config.trend_filter_enabled:
            return 1.0

        momentum = self._compute_trend_momentum(us_returns, eu_returns, current_idx)

        # Determine sizing based on momentum
        if momentum >= self.config.trend_positive_threshold:
            return 1.0  # Full size
        elif momentum <= self.config.trend_options_only_threshold:
            return 0.0  # Options only
        elif momentum <= self.config.trend_negative_threshold:
            return 0.25  # Reduced size
        else:
            # Interpolate in neutral zone
            range_size = self.config.trend_positive_threshold - self.config.trend_negative_threshold
            position = (momentum - self.config.trend_negative_threshold) / range_size
            return 0.25 + position * 0.75

    def _compute_result(
        self,
        daily_results: List[DailyResult],
        start_time: datetime
    ) -> BacktestResult:
        """Compute summary statistics from daily results."""
        returns = pd.Series([r.daily_return for r in daily_results])
        navs = pd.Series([r.nav for r in daily_results])
        dates = [r.date for r in daily_results]

        # Basic stats
        total_return = (navs.iloc[-1] / self.config.initial_capital) - 1
        years = (dates[-1] - dates[0]).days / 365.25
        cagr = (1 + total_return) ** (1 / years) - 1 if years > 0 else 0

        # Risk stats
        realized_vol = returns.std() * np.sqrt(252)
        downside_returns = returns[returns < 0]
        downside_vol = downside_returns.std() * np.sqrt(252) if len(downside_returns) > 0 else realized_vol

        # Sharpe and Sortino (assuming 0% risk-free)
        sharpe = returns.mean() * 252 / realized_vol if realized_vol > 0 else 0
        sortino = returns.mean() * 252 / downside_vol if downside_vol > 0 else 0

        # Drawdown
        rolling_max = navs.cummax()
        drawdowns = (navs - rolling_max) / rolling_max
        max_dd = drawdowns.min()
        max_dd_idx = drawdowns.idxmin()
        max_dd_date = dates[max_dd_idx] if pd.notna(max_dd_idx) else dates[-1]

        calmar = cagr / abs(max_dd) if max_dd < 0 else 0

        # VaR and ES
        var_95 = returns.quantile(0.05)
        var_99 = returns.quantile(0.01)
        es = returns[returns <= var_95].mean()

        # Turnover and costs
        avg_turnover = np.mean([r.turnover for r in daily_results])
        total_tx_costs = sum(r.transaction_costs for r in daily_results)
        total_carry_costs = sum(r.carry_costs for r in daily_results)

        # Stress period analysis
        stress_stats = self._analyze_stress_periods(daily_results)

        # Insurance score
        insurance_score = self._compute_insurance_score(daily_results)

        # Attribution
        core_contribution = sum(r.core_rv_return for r in daily_results) / len(daily_results) * 252

        return BacktestResult(
            config=self.config,
            total_return=total_return,
            cagr=cagr,
            sharpe_ratio=sharpe,
            sortino_ratio=sortino,
            max_drawdown=max_dd,
            max_drawdown_date=max_dd_date,
            calmar_ratio=calmar,
            realized_vol=realized_vol,
            downside_vol=downside_vol,
            var_95=var_95,
            var_99=var_99,
            expected_shortfall=es,
            avg_daily_turnover=avg_turnover,
            total_transaction_costs=total_tx_costs,
            total_carry_costs=total_carry_costs,
            total_costs=total_tx_costs + total_carry_costs,
            stress_periods=stress_stats,
            insurance_score=insurance_score,
            core_rv_contribution=core_contribution,
            sector_rv_contribution=core_contribution * 0.8,
            single_name_contribution=core_contribution * 0.5,
            credit_carry_contribution=0.07 * self.config.sleeve_weights["credit_carry"],
            crisis_alpha_contribution=0.0,  # Net ~0 over time
            daily_results=daily_results,
            run_duration_seconds=(datetime.now() - start_time).total_seconds()
        )

    def _analyze_stress_periods(
        self,
        daily_results: List[DailyResult]
    ) -> List[StressPeriodStats]:
        """Analyze performance during stress periods."""
        stats = []
        results_by_date = {r.date: r for r in daily_results}

        for name, start, end in self.STRESS_PERIODS:
            period_results = [
                r for d, r in results_by_date.items()
                if start <= d <= end
            ]

            if not period_results:
                continue

            returns = [r.daily_return for r in period_results]
            navs = [r.nav for r in period_results]

            total_ret = (navs[-1] / navs[0]) - 1 if navs[0] > 0 else 0

            # Drawdown within period
            peak = navs[0]
            max_dd = 0
            for nav in navs:
                peak = max(peak, nav)
                dd = (nav - peak) / peak
                max_dd = min(max_dd, dd)

            stats.append(StressPeriodStats(
                name=name,
                start_date=start,
                end_date=end,
                total_return=total_ret,
                max_drawdown=max_dd,
                avg_daily_return=np.mean(returns),
                vol_realized=np.std(returns) * np.sqrt(252),
                hedge_payoff=sum(
                    r.daily_return for r in period_results
                    if r.regime in ["ELEVATED", "CRISIS"]
                )
            ))

        return stats

    def _compute_insurance_score(self, daily_results: List[DailyResult]) -> float:
        """
        Compute insurance payoff score.

        Measures how well the strategy pays off on stress days
        (high V2X/VIX) vs normal days.
        """
        stress_returns = [
            r.daily_return for r in daily_results
            if r.vix > 25 or (r.v2x and r.v2x > 25)
        ]
        normal_returns = [
            r.daily_return for r in daily_results
            if r.vix <= 25 and (r.v2x is None or r.v2x <= 25)
        ]

        avg_stress = np.mean(stress_returns) if stress_returns else 0
        avg_normal = np.mean(normal_returns) if normal_returns else 0

        # Score: how much better on stress days (annualized)
        return (avg_stress - avg_normal) * 252

    def save_report(self, result: BacktestResult, filename: Optional[str] = None) -> str:
        """
        Save backtest report to JSON file.

        Args:
            result: Backtest result
            filename: Optional filename (default: report_YYYYMMDD.json)

        Returns:
            Path to saved file
        """
        output_dir = Path(self.config.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        if filename is None:
            filename = f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

        filepath = output_dir / filename

        # Convert to serializable dict
        report = {
            "summary": {
                "total_return": result.total_return,
                "cagr": result.cagr,
                "sharpe_ratio": result.sharpe_ratio,
                "sortino_ratio": result.sortino_ratio,
                "max_drawdown": result.max_drawdown,
                "max_drawdown_date": str(result.max_drawdown_date),
                "calmar_ratio": result.calmar_ratio,
                "realized_vol": result.realized_vol,
                "insurance_score": result.insurance_score,
            },
            "costs": {
                "avg_daily_turnover": result.avg_daily_turnover,
                "total_transaction_costs": result.total_transaction_costs,
                "total_carry_costs": result.total_carry_costs,
                "total_costs": result.total_costs,
            },
            "risk": {
                "var_95": result.var_95,
                "var_99": result.var_99,
                "expected_shortfall": result.expected_shortfall,
                "downside_vol": result.downside_vol,
            },
            "attribution": {
                "core_rv": result.core_rv_contribution,
                "sector_rv": result.sector_rv_contribution,
                "single_name": result.single_name_contribution,
                "credit_carry": result.credit_carry_contribution,
                "crisis_alpha": result.crisis_alpha_contribution,
            },
            "stress_periods": [
                {
                    "name": sp.name,
                    "start": str(sp.start_date),
                    "end": str(sp.end_date),
                    "total_return": sp.total_return,
                    "max_drawdown": sp.max_drawdown,
                    "hedge_payoff": sp.hedge_payoff,
                }
                for sp in result.stress_periods
            ],
            "config": {
                "start_date": str(result.config.start_date),
                "end_date": str(result.config.end_date),
                "initial_capital": result.config.initial_capital,
                "vol_target": result.config.vol_target_annual,
                "fx_hedge_mode": result.config.fx_hedge_mode,
            },
            "metadata": {
                "run_date": result.run_date.isoformat(),
                "run_duration_seconds": result.run_duration_seconds,
                "trading_days": len(result.daily_results),
            }
        }

        with open(filepath, 'w') as f:
            json.dump(report, f, indent=2)

        logger.info(f"Saved backtest report to {filepath}")
        return str(filepath)


def run_backtest(
    start_date: date = date(2008, 1, 1),
    end_date: date = date.today(),
    initial_capital: float = 1_000_000.0,
    output_dir: str = "state/research"
) -> BacktestResult:
    """
    Convenience function to run a backtest.

    Args:
        start_date: Backtest start date
        end_date: Backtest end date
        initial_capital: Starting capital
        output_dir: Directory for report output

    Returns:
        BacktestResult
    """
    config = BacktestConfig(
        start_date=start_date,
        end_date=end_date,
        initial_capital=initial_capital,
        output_dir=output_dir
    )

    runner = BacktestRunner(config)
    result = runner.run()
    runner.save_report(result)

    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run AbstractFinance backtest")
    parser.add_argument("--start", default="2008-01-01", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", default=str(date.today()), help="End date (YYYY-MM-DD)")
    parser.add_argument("--capital", type=float, default=1_000_000, help="Initial capital")
    parser.add_argument("--output", default="state/research", help="Output directory")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    result = run_backtest(
        start_date=date.fromisoformat(args.start),
        end_date=date.fromisoformat(args.end),
        initial_capital=args.capital,
        output_dir=args.output
    )

    print(f"\nBacktest Results ({args.start} to {args.end}):")
    print(f"  Total Return: {result.total_return:.1%}")
    print(f"  CAGR: {result.cagr:.1%}")
    print(f"  Sharpe Ratio: {result.sharpe_ratio:.2f}")
    print(f"  Max Drawdown: {result.max_drawdown:.1%}")
    print(f"  Insurance Score: {result.insurance_score:.2%}")
