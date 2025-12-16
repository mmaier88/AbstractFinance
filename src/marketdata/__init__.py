"""
Market Data Package - Live vs Research data separation.

Phase 2 Enhancement: Hard separation of live trading data (IBKR only)
from research/backtest data (Yahoo allowed).

Strategy Evolution v2.1: Added VSTOXX/V2X data feeds for Europe Vol Engine.

CRITICAL: Live trading must NEVER use Yahoo/yfinance fallback.
"""

from .live import (
    LiveMarketData,
    get_live_market_data,
    init_live_market_data,
    QuoteQualityConfig,
    # ROADMAP Phase B: Europe-First Regime
    EuropeRegimeData,
    get_europe_regime_data,
    init_europe_regime_data,
)
from .research import ResearchMarketData, get_research_market_data

__all__ = [
    # Live market data
    "LiveMarketData",
    "get_live_market_data",
    "init_live_market_data",
    "QuoteQualityConfig",
    # Research market data
    "ResearchMarketData",
    "get_research_market_data",
    # Europe regime data (V2X, VSTOXX, EURUSD)
    "EuropeRegimeData",
    "get_europe_regime_data",
    "init_europe_regime_data",
]
