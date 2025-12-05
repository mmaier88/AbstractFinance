"""
Stock Screening Module for AbstractFinance.
Implements quantitative factor-based stock selection for the Single Name sleeve.

Screening Methodology:
======================

US LONG SELECTION (Quality + Momentum):
- Quality Score (50% weight):
  - ROE > 15%
  - Debt/Equity < 1.0
  - Positive earnings growth (3Y CAGR)
  - Free cash flow positive

- Momentum Score (30% weight):
  - 12-month price return (excluding last month)
  - Relative strength vs S&P 500

- Size Filter (20% weight):
  - Market cap > $50B (large cap focus)
  - Average daily volume > $50M

EU SHORT SELECTION (Zombie + Weakness):
- Zombie Score (50% weight):
  - Interest coverage ratio < 3x
  - Revenue growth < 0% (3Y CAGR)
  - High debt/equity > 1.5
  - Declining margins

- Weakness Score (30% weight):
  - Negative 12-month momentum
  - Underperforming Euro STOXX 50

- Sector Filter (20% weight):
  - Overweight: Banks, Autos, Utilities, Industrials
  - Avoid: Luxury, Tech (relative EU strength)

Rebalancing:
- Monthly screening refresh
- Position limits: Max 5% per name
- Diversification: Min 10 names per side
"""

import pandas as pd
import numpy as np
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from enum import Enum
import yfinance as yf

from .logging_utils import get_trading_logger


class ScreeningFactor(Enum):
    """Factor types for screening."""
    QUALITY = "quality"
    MOMENTUM = "momentum"
    VALUE = "value"
    ZOMBIE = "zombie"
    SIZE = "size"


@dataclass
class StockScore:
    """Scoring result for a single stock."""
    symbol: str
    composite_score: float
    quality_score: float = 0.0
    momentum_score: float = 0.0
    value_score: float = 0.0
    zombie_score: float = 0.0
    size_score: float = 0.0
    market_cap: float = 0.0
    sector: str = ""
    country: str = ""
    reasons: List[str] = field(default_factory=list)
    data_quality: str = "good"  # good, partial, poor


@dataclass
class ScreeningResult:
    """Complete screening result."""
    long_candidates: List[StockScore]
    short_candidates: List[StockScore]
    screening_date: date
    universe_size: int
    methodology_version: str = "1.0"


class StockScreener:
    """
    Quantitative stock screener for single name selection.

    Implements factor-based screening with:
    - Quality factors (ROE, debt, FCF)
    - Momentum factors (price momentum, relative strength)
    - Zombie detection (interest coverage, revenue decline)
    """

    # Factor weights for US Long selection
    US_LONG_WEIGHTS = {
        'quality': 0.50,
        'momentum': 0.30,
        'size': 0.20
    }

    # Factor weights for EU Short selection
    EU_SHORT_WEIGHTS = {
        'zombie': 0.50,
        'momentum': 0.30,  # Negative momentum
        'sector': 0.20
    }

    # Sector preferences for EU shorts
    EU_SHORT_PREFERRED_SECTORS = [
        'Financial Services', 'Banks',
        'Automotive', 'Auto Manufacturers',
        'Utilities', 'Industrials',
        'Basic Materials', 'Energy'
    ]

    EU_SHORT_AVOID_SECTORS = [
        'Luxury Goods', 'Technology',
        'Consumer Cyclical'  # LVMH, etc.
    ]

    def __init__(
        self,
        us_universe: List[str],
        eu_universe: List[str],
        min_market_cap_usd: float = 50e9,
        min_daily_volume_usd: float = 50e6,
        logger=None
    ):
        """
        Initialize screener.

        Args:
            us_universe: List of US stock symbols to screen
            eu_universe: List of EU stock symbols to screen
            min_market_cap_usd: Minimum market cap filter
            min_daily_volume_usd: Minimum daily volume filter
            logger: Optional logger instance
        """
        self.us_universe = us_universe
        self.eu_universe = eu_universe
        self.min_market_cap = min_market_cap_usd
        self.min_volume = min_daily_volume_usd
        self.logger = logger or get_trading_logger()

        # Cache for fundamentals
        self._fundamentals_cache: Dict[str, Dict] = {}
        self._price_cache: Dict[str, pd.DataFrame] = {}
        self._cache_date: Optional[date] = None

    def screen_stocks(
        self,
        top_n_long: int = 10,
        top_n_short: int = 10,
        refresh_data: bool = True
    ) -> ScreeningResult:
        """
        Run full screening process.

        Args:
            top_n_long: Number of top long candidates to return
            top_n_short: Number of top short candidates to return
            refresh_data: Whether to refresh cached data

        Returns:
            ScreeningResult with ranked candidates
        """
        today = date.today()

        # Refresh cache if needed
        if refresh_data or self._cache_date != today:
            self._refresh_data_cache()
            self._cache_date = today

        # Screen US longs
        us_scores = self._screen_us_longs()
        us_scores.sort(key=lambda x: x.composite_score, reverse=True)
        long_candidates = us_scores[:top_n_long]

        # Screen EU shorts
        eu_scores = self._screen_eu_shorts()
        eu_scores.sort(key=lambda x: x.composite_score, reverse=True)
        short_candidates = eu_scores[:top_n_short]

        return ScreeningResult(
            long_candidates=long_candidates,
            short_candidates=short_candidates,
            screening_date=today,
            universe_size=len(self.us_universe) + len(self.eu_universe)
        )

    def _refresh_data_cache(self) -> None:
        """Refresh fundamentals and price data cache."""
        all_symbols = self.us_universe + self.eu_universe

        for symbol in all_symbols:
            try:
                ticker = yf.Ticker(symbol)

                # Get fundamentals
                info = ticker.info
                self._fundamentals_cache[symbol] = {
                    'market_cap': info.get('marketCap', 0),
                    'roe': info.get('returnOnEquity', 0),
                    'debt_to_equity': info.get('debtToEquity', 0),
                    'revenue_growth': info.get('revenueGrowth', 0),
                    'earnings_growth': info.get('earningsGrowth', 0),
                    'free_cash_flow': info.get('freeCashflow', 0),
                    'operating_margins': info.get('operatingMargins', 0),
                    'sector': info.get('sector', ''),
                    'country': info.get('country', ''),
                    'avg_volume': info.get('averageVolume', 0),
                    'current_price': info.get('currentPrice', 0),
                    'fifty_two_week_high': info.get('fiftyTwoWeekHigh', 0),
                    'fifty_two_week_low': info.get('fiftyTwoWeekLow', 0),
                    'forward_pe': info.get('forwardPE', 0),
                    'trailing_pe': info.get('trailingPE', 0),
                    'dividend_yield': info.get('dividendYield', 0),
                    'beta': info.get('beta', 1.0),
                }

                # Get price history for momentum
                hist = ticker.history(period="1y")
                if not hist.empty:
                    self._price_cache[symbol] = hist

            except Exception as e:
                self.logger.logger.warning(
                    f"Failed to fetch data for {symbol}: {e}"
                )
                self._fundamentals_cache[symbol] = {}

    def _screen_us_longs(self) -> List[StockScore]:
        """Screen US universe for long candidates."""
        scores = []

        for symbol in self.us_universe:
            fundamentals = self._fundamentals_cache.get(symbol, {})
            prices = self._price_cache.get(symbol)

            if not fundamentals:
                continue

            # Check basic filters
            market_cap = fundamentals.get('market_cap', 0)
            avg_volume = fundamentals.get('avg_volume', 0)
            current_price = fundamentals.get('current_price', 0)

            if market_cap < self.min_market_cap:
                continue
            if avg_volume * current_price < self.min_volume:
                continue

            # Calculate factor scores
            quality_score = self._calculate_quality_score(fundamentals)
            momentum_score = self._calculate_momentum_score(prices, is_long=True)
            size_score = self._calculate_size_score(market_cap)

            # Composite score
            composite = (
                quality_score * self.US_LONG_WEIGHTS['quality'] +
                momentum_score * self.US_LONG_WEIGHTS['momentum'] +
                size_score * self.US_LONG_WEIGHTS['size']
            )

            # Build reasons
            reasons = []
            if quality_score > 0.7:
                reasons.append(f"High quality (ROE: {fundamentals.get('roe', 0):.1%})")
            if momentum_score > 0.7:
                reasons.append("Strong momentum")
            if fundamentals.get('free_cash_flow', 0) > 0:
                reasons.append("FCF positive")

            scores.append(StockScore(
                symbol=symbol,
                composite_score=composite,
                quality_score=quality_score,
                momentum_score=momentum_score,
                size_score=size_score,
                market_cap=market_cap,
                sector=fundamentals.get('sector', ''),
                country='US',
                reasons=reasons,
                data_quality='good' if prices is not None else 'partial'
            ))

        return scores

    def _screen_eu_shorts(self) -> List[StockScore]:
        """Screen EU universe for short candidates."""
        scores = []

        for symbol in self.eu_universe:
            fundamentals = self._fundamentals_cache.get(symbol, {})
            prices = self._price_cache.get(symbol)

            if not fundamentals:
                continue

            # Calculate factor scores
            zombie_score = self._calculate_zombie_score(fundamentals)
            momentum_score = self._calculate_momentum_score(prices, is_long=False)
            sector_score = self._calculate_sector_score(fundamentals.get('sector', ''))

            # Composite score (higher = better short candidate)
            composite = (
                zombie_score * self.EU_SHORT_WEIGHTS['zombie'] +
                momentum_score * self.EU_SHORT_WEIGHTS['momentum'] +
                sector_score * self.EU_SHORT_WEIGHTS['sector']
            )

            # Build reasons
            reasons = []
            if zombie_score > 0.7:
                reasons.append("Zombie characteristics (high debt, low growth)")
            if momentum_score > 0.7:
                reasons.append("Weak momentum")
            sector = fundamentals.get('sector', '')
            if sector in self.EU_SHORT_PREFERRED_SECTORS:
                reasons.append(f"Weak sector ({sector})")

            scores.append(StockScore(
                symbol=symbol,
                composite_score=composite,
                zombie_score=zombie_score,
                momentum_score=momentum_score,
                market_cap=fundamentals.get('market_cap', 0),
                sector=sector,
                country=fundamentals.get('country', 'EU'),
                reasons=reasons,
                data_quality='good' if prices is not None else 'partial'
            ))

        return scores

    def _calculate_quality_score(self, fundamentals: Dict) -> float:
        """
        Calculate quality factor score (0-1).

        Components:
        - ROE > 15% (25%)
        - Debt/Equity < 1.0 (25%)
        - Positive earnings growth (25%)
        - Positive FCF (25%)
        """
        score = 0.0

        # ROE component
        roe = fundamentals.get('roe', 0) or 0
        if roe > 0.25:
            score += 0.25
        elif roe > 0.15:
            score += 0.20
        elif roe > 0.10:
            score += 0.10

        # Debt/Equity component
        de_ratio = fundamentals.get('debt_to_equity', 100) or 100
        if de_ratio < 0.5:
            score += 0.25
        elif de_ratio < 1.0:
            score += 0.20
        elif de_ratio < 1.5:
            score += 0.10

        # Earnings growth component
        eg = fundamentals.get('earnings_growth', 0) or 0
        if eg > 0.20:
            score += 0.25
        elif eg > 0.10:
            score += 0.20
        elif eg > 0:
            score += 0.10

        # FCF component
        fcf = fundamentals.get('free_cash_flow', 0) or 0
        if fcf > 0:
            score += 0.25

        return min(score, 1.0)

    def _calculate_momentum_score(
        self,
        prices: Optional[pd.DataFrame],
        is_long: bool = True
    ) -> float:
        """
        Calculate momentum factor score (0-1).

        Uses 12-1 month momentum (skip last month to avoid reversal).
        For shorts, inverts the score.
        """
        if prices is None or len(prices) < 252:
            return 0.5  # Neutral if insufficient data

        try:
            # 12-month return excluding last month
            price_12m_ago = prices['Close'].iloc[0]
            price_1m_ago = prices['Close'].iloc[-21]  # ~1 month ago

            momentum_return = (price_1m_ago - price_12m_ago) / price_12m_ago

            # Normalize to 0-1 scale
            # Assume -30% to +50% range maps to 0-1
            normalized = (momentum_return + 0.30) / 0.80
            normalized = max(0, min(1, normalized))

            # Invert for shorts (weak momentum = high score)
            if not is_long:
                normalized = 1 - normalized

            return normalized

        except Exception:
            return 0.5

    def _calculate_size_score(self, market_cap: float) -> float:
        """
        Calculate size factor score (0-1).

        Prefers larger caps for stability.
        """
        if market_cap >= 500e9:  # Mega cap
            return 1.0
        elif market_cap >= 200e9:
            return 0.9
        elif market_cap >= 100e9:
            return 0.8
        elif market_cap >= 50e9:
            return 0.7
        elif market_cap >= 20e9:
            return 0.5
        else:
            return 0.3

    def _calculate_zombie_score(self, fundamentals: Dict) -> float:
        """
        Calculate zombie factor score (0-1).

        Higher score = more zombie-like characteristics:
        - High debt
        - Low/negative growth
        - Declining margins
        """
        score = 0.0

        # High debt component
        de_ratio = fundamentals.get('debt_to_equity', 0) or 0
        if de_ratio > 2.0:
            score += 0.30
        elif de_ratio > 1.5:
            score += 0.20
        elif de_ratio > 1.0:
            score += 0.10

        # Negative revenue growth
        rev_growth = fundamentals.get('revenue_growth', 0) or 0
        if rev_growth < -0.10:
            score += 0.30
        elif rev_growth < 0:
            score += 0.20
        elif rev_growth < 0.05:
            score += 0.10

        # Low/negative margins
        margins = fundamentals.get('operating_margins', 0) or 0
        if margins < 0:
            score += 0.25
        elif margins < 0.05:
            score += 0.15
        elif margins < 0.10:
            score += 0.10

        # Low ROE
        roe = fundamentals.get('roe', 0) or 0
        if roe < 0:
            score += 0.15
        elif roe < 0.05:
            score += 0.10

        return min(score, 1.0)

    def _calculate_sector_score(self, sector: str) -> float:
        """
        Calculate sector preference score for EU shorts.

        Higher score for sectors we prefer to short.
        """
        if sector in self.EU_SHORT_PREFERRED_SECTORS:
            return 1.0
        elif sector in self.EU_SHORT_AVOID_SECTORS:
            return 0.2
        else:
            return 0.5


def get_default_us_universe() -> List[str]:
    """Get default US stock universe for screening."""
    return [
        # Mega-cap Tech
        'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'TSLA',
        # Enterprise Tech
        'CRM', 'ADBE', 'ORCL', 'IBM', 'CSCO', 'INTC', 'AMD', 'AVGO',
        # Healthcare
        'UNH', 'JNJ', 'LLY', 'ABBV', 'MRK', 'PFE', 'TMO', 'ABT',
        # Financials
        'JPM', 'BAC', 'WFC', 'GS', 'MS', 'BLK', 'SCHW',
        # Consumer
        'WMT', 'PG', 'KO', 'PEP', 'COST', 'HD', 'MCD', 'NKE',
        # Industrial
        'CAT', 'DE', 'UNP', 'HON', 'GE', 'MMM', 'LMT', 'RTX',
        # Other Quality
        'V', 'MA', 'DIS', 'NFLX', 'PYPL', 'INTU', 'NOW', 'SNOW'
    ]


def get_default_eu_universe() -> List[str]:
    """Get default EU stock universe for screening (excludes Dutch stocks)."""
    return [
        # German stocks
        'VOW3.DE', 'BMW.DE', 'MBG.DE',  # Autos
        'BAS.DE', 'BAYN.DE',  # Chemicals/Pharma
        'SIE.DE', 'IFX.DE',  # Industrials
        'DBK.DE', 'CBK.DE',  # Banks
        'DTE.DE', 'DHL.DE',  # Telecoms/Logistics (DHL Group, formerly Deutsche Post)
        'RWE.DE', 'EOAN.DE',  # Utilities
        # French stocks
        'BNP.PA', 'GLE.PA', 'ACA.PA',  # Banks
        'SAN.PA',  # Pharma
        'AIR.PA',  # Aerospace
        'TTE.PA', 'ENGI.PA',  # Energy
        'VIV.PA', 'ORA.PA',  # Telecoms
        'SGO.PA',  # Materials
        # Italian stocks
        'UCG.MI', 'ISP.MI',  # Banks
        'ENI.MI', 'ENEL.MI',  # Energy/Utilities
        # Spanish stocks
        'SAN.MC', 'BBVA.MC',  # Banks
        'IBE.MC', 'REP.MC',  # Utilities/Energy
    ]


# Convenience function for strategy integration
def run_screening(
    top_n: int = 10,
    logger=None
) -> Tuple[List[str], List[str], Dict[str, Any]]:
    """
    Run stock screening and return selected symbols.

    Args:
        top_n: Number of stocks per side
        logger: Optional logger

    Returns:
        Tuple of (long_symbols, short_symbols, metadata)
    """
    screener = StockScreener(
        us_universe=get_default_us_universe(),
        eu_universe=get_default_eu_universe(),
        logger=logger
    )

    result = screener.screen_stocks(
        top_n_long=top_n,
        top_n_short=top_n
    )

    long_symbols = [s.symbol for s in result.long_candidates]
    short_symbols = [s.symbol for s in result.short_candidates]

    metadata = {
        'screening_date': result.screening_date.isoformat(),
        'methodology_version': result.methodology_version,
        'universe_size': result.universe_size,
        'long_scores': {s.symbol: s.composite_score for s in result.long_candidates},
        'short_scores': {s.symbol: s.composite_score for s in result.short_candidates},
        'long_reasons': {s.symbol: s.reasons for s in result.long_candidates},
        'short_reasons': {s.symbol: s.reasons for s in result.short_candidates}
    }

    return long_symbols, short_symbols, metadata
