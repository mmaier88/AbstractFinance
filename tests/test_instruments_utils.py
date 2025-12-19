"""
Unit tests for src/utils/instruments.py

Tests centralized ID normalization and price conversion utilities.
"""

import pytest
from src.utils.instruments import (
    normalize_instrument_id,
    extract_expiry_from_id,
    extract_expiry_for_ibkr,
    PriceConverter,
    find_instrument_spec,
)


class TestNormalizeInstrumentId:
    """Tests for instrument ID normalization."""

    def test_futures_with_expiry_suffix(self):
        """Futures with 8-digit expiry suffix should be normalized."""
        assert normalize_instrument_id("eurusd_micro_20260316") == "eurusd_micro"
        assert normalize_instrument_id("M6E_20260316") == "M6E"
        assert normalize_instrument_id("6E_20251215") == "6E"

    def test_normal_ids_unchanged(self):
        """Normal instrument IDs without suffix should be unchanged."""
        assert normalize_instrument_id("us_index_etf") == "us_index_etf"
        assert normalize_instrument_id("CSPX") == "CSPX"
        assert normalize_instrument_id("financials_eufn") == "financials_eufn"

    def test_partial_matches_not_normalized(self):
        """Partial matches (not 8 digits) should not be normalized."""
        assert normalize_instrument_id("test_123") == "test_123"  # 3 digits
        assert normalize_instrument_id("test_12345") == "test_12345"  # 5 digits
        assert normalize_instrument_id("test_1234567") == "test_1234567"  # 7 digits


class TestExtractExpiry:
    """Tests for expiry extraction."""

    def test_extract_expiry_from_id(self):
        """Should extract full YYYYMMDD expiry."""
        assert extract_expiry_from_id("eurusd_micro_20260316") == "20260316"
        assert extract_expiry_from_id("M6E_20251215") == "20251215"

    def test_extract_expiry_from_id_no_expiry(self):
        """Should return None when no expiry present."""
        assert extract_expiry_from_id("us_index_etf") is None
        assert extract_expiry_from_id("CSPX") is None

    def test_extract_expiry_for_ibkr(self):
        """Should extract YYYYMM format for IBKR."""
        assert extract_expiry_for_ibkr("eurusd_micro_20260316") == "202603"
        assert extract_expiry_for_ibkr("M6E_20251215") == "202512"

    def test_extract_expiry_for_ibkr_no_expiry(self):
        """Should return None when no expiry present."""
        assert extract_expiry_for_ibkr("us_index_etf") is None


class TestPriceConverter:
    """Tests for price conversion (GBP/pence)."""

    def test_gbx_to_gbp_conversion(self):
        """Should convert pence to GBP for GBX symbols."""
        converter = PriceConverter()

        # IUKD: 912.5 pence -> 9.125 GBP
        assert converter.from_broker("IUKD", 912.5) == 9.125

        # SMEA: 1050 pence -> 10.50 GBP
        assert converter.from_broker("SMEA", 1050.0) == 10.50

    def test_gbp_to_pence_conversion(self):
        """Should convert GBP to pence for GBX symbols."""
        converter = PriceConverter()

        # IUKD: 9.125 GBP -> 912.5 pence
        assert converter.to_broker("IUKD", 9.125) == 912.5

        # SMEA: 10.50 GBP -> 1050 pence
        assert converter.to_broker("SMEA", 10.50) == 1050.0

    def test_non_gbx_symbols_unchanged(self):
        """Non-GBX symbols should not be converted."""
        converter = PriceConverter()

        # CSPX is USD, not GBP - no conversion
        assert converter.from_broker("CSPX", 500.0) == 500.0
        assert converter.to_broker("CSPX", 500.0) == 500.0

        # Random symbol
        assert converter.from_broker("XYZ", 100.0) == 100.0

    def test_roundtrip_conversion(self):
        """Roundtrip conversion should be identity."""
        converter = PriceConverter()

        original_gbp = 9.25
        pence = converter.to_broker("IUKD", original_gbp)
        back_to_gbp = converter.from_broker("IUKD", pence)

        assert back_to_gbp == original_gbp

    def test_none_handling(self):
        """Should handle None prices gracefully."""
        converter = PriceConverter()

        assert converter.from_broker("IUKD", None) is None
        assert converter.to_broker("IUKD", None) is None

    def test_is_gbx_quoted(self):
        """Should correctly identify GBX-quoted symbols."""
        converter = PriceConverter()

        assert converter.is_gbx_quoted("IUKD") is True
        assert converter.is_gbx_quoted("SMEA") is True
        assert converter.is_gbx_quoted("CSPX") is False
        assert converter.is_gbx_quoted("SPY") is False

    def test_auto_detect_from_config(self):
        """Should auto-detect GBX symbols from config."""
        config = {
            "core": {
                "test_etf": {
                    "symbol": "TEST",
                    "currency": "GBP",
                    "exchange": "LSE",
                    "sec_type": "STK",
                }
            }
        }
        converter = PriceConverter(config)

        # Auto-detected from config
        assert converter.is_gbx_quoted("TEST") is True


class TestFindInstrumentSpec:
    """Tests for instrument spec lookup."""

    @pytest.fixture
    def sample_config(self):
        return {
            "core_index_rv": {
                "us_index_etf": {
                    "symbol": "CSPX",
                    "exchange": "LSE",
                    "currency": "USD",
                },
                "eu_index_etf": {
                    "symbol": "EXS1",
                    "exchange": "XETRA",
                    "currency": "EUR",
                },
            },
            "fx": {
                "eurusd_micro": {
                    "symbol": "M6E",
                    "exchange": "CME",
                    "sec_type": "FUT",
                    "currency": "USD",
                },
            },
        }

    def test_exact_id_match(self, sample_config):
        """Should find spec by exact instrument ID."""
        spec = find_instrument_spec("us_index_etf", sample_config)
        assert spec is not None
        assert spec["symbol"] == "CSPX"

    def test_symbol_match(self, sample_config):
        """Should find spec by symbol."""
        spec = find_instrument_spec("CSPX", sample_config)
        assert spec is not None
        assert spec["exchange"] == "LSE"

    def test_futures_with_expiry(self, sample_config):
        """Should find futures spec even with expiry suffix."""
        spec = find_instrument_spec("eurusd_micro_20260316", sample_config)
        assert spec is not None
        assert spec["symbol"] == "M6E"

    def test_not_found(self, sample_config):
        """Should return None for unknown instruments."""
        spec = find_instrument_spec("unknown_instrument", sample_config)
        assert spec is None


class TestPriceValidation:
    """Tests for order price validation."""

    def test_valid_price(self):
        """Should validate price within tolerance."""
        converter = PriceConverter()

        # 1% difference is OK with 5% tolerance
        assert converter.validate_order_price("CSPX", 100.0, 99.0, tolerance_pct=5.0) is True

    def test_invalid_price(self):
        """Should reject price outside tolerance."""
        converter = PriceConverter()

        # 10% difference exceeds 5% tolerance
        assert converter.validate_order_price("CSPX", 110.0, 100.0, tolerance_pct=5.0) is False

    def test_zero_market_price(self):
        """Should accept any order when market price is zero."""
        converter = PriceConverter()

        # Can't validate without market price
        assert converter.validate_order_price("CSPX", 100.0, 0.0, tolerance_pct=5.0) is True
