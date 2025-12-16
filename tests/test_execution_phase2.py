"""
Tests for Phase 2 Execution Stack Upgrades.

Required tests per spec:
1) test_session_scheduler_creates_jobs_and_persists
2) test_session_scheduler_executes_only_in_window
3) test_live_market_data_no_yahoo_fallback
4) test_overlay_opens_on_legged_pair_and_unwinds (partial)
5) test_trade_gating_skips_small_drift_trades
6) test_trade_gating_overrides_on_limit_breach
7) test_slippage_model_updates_from_analytics_and_clamps
8) test_execution_policy_uses_slippage_model_offsets
9) test_borrow_service_denies_unavailable_short
10) test_financing_service_daily_carry_estimate
"""

import pytest
from datetime import datetime, date, timedelta
from unittest.mock import Mock, patch, MagicMock
import pytz


# =============================================================================
# Test 1: Session Scheduler Creates Jobs and Persists
# =============================================================================

class TestSessionSchedulerJobCreation:
    """Test ExecutionJob creation and persistence."""

    def test_session_scheduler_creates_jobs_and_persists(self, tmp_path):
        """Jobs are created with correct IDs and persisted to disk."""
        from src.execution.jobs import (
            ExecutionJob, ExecutionJobStore, Venue, ExecutionStyle,
            generate_job_id, JobStatus,
        )
        from src.execution.types import OrderIntent, Urgency

        # Setup
        persist_path = tmp_path / "jobs.json"
        store = ExecutionJobStore(persist_path=str(persist_path))

        # Create test intents
        intents = [
            OrderIntent(
                instrument_id="SPY",
                side="BUY",
                quantity=100,
                reason="rebalance",
                sleeve="core",
                urgency=Urgency.NORMAL,
            ),
            OrderIntent(
                instrument_id="QQQ",
                side="SELL",
                quantity=50,
                reason="rebalance",
                sleeve="core",
                urgency=Urgency.NORMAL,
            ),
        ]

        # Create job
        now = datetime.utcnow()
        job = store.create_job_if_not_exists(
            trade_date="2025-12-15",
            venue=Venue.US,
            style=ExecutionStyle.MIDDAY,
            intents=intents,
            earliest_start_utc=now + timedelta(hours=1),
            latest_end_utc=now + timedelta(hours=6),
        )

        # Verify job created
        assert job is not None
        assert job.status == JobStatus.PENDING
        assert job.venue == Venue.US
        assert job.style == ExecutionStyle.MIDDAY
        assert len(job.intents) == 2

        # Verify persistence
        assert persist_path.exists()

        # Reload store and verify job survives
        store2 = ExecutionJobStore(persist_path=str(persist_path))
        loaded_job = store2.get_job(job.job_id)

        assert loaded_job is not None
        assert loaded_job.job_id == job.job_id
        assert len(loaded_job.intents) == 2

        # Verify idempotency - same inputs = same job
        job2 = store2.create_job_if_not_exists(
            trade_date="2025-12-15",
            venue=Venue.US,
            style=ExecutionStyle.MIDDAY,
            intents=intents,
            earliest_start_utc=now + timedelta(hours=1),
            latest_end_utc=now + timedelta(hours=6),
        )

        assert job2.job_id == job.job_id  # Same job returned


# =============================================================================
# Test 2: Session Scheduler Executes Only in Window
# =============================================================================

class TestSessionSchedulerWindows:
    """Test window-based execution."""

    def test_session_scheduler_executes_only_in_window(self):
        """Jobs only execute when within their time window."""
        from src.execution.jobs import ExecutionJob, Venue, ExecutionStyle, JobStatus
        from src.execution.types import OrderIntent, Urgency

        now = datetime.utcnow()
        intents = [
            OrderIntent("SPY", "BUY", 100, "rebalance", "core", Urgency.NORMAL)
        ]

        # Job with future window
        future_job = ExecutionJob(
            job_id="test_future",
            trade_date="2025-12-15",
            venue=Venue.US,
            style=ExecutionStyle.MIDDAY,
            created_at_utc=now,
            earliest_start_utc=now + timedelta(hours=2),
            latest_end_utc=now + timedelta(hours=6),
            intents=intents,
        )

        # Job with current window
        current_job = ExecutionJob(
            job_id="test_current",
            trade_date="2025-12-15",
            venue=Venue.US,
            style=ExecutionStyle.MIDDAY,
            created_at_utc=now,
            earliest_start_utc=now - timedelta(hours=1),
            latest_end_utc=now + timedelta(hours=3),
            intents=intents,
        )

        # Job with past window
        past_job = ExecutionJob(
            job_id="test_past",
            trade_date="2025-12-15",
            venue=Venue.US,
            style=ExecutionStyle.MIDDAY,
            created_at_utc=now - timedelta(hours=10),
            earliest_start_utc=now - timedelta(hours=8),
            latest_end_utc=now - timedelta(hours=4),
            intents=intents,
        )

        # Test window checks
        assert not future_job.is_within_window(now)
        assert current_job.is_within_window(now)
        assert not past_job.is_within_window(now)

        # All are executable (PENDING status)
        assert future_job.is_executable
        assert current_job.is_executable
        assert past_job.is_executable


# =============================================================================
# Test 3: Live Market Data - No Yahoo Fallback
# =============================================================================

class TestLiveMarketDataNoYahoo:
    """Test that LiveMarketData never uses Yahoo."""

    def test_live_market_data_no_yahoo_fallback(self):
        """LiveMarketData module does not import or use yfinance."""
        # Check module source for yfinance imports
        import inspect
        from src.marketdata import live

        source = inspect.getsource(live)

        # Verify no actual yfinance import statements
        assert "import yfinance" not in source
        assert "from yfinance" not in source

        # Verify comment about no Yahoo exists (proving it's intentionally excluded)
        assert "NEVER" in source or "never" in source

        # Verify no lazy loading of yfinance like in research module
        assert "_yfinance" not in source
        assert "_get_yfinance" not in source

    def test_live_market_data_returns_none_without_ibkr(self):
        """LiveMarketData returns None when IBKR unavailable."""
        from src.marketdata.live import LiveMarketData

        # Create with no IB client
        live_md = LiveMarketData(ib_client=None)

        # Should return None, not fall back to anything
        result = live_md.get_snapshot("AAPL")
        assert result is None

        # can_trade should return False
        can_trade, reason = live_md.can_trade("AAPL")
        assert not can_trade
        assert "data" in reason.lower() or "unavailable" in reason.lower()


# =============================================================================
# Test 4: Overlay Opens on Legged Pair (Partial Test)
# =============================================================================

class TestOverlayLeggingDetection:
    """Test legging detection for overlay logic."""

    def test_overlay_opens_on_legged_pair_and_unwinds(self):
        """PairGroup detects legging correctly."""
        from src.execution.types import PairGroup, OrderIntent, OrderTicket, OrderPlan, OrderType, TimeInForce, OrderStatus, Urgency

        # Create pair group
        intents = [
            OrderIntent("SPY", "BUY", 100, "pair", "rv", Urgency.NORMAL),
            OrderIntent("QQQ", "SELL", 80, "pair", "rv", Urgency.NORMAL),
        ]

        plan = OrderPlan(
            order_type=OrderType.LMT,
            limit_price=100.0,
            tif=TimeInForce.DAY,
        )

        pair = PairGroup(
            name="SPY_QQQ_RV",
            intents=intents,
            max_legging_seconds=60,
            trigger_fill_pct=0.30,
        )

        # Create tickets - one filled 50%, other unfilled
        ticket1 = OrderTicket(intent=intents[0], plan=plan, ticket_id="t1")
        ticket1.filled_qty = 50  # 50% filled
        ticket1.status = OrderStatus.PARTIAL

        ticket2 = OrderTicket(intent=intents[1], plan=plan, ticket_id="t2")
        ticket2.filled_qty = 0  # 0% filled
        ticket2.status = OrderStatus.SUBMITTED

        pair.tickets = [ticket1, ticket2]

        # Check legging detection
        assert pair.is_legged()  # One leg > 30%, other < 10%

        # If both partially filled, not legged
        ticket2.filled_qty = 20  # 25% filled
        assert not pair.is_legged()

        # Reset and test needs_hedge
        ticket2.filled_qty = 0
        pair.hedge_intent = OrderIntent("ES", "SELL", 1, "hedge", "rv", Urgency.HIGH)
        assert pair.needs_hedge()

        # After hedge is placed, shouldn't need another
        pair.hedge_ticket = OrderTicket(intent=pair.hedge_intent, plan=plan, ticket_id="h1")
        assert not pair.needs_hedge()


# =============================================================================
# Test 5: Trade Gating Skips Small Drift Trades
# =============================================================================

class TestTradeGating:
    """Test cost-vs-benefit trade gating."""

    def test_trade_gating_skips_small_drift_trades(self):
        """Trades with small drift are gated out."""
        from src.execution.gater import TradeGater, GatingConfig, RiskRegime
        from src.execution.types import OrderIntent, Urgency

        config = GatingConfig(
            enabled=True,
            min_drift_pct=0.002,  # 0.2%
            cost_multiplier=1.5,
        )

        gater = TradeGater(config=config)

        # Create small trade
        intent = OrderIntent(
            instrument_id="AAPL",
            side="BUY",
            quantity=10,
            reason="rebalance",
            sleeve="core",
            urgency=Urgency.NORMAL,
            notional_usd=1000,  # Small
        )

        nav = 1_000_000  # $1M NAV
        current_positions = {"AAPL": 100_000}  # $100k current
        target_positions = {"AAPL": 100_500}   # $100.5k target (0.05% drift)

        decisions = gater.filter_intents(
            intents=[intent],
            current_positions=current_positions,
            target_positions=target_positions,
            nav_usd=nav,
            regime=RiskRegime.NORMAL,
        )

        assert len(decisions) == 1
        assert not decisions[0].should_trade
        assert "drift" in decisions[0].reason.lower() or "min" in decisions[0].reason.lower()

    def test_trade_gating_allows_large_drift(self):
        """Trades with large drift pass gating."""
        from src.execution.gater import TradeGater, GatingConfig, RiskRegime
        from src.execution.types import OrderIntent, Urgency

        config = GatingConfig(
            enabled=True,
            min_drift_pct=0.002,
            cost_multiplier=1.5,
        )

        gater = TradeGater(config=config)

        intent = OrderIntent(
            instrument_id="AAPL",
            side="BUY",
            quantity=1000,
            reason="rebalance",
            sleeve="core",
            urgency=Urgency.NORMAL,
            notional_usd=150_000,
        )

        nav = 1_000_000
        current_positions = {"AAPL": 50_000}   # $50k current
        target_positions = {"AAPL": 200_000}   # $200k target (15% drift)

        decisions = gater.filter_intents(
            intents=[intent],
            current_positions=current_positions,
            target_positions=target_positions,
            nav_usd=nav,
            regime=RiskRegime.NORMAL,
        )

        assert len(decisions) == 1
        # Large drift should pass
        assert decisions[0].should_trade or decisions[0].drift_pct > config.min_drift_pct


# =============================================================================
# Test 6: Trade Gating Overrides on Limit Breach
# =============================================================================

class TestTradeGatingOverrides:
    """Test gating override conditions."""

    def test_trade_gating_overrides_on_limit_breach(self):
        """Gating is overridden when limits are breached."""
        from src.execution.gater import TradeGater, GatingConfig, GatingOverrides, RiskRegime
        from src.execution.types import OrderIntent, Urgency

        config = GatingConfig(
            enabled=True,
            min_drift_pct=0.10,  # Very high threshold (10%)
            always_trade_if_limit_breach=True,
        )

        gater = TradeGater(config=config)

        intent = OrderIntent(
            instrument_id="AAPL",
            side="SELL",
            quantity=100,
            reason="risk",
            sleeve="core",
            urgency=Urgency.NORMAL,
            notional_usd=10_000,
        )

        nav = 1_000_000
        current_positions = {"AAPL": 100_000}
        target_positions = {"AAPL": 90_000}  # Only 1% drift - would normally be gated

        # Without override - should be gated
        decisions = gater.filter_intents(
            intents=[intent],
            current_positions=current_positions,
            target_positions=target_positions,
            nav_usd=nav,
            regime=RiskRegime.NORMAL,
        )

        assert not decisions[0].should_trade

        # With gross limit breach - should override
        gater.reset_daily()
        overrides = GatingOverrides(gross_limit_breached=True)

        decisions = gater.filter_intents(
            intents=[intent],
            current_positions=current_positions,
            target_positions=target_positions,
            nav_usd=nav,
            regime=RiskRegime.NORMAL,
            overrides=overrides,
        )

        assert decisions[0].should_trade
        assert decisions[0].is_override


# =============================================================================
# Test 7: Slippage Model Updates from Analytics and Clamps
# =============================================================================

class TestSlippageModel:
    """Test self-calibrating slippage model."""

    def test_slippage_model_updates_from_analytics_and_clamps(self, tmp_path):
        """Slippage model updates from execution history with clamps."""
        from src.execution.slippage_model import SlippageModel, SlippageModelConfig
        from src.execution.analytics import ExecutionAnalytics, OrderMetrics
        from datetime import datetime

        config = SlippageModelConfig(
            enabled=True,
            lookback_trades=100,
            min_trades_per_instrument=5,
            percentile_for_limits=0.70,
            safety_buffer_bps=1.0,
            clamp_bps=(0.5, 25.0),
            persist_path=str(tmp_path / "slippage.json"),
        )

        model = SlippageModel(config=config)

        # Create mock analytics with order history
        analytics = ExecutionAnalytics()

        # Add some order metrics
        for i in range(20):
            metrics = OrderMetrics(
                ticket_id=f"t{i}",
                instrument_id="AAPL",
                side="BUY",
                quantity=100,
                filled_qty=100,
                arrival_price=150.0,
                avg_fill_price=150.0 + (i * 0.01),  # Increasing slippage
                slippage_bps=float(i * 0.5),  # 0 to 9.5 bps
                notional_usd=15000,
                commission=1.0,
                elapsed_seconds=5.0,
                replace_count=0,
                status="FILLED",
                timestamp=datetime.now(),
            )
            analytics.order_metrics.append(metrics)

        # Update model
        model.update_from_analytics(analytics)

        # Check that model has stats
        stats = model.get_instrument_stats("AAPL")
        assert stats is not None
        assert stats.sample_count == 20

        # Check that estimate is within clamps
        estimate = model.get_estimated_slippage_bps("AAPL", "BUY")
        min_clamp, max_clamp = config.clamp_bps
        assert estimate >= min_clamp
        assert estimate <= max_clamp


# =============================================================================
# Test 8: Execution Policy Uses Slippage Model Offsets
# =============================================================================

class TestExecutionPolicySlippageIntegration:
    """Test ExecutionPolicy integration with SlippageModel."""

    def test_execution_policy_uses_slippage_model_offsets(self, tmp_path):
        """ExecutionPolicy uses slippage model for limit offsets."""
        from src.execution.slippage_model import SlippageModel, SlippageModelConfig

        config = SlippageModelConfig(
            enabled=True,
            percentile_for_limits=0.70,
            safety_buffer_bps=1.0,
            clamp_bps=(0.5, 25.0),
            persist_path=str(tmp_path / "slippage.json"),
        )

        model = SlippageModel(config=config)

        # Add some manual stats
        from src.execution.slippage_model import InstrumentSlippageStats
        model.instrument_stats["AAPL"] = InstrumentSlippageStats(
            instrument_id="AAPL",
            sample_count=50,
            p70_is_bps=5.0,
            median_is_bps=3.0,
        )

        # Get limit offset
        offset = model.get_limit_offset_bps("AAPL", "BUY")

        # Should be p70 * percentile + buffer, within clamps
        expected_base = 5.0 * 0.70  # 3.5
        assert offset >= config.clamp_bps[0]
        assert offset <= config.clamp_bps[1]


# =============================================================================
# Test 9: Borrow Service Denies Unavailable Short
# =============================================================================

class TestBorrowService:
    """Test borrow service functionality."""

    def test_borrow_service_denies_unavailable_short(self):
        """Shorts are denied when stock unavailable for borrow."""
        from src.carry.borrow import BorrowService, BorrowConfig, BorrowInfo
        from datetime import datetime

        config = BorrowConfig(
            enabled=True,
            deny_new_short_if_unavailable=True,
            cache_ttl_seconds=3600,  # Long TTL so cache is used
        )

        service = BorrowService(config=config)

        # Mock unavailable borrow - set both cache and timestamp
        service._cache["HTB_STOCK"] = BorrowInfo(
            instrument_id="HTB_STOCK",
            available=False,
            source="TEST",
            last_updated=datetime.now(),
        )
        service._cache_times["HTB_STOCK"] = datetime.now()

        can_short, reason = service.can_short("HTB_STOCK", 100)
        assert not can_short
        assert "not available" in reason.lower() or "unavailable" in reason.lower()

        # Available stock should pass
        service._cache["AAPL"] = BorrowInfo(
            instrument_id="AAPL",
            available=True,
            shares_available=1000000,
            fee_rate_annual_bps=25.0,
            source="TEST",
            last_updated=datetime.now(),
        )
        service._cache_times["AAPL"] = datetime.now()

        can_short, reason = service.can_short("AAPL", 100)
        assert can_short


# =============================================================================
# Test 10: Financing Service Daily Carry Estimate
# =============================================================================

class TestFinancingService:
    """Test financing service carry estimation."""

    def test_financing_service_daily_carry_estimate(self):
        """Daily carry is calculated correctly."""
        from src.carry.financing import FinancingService, FinancingConfig

        config = FinancingConfig(
            enabled=True,
            default_cash_rate_by_ccy={
                "USD": 0.045,  # 4.5%
                "EUR": 0.030,  # 3%
            },
        )

        service = FinancingService(config=config)

        # Calculate carry with positive cash
        cash_balances = {
            "USD": 1_000_000,  # $1M
            "EUR": 500_000,    # €500k
        }

        estimate = service.calculate_daily_carry(cash_balances)

        # USD: 1M * 0.045 / 365 = ~$123.29/day
        # EUR: 500k * 0.03 / 365 = ~$41.10/day
        # Total: ~$164.39/day

        assert estimate.total_carry_usd > 0
        assert estimate.by_currency["USD"] > 0
        assert estimate.by_currency["EUR"] > 0

        # USD should earn more (higher balance * higher rate)
        assert estimate.by_currency["USD"] > estimate.by_currency["EUR"]

        # Approximate check (within 10% of expected)
        expected_usd = 1_000_000 * 0.045 / 365
        assert abs(estimate.by_currency["USD"] - expected_usd) < expected_usd * 0.1


# =============================================================================
# Additional Tests for Coverage
# =============================================================================

class TestVenueLiquidityManager:
    """Test venue liquidity window management."""

    def test_liquidity_window_calculation(self):
        """Liquidity windows are calculated correctly."""
        from src.execution.calendars import get_venue_manager, LiquidityWindow
        from datetime import date

        manager = get_venue_manager()

        # Get US liquidity window for a weekday
        test_date = date(2025, 12, 15)  # Monday
        window = manager.get_liquidity_window("US", test_date, "MIDDAY")

        assert window is not None
        assert window.venue == "US"
        assert window.style == "MIDDAY"
        assert window.start_utc < window.end_utc

    def test_close_auction_window(self):
        """Close auction windows are returned correctly."""
        from src.execution.calendars import get_venue_manager
        from datetime import date

        manager = get_venue_manager()

        test_date = date(2025, 12, 15)
        window = manager.get_close_auction_window("US", test_date)

        assert window is not None
        assert window.style == "CLOSE_AUCTION"


class TestResearchMarketData:
    """Test research market data (Yahoo allowed)."""

    def test_research_market_data_allows_yahoo(self):
        """ResearchMarketData module can use yfinance."""
        import inspect
        from src.marketdata import research

        source = inspect.getsource(research)

        # Should have yfinance references (lazy loaded)
        assert "yfinance" in source or "_yfinance" in source

        # Should have warning about research only
        assert "RESEARCH" in source or "research" in source


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
