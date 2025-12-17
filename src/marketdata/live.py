"""
Live Market Data - IBKR-only data source for live trading.

CRITICAL: This module must NEVER import or use yfinance.
Live trading data comes exclusively from IBKR.

Phase 2 Enhancement: Hard separation ensures live trading
never falls back to Yahoo data.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from typing import Dict, Optional, Any, List

# CRITICAL: NO yfinance import allowed in this module
# Any attempt to add fallback to Yahoo must be rejected

from ..execution.types import MarketDataSnapshot


logger = logging.getLogger(__name__)


@dataclass
class QuoteQualityConfig:
    """Configuration for quote quality requirements."""
    require_bid_ask_for_limit_pricing: bool = True
    max_mid_staleness_seconds: int = 5
    min_bid: float = 0.0
    min_ask: float = 0.0
    max_spread_bps: float = 500.0  # 5% max spread


class LiveMarketData:
    """
    Live market data provider using IBKR only.

    CRITICAL INVARIANT: This class must never use Yahoo/yfinance.
    If IBKR data is unavailable, return None - do not fallback.

    Usage:
        live_md = LiveMarketData(ib_client)
        snapshot = live_md.get_snapshot("AAPL")
        if snapshot is None:
            # NO TRADE - data unavailable
            pass
    """

    def __init__(
        self,
        ib_client: Any,  # IBClient from execution_ibkr
        quality_config: Optional[QuoteQualityConfig] = None,
        timeout_ms: int = 1500,
    ):
        """
        Initialize live market data provider.

        Args:
            ib_client: IBClient instance for IBKR connection
            quality_config: Quote quality requirements
            timeout_ms: Timeout for data requests in milliseconds
        """
        self.ib_client = ib_client
        self.quality_config = quality_config or QuoteQualityConfig()
        self.timeout_ms = timeout_ms

        # Cache of recent snapshots
        self._cache: Dict[str, MarketDataSnapshot] = {}
        self._cache_timestamps: Dict[str, datetime] = {}

    def get_snapshot(
        self,
        instrument_id: str,
        require_quotes: bool = True,
    ) -> Optional[MarketDataSnapshot]:
        """
        Get live market data snapshot from IBKR.

        Args:
            instrument_id: Instrument identifier
            require_quotes: If True, return None if bid/ask missing

        Returns:
            MarketDataSnapshot or None if unavailable/invalid

        CRITICAL: Returns None if data unavailable. NO Yahoo fallback.
        """
        try:
            # Get data from IBKR
            snapshot = self._fetch_ibkr_snapshot(instrument_id)

            if snapshot is None:
                logger.warning(f"No IBKR data for {instrument_id} - NO TRADE")
                return None

            # Quality checks
            if not self._validate_quality(snapshot, require_quotes):
                logger.warning(f"Quote quality check failed for {instrument_id} - NO TRADE")
                return None

            # Update cache
            self._cache[instrument_id] = snapshot
            self._cache_timestamps[instrument_id] = datetime.now()

            return snapshot

        except Exception as e:
            logger.error(f"Failed to get live data for {instrument_id}: {e}")
            return None

    def get_snapshots(
        self,
        instrument_ids: List[str],
        require_quotes: bool = True,
    ) -> Dict[str, Optional[MarketDataSnapshot]]:
        """
        Get live snapshots for multiple instruments with batch request.

        Uses single wait for all instruments instead of sequential waits.

        Args:
            instrument_ids: List of instrument identifiers
            require_quotes: If True, exclude instruments without bid/ask

        Returns:
            Dictionary of instrument_id -> MarketDataSnapshot (or None)
        """
        results: Dict[str, Optional[MarketDataSnapshot]] = {}

        if not instrument_ids:
            return results

        # Check if we have direct ib_insync access for batch fetching
        if (
            self.ib_client is not None
            and hasattr(self.ib_client, 'ib')
            and self.ib_client.ib is not None
        ):
            results = self._batch_fetch_from_ib_insync(instrument_ids, require_quotes)
        else:
            # Fallback to sequential fetch if no direct IB access
            for inst_id in instrument_ids:
                results[inst_id] = self.get_snapshot(inst_id, require_quotes)

        return results

    def _batch_fetch_from_ib_insync(
        self,
        instrument_ids: List[str],
        require_quotes: bool = True,
    ) -> Dict[str, Optional[MarketDataSnapshot]]:
        """
        Batch fetch snapshots from ib_insync with single wait.

        Performance improvement: N instruments = 1 wait instead of N waits.
        """
        results: Dict[str, Optional[MarketDataSnapshot]] = {}
        ib = self.ib_client.ib

        # Build contracts for each instrument
        contracts_to_fetch: List[tuple] = []  # (inst_id, contract)

        for inst_id in instrument_ids:
            contract = None

            # Find contract in trades
            for trade in ib.trades():
                if trade.contract.symbol == inst_id:
                    contract = trade.contract
                    break

            # Find contract in positions
            if contract is None:
                for pos in ib.positions():
                    if pos.contract.symbol == inst_id:
                        contract = pos.contract
                        break

            if contract is not None:
                contracts_to_fetch.append((inst_id, contract))
            else:
                logger.debug(f"Contract not found for {inst_id} in batch fetch")
                results[inst_id] = None

        if not contracts_to_fetch:
            return results

        # Request market data for all contracts
        tickers: List[tuple] = []  # (inst_id, contract, ticker)
        for inst_id, contract in contracts_to_fetch:
            try:
                ticker = ib.reqMktData(contract, '', False, False)
                tickers.append((inst_id, contract, ticker))
            except Exception as e:
                logger.debug(f"Failed to request market data for {inst_id}: {e}")
                results[inst_id] = None

        # Single wait for all tickers
        if tickers:
            ib.sleep(self.timeout_ms / 1000.0)

        # Collect results and cancel market data
        for inst_id, contract, ticker in tickers:
            try:
                if ticker is None:
                    results[inst_id] = None
                    continue

                snapshot = MarketDataSnapshot(
                    symbol=inst_id,
                    ts=datetime.now(),
                    bid=ticker.bid if ticker.bid and ticker.bid > 0 else None,
                    ask=ticker.ask if ticker.ask and ticker.ask > 0 else None,
                    last=ticker.last if ticker.last and ticker.last > 0 else None,
                    close=ticker.close if ticker.close and ticker.close > 0 else None,
                    volume=int(ticker.volume) if ticker.volume else None,
                )

                # Validate quality
                if not self._validate_quality(snapshot, require_quotes):
                    logger.warning(f"Quote quality check failed for {inst_id} - NO TRADE")
                    results[inst_id] = None
                else:
                    # Update cache
                    self._cache[inst_id] = snapshot
                    self._cache_timestamps[inst_id] = datetime.now()
                    results[inst_id] = snapshot

                # Cancel market data subscription
                ib.cancelMktData(contract)

            except Exception as e:
                logger.error(f"Failed to process ticker for {inst_id}: {e}")
                results[inst_id] = None

        return results

    def _fetch_ibkr_snapshot(
        self,
        instrument_id: str,
    ) -> Optional[MarketDataSnapshot]:
        """
        Fetch snapshot from IBKR.

        Internal method - handles IBKR-specific logic.
        """
        if self.ib_client is None:
            logger.error("IBKR client not available")
            return None

        try:
            # Check if IB client has get_market_data method
            if hasattr(self.ib_client, 'get_market_data'):
                return self.ib_client.get_market_data(instrument_id)

            # Alternative: use IBKRTransport if available
            if hasattr(self.ib_client, 'transport'):
                return self.ib_client.transport.get_market_data(instrument_id)

            # Fallback: try to get ticker data directly from ib_insync
            if hasattr(self.ib_client, 'ib') and self.ib_client.ib is not None:
                return self._fetch_from_ib_insync(instrument_id)

            logger.error("No valid IBKR data method available")
            return None

        except Exception as e:
            logger.error(f"IBKR fetch error for {instrument_id}: {e}")
            return None

    def _fetch_from_ib_insync(
        self,
        instrument_id: str,
    ) -> Optional[MarketDataSnapshot]:
        """
        Fetch data directly from ib_insync client.
        """
        try:
            ib = self.ib_client.ib

            # Find contract in portfolio or create one
            contract = None
            for trade in ib.trades():
                if trade.contract.symbol == instrument_id:
                    contract = trade.contract
                    break

            if contract is None:
                # Try to find in positions
                for pos in ib.positions():
                    if pos.contract.symbol == instrument_id:
                        contract = pos.contract
                        break

            if contract is None:
                logger.warning(f"Contract not found for {instrument_id}")
                return None

            # Request market data
            ticker = ib.reqMktData(contract, '', False, False)
            ib.sleep(self.timeout_ms / 1000.0)

            if ticker is None:
                return None

            return MarketDataSnapshot(
                symbol=instrument_id,
                ts=datetime.now(),
                bid=ticker.bid if ticker.bid > 0 else None,
                ask=ticker.ask if ticker.ask > 0 else None,
                last=ticker.last if ticker.last > 0 else None,
                close=ticker.close if ticker.close > 0 else None,
                volume=int(ticker.volume) if ticker.volume else None,
            )

        except Exception as e:
            logger.error(f"ib_insync fetch error: {e}")
            return None

    def _validate_quality(
        self,
        snapshot: MarketDataSnapshot,
        require_quotes: bool,
    ) -> bool:
        """
        Validate quote quality against requirements.

        Returns False if snapshot doesn't meet quality standards.
        """
        config = self.quality_config

        # Check freshness
        if not snapshot.is_fresh(config.max_mid_staleness_seconds):
            logger.debug(f"Stale data for {snapshot.symbol}")
            return False

        # Check bid/ask requirement
        if require_quotes and config.require_bid_ask_for_limit_pricing:
            if not snapshot.has_quotes():
                logger.debug(f"Missing bid/ask for {snapshot.symbol}")
                return False

            # Validate bid/ask values
            if snapshot.bid <= config.min_bid:
                logger.debug(f"Invalid bid for {snapshot.symbol}: {snapshot.bid}")
                return False

            if snapshot.ask <= config.min_ask:
                logger.debug(f"Invalid ask for {snapshot.symbol}: {snapshot.ask}")
                return False

            # Check spread
            if snapshot.spread_bps and snapshot.spread_bps > config.max_spread_bps:
                logger.debug(f"Spread too wide for {snapshot.symbol}: {snapshot.spread_bps:.1f} bps")
                return False

        # If quotes not required, ensure we have at least reference price
        if not require_quotes:
            if snapshot.reference_price is None:
                logger.debug(f"No reference price for {snapshot.symbol}")
                return False

        return True

    def get_arrival_price(
        self,
        instrument_id: str,
    ) -> Optional[float]:
        """
        Get arrival price (mid) for slippage calculation.

        Returns mid price if available, otherwise None.
        """
        snapshot = self.get_snapshot(instrument_id, require_quotes=True)
        if snapshot is None:
            return None
        return snapshot.mid

    def can_trade(
        self,
        instrument_id: str,
    ) -> tuple[bool, str]:
        """
        Check if instrument can be traded based on data availability.

        Returns:
            Tuple of (can_trade, reason)
        """
        snapshot = self.get_snapshot(instrument_id, require_quotes=True)

        if snapshot is None:
            return False, "No live data available"

        if not snapshot.has_quotes():
            return False, "Missing bid/ask quotes"

        if not snapshot.is_fresh(self.quality_config.max_mid_staleness_seconds):
            return False, "Data is stale"

        return True, "OK"

    def get_cached_snapshot(
        self,
        instrument_id: str,
        max_age_seconds: int = 5,
    ) -> Optional[MarketDataSnapshot]:
        """
        Get cached snapshot if still valid.

        Used to avoid repeated IBKR calls for same data.
        """
        if instrument_id not in self._cache:
            return None

        cache_time = self._cache_timestamps.get(instrument_id)
        if cache_time is None:
            return None

        age = (datetime.now() - cache_time).total_seconds()
        if age > max_age_seconds:
            return None

        return self._cache[instrument_id]

    def clear_cache(self) -> None:
        """Clear the data cache."""
        self._cache.clear()
        self._cache_timestamps.clear()


# Singleton instance
_live_market_data: Optional[LiveMarketData] = None


def get_live_market_data(
    ib_client: Optional[Any] = None,
) -> Optional[LiveMarketData]:
    """
    Get singleton LiveMarketData instance.

    Must be initialized with an IB client before use.
    """
    global _live_market_data

    if ib_client is not None:
        _live_market_data = LiveMarketData(ib_client)

    return _live_market_data


def init_live_market_data(
    ib_client: Any,
    quality_config: Optional[QuoteQualityConfig] = None,
    timeout_ms: int = 1500,
) -> LiveMarketData:
    """
    Initialize the live market data singleton.

    Args:
        ib_client: IBClient instance
        quality_config: Optional quality configuration
        timeout_ms: Timeout for data requests

    Returns:
        Initialized LiveMarketData instance
    """
    global _live_market_data
    _live_market_data = LiveMarketData(
        ib_client=ib_client,
        quality_config=quality_config,
        timeout_ms=timeout_ms,
    )
    return _live_market_data


# =============================================================================
# ROADMAP Phase B: Europe-First Regime Data Feeds
# =============================================================================

class EuropeRegimeData:
    """
    Data provider for Europe-first regime detection.

    Provides V2X (VSTOXX), VIX, and EURUSD data from IBKR.
    Falls back gracefully when data unavailable.

    CRITICAL: Uses IBKR only - no Yahoo fallback for live trading.
    """

    def __init__(self, ib_client: Any, timeout_ms: int = 2000):
        """
        Initialize Europe regime data provider.

        Args:
            ib_client: IBClient instance
            timeout_ms: Timeout for data requests
        """
        self.ib_client = ib_client
        self.timeout_ms = timeout_ms

        # Cache for EURUSD history (for trend calculation)
        self._eurusd_history: List[float] = []
        self._eurusd_history_dates: List[datetime] = []

    def get_v2x_level(self) -> Optional[float]:
        """
        Get current V2X (VSTOXX) level from IBKR.

        V2X is the European volatility index, based on EURO STOXX 50 options.
        IBKR symbol: FVS (V2X futures) or use STOXX Europe index options.

        Returns:
            V2X level or None if unavailable
        """
        if self.ib_client is None:
            return None

        try:
            # Try to get V2X from ib_insync
            if hasattr(self.ib_client, 'ib') and self.ib_client.ib is not None:
                from ib_insync import Index

                ib = self.ib_client.ib

                # V2X Index on EUREX
                contract = Index('V2TX', 'EUREX', 'EUR')
                ib.qualifyContracts(contract)

                ticker = ib.reqMktData(contract, '', False, False)
                ib.sleep(self.timeout_ms / 1000.0)

                v2x = ticker.last if ticker.last and ticker.last > 0 else ticker.close
                ib.cancelMktData(contract)

                if v2x and v2x > 0:
                    logger.debug(f"V2X level: {v2x}")
                    return float(v2x)

            return None

        except Exception as e:
            logger.warning(f"Failed to get V2X: {e}")
            return None

    def get_vix_level(self) -> Optional[float]:
        """
        Get current VIX level from IBKR.

        Returns:
            VIX level or None if unavailable
        """
        if self.ib_client is None:
            return None

        try:
            if hasattr(self.ib_client, 'ib') and self.ib_client.ib is not None:
                from ib_insync import Index

                ib = self.ib_client.ib

                # VIX Index on CBOE
                contract = Index('VIX', 'CBOE', 'USD')
                ib.qualifyContracts(contract)

                ticker = ib.reqMktData(contract, '', False, False)
                ib.sleep(self.timeout_ms / 1000.0)

                vix = ticker.last if ticker.last and ticker.last > 0 else ticker.close
                ib.cancelMktData(contract)

                if vix and vix > 0:
                    logger.debug(f"VIX level: {vix}")
                    return float(vix)

            return None

        except Exception as e:
            logger.warning(f"Failed to get VIX: {e}")
            return None

    def get_eurusd_spot(self) -> Optional[float]:
        """
        Get current EUR/USD spot rate from IBKR.

        Returns:
            EUR/USD rate or None if unavailable
        """
        if self.ib_client is None:
            return None

        try:
            if hasattr(self.ib_client, 'ib') and self.ib_client.ib is not None:
                from ib_insync import Forex

                ib = self.ib_client.ib

                contract = Forex('EURUSD')
                ib.qualifyContracts(contract)

                ticker = ib.reqMktData(contract, '', False, False)
                ib.sleep(self.timeout_ms / 1000.0)

                rate = ticker.last if ticker.last and ticker.last > 0 else ticker.close
                ib.cancelMktData(contract)

                if rate and rate > 0:
                    # Update history for trend calculation
                    self._eurusd_history.append(rate)
                    self._eurusd_history_dates.append(datetime.now())

                    # Keep last 120 days
                    if len(self._eurusd_history) > 120:
                        self._eurusd_history = self._eurusd_history[-120:]
                        self._eurusd_history_dates = self._eurusd_history_dates[-120:]

                    logger.debug(f"EURUSD spot: {rate}")
                    return float(rate)

            return None

        except Exception as e:
            logger.warning(f"Failed to get EURUSD: {e}")
            return None

    def get_eurusd_trend(self, lookback_days: int = 60) -> Optional[float]:
        """
        Calculate EURUSD trend over lookback period.

        Args:
            lookback_days: Number of days for trend calculation

        Returns:
            Annualized trend (negative = EUR weakening) or None
        """
        if len(self._eurusd_history) < 5:
            return None

        try:
            import pandas as pd

            # Get recent history
            lookback = min(len(self._eurusd_history), lookback_days)
            prices = pd.Series(self._eurusd_history[-lookback:])

            # Calculate daily returns
            returns = prices.pct_change().dropna()

            if len(returns) < 5:
                return None

            # Annualize mean return
            trend = returns.mean() * 252
            logger.debug(f"EURUSD trend ({lookback}d): {trend:.2%}")
            return trend

        except Exception as e:
            logger.warning(f"Failed to calculate EURUSD trend: {e}")
            return None

    # =========================================================================
    # Strategy Evolution v2.1: VSTOXX Futures for Term Structure
    # =========================================================================

    def get_vstoxx_spot(self) -> Optional[float]:
        """
        Get VSTOXX spot level.

        Alias for get_v2x_level() - provided for EuropeVolEngine compatibility.

        Returns:
            V2X level or None if unavailable
        """
        return self.get_v2x_level()

    def get_vstoxx_futures(self) -> Optional[Dict[str, float]]:
        """
        Get VSTOXX Mini Futures (FVS) front and back month prices.

        Used for term structure signal in EuropeVolEngine.

        Returns:
            Dict with 'front' and 'back' month prices, or None if unavailable
        """
        if self.ib_client is None:
            return None

        try:
            if hasattr(self.ib_client, 'ib') and self.ib_client.ib is not None:
                from ib_insync import Future

                ib = self.ib_client.ib

                # Determine current and next expiry months
                # VSTOXX futures expire on 3rd Wednesday of each month
                today = date.today()

                # Get front month (current or next month if close to expiry)
                front_expiry = self._get_fvs_expiry(today, 0)
                back_expiry = self._get_fvs_expiry(today, 1)

                # Create FVS contracts
                # FVS = VSTOXX Mini Futures on EUREX
                front_contract = Future(
                    symbol='FVS',
                    exchange='EUREX',
                    currency='EUR',
                    lastTradeDateOrContractMonth=front_expiry.strftime('%Y%m')
                )

                back_contract = Future(
                    symbol='FVS',
                    exchange='EUREX',
                    currency='EUR',
                    lastTradeDateOrContractMonth=back_expiry.strftime('%Y%m')
                )

                # Qualify contracts
                try:
                    ib.qualifyContracts(front_contract)
                    ib.qualifyContracts(back_contract)
                except Exception as e:
                    logger.warning(f"Failed to qualify FVS contracts: {e}")
                    return None

                # Request market data
                front_ticker = ib.reqMktData(front_contract, '', False, False)
                back_ticker = ib.reqMktData(back_contract, '', False, False)

                ib.sleep(self.timeout_ms / 1000.0)

                front_price = (
                    front_ticker.last if front_ticker.last and front_ticker.last > 0
                    else front_ticker.close
                )
                back_price = (
                    back_ticker.last if back_ticker.last and back_ticker.last > 0
                    else back_ticker.close
                )

                # Cancel market data
                ib.cancelMktData(front_contract)
                ib.cancelMktData(back_contract)

                if front_price and front_price > 0 and back_price and back_price > 0:
                    result = {
                        'front': float(front_price),
                        'back': float(back_price),
                        'front_expiry': front_expiry.strftime('%Y-%m-%d'),
                        'back_expiry': back_expiry.strftime('%Y-%m-%d'),
                    }
                    logger.debug(
                        f"VSTOXX futures: front={front_price:.2f} ({front_expiry}), "
                        f"back={back_price:.2f} ({back_expiry})"
                    )
                    return result

                # Partial data
                if front_price and front_price > 0:
                    logger.warning("Only front month VSTOXX available")
                    return {
                        'front': float(front_price),
                        'back': float(front_price * 1.02),  # Estimate: ~2% contango
                        'front_expiry': front_expiry.strftime('%Y-%m-%d'),
                        'back_expiry': back_expiry.strftime('%Y-%m-%d'),
                    }

            return None

        except Exception as e:
            logger.warning(f"Failed to get VSTOXX futures: {e}")
            return None

    def _get_fvs_expiry(self, ref_date: date, months_ahead: int) -> date:
        """
        Calculate VSTOXX futures expiry date.

        FVS expires on 3rd Wednesday of the month.

        Args:
            ref_date: Reference date
            months_ahead: Number of months ahead (0 = current, 1 = next)

        Returns:
            Expiry date
        """
        # Determine target month
        target_month = ref_date.month + months_ahead
        target_year = ref_date.year

        while target_month > 12:
            target_month -= 12
            target_year += 1

        # Find 3rd Wednesday
        # Get first day of month
        first_day = date(target_year, target_month, 1)

        # Find first Wednesday
        days_until_wed = (2 - first_day.weekday()) % 7
        first_wed = first_day + timedelta(days=days_until_wed)

        # 3rd Wednesday is 2 weeks after first Wednesday
        third_wed = first_wed + timedelta(days=14)

        # If we're past expiry this month, use next month
        if months_ahead == 0 and ref_date >= third_wed:
            return self._get_fvs_expiry(ref_date, 1)

        return third_wed

    def get_vstoxx_term_spread(self) -> Optional[float]:
        """
        Get VSTOXX term spread (back - front).

        Positive = contango (vol cheap in futures)
        Negative = backwardation (vol expensive)

        Returns:
            Term spread in V2X points, or None if unavailable
        """
        futures = self.get_vstoxx_futures()
        if futures is None:
            return None

        spread = futures['back'] - futures['front']
        logger.debug(f"VSTOXX term spread: {spread:+.2f} points")
        return spread

    def get_vstoxx_all(self) -> Dict[str, Any]:
        """
        Get all VSTOXX data needed for EuropeVolEngine.

        Returns:
            Dict with 'spot', 'front', 'back', 'term_spread'
        """
        spot = self.get_v2x_level()
        futures = self.get_vstoxx_futures()

        if futures:
            return {
                'spot': spot or futures['front'],  # Use front as proxy if spot unavailable
                'front': futures['front'],
                'back': futures['back'],
                'term_spread': futures['back'] - futures['front'],
                'front_expiry': futures.get('front_expiry'),
                'back_expiry': futures.get('back_expiry'),
            }
        elif spot:
            # Estimate futures from spot with typical contango
            return {
                'spot': spot,
                'front': spot * 0.98,  # Front typically at small discount
                'back': spot * 1.02,   # Back typically at premium
                'term_spread': spot * 0.04,  # Estimated ~4% contango
            }
        else:
            return {}

    def get_all_regime_inputs(self) -> Dict[str, Optional[float]]:
        """
        Get all inputs needed for Europe-first regime detection.

        Returns:
            Dict with 'v2x', 'vix', 'eurusd_spot', 'eurusd_trend'
        """
        return {
            'v2x': self.get_v2x_level(),
            'vix': self.get_vix_level(),
            'eurusd_spot': self.get_eurusd_spot(),
            'eurusd_trend': self.get_eurusd_trend(),
        }


# Europe regime data singleton
_europe_regime_data: Optional[EuropeRegimeData] = None


def get_europe_regime_data(ib_client: Optional[Any] = None) -> Optional[EuropeRegimeData]:
    """
    Get singleton EuropeRegimeData instance.

    Must be initialized with an IB client before use.
    """
    global _europe_regime_data

    if ib_client is not None:
        _europe_regime_data = EuropeRegimeData(ib_client)

    return _europe_regime_data


def init_europe_regime_data(
    ib_client: Any,
    timeout_ms: int = 2000,
) -> EuropeRegimeData:
    """
    Initialize the Europe regime data singleton.

    Args:
        ib_client: IBClient instance
        timeout_ms: Timeout for data requests

    Returns:
        Initialized EuropeRegimeData instance
    """
    global _europe_regime_data
    _europe_regime_data = EuropeRegimeData(
        ib_client=ib_client,
        timeout_ms=timeout_ms,
    )
    return _europe_regime_data
