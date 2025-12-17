"""
Tests for Execution Engine Reliability components.

Tests:
- ReferencePriceResolver tiered fallback
- PriceCache persistence and TTL
- LimitOrderGenerator confidence scoring
- OptionContractFactory resolution
"""

import pytest
import tempfile
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch

# Import modules under test
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from pricing.cache import PriceCache, CachedPrice
from pricing.reference_price_resolver import (
    ReferencePriceResolver,
    PriceResult,
    PriceTier,
    PriceSource,
)
from orders.limit_generator import (
    LimitOrderGenerator,
    LimitOrderSpec,
    OrderSide,
    PriceAdjustment,
    create_limit_from_price,
)
from contracts.option_factory import (
    OptionContractFactory,
    OptionContractSpec,
    OptionSelection,
)


# =============================================================================
# PriceCache Tests
# =============================================================================

class TestPriceCache:
    """Test price cache functionality."""

    def test_cache_init_creates_empty_cache(self):
        """Test cache initializes empty when no file exists."""
        with tempfile.NamedTemporaryFile(suffix='.json', delete=True) as f:
            cache = PriceCache(cache_file=f.name)
            assert len(cache.get_all()) == 0

    def test_cache_set_and_get(self):
        """Test setting and getting cached prices."""
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            cache = PriceCache(cache_file=f.name)

            # Create a mock price result
            price_result = Mock()
            price_result.price = 100.50
            price_result.symbol = "SPY"
            price_result.source = "ibkr_realtime"
            price_result.tier = "A"
            price_result.bid = 100.45
            price_result.ask = 100.55

            cache.set("spy_etf", price_result)

            # Retrieve
            cached = cache.get("spy_etf")
            assert cached is not None
            assert cached.price == 100.50
            assert cached.symbol == "SPY"

    def test_cache_ttl_expiration(self):
        """Test that cached prices expire after TTL."""
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            # Short TTL for testing
            cache = PriceCache(cache_file=f.name, default_ttl_seconds=1)

            price_result = {"price": 50.0, "symbol": "TEST"}
            cache.set("test_instrument", price_result)

            # Should be available immediately
            assert cache.get("test_instrument") is not None

            # After TTL expires, should return None
            import time
            time.sleep(1.5)
            assert cache.get("test_instrument") is None

    def test_cache_persistence(self):
        """Test that cache persists to disk."""
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            cache_file = f.name

        # Write to cache and flush
        cache1 = PriceCache(cache_file=cache_file)
        cache1.set("persist_test", {"price": 123.45, "symbol": "TEST"})
        cache1.flush()

        # Load in new cache instance
        cache2 = PriceCache(cache_file=cache_file)
        cached = cache2.get("persist_test")
        assert cached is not None
        assert cached.price == 123.45

    def test_cache_metrics(self):
        """Test cache metrics tracking."""
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            cache = PriceCache(cache_file=f.name)

            # Cause some hits and misses
            cache.set("hit_test", {"price": 100.0, "symbol": "HIT"})
            cache.get("hit_test")  # Hit
            cache.get("miss_test")  # Miss

            metrics = cache.get_metrics()
            assert metrics["hits"] >= 1
            assert metrics["misses"] >= 1
            assert "hit_rate_pct" in metrics

    def test_cache_cleanup_expired(self):
        """Test cleanup of expired entries."""
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            cache = PriceCache(cache_file=f.name, default_ttl_seconds=1)

            # Add entries
            cache.set("entry1", {"price": 100.0, "symbol": "E1"})
            cache.set("entry2", {"price": 200.0, "symbol": "E2"})

            # Wait for expiration
            import time
            time.sleep(1.5)

            # Cleanup
            removed = cache.cleanup_expired()
            assert removed >= 2


# =============================================================================
# ReferencePriceResolver Tests
# =============================================================================

class TestReferencePriceResolver:
    """Test reference price resolver tiered fallback."""

    def test_guardrail_prices_exist(self):
        """Test that default guardrail prices are defined."""
        guardrails = ReferencePriceResolver.DEFAULT_GUARDRAILS
        assert "vix_call" in guardrails
        assert "vstoxx_call" in guardrails
        assert guardrails["vix_call"] > 0

    def test_resolver_init(self):
        """Test resolver initialization."""
        resolver = ReferencePriceResolver(
            ib_client=None,
            price_cache=None,
            enable_delayed_data=True,
        )
        assert resolver is not None
        assert resolver.enable_delayed_data is True

    def test_resolver_guardrail_fallback(self):
        """Test that resolver falls back to guardrail for option hedges."""
        resolver = ReferencePriceResolver(
            ib_client=None,
            price_cache=None,
        )

        # vix_call should have a guardrail
        result = resolver.get_reference_price("vix_call")
        assert result is not None
        assert result.tier == PriceTier.GUARDRAIL
        assert result.price == ReferencePriceResolver.DEFAULT_GUARDRAILS["vix_call"]

    def test_resolver_metrics(self):
        """Test resolver metrics collection."""
        resolver = ReferencePriceResolver(ib_client=None, price_cache=None)

        # Make some requests
        resolver.get_reference_price("vix_call")
        resolver.get_reference_price("unknown_instrument")

        metrics = resolver.get_metrics()
        assert "total_requests" in metrics
        assert metrics["total_requests"] >= 2
        assert "tier_e_guardrail" in metrics  # vix_call should use guardrail

    def test_price_result_confidence(self):
        """Test PriceResult confidence scoring."""
        # Realtime tier should have highest confidence
        result_a = PriceResult(
            price=100.0,
            tier=PriceTier.REALTIME,
            source=PriceSource.IBKR_REALTIME,
            confidence_score=0.95,
            symbol="TEST",
            instrument_id="test",
            timestamp=datetime.now(),
        )
        assert result_a.confidence_score == 0.95

        # Guardrail tier should have lower confidence
        result_e = PriceResult(
            price=5.0,
            tier=PriceTier.GUARDRAIL,
            source=PriceSource.CONFIG_DEFAULT,
            confidence_score=0.25,
            symbol="TEST",
            instrument_id="test",
            timestamp=datetime.now(),
        )
        assert result_e.confidence_score == 0.25


# =============================================================================
# LimitOrderGenerator Tests
# =============================================================================

class TestLimitOrderGenerator:
    """Test limit order generation with confidence scoring."""

    def test_generator_init(self):
        """Test generator initialization."""
        generator = LimitOrderGenerator()
        assert generator is not None
        assert generator.max_spread_bps == 200

    def test_generate_buy_order(self):
        """Test generating a buy limit order."""
        generator = LimitOrderGenerator()

        price_result = {
            "price": 100.0,
            "confidence": 0.9,
            "tier": "a",
            "source": "ibkr_realtime",
        }

        order = generator.generate(
            instrument_id="spy_etf",
            side=OrderSide.BUY,
            quantity=10,
            price_result=price_result,
        )

        assert order is not None
        assert order.side == OrderSide.BUY
        assert order.quantity == 10
        # Buy limit should be above reference (allowing room)
        assert order.limit_price >= order.reference_price

    def test_generate_sell_order(self):
        """Test generating a sell limit order."""
        generator = LimitOrderGenerator()

        price_result = {
            "price": 100.0,
            "confidence": 0.9,
            "tier": "a",
            "source": "ibkr_realtime",
        }

        order = generator.generate(
            instrument_id="spy_etf",
            side=OrderSide.SELL,
            quantity=5,
            price_result=price_result,
        )

        assert order is not None
        assert order.side == OrderSide.SELL
        # Sell limit should be below reference
        assert order.limit_price <= order.reference_price

    def test_spread_based_on_confidence(self):
        """Test that lower confidence = wider spreads."""
        generator = LimitOrderGenerator()

        # High confidence price
        high_conf = {"price": 100.0, "confidence": 0.95, "tier": "a", "source": "rt"}
        order_high = generator.generate("test", OrderSide.BUY, 1, high_conf)

        # Low confidence price
        low_conf = {"price": 100.0, "confidence": 0.5, "tier": "e", "source": "gr"}
        order_low = generator.generate("test", OrderSide.BUY, 1, low_conf)

        # Low confidence should have wider spread
        assert order_low.spread_bps > order_high.spread_bps

    def test_instrument_specific_spreads(self):
        """Test instrument-specific spread adjustments."""
        generator = LimitOrderGenerator()

        price_result = {"price": 5.0, "confidence": 0.7, "tier": "c", "source": "cache"}

        # Options should have wider spreads
        order_option = generator.generate("vix_call", OrderSide.BUY, 1, price_result)
        order_etf = generator.generate("us_index_etf", OrderSide.BUY, 1, price_result)

        assert order_option.spread_bps > order_etf.spread_bps

    def test_aggressive_adjustment(self):
        """Test aggressive pricing adjustment."""
        generator = LimitOrderGenerator()

        price_result = {
            "price": 100.0,
            "confidence": 0.9,
            "tier": "a",
            "source": "rt",
            "bid": 99.95,
            "ask": 100.05,
        }

        order = generator.generate(
            "test",
            OrderSide.BUY,
            1,
            price_result,
            adjustment=PriceAdjustment.AGGRESSIVE,
        )

        # Aggressive buy should use ask price
        assert order.limit_price == 100.05

    def test_convenience_function(self):
        """Test create_limit_from_price convenience function."""
        order = create_limit_from_price(
            instrument_id="test",
            side="BUY",
            quantity=100,
            price=50.0,
            confidence=0.8,
            tier="b",
        )

        assert order is not None
        assert order.quantity == 100
        assert order.reference_price == 50.0

    def test_generator_metrics(self):
        """Test generator metrics tracking."""
        generator = LimitOrderGenerator()

        price_result = {"price": 100.0, "confidence": 0.9, "tier": "a", "source": "rt"}
        generator.generate("test1", OrderSide.BUY, 1, price_result)
        generator.generate("test2", OrderSide.SELL, 1, price_result)

        metrics = generator.get_metrics()
        assert metrics["orders_generated"] == 2
        assert "avg_spread_bps" in metrics


# =============================================================================
# OptionContractFactory Tests
# =============================================================================

class TestOptionContractFactory:
    """Test option contract factory resolution."""

    def test_option_specs_defined(self):
        """Test that option specs are defined for hedge instruments."""
        specs = OptionContractFactory.OPTION_SPECS
        assert "vix_call" in specs
        assert "vstoxx_call" in specs
        assert "sx5e_put" in specs
        assert "eu_bank_put" in specs
        assert "hyg_put" in specs

    def test_option_spec_properties(self):
        """Test option spec has required properties."""
        spec = OptionContractFactory.OPTION_SPECS["vix_call"]
        assert spec.underlying == "VIX"
        assert spec.option_type == "CALL"
        assert spec.preferred_dte > 0
        assert spec.strike_offset_pct > 0

    def test_factory_init(self):
        """Test factory initialization."""
        factory = OptionContractFactory(ib_client=None)
        assert factory is not None

    def test_factory_fallback_selection(self):
        """Test factory creates fallback when no IBKR connection."""
        factory = OptionContractFactory(ib_client=None)

        selection = factory.get_contract("vix_call")
        assert selection is not None
        # Fallback selections have no con_id
        assert selection.con_id is None
        assert selection.underlying == "VIX"

    def test_fallback_expiration_calculation(self):
        """Test fallback calculates reasonable expiration."""
        factory = OptionContractFactory(ib_client=None)

        selection = factory.get_contract("vix_call")

        # Should be about 30 days out (VIX preferred_dte is 30)
        assert selection.dte >= 25
        assert selection.dte <= 35

    def test_fallback_strike_calculation(self):
        """Test fallback calculates reasonable strike."""
        factory = OptionContractFactory(ib_client=None)

        selection = factory.get_contract("vix_call")

        # VIX call should have strike in reasonable range
        assert selection.strike > 0
        assert selection.strike < 100  # VIX doesn't usually go above 100


# =============================================================================
# Integration Tests
# =============================================================================

class TestPricingIntegration:
    """Integration tests for pricing components."""

    def test_resolver_to_generator_flow(self):
        """Test flow from resolver to limit generator."""
        # Setup - use vix_call which has a built-in guardrail
        resolver = ReferencePriceResolver(
            ib_client=None,
            price_cache=None,
        )
        generator = LimitOrderGenerator()

        # Get price from resolver (vix_call has guardrail at 15.0)
        price_result = resolver.get_reference_price("vix_call")
        assert price_result is not None
        assert price_result.is_valid

        # Generate limit order
        order = generator.generate(
            instrument_id="vix_call",
            side=OrderSide.BUY,
            quantity=10,
            price_result=price_result,
        )

        assert order is not None
        assert order.reference_price == 15.0
        assert order.price_tier == "guardrail"  # Guardrail tier

    def test_option_factory_with_guardrail_pricing(self):
        """Test option factory with guardrail pricing for validation."""
        factory = OptionContractFactory(ib_client=None)

        # Get contract
        selection = factory.get_contract("vstoxx_call")
        assert selection is not None

        # Should have reasonable values
        assert selection.strike > 0
        assert selection.dte > 0


# =============================================================================
# Edge Cases and Error Handling
# =============================================================================

class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_cache_invalid_price(self):
        """Test cache rejects invalid prices."""
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            cache = PriceCache(cache_file=f.name)

            # Negative price should not be cached
            cache.set("bad_price", {"price": -10.0, "symbol": "BAD"})
            assert cache.get("bad_price") is None

            # Zero price should not be cached
            cache.set("zero_price", {"price": 0, "symbol": "ZERO"})
            assert cache.get("zero_price") is None

    def test_generator_handles_none_price(self):
        """Test generator handles None price result gracefully."""
        generator = LimitOrderGenerator()

        order = generator.generate("test", OrderSide.BUY, 1, None)
        assert order is None

    def test_generator_handles_invalid_price(self):
        """Test generator handles invalid price data."""
        generator = LimitOrderGenerator()

        # Invalid price value
        order = generator.generate("test", OrderSide.BUY, 1, {"price": -5})
        assert order is None

    def test_resolver_unknown_instrument(self):
        """Test resolver handles unknown instruments."""
        resolver = ReferencePriceResolver(ib_client=None, price_cache=None)

        result = resolver.get_reference_price("completely_unknown_xyz")
        # Unknown instruments return failed result (not None)
        assert result is not None
        assert result.tier == PriceTier.FAILED
        assert not result.is_valid

    def test_factory_unknown_abstract(self):
        """Test factory handles unknown abstract instruments."""
        factory = OptionContractFactory(ib_client=None)

        selection = factory.get_contract("unknown_option_type")
        assert selection is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
