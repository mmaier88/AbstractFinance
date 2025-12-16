"""
Execution package - Stateful execution layer for alpha capture.

This package provides:
- ExecutionPolicy: Order type selection and parameterization
- OrderManager: State machine for order lifecycle management
- BasketExecutor: Trade netting and priority ordering
- PairExecutor: Legging protection for paired trades
- ExecutionAnalytics: Slippage and cost tracking

Phase 2 Enhancements:
- ExecutionJob: Session-aware job scheduling
- ExecutionJobStore: Persistent job management
- TradeGater: Cost-vs-benefit trade filtering
- SlippageModel: Self-calibrating slippage estimation
- VenueLiquidityManager: Venue-specific trading windows

Key design principles:
1. No market orders unless explicitly allowed
2. Hard price collars on all orders
3. Time-to-live with cancel/replace logic
4. Trade netting across sleeves
5. Measurable execution metrics
6. Session-aware execution windows (Phase 2)
7. Cost-vs-benefit trade gating (Phase 2)
"""

from .types import (
    MarketDataSnapshot,
    OrderIntent,
    OrderPlan,
    OrderTicket,
    ExecutionResult,
    PairGroup,
)
from .policy import ExecutionPolicy
from .order_manager import OrderManager
from .basket import BasketExecutor
from .pair import PairExecutor
from .slippage import compute_slippage_bps
from .analytics import ExecutionAnalytics
from .calendars import (
    MarketCalendar,
    is_market_open,
    get_session_phase,
    should_avoid_trading,
    # Phase 2: Venue liquidity
    VenueLiquidityManager,
    LiquidityWindow,
    get_venue_manager,
    get_liquidity_window,
    is_within_liquidity_window,
)

# Phase 2 imports
from .jobs import (
    ExecutionJob,
    ExecutionJobStore,
    Venue,
    ExecutionStyle,
    JobStatus,
    get_job_store,
    generate_job_id,
)
from .gater import (
    TradeGater,
    GatingConfig,
    GatingDecision,
    GatingOverrides,
    RiskRegime,
    get_trade_gater,
)
from .slippage_model import (
    SlippageModel,
    SlippageModelConfig,
    get_slippage_model,
)

__all__ = [
    # Types
    "MarketDataSnapshot",
    "OrderIntent",
    "OrderPlan",
    "OrderTicket",
    "ExecutionResult",
    "PairGroup",
    # Core components
    "ExecutionPolicy",
    "OrderManager",
    "BasketExecutor",
    "PairExecutor",
    "ExecutionAnalytics",
    "MarketCalendar",
    # Utilities
    "compute_slippage_bps",
    "is_market_open",
    "get_session_phase",
    "should_avoid_trading",
    # Phase 2: Venue liquidity
    "VenueLiquidityManager",
    "LiquidityWindow",
    "get_venue_manager",
    "get_liquidity_window",
    "is_within_liquidity_window",
    # Phase 2: Jobs
    "ExecutionJob",
    "ExecutionJobStore",
    "Venue",
    "ExecutionStyle",
    "JobStatus",
    "get_job_store",
    "generate_job_id",
    # Phase 2: Gating
    "TradeGater",
    "GatingConfig",
    "GatingDecision",
    "GatingOverrides",
    "RiskRegime",
    "get_trade_gater",
    # Phase 2: Slippage model
    "SlippageModel",
    "SlippageModelConfig",
    "get_slippage_model",
]
