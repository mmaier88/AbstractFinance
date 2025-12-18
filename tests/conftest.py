"""
Shared pytest fixtures for AbstractFinance tests.

These fixtures provide realistic test data and mocks for integration testing.
"""

import pytest
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Any, List
from dataclasses import dataclass
from unittest.mock import MagicMock, patch
import tempfile


# ============================================================================
# Sample Instruments Configuration
# ============================================================================

@pytest.fixture
def sample_instruments_config() -> Dict[str, Any]:
    """
    Sample instruments configuration matching production structure.
    Includes all edge cases:
    - Internal ID different from IBKR symbol
    - GBP vs USD currencies
    - Futures with expiry
    - Options
    """
    return {
        "core_index_rv": {
            "us_index_etf": {
                "symbol": "CSPX",
                "exchange": "LSE",
                "currency": "USD",
                "sec_type": "STK",
                "description": "S&P 500 ETF on LSE"
            },
            "eu_index_etf": {
                "symbol": "CS51",
                "exchange": "XETRA",
                "currency": "EUR",
                "sec_type": "STK",
                "description": "Stoxx 50 ETF"
            },
            "eurusd_micro": {
                "symbol": "M6E",
                "exchange": "CME",
                "currency": "USD",
                "sec_type": "FUT",
                "expiry": "20260316",
                "description": "Micro EUR/USD Future"
            },
        },
        "credit_carry": {
            "ig_lqd": {
                "symbol": "LQDE",
                "exchange": "LSE",
                "currency": "USD",
                "sec_type": "STK",
                "description": "Investment Grade Corporate Bond ETF"
            },
            "hy_hyg": {
                "symbol": "IHYU",
                "exchange": "LSE",
                "currency": "USD",
                "sec_type": "STK",
                "description": "High Yield Corporate Bond ETF"
            },
            "loans_bkln": {
                "symbol": "FLOT",
                "exchange": "LSE",
                "currency": "USD",
                "sec_type": "STK",
                "description": "Floating Rate Bond ETF"
            },
        },
        "sector_rv": {
            "financials_eufn": {
                "symbol": "EXV1",
                "exchange": "XETRA",
                "currency": "EUR",
                "sec_type": "STK",
                "description": "European Financials ETF"
            },
            "value_ewu": {
                "symbol": "IUKD",
                "exchange": "LSE",
                "currency": "GBP",  # GBP currency - needs pence conversion
                "sec_type": "STK",
                "description": "UK Dividend ETF"
            },
        },
        "europe_vol_convex": {
            "vstoxx_call": {
                "symbol": "FVS",
                "exchange": "EUREX",
                "currency": "EUR",
                "sec_type": "OPT",
                "description": "VSTOXX Call Option"
            },
        },
    }


@pytest.fixture
def symbol_to_config_id(sample_instruments_config) -> Dict[str, str]:
    """Mapping from IBKR symbols to internal config IDs."""
    mapping = {}
    for sleeve, instruments in sample_instruments_config.items():
        for config_id, spec in instruments.items():
            symbol = spec.get("symbol", config_id)
            mapping[symbol] = config_id
    return mapping


@pytest.fixture
def config_id_to_symbol(sample_instruments_config) -> Dict[str, str]:
    """Mapping from internal config IDs to IBKR symbols."""
    mapping = {}
    for sleeve, instruments in sample_instruments_config.items():
        for config_id, spec in instruments.items():
            symbol = spec.get("symbol", config_id)
            mapping[config_id] = symbol
    return mapping


# ============================================================================
# Mock IBKR Objects
# ============================================================================

@dataclass
class MockContract:
    """Mock IB Contract for testing."""
    symbol: str
    secType: str = "STK"
    exchange: str = "SMART"
    currency: str = "USD"
    primaryExchange: str = ""
    lastTradeDateOrContractMonth: str = ""
    multiplier: str = ""
    strike: float = 0.0
    right: str = ""


@dataclass
class MockPortfolioItem:
    """Mock IB PortfolioItem for testing."""
    contract: MockContract
    position: float
    marketPrice: float
    marketValue: float
    averageCost: float
    unrealizedPNL: float
    realizedPNL: float


@pytest.fixture
def mock_ibkr_portfolio() -> List[MockPortfolioItem]:
    """
    Mock IBKR portfolio matching the sample instruments.
    Uses IBKR symbols (not internal config IDs) to test mapping.
    """
    return [
        MockPortfolioItem(
            contract=MockContract(symbol="CSPX", currency="USD", exchange="LSEETF"),
            position=4.0,
            marketPrice=730.50,
            marketValue=2922.0,
            averageCost=725.0,
            unrealizedPNL=22.0,
            realizedPNL=0.0,
        ),
        MockPortfolioItem(
            contract=MockContract(symbol="EXV1", currency="EUR", exchange="IBIS"),
            position=-174.0,
            marketPrice=34.50,
            marketValue=-6003.0,
            averageCost=34.20,
            unrealizedPNL=-52.2,
            realizedPNL=0.0,
        ),
        MockPortfolioItem(
            contract=MockContract(symbol="IUKD", currency="GBP", exchange="LSEETF"),
            position=-224.0,
            marketPrice=912.5,  # In pence - needs conversion!
            marketValue=-2044.0,
            averageCost=895.0,
            unrealizedPNL=-39.2,
            realizedPNL=0.0,
        ),
        MockPortfolioItem(
            contract=MockContract(
                symbol="M6E",
                secType="FUT",
                currency="USD",
                exchange="CME",
                lastTradeDateOrContractMonth="20260316",
                multiplier="12500",
            ),
            position=-3.0,
            marketPrice=1.178,
            marketValue=-44175.0,
            averageCost=1.180,
            unrealizedPNL=75.0,
            realizedPNL=0.0,
        ),
        MockPortfolioItem(
            contract=MockContract(symbol="LQDE", currency="USD", exchange="LSEETF"),
            position=6.0,
            marketPrice=103.0,
            marketValue=618.0,
            averageCost=102.5,
            unrealizedPNL=3.0,
            realizedPNL=0.0,
        ),
        MockPortfolioItem(
            contract=MockContract(symbol="IHYU", currency="USD", exchange="LSEETF"),
            position=4.0,
            marketPrice=95.80,
            marketValue=383.2,
            averageCost=96.0,
            unrealizedPNL=-0.8,
            realizedPNL=0.0,
        ),
        MockPortfolioItem(
            contract=MockContract(symbol="FLOT", currency="USD", exchange="LSEETF"),
            position=62.0,
            marketPrice=5.03,
            marketValue=311.86,
            averageCost=5.05,
            unrealizedPNL=-1.24,
            realizedPNL=0.0,
        ),
    ]


# ============================================================================
# Order Test Fixtures
# ============================================================================

@dataclass
class MockOrderSpec:
    """Mock OrderSpec for testing."""
    instrument_id: str
    side: str
    quantity: float
    order_type: str = "LMT"
    limit_price: float = None


@pytest.fixture
def sample_orders() -> List[MockOrderSpec]:
    """Sample orders using internal config IDs."""
    return [
        MockOrderSpec(instrument_id="us_index_etf", side="BUY", quantity=4),
        MockOrderSpec(instrument_id="financials_eufn", side="SELL", quantity=10),
        MockOrderSpec(instrument_id="ig_lqd", side="BUY", quantity=6),
    ]


@pytest.fixture
def conflicting_orders() -> List[MockOrderSpec]:
    """Orders that conflict (same instrument, BUY and SELL) - should fail invariant."""
    return [
        MockOrderSpec(instrument_id="us_index_etf", side="BUY", quantity=4),
        MockOrderSpec(instrument_id="us_index_etf", side="SELL", quantity=2),  # Conflict!
    ]


@pytest.fixture
def mixed_id_orders() -> List[MockOrderSpec]:
    """
    Orders mixing internal IDs and IBKR symbols.
    This is a bug scenario that the invariant should catch.
    """
    return [
        MockOrderSpec(instrument_id="us_index_etf", side="BUY", quantity=4),  # Config ID
        MockOrderSpec(instrument_id="CSPX", side="SELL", quantity=4),  # IBKR symbol - same instrument!
    ]


# ============================================================================
# Glidepath Test Fixtures
# ============================================================================

@pytest.fixture
def sample_initial_positions() -> Dict[str, float]:
    """Initial positions for glidepath testing."""
    return {
        "financials_eufn": -165.0,
        "value_ewu": -224.0,
        "eurusd_micro_20260316": -3.0,
    }


@pytest.fixture
def sample_target_positions() -> Dict[str, float]:
    """Target positions for glidepath testing."""
    return {
        "us_index_etf": 40.0,
        "eu_index_etf": -10.0,
        "ig_lqd": 60.0,
        "hy_hyg": 40.0,
        "loans_bkln": 620.0,
        "financials_eufn": -200.0,
        "value_ewu": -200.0,
        "eurusd_micro_20260316": -5.0,
    }


# ============================================================================
# Market Data Test Fixtures
# ============================================================================

@pytest.fixture
def sample_market_prices() -> Dict[str, float]:
    """Sample market prices for testing."""
    return {
        "us_index_etf": 730.50,
        "eu_index_etf": 220.00,
        "ig_lqd": 103.00,
        "hy_hyg": 95.80,
        "loans_bkln": 5.03,
        "financials_eufn": 34.50,
        "value_ewu": 9.125,  # In pounds (after pence conversion)
        "eurusd_micro_20260316": 1.178,
    }


@pytest.fixture
def gbp_instruments() -> set:
    """Set of instruments with GBP currency."""
    return {"value_ewu"}


@pytest.fixture
def usd_lse_instruments() -> set:
    """Set of USD-denominated instruments on LSE (should NOT be GBX converted)."""
    return {"us_index_etf", "ig_lqd", "hy_hyg", "loans_bkln"}


# ============================================================================
# Temporary State Directory
# ============================================================================

@pytest.fixture
def temp_state_dir():
    """Create a temporary directory for test state files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def sample_portfolio_state_json() -> Dict[str, Any]:
    """Sample portfolio state JSON for testing."""
    return {
        "nav": 280000.0,
        "cash_by_ccy": {"EUR": 250000.0, "USD": 5000.0},
        "initial_capital": 238000.0,
        "broker_nlv": 280000.0,
        "reconciliation_status": "PASS",
        "positions": {
            "us_index_etf": {
                "quantity": 4.0,
                "avg_cost": 725.0,
                "market_price": 730.50,
                "currency": "USD",
            },
            "financials_eufn": {
                "quantity": -174.0,
                "avg_cost": 34.20,
                "market_price": 34.50,
                "currency": "EUR",
            },
        },
    }


# ============================================================================
# FX Rates Fixtures
# ============================================================================

@pytest.fixture
def sample_fx_rates() -> Dict[str, float]:
    """Sample FX rates for testing."""
    return {
        "EURUSD": 1.05,
        "GBPUSD": 1.27,
        "CHFUSD": 1.12,
        "JPYUSD": 0.0067,
    }


# ============================================================================
# Invariant Testing Helpers
# ============================================================================

@pytest.fixture
def gbx_quoted_etfs_valid() -> set:
    """Valid GBX whitelist - only GBP instruments."""
    return {"SMEA", "IUKD", "IEAC", "IHYG"}


@pytest.fixture
def gbx_quoted_etfs_invalid() -> set:
    """Invalid GBX whitelist - includes USD instruments (bug scenario)."""
    return {"SMEA", "IUKD", "IEAC", "IHYG", "CSPX", "LQDE"}  # CSPX and LQDE are USD!
