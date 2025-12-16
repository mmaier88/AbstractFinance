"""
Market Data Package - Live vs Research data separation.

Phase 2 Enhancement: Hard separation of live trading data (IBKR only)
from research/backtest data (Yahoo allowed).

CRITICAL: Live trading must NEVER use Yahoo/yfinance fallback.
"""

from .live import LiveMarketData, get_live_market_data
from .research import ResearchMarketData, get_research_market_data

__all__ = [
    "LiveMarketData",
    "get_live_market_data",
    "ResearchMarketData",
    "get_research_market_data",
]
