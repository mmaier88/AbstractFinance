"""
Market data abstraction layer for AbstractFinance.
Provides unified access to IBKR data with fallbacks to external sources.
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Union
from dataclasses import dataclass
import yaml
import yfinance as yf
from pathlib import Path

try:
    from ib_insync import IB, Contract, Stock, Future, Forex, Option, util
    IB_AVAILABLE = True
except ImportError:
    IB_AVAILABLE = False


@dataclass
class InstrumentSpec:
    """Specification for a tradeable instrument."""
    instrument_id: str
    symbol: str
    exchange: str
    sec_type: str
    currency: str
    multiplier: float = 1.0
    underlying: Optional[str] = None
    description: Optional[str] = None


class DataFeed:
    """
    Market data feed abstraction.
    Supports IBKR as primary source with yfinance as fallback for backtesting.
    """

    # Mapping from internal symbols to yfinance tickers
    YFINANCE_MAPPING = {
        # US Futures - use index proxies
        "ES": "^GSPC",      # S&P 500 index as proxy for ES
        "MES": "^GSPC",
        "FESX": "^STOXX50E",  # Euro STOXX 50
        "6E": "EURUSD=X",
        "M6E": "EURUSD=X",
        "VIX": "^VIX",
        "VX": "^VIX",

        # US ETFs (legacy - kept for backwards compatibility)
        "SPY": "SPY",
        "FEZ": "FEZ",
        "IEUR": "IEUR",
        "XLK": "XLK",
        "QQQ": "QQQ",
        "IGV": "IGV",
        "SMH": "SMH",
        "XLV": "XLV",
        "XBI": "XBI",
        "IBB": "IBB",
        "QUAL": "QUAL",
        "MTUM": "MTUM",
        "EUFN": "EUFN",
        "EWU": "EWU",
        "EWG": "EWG",
        "LQD": "LQD",
        "HYG": "HYG",
        "JNK": "JNK",
        "BKLN": "BKLN",
        "SRLN": "SRLN",

        # UCITS ETFs on LSE (add .L suffix for yfinance)
        "CSPX": "CSPX.L",   # iShares Core S&P 500 UCITS
        "CNDX": "CNDX.L",   # iShares NASDAQ 100 UCITS
        "IUIT": "IUIT.L",   # iShares S&P 500 IT Sector UCITS
        "WTCH": "WTCH.L",   # WisdomTree Cloud Computing UCITS
        "SEMI": "SEMI.L",   # VanEck Semiconductor UCITS
        "IUHC": "IUHC.L",   # iShares S&P 500 Health Care UCITS
        "SBIO": "SBIO.L",   # Invesco NASDAQ Biotech UCITS
        "BTEK": "BTEK.L",   # iShares Nasdaq US Biotechnology UCITS
        "IUQA": "IUQA.L",   # iShares Edge MSCI USA Quality UCITS
        "IUMO": "IUMO.L",   # iShares Edge MSCI USA Momentum UCITS
        "SMEA": "SMEA.L",   # iShares Core MSCI Europe UCITS
        "IUKD": "IUKD.L",   # iShares UK Dividend UCITS
        "LQDE": "LQDE.L",   # iShares $ Corp Bond UCITS
        "IHYU": "IHYU.L",   # iShares $ High Yield Corp Bond UCITS
        "HYLD": "HYLD.L",   # iShares $ High Yield Corp Bond ESG UCITS
        "FLOT": "FLOT.L",   # iShares $ Floating Rate Bond UCITS
        "FLOA": "FLOA.L",   # iShares $ Ultrashort Bond UCITS
        "IHYG": "IHYG.L",   # iShares EUR High Yield Corp Bond UCITS

        # UCITS ETFs on XETRA (Yahoo Finance uses different tickers)
        "CS51": "SXRT.DE",  # iShares Core Euro STOXX 50 UCITS (IBKR: CS51 -> YF: SXRT.DE)
        "EXV1": "EXV1.DE",  # iShares STOXX Europe 600 Banks UCITS
        "EXS1": "EXS1.DE",  # iShares Core DAX UCITS

        # Individual stocks (no suffix needed)
        "ARCC": "ARCC",
        "MAIN": "MAIN",

        # Bond futures - approximation with 10Y Treasury
        "FGBL": "^TNX",
        "FOAT": "^TNX",
        "FBTP": "^TNX",
    }

    def __init__(
        self,
        ib: Optional[Any] = None,
        instruments_config: Optional[Dict] = None,
        settings: Optional[Dict] = None,
        use_cache: bool = True,
        cache_ttl_seconds: int = 60
    ):
        """
        Initialize data feed.

        Args:
            ib: Connected IB instance (ib_insync.IB)
            instruments_config: Instrument configuration dictionary
            settings: Application settings
            use_cache: Whether to cache price data
            cache_ttl_seconds: Cache time-to-live in seconds
        """
        self.ib = ib
        self.instruments_config = instruments_config or {}
        self.settings = settings or {}
        self.use_cache = use_cache
        self.cache_ttl_seconds = cache_ttl_seconds

        # Price cache: {instrument_id: (price, timestamp)}
        self._price_cache: Dict[str, tuple] = {}
        # History cache: {(instrument_id, lookback): (df, timestamp)}
        self._history_cache: Dict[tuple, tuple] = {}

        # Build instrument lookup
        self._instruments: Dict[str, InstrumentSpec] = {}
        self._build_instrument_lookup()

    def _build_instrument_lookup(self) -> None:
        """Build instrument specification lookup from config."""
        for category, instruments in self.instruments_config.items():
            if isinstance(instruments, dict):
                for inst_id, spec in instruments.items():
                    if isinstance(spec, dict) and "symbol" in spec:
                        self._instruments[inst_id] = InstrumentSpec(
                            instrument_id=inst_id,
                            symbol=spec.get("symbol"),
                            exchange=spec.get("exchange", "SMART"),
                            sec_type=spec.get("sec_type", "STK"),
                            currency=spec.get("currency", "USD"),
                            multiplier=spec.get("multiplier", 1.0),
                            underlying=spec.get("underlying"),
                            description=spec.get("description")
                        )

    def _get_ib_contract(self, instrument_id: str) -> Optional[Any]:
        """Create IB contract from instrument spec."""
        if not IB_AVAILABLE or instrument_id not in self._instruments:
            return None

        spec = self._instruments[instrument_id]

        if spec.sec_type == "STK":
            # For European exchanges, use SMART routing with correct primaryExchange
            # IBKR uses LSEETF for LSE ETFs, IBIS for XETRA
            primary_exchange_map = {
                'LSE': 'LSEETF',
                'XETRA': 'IBIS',
                'SBF': 'SBF',
                'IBIS': 'IBIS',
            }
            if spec.exchange in primary_exchange_map:
                primary = primary_exchange_map[spec.exchange]
                return Stock(spec.symbol, 'SMART', spec.currency, primaryExchange=primary)
            return Stock(spec.symbol, spec.exchange, spec.currency)
        elif spec.sec_type == "FUT":
            # For futures, we need to specify expiry - use front month
            from datetime import date
            today = date.today()
            if today.day > 15:
                month = today.month + 1
                year = today.year
                if month > 12:
                    month = 1
                    year += 1
            else:
                month = today.month
                year = today.year
            expiry = f"{year}{month:02d}"
            return Future(spec.symbol, exchange=spec.exchange, lastTradeDateOrContractMonth=expiry)
        elif spec.sec_type == "CASH":
            return Forex(spec.symbol + spec.currency)
        elif spec.sec_type == "OPT":
            # Options require more parameters
            return None  # Handle separately
        return None

    def _is_cache_valid(self, timestamp: datetime) -> bool:
        """Check if cached data is still valid."""
        if not self.use_cache:
            return False
        return (datetime.now() - timestamp).total_seconds() < self.cache_ttl_seconds

    def _get_yfinance_ticker(self, instrument_id: str) -> str:
        """Get yfinance ticker for an instrument."""
        spec = self._instruments.get(instrument_id)
        if spec:
            symbol = spec.symbol
        else:
            symbol = instrument_id

        return self.YFINANCE_MAPPING.get(symbol, symbol)

    def get_last_price(self, instrument_id: str) -> float:
        """
        Get last traded price for an instrument.

        Args:
            instrument_id: Logical instrument identifier

        Returns:
            Last traded price
        """
        # Check cache
        if instrument_id in self._price_cache:
            price, timestamp = self._price_cache[instrument_id]
            if self._is_cache_valid(timestamp):
                return price

        price = None

        # Try IBKR first
        if self.ib and self.ib.isConnected():
            contract = self._get_ib_contract(instrument_id)
            if contract:
                try:
                    self.ib.qualifyContracts(contract)
                    ticker = self.ib.reqMktData(contract, '', False, False)
                    self.ib.sleep(1)  # Wait for data
                    if ticker.last and ticker.last > 0:
                        price = ticker.last
                    elif ticker.close and ticker.close > 0:
                        price = ticker.close
                    self.ib.cancelMktData(contract)
                except Exception:
                    pass

        # Fallback to yfinance
        if price is None:
            try:
                yf_ticker = self._get_yfinance_ticker(instrument_id)
                data = yf.Ticker(yf_ticker)
                hist = data.history(period="1d")
                if not hist.empty:
                    price = hist['Close'].iloc[-1]
            except Exception:
                pass

        # Cache the result
        if price is not None:
            self._price_cache[instrument_id] = (price, datetime.now())
            return price

        raise ValueError(f"Could not get price for {instrument_id}")

    def get_history(
        self,
        instrument_id: str,
        lookback_days: int = 252,
        end_date: Optional[datetime] = None
    ) -> pd.DataFrame:
        """
        Get historical OHLCV data for an instrument.

        Args:
            instrument_id: Logical instrument identifier
            lookback_days: Number of days of history
            end_date: End date (defaults to today)

        Returns:
            DataFrame with columns: Open, High, Low, Close, Volume
        """
        cache_key = (instrument_id, lookback_days, end_date)

        # Check cache
        if cache_key in self._history_cache:
            df, timestamp = self._history_cache[cache_key]
            if self._is_cache_valid(timestamp):
                return df.copy()

        df = None
        end_date = end_date or datetime.now()
        start_date = end_date - timedelta(days=int(lookback_days * 1.5))  # Buffer for non-trading days

        # Try IBKR first
        if self.ib and self.ib.isConnected():
            contract = self._get_ib_contract(instrument_id)
            if contract:
                try:
                    self.ib.qualifyContracts(contract)
                    bars = self.ib.reqHistoricalData(
                        contract,
                        endDateTime=end_date,
                        durationStr=f"{lookback_days} D",
                        barSizeSetting="1 day",
                        whatToShow="TRADES",
                        useRTH=True
                    )
                    if bars:
                        df = util.df(bars)
                        df = df.rename(columns={
                            'open': 'Open',
                            'high': 'High',
                            'low': 'Low',
                            'close': 'Close',
                            'volume': 'Volume'
                        })
                        df.index = pd.to_datetime(df['date'])
                        df = df[['Open', 'High', 'Low', 'Close', 'Volume']]
                except Exception:
                    pass

        # Fallback to yfinance
        if df is None or df.empty:
            try:
                yf_ticker = self._get_yfinance_ticker(instrument_id)
                data = yf.Ticker(yf_ticker)
                df = data.history(
                    start=start_date.strftime("%Y-%m-%d"),
                    end=end_date.strftime("%Y-%m-%d")
                )
                if not df.empty:
                    df = df[['Open', 'High', 'Low', 'Close', 'Volume']]
            except Exception:
                pass

        if df is not None and not df.empty:
            # Ensure we have the right number of days
            df = df.tail(lookback_days)
            self._history_cache[cache_key] = (df, datetime.now())
            return df.copy()

        raise ValueError(f"Could not get history for {instrument_id}")

    def get_vix_level(self) -> float:
        """
        Get current VIX level.

        Returns:
            Current VIX index value
        """
        # Check cache
        if "VIX" in self._price_cache:
            price, timestamp = self._price_cache["VIX"]
            if self._is_cache_valid(timestamp):
                return price

        vix = None

        # Try yfinance (VIX is readily available)
        try:
            data = yf.Ticker("^VIX")
            hist = data.history(period="1d")
            if not hist.empty:
                vix = hist['Close'].iloc[-1]
        except Exception:
            pass

        if vix is not None:
            self._price_cache["VIX"] = (vix, datetime.now())
            return vix

        # Default fallback
        return 20.0  # Historical average

    def get_ratio_series_spx_sx5e(self, lookback_days: int = 252) -> pd.Series:
        """
        Get SPX/SX5E ratio time series for regime detection.

        Args:
            lookback_days: Number of days of history

        Returns:
            Series of SPX/SX5E price ratios
        """
        try:
            spx_hist = self.get_history("SPY", lookback_days)
            sx5e_hist = self.get_history("FEZ", lookback_days)

            # Align dates
            common_dates = spx_hist.index.intersection(sx5e_hist.index)
            spx_prices = spx_hist.loc[common_dates, 'Close']
            sx5e_prices = sx5e_hist.loc[common_dates, 'Close']

            # Normalize and compute ratio
            ratio = (spx_prices / spx_prices.iloc[0]) / (sx5e_prices / sx5e_prices.iloc[0])
            return ratio

        except Exception as e:
            # Return neutral ratio if data unavailable
            return pd.Series([1.0] * lookback_days)

    def get_equity_universe_prices(
        self,
        universe: List[str],
        lookback_days: int = 252
    ) -> pd.DataFrame:
        """
        Get price panel for equity universe.

        Args:
            universe: List of ticker symbols
            lookback_days: Number of days of history

        Returns:
            DataFrame with columns as tickers, rows as dates
        """
        prices = {}

        for ticker in universe:
            try:
                hist = self.get_history(ticker, lookback_days)
                prices[ticker] = hist['Close']
            except Exception:
                continue

        if prices:
            df = pd.DataFrame(prices)
            return df.dropna(how='all')

        return pd.DataFrame()

    def get_returns(
        self,
        instrument_id: str,
        lookback_days: int = 252
    ) -> pd.Series:
        """
        Get daily returns for an instrument.

        Args:
            instrument_id: Instrument identifier
            lookback_days: Number of days

        Returns:
            Series of daily returns
        """
        hist = self.get_history(instrument_id, lookback_days)
        returns = hist['Close'].pct_change().dropna()
        return returns

    def get_spread_returns(
        self,
        long_instrument: str,
        short_instrument: str,
        lookback_days: int = 252,
        hedge_ratio: float = 1.0
    ) -> pd.Series:
        """
        Get returns for a long/short spread.

        Args:
            long_instrument: Long leg instrument
            short_instrument: Short leg instrument
            lookback_days: Number of days
            hedge_ratio: Short leg hedge ratio

        Returns:
            Series of spread returns
        """
        long_returns = self.get_returns(long_instrument, lookback_days)
        short_returns = self.get_returns(short_instrument, lookback_days)

        # Align dates
        common_dates = long_returns.index.intersection(short_returns.index)
        spread_returns = long_returns.loc[common_dates] - hedge_ratio * short_returns.loc[common_dates]

        return spread_returns

    def get_correlation(
        self,
        instrument1: str,
        instrument2: str,
        lookback_days: int = 252
    ) -> float:
        """
        Get rolling correlation between two instruments.

        Args:
            instrument1: First instrument
            instrument2: Second instrument
            lookback_days: Lookback period

        Returns:
            Correlation coefficient
        """
        returns1 = self.get_returns(instrument1, lookback_days)
        returns2 = self.get_returns(instrument2, lookback_days)

        common_dates = returns1.index.intersection(returns2.index)
        return returns1.loc[common_dates].corr(returns2.loc[common_dates])

    def get_realized_volatility(
        self,
        instrument_id: str,
        lookback_days: int = 20
    ) -> float:
        """
        Get annualized realized volatility.

        Args:
            instrument_id: Instrument identifier
            lookback_days: Lookback period

        Returns:
            Annualized volatility
        """
        returns = self.get_returns(instrument_id, lookback_days + 10)
        daily_vol = returns.tail(lookback_days).std()
        return daily_vol * np.sqrt(252)

    def clear_cache(self) -> None:
        """Clear all cached data."""
        self._price_cache.clear()
        self._history_cache.clear()


def load_instruments_config(config_path: str = "config/instruments.yaml") -> Dict:
    """Load instruments configuration from YAML file."""
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)


def load_settings(config_path: str = "config/settings.yaml") -> Dict:
    """Load application settings from YAML file."""
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)
