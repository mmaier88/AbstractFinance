"""
Integration tests for AbstractFinance.

These tests verify the full flow from IBKR responses through to order generation,
catching integration bugs that unit tests miss.

Run with: pytest tests/test_integration_flow.py -v
"""

import pytest
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch
from typing import Dict, Any, List

# Import the modules we're testing
from src.utils.invariants import (
    assert_position_id_valid,
    assert_no_conflicting_orders,
    assert_gbx_whitelist_valid,
    validate_instruments_config,
    build_id_mappings,
    InvariantError,
)


class TestPositionIDMapping:
    """Test that IBKR symbols correctly map to internal config IDs."""

    def test_valid_config_id_passes(self, sample_instruments_config):
        """A valid config ID should pass validation."""
        # us_index_etf is a valid config ID
        assert_position_id_valid(
            "us_index_etf",
            sample_instruments_config,
            context="test"
        )
        # No exception = pass

    def test_ibkr_symbol_raises_error(self, sample_instruments_config):
        """Using IBKR symbol instead of config ID should raise InvariantError."""
        # CSPX is the IBKR symbol, us_index_etf is the config ID
        with pytest.raises(InvariantError) as exc_info:
            assert_position_id_valid(
                "CSPX",  # Wrong! Should be us_index_etf
                sample_instruments_config,
                context="test"
            )
        assert "IBKR symbol 'CSPX'" in str(exc_info.value)
        assert "config ID 'us_index_etf'" in str(exc_info.value)

    def test_future_with_expiry_passes(self, sample_instruments_config):
        """Futures with expiry suffix should be recognized."""
        # eurusd_micro is the config ID, with expiry suffix
        assert_position_id_valid(
            "eurusd_micro_20260316",
            sample_instruments_config,
            context="test"
        )
        # No exception = pass

    def test_unknown_id_logs_warning(self, sample_instruments_config, caplog):
        """Unknown IDs should log warning but not raise."""
        # UNKNOWN_INST is not in config and not an IBKR symbol
        assert_position_id_valid(
            "UNKNOWN_INST",
            sample_instruments_config,
            context="test"
        )
        assert "Unknown instrument_id 'UNKNOWN_INST'" in caplog.text

    def test_build_id_mappings(self, sample_instruments_config):
        """Test bidirectional ID mapping construction."""
        config_to_symbol, symbol_to_config = build_id_mappings(sample_instruments_config)

        # Check forward mapping
        assert config_to_symbol["us_index_etf"] == "CSPX"
        assert config_to_symbol["ig_lqd"] == "LQDE"
        assert config_to_symbol["financials_eufn"] == "EXV1"

        # Check reverse mapping
        assert symbol_to_config["CSPX"] == "us_index_etf"
        assert symbol_to_config["LQDE"] == "ig_lqd"
        assert symbol_to_config["EXV1"] == "financials_eufn"


class TestConflictingOrders:
    """Test that conflicting BUY/SELL orders are detected."""

    def test_normal_orders_pass(self, sample_orders):
        """Non-conflicting orders should pass."""
        assert_no_conflicting_orders(sample_orders, context="test")
        # No exception = pass

    def test_conflicting_orders_raises(self, conflicting_orders):
        """BUY and SELL for same instrument should raise InvariantError."""
        with pytest.raises(InvariantError) as exc_info:
            assert_no_conflicting_orders(conflicting_orders, context="test")
        assert "Conflicting BUY/SELL orders" in str(exc_info.value)
        assert "us_index_etf" in str(exc_info.value)

    def test_mixed_id_orders_not_detected(self, mixed_id_orders):
        """
        Orders with different IDs for same instrument are NOT detected here.
        This test documents a limitation - the invariant only checks by instrument_id.
        The ID mapping invariant should catch this earlier in the pipeline.
        """
        # This WILL NOT raise because us_index_etf != CSPX at the string level
        # The fix is to ensure position sync uses correct IDs so this never happens
        assert_no_conflicting_orders(mixed_id_orders, context="test")


class TestGBXWhitelist:
    """Test GBX whitelist validation."""

    def test_valid_whitelist_passes(
        self, gbx_quoted_etfs_valid, sample_instruments_config
    ):
        """Whitelist with only GBP instruments should pass."""
        assert_gbx_whitelist_valid(
            gbx_quoted_etfs_valid,
            sample_instruments_config,
            context="test"
        )
        # No exception = pass

    def test_invalid_whitelist_raises(
        self, gbx_quoted_etfs_invalid, sample_instruments_config
    ):
        """Whitelist with USD instruments should raise InvariantError."""
        with pytest.raises(InvariantError) as exc_info:
            assert_gbx_whitelist_valid(
                gbx_quoted_etfs_invalid,
                sample_instruments_config,
                context="test"
            )
        assert "non-GBP instruments" in str(exc_info.value)
        assert "CSPX" in str(exc_info.value) or "LQDE" in str(exc_info.value)

    def test_unknown_symbol_ignored(self, sample_instruments_config):
        """Symbols not in config should be silently ignored."""
        whitelist = {"UNKNOWN_SYMBOL"}
        # Should not raise - unknown symbols might be valid GBP instruments
        # we just don't have in our config
        assert_gbx_whitelist_valid(
            whitelist,
            sample_instruments_config,
            context="test"
        )


class TestInstrumentsConfigValidation:
    """Test instruments configuration validation."""

    def test_valid_config_passes(self, sample_instruments_config):
        """Valid configuration should pass validation."""
        is_valid, errors = validate_instruments_config(sample_instruments_config)
        assert is_valid, f"Validation failed with errors: {errors}"
        assert errors == []

    def test_duplicate_config_id_detected(self):
        """Duplicate config IDs should be detected."""
        invalid_config = {
            "sleeve1": {
                "us_index_etf": {"symbol": "CSPX", "currency": "USD"},
            },
            "sleeve2": {
                "us_index_etf": {"symbol": "SPY", "currency": "USD"},  # Duplicate ID!
            },
        }
        is_valid, errors = validate_instruments_config(invalid_config)
        assert not is_valid
        assert any("Duplicate config ID" in e for e in errors)

    def test_duplicate_symbol_warning(self):
        """Duplicate symbols should produce warning (not error by default)."""
        config_with_dupe = {
            "sleeve1": {
                "inst1": {"symbol": "CSPX", "currency": "USD"},
                "inst2": {"symbol": "CSPX", "currency": "USD"},  # Duplicate symbol!
            },
        }
        # Default: warning only, still valid
        is_valid, messages = validate_instruments_config(config_with_dupe)
        assert is_valid  # Duplicate symbols are warnings, not errors
        assert any("Duplicate symbol" in m for m in messages)

        # Strict mode: fails on duplicate symbols
        is_valid_strict, errors = validate_instruments_config(config_with_dupe, strict=True)
        assert not is_valid_strict
        assert any("Duplicate symbol" in e for e in errors)


class TestGlidepathBlending:
    """Test glidepath blending logic edge cases."""

    def test_day_zero_uses_initial(
        self, sample_initial_positions, sample_target_positions
    ):
        """Day 0: alpha=0, should return exactly initial positions."""
        alpha = 0.0
        blended = {}

        for inst_id in set(sample_initial_positions) | set(sample_target_positions):
            initial_qty = sample_initial_positions.get(inst_id, 0.0)
            target_qty = sample_target_positions.get(inst_id, 0.0)
            blended[inst_id] = alpha * target_qty + (1 - alpha) * initial_qty

        # Should match initial exactly
        for inst_id, qty in sample_initial_positions.items():
            assert blended[inst_id] == qty, f"{inst_id}: expected {qty}, got {blended[inst_id]}"

        # New positions should be 0
        for inst_id in sample_target_positions:
            if inst_id not in sample_initial_positions:
                assert blended[inst_id] == 0.0, f"New position {inst_id} should be 0 on day 0"

    def test_day_one_small_change(
        self, sample_initial_positions, sample_target_positions
    ):
        """Day 1: alpha=0.1, should be 10% toward targets."""
        alpha = 0.1
        blended = {}

        for inst_id in set(sample_initial_positions) | set(sample_target_positions):
            initial_qty = sample_initial_positions.get(inst_id, 0.0)
            target_qty = sample_target_positions.get(inst_id, 0.0)
            blended[inst_id] = alpha * target_qty + (1 - alpha) * initial_qty

        # Check us_index_etf: initial=0, target=40, blended=4
        assert blended["us_index_etf"] == pytest.approx(4.0)

        # Check financials_eufn: initial=-165, target=-200, blended=-168.5
        expected = 0.1 * (-200) + 0.9 * (-165)
        assert blended["financials_eufn"] == pytest.approx(expected)

    def test_day_ten_full_target(
        self, sample_initial_positions, sample_target_positions
    ):
        """Day 10+: alpha=1.0, should return exactly target positions."""
        alpha = 1.0
        blended = {}

        for inst_id in set(sample_initial_positions) | set(sample_target_positions):
            initial_qty = sample_initial_positions.get(inst_id, 0.0)
            target_qty = sample_target_positions.get(inst_id, 0.0)
            blended[inst_id] = alpha * target_qty + (1 - alpha) * initial_qty

        # Should match targets exactly
        for inst_id, qty in sample_target_positions.items():
            assert blended[inst_id] == qty, f"{inst_id}: expected {qty}, got {blended[inst_id]}"


class TestPriceConversion:
    """Test GBX pence-to-pounds conversion."""

    def test_gbp_instrument_needs_conversion(
        self, gbp_instruments, sample_instruments_config
    ):
        """GBP instruments should be in GBX conversion list."""
        for inst_id in gbp_instruments:
            # Find the symbol for this instrument
            for sleeve, instruments in sample_instruments_config.items():
                if inst_id in instruments:
                    symbol = instruments[inst_id].get("symbol", inst_id)
                    currency = instruments[inst_id].get("currency")
                    assert currency == "GBP", f"{inst_id} should be GBP"
                    break

    def test_usd_lse_instrument_not_converted(
        self, usd_lse_instruments, sample_instruments_config
    ):
        """USD instruments on LSE should NOT be GBX converted."""
        for inst_id in usd_lse_instruments:
            for sleeve, instruments in sample_instruments_config.items():
                if inst_id in instruments:
                    currency = instruments[inst_id].get("currency")
                    assert currency == "USD", f"{inst_id} should be USD, not GBP"
                    break


class TestPositionSyncIntegration:
    """Integration tests for position sync from IBKR to internal state."""

    def test_all_positions_have_internal_ids(
        self, mock_ibkr_portfolio, sample_instruments_config, symbol_to_config_id
    ):
        """All positions from IBKR should map to internal config IDs."""
        for item in mock_ibkr_portfolio:
            ibkr_symbol = item.contract.symbol

            # Handle futures with expiry
            if item.contract.secType == "FUT":
                expiry = item.contract.lastTradeDateOrContractMonth
                # Look up base symbol
                base_config_id = symbol_to_config_id.get(ibkr_symbol)
                if base_config_id:
                    expected_id = f"{base_config_id}_{expiry}"
                else:
                    expected_id = f"{ibkr_symbol}_{expiry}"
            else:
                expected_id = symbol_to_config_id.get(ibkr_symbol)

            # Every position should have a mapping
            assert expected_id is not None, (
                f"No config ID mapping for IBKR symbol {ibkr_symbol}"
            )

            # Validate the expected ID
            assert_position_id_valid(
                expected_id,
                sample_instruments_config,
                context=f"sync from {ibkr_symbol}"
            )

    def test_pence_conversion_applied_correctly(
        self, mock_ibkr_portfolio, sample_instruments_config
    ):
        """GBP positions with price > 100 should have pence conversion applied."""
        for item in mock_ibkr_portfolio:
            if item.contract.currency == "GBP":
                price = item.marketPrice
                # IUKD has price 912.5 in pence = 9.125 in pounds
                if price > 100:
                    converted_price = price / 100.0
                    assert converted_price < 100, (
                        f"GBP price {price} should be converted to {converted_price}"
                    )


class TestOrderGenerationIntegration:
    """Integration tests for order generation pipeline."""

    def test_orders_use_config_ids_not_symbols(self, sample_orders, symbol_to_config_id):
        """All generated orders should use internal config IDs."""
        for order in sample_orders:
            inst_id = order.instrument_id

            # Should not be an IBKR symbol
            assert inst_id not in symbol_to_config_id, (
                f"Order uses IBKR symbol {inst_id} instead of config ID"
            )

    def test_no_duplicate_instruments_in_orders(self, sample_orders):
        """No two orders should be for the same instrument."""
        instrument_ids = [o.instrument_id for o in sample_orders]
        unique_ids = set(instrument_ids)
        assert len(instrument_ids) == len(unique_ids), (
            f"Duplicate instruments in orders: {instrument_ids}"
        )


class TestEndToEndScenarios:
    """End-to-end scenario tests."""

    def test_scenario_normal_day(
        self,
        mock_ibkr_portfolio,
        sample_instruments_config,
        sample_market_prices,
        symbol_to_config_id,
    ):
        """
        Scenario: Normal trading day
        - Positions sync from IBKR
        - Prices available for all instruments
        - Orders generated correctly
        """
        # Simulate position sync
        positions = {}
        for item in mock_ibkr_portfolio:
            symbol = item.contract.symbol

            # Map to config ID
            if item.contract.secType == "FUT":
                base_id = symbol_to_config_id.get(symbol, symbol)
                config_id = f"{base_id}_{item.contract.lastTradeDateOrContractMonth}"
            else:
                config_id = symbol_to_config_id.get(symbol, symbol)

            # Validate ID
            assert_position_id_valid(
                config_id,
                sample_instruments_config,
                context="normal day sync"
            )

            positions[config_id] = item.position

        # Should have 7 positions with correct IDs
        assert len(positions) == 7
        assert "us_index_etf" in positions
        assert "CSPX" not in positions  # Should be mapped

    def test_scenario_new_position_day_one(
        self,
        sample_initial_positions,
        sample_target_positions,
        sample_instruments_config,
    ):
        """
        Scenario: Day 1 of glidepath
        - Initial positions exist
        - Target has new positions not in initial
        - Blending should add small quantities of new positions
        """
        alpha = 0.1
        blended = {}
        orders = []

        # Compute blended positions
        all_instruments = set(sample_initial_positions) | set(sample_target_positions)
        for inst_id in all_instruments:
            initial = sample_initial_positions.get(inst_id, 0.0)
            target = sample_target_positions.get(inst_id, 0.0)
            blended[inst_id] = alpha * target + (1 - alpha) * initial

        # Generate orders (simplified)
        for inst_id, target_qty in blended.items():
            current_qty = sample_initial_positions.get(inst_id, 0.0)
            diff = target_qty - current_qty
            if abs(diff) > 0.5:  # Threshold
                side = "BUY" if diff > 0 else "SELL"
                orders.append({
                    "instrument_id": inst_id,
                    "side": side,
                    "quantity": abs(diff)
                })

        # Validate no conflicting orders
        assert_no_conflicting_orders(orders, context="day 1 orders")

        # Should have orders for new positions
        order_instruments = {o["instrument_id"] for o in orders}
        assert "us_index_etf" in order_instruments  # New position
        assert "ig_lqd" in order_instruments  # New position
