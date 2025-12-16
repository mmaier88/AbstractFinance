"""
Research Market Data - Yahoo/yfinance allowed for backtests and research.

This module is ONLY for:
- Backtesting
- Offline analytics
- Historical data analysis
- Research and development

CRITICAL: This module must NEVER be used in live trading code paths.
For live trading, use LiveMarketData (IBKR only).
"""

import logging
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from typing import Dict, Optional, Any, List, Union

import pandas as pd

from ..execution.types import MarketDataSnapshot


logger = logging.getLogger(__name__)


# Lazy import yfinance to avoid pulling it in production
_yfinance = None


def _get_yfinance():
    """Lazy load yfinance module."""
    global _yfinance
    if _yfinance is None:
        try:
            import yfinance as yf
            _yfinance = yf
        except ImportError:
            logger.error("yfinance not installed - research data unavailable")
            return None
    return _yfinance


@dataclass
class ResearchDataConfig:
    """Configuration for research data provider."""
    cache_ttl_minutes: int = 15
    default_period: str = "1d"
    default_interval: str = "1m"
    max_retries: int = 3


class ResearchMarketData:
    """
    Research market data provider using Yahoo Finance.

    WARNING: This class is for RESEARCH/BACKTEST ONLY.
    Never use this for live trading decisions.

    Usage:
        research_md = ResearchMarketData()
        snapshot = research_md.get_snapshot("AAPL")
        # Use for analysis, NOT for live trading
    """

    def __init__(
        self,
        config: Optional[ResearchDataConfig] = None,
    ):
        """
        Initialize research market data provider.

        Args:
            config: Optional configuration
        """
        self.config = config or ResearchDataConfig()

        # Cache for Yahoo data
        self._cache: Dict[str, pd.DataFrame] = {}
        self._cache_timestamps: Dict[str, datetime] = {}

    def get_snapshot(
        self,
        instrument_id: str,
    ) -> Optional[MarketDataSnapshot]:
        """
        Get research data snapshot from Yahoo.

        WARNING: For research/backtest only. Never use in live trading.

        Args:
            instrument_id: Instrument identifier (ticker symbol)

        Returns:
            MarketDataSnapshot or None if unavailable
        """
        yf = _get_yfinance()
        if yf is None:
            return None

        try:
            ticker = yf.Ticker(instrument_id)
            info = ticker.fast_info

            # Get recent price data
            hist = ticker.history(period="1d", interval="1m")
            if hist.empty:
                logger.warning(f"No Yahoo data for {instrument_id}")
                return None

            latest = hist.iloc[-1]

            return MarketDataSnapshot(
                symbol=instrument_id,
                ts=datetime.now(),
                last=float(latest['Close']),
                bid=None,  # Yahoo doesn't provide real-time bid/ask
                ask=None,
                close=float(info.previous_close) if hasattr(info, 'previous_close') else None,
                volume=int(latest['Volume']) if 'Volume' in latest else None,
            )

        except Exception as e:
            logger.error(f"Yahoo fetch error for {instrument_id}: {e}")
            return None

    def get_historical_data(
        self,
        instrument_id: str,
        start_date: Union[str, date],
        end_date: Optional[Union[str, date]] = None,
        interval: str = "1d",
    ) -> Optional[pd.DataFrame]:
        """
        Get historical OHLCV data from Yahoo.

        Args:
            instrument_id: Ticker symbol
            start_date: Start date (YYYY-MM-DD or date object)
            end_date: End date (default: today)
            interval: Data interval (1m, 5m, 1h, 1d, etc.)

        Returns:
            DataFrame with OHLCV data or None
        """
        yf = _get_yfinance()
        if yf is None:
            return None

        try:
            if isinstance(start_date, date):
                start_date = start_date.isoformat()
            if end_date and isinstance(end_date, date):
                end_date = end_date.isoformat()
            if end_date is None:
                end_date = date.today().isoformat()

            ticker = yf.Ticker(instrument_id)
            hist = ticker.history(start=start_date, end=end_date, interval=interval)

            if hist.empty:
                logger.warning(f"No historical data for {instrument_id}")
                return None

            return hist

        except Exception as e:
            logger.error(f"Historical data fetch error for {instrument_id}: {e}")
            return None

    def get_bulk_historical(
        self,
        instrument_ids: List[str],
        start_date: Union[str, date],
        end_date: Optional[Union[str, date]] = None,
        interval: str = "1d",
    ) -> Dict[str, Optional[pd.DataFrame]]:
        """
        Get historical data for multiple instruments.

        Args:
            instrument_ids: List of ticker symbols
            start_date: Start date
            end_date: End date
            interval: Data interval

        Returns:
            Dictionary of instrument_id -> DataFrame
        """
        yf = _get_yfinance()
        if yf is None:
            return {inst_id: None for inst_id in instrument_ids}

        results = {}
        try:
            if isinstance(start_date, date):
                start_date = start_date.isoformat()
            if end_date and isinstance(end_date, date):
                end_date = end_date.isoformat()
            if end_date is None:
                end_date = date.today().isoformat()

            # Bulk download
            tickers = " ".join(instrument_ids)
            data = yf.download(tickers, start=start_date, end=end_date, interval=interval, group_by='ticker')

            if data.empty:
                return {inst_id: None for inst_id in instrument_ids}

            # Split by ticker
            for inst_id in instrument_ids:
                try:
                    if len(instrument_ids) == 1:
                        results[inst_id] = data
                    else:
                        results[inst_id] = data[inst_id] if inst_id in data.columns.get_level_values(0) else None
                except Exception:
                    results[inst_id] = None

            return results

        except Exception as e:
            logger.error(f"Bulk historical fetch error: {e}")
            return {inst_id: None for inst_id in instrument_ids}

    def get_fundamentals(
        self,
        instrument_id: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Get fundamental data from Yahoo.

        Args:
            instrument_id: Ticker symbol

        Returns:
            Dictionary of fundamental metrics or None
        """
        yf = _get_yfinance()
        if yf is None:
            return None

        try:
            ticker = yf.Ticker(instrument_id)
            info = ticker.info

            return {
                "market_cap": info.get("marketCap"),
                "pe_ratio": info.get("trailingPE"),
                "forward_pe": info.get("forwardPE"),
                "dividend_yield": info.get("dividendYield"),
                "beta": info.get("beta"),
                "52_week_high": info.get("fiftyTwoWeekHigh"),
                "52_week_low": info.get("fiftyTwoWeekLow"),
                "avg_volume": info.get("averageVolume"),
                "avg_volume_10d": info.get("averageDailyVolume10Day"),
                "sector": info.get("sector"),
                "industry": info.get("industry"),
            }

        except Exception as e:
            logger.error(f"Fundamentals fetch error for {instrument_id}: {e}")
            return None

    def get_dividend_calendar(
        self,
        instrument_id: str,
    ) -> Optional[pd.DataFrame]:
        """
        Get dividend history and upcoming dividends.

        Args:
            instrument_id: Ticker symbol

        Returns:
            DataFrame of dividend history or None
        """
        yf = _get_yfinance()
        if yf is None:
            return None

        try:
            ticker = yf.Ticker(instrument_id)
            dividends = ticker.dividends

            if dividends.empty:
                return None

            return dividends.reset_index()

        except Exception as e:
            logger.error(f"Dividend fetch error for {instrument_id}: {e}")
            return None

    def get_average_daily_volume(
        self,
        instrument_id: str,
        lookback_days: int = 20,
    ) -> Optional[int]:
        """
        Calculate average daily volume.

        Args:
            instrument_id: Ticker symbol
            lookback_days: Days to average

        Returns:
            Average daily volume or None
        """
        try:
            end_date = date.today()
            start_date = end_date - timedelta(days=lookback_days + 10)  # Extra buffer for weekends

            hist = self.get_historical_data(
                instrument_id,
                start_date=start_date,
                end_date=end_date,
                interval="1d",
            )

            if hist is None or hist.empty:
                return None

            # Take last N trading days
            volumes = hist['Volume'].tail(lookback_days)
            return int(volumes.mean())

        except Exception as e:
            logger.error(f"ADV calculation error for {instrument_id}: {e}")
            return None

    def is_available(self) -> bool:
        """Check if Yahoo data is available."""
        return _get_yfinance() is not None


# Singleton instance
_research_market_data: Optional[ResearchMarketData] = None


def get_research_market_data() -> ResearchMarketData:
    """Get singleton ResearchMarketData instance."""
    global _research_market_data
    if _research_market_data is None:
        _research_market_data = ResearchMarketData()
    return _research_market_data
