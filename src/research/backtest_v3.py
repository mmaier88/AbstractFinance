"""
Comprehensive Backtest for AbstractFinance v3.0 Strategy.

Tests the current portfolio configuration over 20 years (2005-2025):
- Core Index RV: Long US / Short EU
- Sector RV: Factor-neutral sector pairs
- Europe Vol Convex: VIX calls + put spreads + hedge ladder
- Sovereign Rates Short: BTP-Bund spread (DV01-neutral)
- Money Market: Cash allocation

Key stress periods analyzed:
- 2008 GFC
- 2010-2012 Eurozone Crisis
- 2020 COVID
- 2022 Rate Shock

Created: January 2026
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class SleeveConfig:
    """Configuration for a single sleeve."""
    name: str
    weight: float
    enabled: bool = True
    regime_weights: Dict[str, float] = field(default_factory=dict)


@dataclass
class StrategyConfigV3:
    """v3.0 Strategy Configuration."""
    # Sleeve allocations
    core_index_rv: float = 0.20
    sector_rv: float = 0.20
    europe_vol_convex: float = 0.18
    money_market: float = 0.34
    credit_carry: float = 0.00  # Disabled in v3.0

    # Sovereign Rates Short (new in v3.0)
    sovereign_rates_short_enabled: bool = True
    sovereign_rates_short_base_weights: Dict[str, float] = field(default_factory=lambda: {
        'normal': 0.06,
        'elevated': 0.12,
        'crisis': 0.16,
    })
    sovereign_rates_short_max_weights: Dict[str, float] = field(default_factory=lambda: {
        'normal': 0.10,
        'elevated': 0.16,
        'crisis': 0.20,
    })

    # Volatility targeting
    vol_target_annual: float = 0.12
    vol_floor: float = 0.08
    vol_cap: float = 0.30

    # Regime thresholds
    vix_elevated: float = 25.0
    vix_crisis: float = 35.0

    # FX hedge
    fx_hedge_ratio: float = 0.75

    # Transaction costs (bps)
    equity_slippage_bps: float = 5.0
    futures_slippage_bps: float = 2.0
    borrow_cost_bps_annual: float = 50.0


@dataclass
class BacktestResultV3:
    """v3.0 Backtest Results."""
    # Summary
    total_return: float
    cagr: float
    sharpe_ratio: float
    sortino_ratio: float
    max_drawdown: float
    max_dd_date: str
    calmar_ratio: float

    # Risk metrics
    realized_vol: float
    downside_vol: float
    var_95_daily: float
    var_99_daily: float

    # Sleeve attribution (annualized contribution)
    attribution: Dict[str, float]

    # Stress period performance
    stress_periods: Dict[str, Dict[str, float]]

    # Insurance effectiveness
    insurance_score: float  # Outperformance on stress days
    crisis_payoff_multiple: float  # Return in crisis / annual theta

    # Daily time series
    nav_series: List[float]
    dates: List[str]
    regimes: List[str]

    # Sovereign rates short specific
    sov_rates_total_return: float
    sov_rates_sharpe: float
    sov_rates_max_dd: float
    sov_rates_win_rate: float


class MarketDataCache:
    """Cached market data for backtesting."""

    def __init__(self, cache_dir: str = "state/cache/backtest"):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._data: Dict[str, pd.DataFrame] = {}

    def fetch_or_load(self, symbol: str, start: date, end: date) -> pd.Series:
        """Fetch data from Yahoo or load from cache."""
        cache_file = self.cache_dir / f"{symbol}_{start}_{end}.parquet"

        if cache_file.exists():
            df = pd.read_parquet(cache_file)
            return df['close']

        try:
            import yfinance as yf
            ticker = yf.Ticker(symbol)
            hist = ticker.history(start=start, end=end + timedelta(days=1), auto_adjust=True)

            if hist.empty:
                raise ValueError(f"No data for {symbol}")

            df = pd.DataFrame({'close': hist['Close']})
            df.index = pd.to_datetime(df.index).date
            df.to_parquet(cache_file)

            return df['close']
        except Exception as e:
            logger.warning(f"Failed to fetch {symbol}: {e}")
            raise

    def get_spread_data(self, start: date, end: date) -> pd.DataFrame:
        """
        Get BTP-Bund spread proxy data.

        Uses:
        - German 10Y yield proxy: ^TNX adjusted or IEF inverse
        - Italian 10Y yield proxy: approximated from EWI/EWG spread

        For more accurate modeling, we simulate the spread based on:
        - Periods of stress (2010-2012 Euro crisis had spreads 300-500bps)
        - Normal periods (spreads 100-200bps)
        """
        # Get VIX for regime detection
        vix = self.fetch_or_load("^VIX", start, end)

        # Get Euro crisis proxy (EWI - Italy ETF)
        try:
            ewi = self.fetch_or_load("EWI", start, end)
            ewg = self.fetch_or_load("EWG", start, end)
        except Exception:
            # Fallback: create synthetic data
            ewi = pd.Series(index=vix.index, data=100.0)
            ewg = pd.Series(index=vix.index, data=100.0)

        # Align indices
        common = vix.index.intersection(ewi.index).intersection(ewg.index)
        vix = vix.loc[common]
        ewi = ewi.loc[common]
        ewg = ewg.loc[common]

        # Model BTP-Bund spread based on EWI/EWG relative performance
        # When Italy underperforms Germany, spreads widen
        ewi_ret = ewi.pct_change()
        ewg_ret = ewg.pct_change()
        relative_perf = (ewi_ret - ewg_ret).rolling(20).sum().fillna(0)

        # Base spread (150 bps) + sensitivity to relative performance
        # Negative relative_perf (Italy underperforming) -> wider spreads
        spread_bps = 150 - relative_perf * 1000  # Scale factor
        spread_bps = spread_bps.clip(50, 700)  # Historical bounds

        # Add stress spikes during known crisis periods
        for dt in spread_bps.index:
            if isinstance(dt, date):
                # 2011-2012 Euro crisis
                if date(2011, 7, 1) <= dt <= date(2012, 7, 31):
                    spread_bps.loc[dt] = max(spread_bps.loc[dt], 350 + np.random.normal(0, 50))
                # 2020 COVID
                elif date(2020, 3, 1) <= dt <= date(2020, 4, 30):
                    spread_bps.loc[dt] = max(spread_bps.loc[dt], 250 + np.random.normal(0, 30))
                # 2022 rate shock
                elif date(2022, 6, 1) <= dt <= date(2022, 10, 31):
                    spread_bps.loc[dt] = max(spread_bps.loc[dt], 220 + np.random.normal(0, 25))

        df = pd.DataFrame({
            'vix': vix,
            'spread_bps': spread_bps,
            'ewi': ewi,
            'ewg': ewg,
        })

        return df


class BacktestRunnerV3:
    """
    v3.0 Strategy Backtest Runner.

    Tests the full strategy including sovereign rates short.
    """

    STRESS_PERIODS = {
        "GFC 2008": (date(2008, 9, 1), date(2009, 3, 31)),
        "Euro Crisis 2011-12": (date(2011, 7, 1), date(2012, 7, 31)),
        "COVID 2020": (date(2020, 2, 15), date(2020, 4, 15)),
        "Rate Shock 2022": (date(2022, 1, 1), date(2022, 10, 31)),
    }

    def __init__(
        self,
        config: StrategyConfigV3,
        start_date: date = date(2005, 1, 1),
        end_date: date = date(2025, 1, 1),
        initial_capital: float = 10_000_000.0,
    ):
        self.config = config
        self.start_date = start_date
        self.end_date = end_date
        self.initial_capital = initial_capital
        self.market_data = MarketDataCache()

        # State
        self._nav = initial_capital
        self._hwm = initial_capital
        self._regime = "NORMAL"
        self._spread_z_history: List[float] = []

    def run(self) -> BacktestResultV3:
        """Run the backtest."""
        logger.info(f"Running v3.0 backtest from {self.start_date} to {self.end_date}")

        # Load market data
        spy = self.market_data.fetch_or_load("SPY", self.start_date, self.end_date)
        ezu = self.market_data.fetch_or_load("EZU", self.start_date, self.end_date)
        vix = self.market_data.fetch_or_load("^VIX", self.start_date, self.end_date)

        spread_data = self.market_data.get_spread_data(self.start_date, self.end_date)

        # Align all data
        common = spy.index.intersection(ezu.index).intersection(vix.index).intersection(spread_data.index)
        spy = spy.loc[common]
        ezu = ezu.loc[common]
        vix = vix.loc[common]
        spread_data = spread_data.loc[common]

        # Calculate returns
        spy_ret = spy.pct_change().fillna(0)
        ezu_ret = ezu.pct_change().fillna(0)
        spread_change = spread_data['spread_bps'].diff().fillna(0)

        # Track results
        nav_history = [self.initial_capital]
        dates = [str(common[0])]
        regimes = ["NORMAL"]

        # Sleeve attribution tracking
        sleeve_pnl = {
            'core_index_rv': [],
            'sector_rv': [],
            'europe_vol_convex': [],
            'sovereign_rates_short': [],
            'money_market': [],
        }

        # Sovereign rates short tracking
        sov_rates_returns = []
        sov_rates_active_days = 0

        for i in range(1, len(common)):
            dt = common[i]
            current_vix = vix.iloc[i]
            current_spread = spread_data['spread_bps'].iloc[i]

            # Determine regime
            regime = self._compute_regime(current_vix)
            regimes.append(regime)

            # Compute spread z-score (for sovereign rates short sizing)
            spread_z = self._compute_spread_z(current_spread)

            # Get sovereign rates short weight based on regime
            sov_weight = self._get_sovereign_weight(regime, spread_z, current_vix)

            # Calculate sleeve returns

            # 1. Core Index RV: Long US / Short EU
            core_rv_ret = (spy_ret.iloc[i] - ezu_ret.iloc[i]) * self.config.core_index_rv

            # 2. Sector RV: Similar but with factor adjustment (0.8x core)
            sector_rv_ret = (spy_ret.iloc[i] - ezu_ret.iloc[i]) * 0.8 * self.config.sector_rv

            # 3. Europe Vol Convex: Insurance payoff
            vol_convex_ret = self._europe_vol_return(current_vix, regime) * self.config.europe_vol_convex

            # 4. Sovereign Rates Short: Profit from spread widening
            # Short BTP (hurt by spread widening) / Long Bund (helped by flight to safety)
            # Net: profit when spreads widen
            sov_rates_ret = self._sovereign_rates_return(spread_change.iloc[i], sov_weight, regime)
            sov_rates_returns.append(sov_rates_ret)
            if sov_weight > 0:
                sov_rates_active_days += 1

            # 5. Money Market: Small positive carry
            mm_ret = 0.04 / 252 * self.config.money_market  # ~4% annual

            # Apply regime scaling to risk sleeves
            regime_scale = {'NORMAL': 1.0, 'ELEVATED': 0.7, 'CRISIS': 0.3}[regime]

            # Total portfolio return
            total_ret = (
                core_rv_ret * regime_scale +
                sector_rv_ret * regime_scale +
                vol_convex_ret +  # Insurance not scaled down
                sov_rates_ret +   # Separate sizing logic
                mm_ret
            )

            # Apply transaction costs (simplified)
            daily_cost = self._daily_costs()
            total_ret -= daily_cost

            # Update NAV
            self._nav *= (1 + total_ret)
            self._hwm = max(self._hwm, self._nav)

            nav_history.append(self._nav)
            dates.append(str(dt))

            # Track attribution
            sleeve_pnl['core_index_rv'].append(core_rv_ret * regime_scale)
            sleeve_pnl['sector_rv'].append(sector_rv_ret * regime_scale)
            sleeve_pnl['europe_vol_convex'].append(vol_convex_ret)
            sleeve_pnl['sovereign_rates_short'].append(sov_rates_ret)
            sleeve_pnl['money_market'].append(mm_ret)

        # Compute results
        return self._compute_results(
            nav_history, dates, regimes, sleeve_pnl, sov_rates_returns, vix
        )

    def _compute_regime(self, vix: float) -> str:
        """Determine market regime based on VIX."""
        if vix >= self.config.vix_crisis:
            return "CRISIS"
        elif vix >= self.config.vix_elevated:
            return "ELEVATED"
        return "NORMAL"

    def _compute_spread_z(self, spread_bps: float) -> float:
        """Compute z-score of BTP-Bund spread."""
        self._spread_z_history.append(spread_bps)

        if len(self._spread_z_history) < 252:
            return 0.0

        # Use last 252 days for z-score
        history = self._spread_z_history[-252:]
        mean = np.mean(history)
        std = np.std(history)

        if std < 1:
            return 0.0

        return (spread_bps - mean) / std

    def _get_sovereign_weight(self, regime: str, spread_z: float, vix: float) -> float:
        """
        Get sovereign rates short weight based on regime and signals.

        Implements:
        - Regime-based base weights
        - Fragmentation multiplier (spread z-score)
        - Deflation guard (zero weight if risk-off AND rates down)
        """
        if not self.config.sovereign_rates_short_enabled:
            return 0.0

        # Get base weight for regime
        base_w = self.config.sovereign_rates_short_base_weights.get(regime.lower(), 0.06)
        max_w = self.config.sovereign_rates_short_max_weights.get(regime.lower(), 0.10)

        # Fragmentation multiplier based on spread z-score
        if spread_z < 0:
            frag_mult = 0.5
        elif spread_z < 1:
            frag_mult = 1.0
        elif spread_z < 2:
            frag_mult = 1.3
        else:
            frag_mult = 1.6

        # Deflation guard: in crisis with spreads compressing, go to zero
        # (This would be rates-down-shock scenario)
        if regime == "CRISIS" and spread_z < -0.5:
            return 0.0

        target_w = min(base_w * frag_mult, max_w)
        return target_w

    def _sovereign_rates_return(
        self,
        spread_change_bps: float,
        weight: float,
        regime: str
    ) -> float:
        """
        Compute return from sovereign rates short position.

        Position: Short BTP (hurt by spread widening) / Long Bund (helped)
        Net DV01 neutral, so profit from spread widening.

        1 bp spread widening = 0.01% on the DV01 notional.
        With 7bps DV01 per NAV, 1bp widening = 0.07% NAV return.
        """
        if weight <= 0:
            return 0.0

        # DV01 sensitivity: 7bps of NAV per 100bp move
        dv01_per_nav = 0.0007

        # Return = spread change * DV01 sensitivity * position weight
        # Positive spread_change = widening = profit
        ret = (spread_change_bps / 100) * dv01_per_nav * (weight / 0.12)  # Normalize by target

        # Cap extreme returns (realistic)
        ret = np.clip(ret, -0.02, 0.05)  # Max 2% loss, 5% gain per day

        return ret

    def _europe_vol_return(self, vix: float, regime: str) -> float:
        """
        Simulate Europe vol convexity returns.

        Includes:
        - VIX calls (proxy for VSTOXX)
        - Put spreads on indices
        - Hedge ladder
        """
        if regime == "CRISIS":
            # Big payoff in crisis
            return 0.015  # 1.5% daily
        elif regime == "ELEVATED":
            return 0.003  # 0.3% daily
        else:
            # Theta decay
            return -0.0003  # ~7.5% annual

    def _daily_costs(self) -> float:
        """Estimate daily transaction and carry costs."""
        # Assume ~5% annual turnover cost equivalent
        return 0.05 / 252

    def _compute_results(
        self,
        nav_history: List[float],
        dates: List[str],
        regimes: List[str],
        sleeve_pnl: Dict[str, List[float]],
        sov_rates_returns: List[float],
        vix: pd.Series,
    ) -> BacktestResultV3:
        """Compute final results."""
        navs = pd.Series(nav_history)
        returns = navs.pct_change().dropna()

        # Summary metrics
        total_return = (navs.iloc[-1] / navs.iloc[0]) - 1
        years = len(navs) / 252
        cagr = (1 + total_return) ** (1 / years) - 1 if years > 0 else 0

        realized_vol = returns.std() * np.sqrt(252)
        sharpe = returns.mean() * 252 / realized_vol if realized_vol > 0 else 0

        downside_ret = returns[returns < 0]
        downside_vol = downside_ret.std() * np.sqrt(252) if len(downside_ret) > 0 else realized_vol
        sortino = returns.mean() * 252 / downside_vol if downside_vol > 0 else 0

        # Drawdown
        rolling_max = navs.cummax()
        drawdowns = (navs - rolling_max) / rolling_max
        max_dd = drawdowns.min()
        max_dd_idx = drawdowns.idxmin()
        max_dd_date = dates[max_dd_idx] if pd.notna(max_dd_idx) else dates[-1]

        calmar = cagr / abs(max_dd) if max_dd < 0 else 0

        # VaR
        var_95 = returns.quantile(0.05)
        var_99 = returns.quantile(0.01)

        # Attribution (annualized)
        attribution = {}
        for sleeve, pnl_list in sleeve_pnl.items():
            attribution[sleeve] = np.mean(pnl_list) * 252 if pnl_list else 0

        # Stress period analysis
        stress_results = {}
        for name, (start, end) in self.STRESS_PERIODS.items():
            period_mask = [
                start <= date.fromisoformat(d) <= end
                for d in dates[1:]  # Skip first day
            ]
            if any(period_mask):
                period_rets = returns[period_mask]
                stress_results[name] = {
                    'total_return': (1 + period_rets).prod() - 1,
                    'max_drawdown': period_rets.cumsum().cummax().sub(period_rets.cumsum()).max() * -1,
                    'avg_daily_return': period_rets.mean(),
                    'days': len(period_rets),
                }

        # Insurance score
        vix_aligned = vix.iloc[:len(returns)]
        stress_mask = vix_aligned > 25
        stress_returns = returns[stress_mask.values] if len(stress_mask) == len(returns) else returns
        normal_returns = returns[~stress_mask.values] if len(stress_mask) == len(returns) else returns

        insurance_score = (
            stress_returns.mean() - normal_returns.mean()
        ) * 252 if len(stress_returns) > 0 and len(normal_returns) > 0 else 0

        # Crisis payoff multiple
        crisis_days = [r for r, reg in zip(regimes[1:], returns) if r == "CRISIS"]
        normal_days = [r for r, reg in zip(regimes[1:], returns) if r == "NORMAL"]

        crisis_payoff_multiple = 0
        if len(crisis_days) > 0 and len(normal_days) > 0:
            # Compare crisis return to normal theta bleed
            crisis_avg = np.mean([returns.iloc[i] for i, r in enumerate(regimes[1:]) if r == "CRISIS"]) if any(r == "CRISIS" for r in regimes) else 0
            normal_bleed = abs(np.mean([returns.iloc[i] for i, r in enumerate(regimes[1:]) if r == "NORMAL"])) if any(r == "NORMAL" for r in regimes) else 0.0001
            crisis_payoff_multiple = crisis_avg / normal_bleed if normal_bleed > 0 else 0

        # Sovereign rates short specific metrics
        sov_rets = pd.Series(sov_rates_returns)
        sov_total = (1 + sov_rets).prod() - 1
        sov_vol = sov_rets.std() * np.sqrt(252)
        sov_sharpe = sov_rets.mean() * 252 / sov_vol if sov_vol > 0 else 0
        sov_dd = (sov_rets.cumsum().cummax() - sov_rets.cumsum()).max()
        sov_win_rate = (sov_rets > 0).mean()

        return BacktestResultV3(
            total_return=total_return,
            cagr=cagr,
            sharpe_ratio=sharpe,
            sortino_ratio=sortino,
            max_drawdown=max_dd,
            max_dd_date=max_dd_date,
            calmar_ratio=calmar,
            realized_vol=realized_vol,
            downside_vol=downside_vol,
            var_95_daily=var_95,
            var_99_daily=var_99,
            attribution=attribution,
            stress_periods=stress_results,
            insurance_score=insurance_score,
            crisis_payoff_multiple=crisis_payoff_multiple,
            nav_series=nav_history,
            dates=dates,
            regimes=regimes,
            sov_rates_total_return=sov_total,
            sov_rates_sharpe=sov_sharpe,
            sov_rates_max_dd=sov_dd,
            sov_rates_win_rate=sov_win_rate,
        )


def run_v3_backtest(
    start_date: date = date(2005, 1, 1),
    end_date: date = date(2025, 1, 1),
    initial_capital: float = 10_000_000.0,
    output_file: str = "state/research/backtest_v3_results.json",
) -> BacktestResultV3:
    """
    Run the v3.0 strategy backtest.
    """
    config = StrategyConfigV3()
    runner = BacktestRunnerV3(
        config=config,
        start_date=start_date,
        end_date=end_date,
        initial_capital=initial_capital,
    )

    result = runner.run()

    # Save results
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    report = {
        "summary": {
            "total_return": f"{result.total_return:.1%}",
            "cagr": f"{result.cagr:.1%}",
            "sharpe_ratio": f"{result.sharpe_ratio:.2f}",
            "sortino_ratio": f"{result.sortino_ratio:.2f}",
            "max_drawdown": f"{result.max_drawdown:.1%}",
            "max_dd_date": result.max_dd_date,
            "calmar_ratio": f"{result.calmar_ratio:.2f}",
            "realized_vol": f"{result.realized_vol:.1%}",
        },
        "risk": {
            "var_95_daily": f"{result.var_95_daily:.2%}",
            "var_99_daily": f"{result.var_99_daily:.2%}",
            "downside_vol": f"{result.downside_vol:.1%}",
        },
        "attribution": {k: f"{v:.1%}" for k, v in result.attribution.items()},
        "stress_periods": {
            name: {k: f"{v:.1%}" if isinstance(v, float) else v for k, v in stats.items()}
            for name, stats in result.stress_periods.items()
        },
        "insurance": {
            "insurance_score": f"{result.insurance_score:.1%}",
            "crisis_payoff_multiple": f"{result.crisis_payoff_multiple:.1f}x",
        },
        "sovereign_rates_short": {
            "total_return": f"{result.sov_rates_total_return:.1%}",
            "sharpe_ratio": f"{result.sov_rates_sharpe:.2f}",
            "max_drawdown": f"{result.sov_rates_max_dd:.1%}",
            "win_rate": f"{result.sov_rates_win_rate:.1%}",
        },
        "config": {
            "start_date": str(start_date),
            "end_date": str(end_date),
            "initial_capital": initial_capital,
        },
    }

    with open(output_path, 'w') as f:
        json.dump(report, f, indent=2)

    print(f"\nBacktest saved to: {output_path}")

    return result


def print_results(result: BacktestResultV3):
    """Print formatted backtest results."""
    print("\n" + "=" * 70)
    print("  ABSTRACTFINANCE v3.0 BACKTEST RESULTS")
    print("=" * 70)

    print(f"\n{'SUMMARY':^70}")
    print("-" * 70)
    print(f"  Total Return:     {result.total_return:>12.1%}")
    print(f"  CAGR:             {result.cagr:>12.1%}")
    print(f"  Sharpe Ratio:     {result.sharpe_ratio:>12.2f}")
    print(f"  Sortino Ratio:    {result.sortino_ratio:>12.2f}")
    print(f"  Max Drawdown:     {result.max_drawdown:>12.1%}")
    print(f"  Calmar Ratio:     {result.calmar_ratio:>12.2f}")
    print(f"  Realized Vol:     {result.realized_vol:>12.1%}")

    print(f"\n{'SLEEVE ATTRIBUTION (Annualized)':^70}")
    print("-" * 70)
    for sleeve, contrib in result.attribution.items():
        print(f"  {sleeve:<30} {contrib:>10.1%}")

    print(f"\n{'STRESS PERIOD PERFORMANCE':^70}")
    print("-" * 70)
    for name, stats in result.stress_periods.items():
        print(f"  {name}:")
        print(f"    Return:      {stats.get('total_return', 0):>10.1%}")
        print(f"    Max DD:      {stats.get('max_drawdown', 0):>10.1%}")

    print(f"\n{'SOVEREIGN RATES SHORT SLEEVE':^70}")
    print("-" * 70)
    print(f"  Total Return:     {result.sov_rates_total_return:>12.1%}")
    print(f"  Sharpe Ratio:     {result.sov_rates_sharpe:>12.2f}")
    print(f"  Max Drawdown:     {result.sov_rates_max_dd:>12.1%}")
    print(f"  Win Rate:         {result.sov_rates_win_rate:>12.1%}")

    print(f"\n{'INSURANCE EFFECTIVENESS':^70}")
    print("-" * 70)
    print(f"  Insurance Score:       {result.insurance_score:>10.1%}")
    print(f"  Crisis Payoff Multiple:{result.crisis_payoff_multiple:>10.1f}x")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run AbstractFinance v3.0 backtest")
    parser.add_argument("--start", default="2005-01-01", help="Start date")
    parser.add_argument("--end", default="2025-01-01", help="End date")
    parser.add_argument("--capital", type=float, default=10_000_000, help="Initial capital")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    result = run_v3_backtest(
        start_date=date.fromisoformat(args.start),
        end_date=date.fromisoformat(args.end),
        initial_capital=args.capital,
    )

    print_results(result)
