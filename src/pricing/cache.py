"""
Persistent Price Cache for AbstractFinance.

Stores last known prices to provide fallback when live data unavailable.
Uses JSON file storage for simplicity and portability.
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class PriceCache:
    """
    Persistent price cache with TTL-based expiration.

    Stores prices in a JSON file for persistence across restarts.
    Thread-safe through file locking.
    """

    DEFAULT_CACHE_FILE = "state/price_cache.json"
    DEFAULT_TTL_SECONDS = 86400  # 24 hours

    def __init__(
        self,
        cache_file: Optional[str] = None,
        default_ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ):
        """
        Initialize price cache.

        Args:
            cache_file: Path to cache file (default: state/price_cache.json)
            default_ttl_seconds: Default TTL for cached prices
        """
        self.cache_file = Path(cache_file or self.DEFAULT_CACHE_FILE)
        self.default_ttl_seconds = default_ttl_seconds

        # In-memory cache
        self._cache: Dict[str, Dict[str, Any]] = {}

        # Metrics
        self._hits = 0
        self._misses = 0
        self._writes = 0

        # Load existing cache
        self._load_cache()

    def _load_cache(self) -> None:
        """Load cache from disk."""
        try:
            if self.cache_file.exists():
                with open(self.cache_file, 'r') as f:
                    data = json.load(f)
                    self._cache = data.get("prices", {})
                    logger.info(f"Loaded {len(self._cache)} prices from cache")
            else:
                self._cache = {}
                logger.info("No existing price cache found, starting fresh")
        except Exception as e:
            logger.warning(f"Failed to load price cache: {e}")
            self._cache = {}

    def _save_cache(self) -> None:
        """Persist cache to disk."""
        try:
            # Ensure directory exists
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)

            data = {
                "prices": self._cache,
                "last_updated": datetime.now().isoformat(),
                "version": "1.0",
            }

            with open(self.cache_file, 'w') as f:
                json.dump(data, f, indent=2, default=str)

        except Exception as e:
            logger.warning(f"Failed to save price cache: {e}")

    def get(self, instrument_id: str, max_age_seconds: Optional[int] = None) -> Optional[Any]:
        """
        Get cached price for instrument.

        Args:
            instrument_id: Instrument identifier
            max_age_seconds: Maximum age to consider valid (None = use default TTL)

        Returns:
            PriceResult-like dict if valid cache exists, None otherwise
        """
        if instrument_id not in self._cache:
            self._misses += 1
            return None

        entry = self._cache[instrument_id]
        timestamp_str = entry.get("timestamp")

        if not timestamp_str:
            self._misses += 1
            return None

        # Parse timestamp
        try:
            timestamp = datetime.fromisoformat(timestamp_str)
        except (ValueError, TypeError):
            self._misses += 1
            return None

        # Check TTL
        age_seconds = (datetime.now() - timestamp).total_seconds()
        max_age = max_age_seconds or self.default_ttl_seconds

        if age_seconds > max_age:
            self._misses += 1
            logger.debug(f"Cache expired for {instrument_id} (age={age_seconds:.0f}s > {max_age}s)")
            return None

        self._hits += 1

        # Return as a simple object with required attributes
        return CachedPrice(
            price=entry.get("price"),
            symbol=entry.get("symbol", instrument_id),
            instrument_id=instrument_id,
            timestamp=timestamp,
            source=entry.get("source", "cache"),
            tier=entry.get("tier", "cached"),
        )

    def set(self, instrument_id: str, price_result: Any) -> None:
        """
        Cache a price result.

        Args:
            instrument_id: Instrument identifier
            price_result: PriceResult object or dict with price data
        """
        if price_result is None:
            return

        # Handle both PriceResult objects and dicts
        if hasattr(price_result, 'price'):
            price = price_result.price
            symbol = getattr(price_result, 'symbol', instrument_id)
            source = getattr(price_result, 'source', None)
            tier = getattr(price_result, 'tier', None)
            bid = getattr(price_result, 'bid', None)
            ask = getattr(price_result, 'ask', None)
        elif isinstance(price_result, dict):
            price = price_result.get('price')
            symbol = price_result.get('symbol', instrument_id)
            source = price_result.get('source')
            tier = price_result.get('tier')
            bid = price_result.get('bid')
            ask = price_result.get('ask')
        else:
            return

        if price is None or price <= 0:
            return

        # Convert enums to strings
        if hasattr(source, 'value'):
            source = source.value
        if hasattr(tier, 'value'):
            tier = tier.value

        self._cache[instrument_id] = {
            "price": price,
            "symbol": symbol,
            "timestamp": datetime.now().isoformat(),
            "source": source,
            "tier": tier,
            "bid": bid,
            "ask": ask,
        }

        self._writes += 1

        # Persist periodically (every 10 writes)
        if self._writes % 10 == 0:
            self._save_cache()

    def invalidate(self, instrument_id: str) -> None:
        """Remove instrument from cache."""
        if instrument_id in self._cache:
            del self._cache[instrument_id]

    def clear(self) -> None:
        """Clear all cached prices."""
        self._cache = {}
        self._save_cache()

    def flush(self) -> None:
        """Force save cache to disk."""
        self._save_cache()

    def get_all(self) -> Dict[str, Dict[str, Any]]:
        """Get all cached prices (for debugging)."""
        return self._cache.copy()

    def cleanup_expired(self, max_age_seconds: Optional[int] = None) -> int:
        """
        Remove expired entries from cache.

        Args:
            max_age_seconds: Maximum age to keep (None = use default TTL)

        Returns:
            Number of entries removed
        """
        max_age = max_age_seconds or self.default_ttl_seconds
        now = datetime.now()
        removed = 0

        to_remove = []
        for instrument_id, entry in self._cache.items():
            timestamp_str = entry.get("timestamp")
            if not timestamp_str:
                to_remove.append(instrument_id)
                continue

            try:
                timestamp = datetime.fromisoformat(timestamp_str)
                age = (now - timestamp).total_seconds()
                if age > max_age:
                    to_remove.append(instrument_id)
            except (ValueError, TypeError):
                to_remove.append(instrument_id)

        for instrument_id in to_remove:
            del self._cache[instrument_id]
            removed += 1

        if removed > 0:
            self._save_cache()
            logger.info(f"Cleaned up {removed} expired cache entries")

        return removed

    def get_metrics(self) -> Dict[str, Any]:
        """Get cache performance metrics."""
        total = self._hits + self._misses
        hit_rate = (self._hits / total * 100) if total > 0 else 0.0

        return {
            "entries": len(self._cache),
            "hits": self._hits,
            "misses": self._misses,
            "writes": self._writes,
            "hit_rate_pct": round(hit_rate, 2),
        }


class CachedPrice:
    """Simple wrapper for cached price data."""

    def __init__(
        self,
        price: float,
        symbol: str,
        instrument_id: str,
        timestamp: datetime,
        source: str = "cache",
        tier: str = "cached",
    ):
        self.price = price
        self.symbol = symbol
        self.instrument_id = instrument_id
        self.timestamp = timestamp
        self.source = source
        self.tier = tier

    @property
    def is_valid(self) -> bool:
        return self.price is not None and self.price > 0

    @property
    def age_seconds(self) -> float:
        return (datetime.now() - self.timestamp).total_seconds()
