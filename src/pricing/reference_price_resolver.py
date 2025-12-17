"""
Reference Price Resolver for AbstractFinance Execution Stack.

Provides tiered reference pricing with graceful fallback:
- Tier A: IBKR real-time market data
- Tier B: IBKR delayed/frozen market data
- Tier C: Portfolio mark price (from updatePortfolio)
- Tier D: Cached last known price
- Tier E: Config guardrail fallback (emergency)

Never rejects for missing price data - always provides best available estimate.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class PriceTier(Enum):
    """Pricing tier indicating data quality and freshness."""
    REALTIME = "realtime"        # Tier A: Live market data
    DELAYED = "delayed"          # Tier B: Delayed/frozen data (15-20 min)
    PORTFOLIO = "portfolio"      # Tier C: Portfolio mark price
    CACHED = "cached"            # Tier D: Cached last known price
    GUARDRAIL = "guardrail"      # Tier E: Config fallback
    FAILED = "failed"            # All tiers failed


class PriceSource(Enum):
    """Source of the price data."""
    IBKR_REALTIME = "ibkr_realtime"
    IBKR_DELAYED = "ibkr_delayed"
    IBKR_FROZEN = "ibkr_frozen"
    IBKR_PORTFOLIO = "ibkr_portfolio"
    YAHOO_FINANCE = "yahoo_finance"
    CACHE = "cache"
    CONFIG_DEFAULT = "config_default"
    NONE = "none"


@dataclass
class PriceResult:
    """Result of reference price resolution."""
    price: Optional[float]
    tier: PriceTier
    source: PriceSource
    symbol: str
    instrument_id: str
    timestamp: datetime = field(default_factory=datetime.now)
    age_seconds: float = 0.0
    confidence_score: float = 1.0  # 0.0 to 1.0
    bid: Optional[float] = None
    ask: Optional[float] = None
    spread_bps: Optional[float] = None
    error_message: Optional[str] = None

    @property
    def is_valid(self) -> bool:
        """Check if we have a usable price."""
        return self.price is not None and self.price > 0

    @property
    def is_stale(self) -> bool:
        """Check if price is older than 5 minutes."""
        return self.age_seconds > 300

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging/metrics."""
        return {
            "price": self.price,
            "tier": self.tier.value,
            "source": self.source.value,
            "symbol": self.symbol,
            "instrument_id": self.instrument_id,
            "timestamp": self.timestamp.isoformat(),
            "age_seconds": round(self.age_seconds, 2),
            "confidence_score": round(self.confidence_score, 3),
            "bid": self.bid,
            "ask": self.ask,
            "spread_bps": self.spread_bps,
            "is_valid": self.is_valid,
            "is_stale": self.is_stale,
            "error_message": self.error_message,
        }


@dataclass
class ResolverMetrics:
    """Metrics for price resolution performance."""
    total_requests: int = 0
    tier_a_hits: int = 0  # Realtime
    tier_b_hits: int = 0  # Delayed
    tier_c_hits: int = 0  # Portfolio
    tier_d_hits: int = 0  # Cache
    tier_e_hits: int = 0  # Guardrail
    failures: int = 0
    total_latency_ms: float = 0.0

    @property
    def avg_latency_ms(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return self.total_latency_ms / self.total_requests

    def record_hit(self, tier: PriceTier, latency_ms: float) -> None:
        """Record a price resolution hit."""
        self.total_requests += 1
        self.total_latency_ms += latency_ms

        if tier == PriceTier.REALTIME:
            self.tier_a_hits += 1
        elif tier == PriceTier.DELAYED:
            self.tier_b_hits += 1
        elif tier == PriceTier.PORTFOLIO:
            self.tier_c_hits += 1
        elif tier == PriceTier.CACHED:
            self.tier_d_hits += 1
        elif tier == PriceTier.GUARDRAIL:
            self.tier_e_hits += 1
        elif tier == PriceTier.FAILED:
            self.failures += 1

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging."""
        return {
            "total_requests": self.total_requests,
            "tier_a_realtime": self.tier_a_hits,
            "tier_b_delayed": self.tier_b_hits,
            "tier_c_portfolio": self.tier_c_hits,
            "tier_d_cache": self.tier_d_hits,
            "tier_e_guardrail": self.tier_e_hits,
            "failures": self.failures,
            "avg_latency_ms": round(self.avg_latency_ms, 2),
            "success_rate_pct": round(
                (self.total_requests - self.failures) / max(self.total_requests, 1) * 100, 2
            ),
        }


class ReferencePriceResolver:
    """
    Tiered reference price resolver for execution stack.

    Provides best-effort pricing with graceful degradation:
    - Tier A: Real-time IBKR market data
    - Tier B: Delayed IBKR data (reqMarketDataType=3)
    - Tier C: Portfolio mark prices
    - Tier D: Persistent cache
    - Tier E: Config guardrails

    Never fails outright - always returns best available estimate.
    """

    # Confidence scores by tier (used for limit order generation)
    CONFIDENCE_SCORES = {
        PriceTier.REALTIME: 1.0,
        PriceTier.DELAYED: 0.85,
        PriceTier.PORTFOLIO: 0.75,
        PriceTier.CACHED: 0.5,
        PriceTier.GUARDRAIL: 0.25,
        PriceTier.FAILED: 0.0,
    }

    # Default guardrail prices for instruments without other data
    DEFAULT_GUARDRAILS = {
        # Indices
        "VIX": 18.0,
        "V2X": 20.0,
        "SX5E": 4800.0,
        "ESTX50": 4800.0,
        "SX7E": 100.0,
        "SPX": 5800.0,

        # Option hedge placeholders
        "vix_call": 15.0,
        "vstoxx_call": 18.0,
        "sx5e_put": 4800.0,
        "eu_bank_put": 100.0,
        "hyg_put": 75.0,

        # Futures
        "FVS": 20.0,  # VSTOXX futures
        "VX": 18.0,   # VIX futures
        "ES": 5800.0, # E-mini S&P 500
        "MES": 5800.0,
        "M6E": 1.10,  # Micro EUR/USD
        "6E": 1.10,   # EUR/USD futures
    }

    def __init__(
        self,
        ib_client: Optional[Any] = None,
        instruments_config: Optional[Dict] = None,
        price_cache: Optional[Any] = None,
        enable_delayed_data: bool = True,
        cache_ttl_seconds: int = 3600,
    ):
        """
        Initialize the reference price resolver.

        Args:
            ib_client: IBClient instance for IBKR data
            instruments_config: Instrument configuration dict
            price_cache: PriceCache instance for persistent caching
            enable_delayed_data: Whether to try delayed data if realtime fails
            cache_ttl_seconds: How long to trust cached prices
        """
        self.ib_client = ib_client
        self.instruments_config = instruments_config or {}
        self.price_cache = price_cache
        self.enable_delayed_data = enable_delayed_data
        self.cache_ttl_seconds = cache_ttl_seconds

        # Metrics
        self.metrics = ResolverMetrics()

        # Track market data type setting
        self._market_data_type = 1  # 1=realtime, 3=delayed, 4=frozen

    def get_reference_price(
        self,
        instrument_id: str,
        symbol: Optional[str] = None,
        con_id: Optional[int] = None,
    ) -> PriceResult:
        """
        Get reference price with tiered fallback.

        Args:
            instrument_id: Internal instrument identifier
            symbol: IBKR symbol (optional, resolved from config)
            con_id: IBKR contract ID (optional)

        Returns:
            PriceResult with best available price
        """
        start_time = time.time()

        # Resolve symbol from config if not provided
        if symbol is None:
            symbol = self._resolve_symbol(instrument_id)

        result = PriceResult(
            price=None,
            tier=PriceTier.FAILED,
            source=PriceSource.NONE,
            symbol=symbol or instrument_id,
            instrument_id=instrument_id,
        )

        # Try each tier in order
        tiers = [
            (PriceTier.REALTIME, self._try_realtime),
            (PriceTier.DELAYED, self._try_delayed),
            (PriceTier.PORTFOLIO, self._try_portfolio),
            (PriceTier.CACHED, self._try_cache),
            (PriceTier.GUARDRAIL, self._try_guardrail),
        ]

        for tier, fetch_func in tiers:
            try:
                tier_result = fetch_func(instrument_id, symbol, con_id)
                if tier_result and tier_result.is_valid:
                    result = tier_result
                    result.tier = tier
                    result.confidence_score = self.CONFIDENCE_SCORES[tier]
                    break
            except Exception as e:
                logger.debug(f"Tier {tier.value} failed for {instrument_id}: {e}")
                continue

        # Calculate latency and record metrics
        latency_ms = (time.time() - start_time) * 1000
        self.metrics.record_hit(result.tier, latency_ms)

        # Log the resolution
        self._log_resolution(result)

        # Update cache if we got a valid price
        if result.is_valid and self.price_cache:
            self.price_cache.set(instrument_id, result)

        return result

    def get_reference_prices_batch(
        self,
        instrument_ids: List[str],
    ) -> Dict[str, PriceResult]:
        """
        Get reference prices for multiple instruments.

        Optimizes by batching IBKR requests where possible.

        Args:
            instrument_ids: List of instrument identifiers

        Returns:
            Dict mapping instrument_id to PriceResult
        """
        results = {}

        # TODO: Implement batch optimization
        # For now, iterate sequentially
        for inst_id in instrument_ids:
            results[inst_id] = self.get_reference_price(inst_id)

        return results

    def _resolve_symbol(self, instrument_id: str) -> Optional[str]:
        """Resolve IBKR symbol from instrument config."""
        # Check all categories in instruments config
        for category, instruments in self.instruments_config.items():
            if isinstance(instruments, dict):
                if instrument_id in instruments:
                    spec = instruments[instrument_id]
                    if isinstance(spec, dict) and "symbol" in spec:
                        return spec["symbol"]

        # If not found, use instrument_id as symbol
        return instrument_id

    def _try_realtime(
        self,
        instrument_id: str,
        symbol: str,
        con_id: Optional[int],
    ) -> Optional[PriceResult]:
        """Tier A: Try real-time IBKR market data."""
        if not self.ib_client or not self.ib_client.is_connected():
            return None

        try:
            # Ensure we're requesting realtime data
            self._set_market_data_type(1)

            # Build contract
            contract = self.ib_client.build_contract(instrument_id, self.instruments_config)
            if not contract:
                return None

            # Request market data
            ticker = self.ib_client.ib.reqMktData(contract, '', False, False)
            self.ib_client.ib.sleep(1)

            # Extract prices
            last = ticker.last if ticker.last and ticker.last > 0 else None
            bid = ticker.bid if ticker.bid and ticker.bid > 0 else None
            ask = ticker.ask if ticker.ask and ticker.ask > 0 else None
            close = ticker.close if ticker.close and ticker.close > 0 else None

            self.ib_client.ib.cancelMktData(contract)

            price = last or close
            if not price:
                return None

            # Calculate spread
            spread_bps = None
            if bid and ask and ask > 0:
                mid = (bid + ask) / 2
                spread_bps = ((ask - bid) / mid) * 10000

            return PriceResult(
                price=price,
                tier=PriceTier.REALTIME,
                source=PriceSource.IBKR_REALTIME,
                symbol=symbol,
                instrument_id=instrument_id,
                bid=bid,
                ask=ask,
                spread_bps=spread_bps,
                age_seconds=0,
            )

        except Exception as e:
            logger.debug(f"Realtime fetch failed for {instrument_id}: {e}")
            return None

    def _try_delayed(
        self,
        instrument_id: str,
        symbol: str,
        con_id: Optional[int],
    ) -> Optional[PriceResult]:
        """Tier B: Try delayed/frozen IBKR market data."""
        if not self.enable_delayed_data:
            return None

        if not self.ib_client or not self.ib_client.is_connected():
            return None

        try:
            # Request delayed data
            self._set_market_data_type(3)  # 3 = delayed

            contract = self.ib_client.build_contract(instrument_id, self.instruments_config)
            if not contract:
                return None

            ticker = self.ib_client.ib.reqMktData(contract, '', False, False)
            self.ib_client.ib.sleep(1.5)  # Slightly longer wait for delayed

            last = ticker.last if ticker.last and ticker.last > 0 else None
            close = ticker.close if ticker.close and ticker.close > 0 else None

            self.ib_client.ib.cancelMktData(contract)

            # Reset to realtime for next request
            self._set_market_data_type(1)

            price = last or close
            if not price:
                return None

            return PriceResult(
                price=price,
                tier=PriceTier.DELAYED,
                source=PriceSource.IBKR_DELAYED,
                symbol=symbol,
                instrument_id=instrument_id,
                age_seconds=900,  # Assume 15 min delay
            )

        except Exception as e:
            logger.debug(f"Delayed fetch failed for {instrument_id}: {e}")
            # Reset market data type
            self._set_market_data_type(1)
            return None

    def _try_portfolio(
        self,
        instrument_id: str,
        symbol: str,
        con_id: Optional[int],
    ) -> Optional[PriceResult]:
        """Tier C: Try portfolio mark price from IBKR."""
        if not self.ib_client or not self.ib_client.is_connected():
            return None

        try:
            # Search portfolio for matching symbol
            for item in self.ib_client.ib.portfolio():
                if item.contract.symbol == symbol:
                    if item.marketPrice and item.marketPrice > 0:
                        return PriceResult(
                            price=item.marketPrice,
                            tier=PriceTier.PORTFOLIO,
                            source=PriceSource.IBKR_PORTFOLIO,
                            symbol=symbol,
                            instrument_id=instrument_id,
                            age_seconds=60,  # Assume ~1 min age
                        )
            return None

        except Exception as e:
            logger.debug(f"Portfolio price failed for {instrument_id}: {e}")
            return None

    def _try_cache(
        self,
        instrument_id: str,
        symbol: str,
        con_id: Optional[int],
    ) -> Optional[PriceResult]:
        """Tier D: Try cached last known price."""
        if not self.price_cache:
            return None

        try:
            cached = self.price_cache.get(instrument_id)
            if cached and cached.is_valid:
                # Check TTL
                age = (datetime.now() - cached.timestamp).total_seconds()
                if age <= self.cache_ttl_seconds:
                    cached.age_seconds = age
                    cached.tier = PriceTier.CACHED
                    cached.source = PriceSource.CACHE
                    return cached
            return None

        except Exception as e:
            logger.debug(f"Cache lookup failed for {instrument_id}: {e}")
            return None

    def _try_guardrail(
        self,
        instrument_id: str,
        symbol: str,
        con_id: Optional[int],
    ) -> Optional[PriceResult]:
        """Tier E: Use config guardrail fallback."""
        # Check instrument_id first, then symbol
        guardrail_price = self.DEFAULT_GUARDRAILS.get(
            instrument_id,
            self.DEFAULT_GUARDRAILS.get(symbol)
        )

        if guardrail_price:
            return PriceResult(
                price=guardrail_price,
                tier=PriceTier.GUARDRAIL,
                source=PriceSource.CONFIG_DEFAULT,
                symbol=symbol,
                instrument_id=instrument_id,
                age_seconds=float('inf'),  # Unknown age
                error_message="Using guardrail fallback price",
            )

        return None

    def _set_market_data_type(self, data_type: int) -> None:
        """Set IBKR market data type (1=realtime, 3=delayed, 4=frozen)."""
        if self._market_data_type == data_type:
            return

        if self.ib_client and self.ib_client.is_connected():
            try:
                self.ib_client.ib.reqMarketDataType(data_type)
                self._market_data_type = data_type
                logger.debug(f"Set market data type to {data_type}")
            except Exception as e:
                logger.warning(f"Failed to set market data type: {e}")

    def _log_resolution(self, result: PriceResult) -> None:
        """Log price resolution result."""
        if result.is_valid:
            logger.info(
                f"Price resolved: {result.instrument_id} = {result.price:.4f} "
                f"(tier={result.tier.value}, source={result.source.value}, "
                f"age={result.age_seconds:.0f}s, confidence={result.confidence_score:.2f})"
            )
        else:
            logger.warning(
                f"Price resolution FAILED for {result.instrument_id}: "
                f"{result.error_message or 'All tiers exhausted'}"
            )

    def get_metrics(self) -> Dict[str, Any]:
        """Get resolver metrics."""
        return self.metrics.to_dict()

    def reset_metrics(self) -> None:
        """Reset metrics counters."""
        self.metrics = ResolverMetrics()
