# Execution Phase 2 - Production Alpha Upgrades

**Date:** December 2025
**Scope:** Session-aware scheduling, live market data separation, cost gating, self-tuning slippage, carry realism

---

## Overview

Phase 2 upgrades the execution stack to production-ready status with:
- Session-aware job scheduling (compute early, execute in liquidity windows)
- Hard separation of live (IBKR-only) vs research (Yahoo allowed) market data
- Cost-vs-benefit trade gating ("no-trade zones")
- Self-calibrating slippage model from execution history
- Borrow/dividend/financing hooks for realistic cost estimation

---

## New Modules Created

### Execution Package Extensions

#### `src/execution/jobs.py`
Session-aware execution job management:
- `ExecutionJob` - Scheduled job with venue, style, and time window
- `ExecutionJobStore` - Persistent JSON storage with idempotency
- `Venue` enum - EU, US, FX, FUT
- `ExecutionStyle` enum - MIDDAY, CLOSE_AUCTION, OPEN_AUCTION
- `JobStatus` enum - PENDING, RUNNING, DONE, FAILED, CANCELED, SKIPPED
- `generate_job_id()` - Deterministic job ID generation

#### `src/execution/gater.py`
Cost-vs-benefit trade filtering:
- `TradeGater` - Filters trades based on predicted costs vs benefits
- `GatingConfig` - Configuration with regime multipliers
- `GatingDecision` - Result with should_trade, reason, costs
- `GatingOverrides` - Override conditions (limit breach, emergency, etc.)
- `RiskRegime` enum - NORMAL, ELEVATED, CRISIS

#### `src/execution/slippage_model.py`
Self-calibrating slippage estimation:
- `SlippageModel` - Updates from execution history
- `SlippageModelConfig` - Lookback, percentiles, clamps
- `InstrumentSlippageStats` - Per-instrument statistics
- `AssetClassSlippageStats` - Per-asset-class statistics
- Persists to `state/slippage_model.json`

#### `src/execution/calendars.py` (Extended)
Venue-based liquidity windows:
- `VenueLiquidityManager` - Manages venue trading windows
- `VenueConfig` - Venue-specific configuration
- `LiquidityWindow` - Time window with start/end UTC
- `get_liquidity_window()` - Get window for venue/date/style
- `is_within_liquidity_window()` - Check if time is in window

### Market Data Package

#### `src/marketdata/__init__.py`
Package initialization with live/research separation.

#### `src/marketdata/live.py`
IBKR-only live market data:
- `LiveMarketData` - IBKR data with NO Yahoo fallback
- `QuoteQualityConfig` - Bid/ask requirements, staleness
- Returns None if data unavailable (NO FALLBACK)
- CRITICAL: No yfinance imports allowed

#### `src/marketdata/research.py`
Yahoo-allowed research data:
- `ResearchMarketData` - Yahoo/yfinance for backtests
- Historical data, fundamentals, dividends
- WARNING: Research only, never for live trading

### Carry Services Package

#### `src/carry/__init__.py`
Package initialization with all services.

#### `src/carry/borrow.py`
Stock borrow service:
- `BorrowService` - Borrow availability and fees
- `BorrowInfo` - Availability, shares, fee rate
- `BorrowConfig` - Denial policy, default fees
- IBKR integration with conservative fallbacks

#### `src/carry/corporate_actions.py`
Dividend and ex-div tracking:
- `CorporateActionsService` - Dividend awareness
- `DividendInfo` - Ex-div date, yield, frequency
- `DividendConfig` - Warning days, buffers
- Warns about shorts near ex-div

#### `src/carry/financing.py`
Cash carry estimation:
- `FinancingService` - Daily carry calculation
- `CarryEstimate` - Breakdown by currency
- `FinancingConfig` - Interest rates by currency
- Tracks borrow costs and dividend exposure

---

## Configuration Added

Added `execution_phase2` section to `config/settings.yaml`:

```yaml
execution_phase2:
  enabled: true

  session_scheduler:
    enabled: true
    precompute_time_utc: "06:00"
    poll_interval_seconds: 30
    job_persistence_path: "state/execution_jobs.json"
    venues:
      EU: { timezone: "Europe/Berlin", open_offset_minutes: 15, ... }
      US: { timezone: "America/New_York", ... }
      FX: { timezone: "UTC", avoid_window_utc: ["21:55", "22:10"], ... }
      FUT: { timezone: "UTC", ... }

  market_data:
    live_provider: "ibkr_only"
    allow_yahoo_in_live: false
    live_snapshot_timeout_ms: 1500
    min_quote_quality:
      require_bid_ask_for_limit_pricing: true
      max_mid_staleness_seconds: 5

  overlays:
    enabled: true
    max_unhedged_seconds: 60
    overlay_notional_cap_pct_nav: 0.10
    overlay_proxies: { US_EQUITY: "ES", EU_EQUITY: "FESX", ... }

  gating:
    enabled: true
    min_drift_pct: 0.002
    cost_multiplier: 1.5
    always_trade_if_limit_breach: true
    regime_multipliers: { NORMAL: 1.0, ELEVATED: 1.5, CRISIS: 2.5 }

  slippage_model:
    enabled: true
    lookback_trades: 200
    min_trades_per_instrument: 15
    percentile_for_limits: 0.70
    safety_buffer_bps: 1.0
    clamp_bps: [0.5, 25.0]
    persist_path: "state/slippage_model.json"

  carry_realism:
    enabled: true
    borrow:
      deny_new_short_if_unavailable: true
      default_borrow_fee_bps_annual: 150.0
    dividends:
      warn_on_upcoming_ex_div_days: 3
      default_short_dividend_buffer_bps: 5.0
    financing:
      default_cash_rate_by_ccy: { USD: 0.045, EUR: 0.030, ... }
```

---

## Tests Added

Created `tests/test_execution_phase2.py` with 10+ tests:

1. `test_session_scheduler_creates_jobs_and_persists`
2. `test_session_scheduler_executes_only_in_window`
3. `test_live_market_data_no_yahoo_fallback`
4. `test_overlay_opens_on_legged_pair_and_unwinds`
5. `test_trade_gating_skips_small_drift_trades`
6. `test_trade_gating_overrides_on_limit_breach`
7. `test_slippage_model_updates_from_analytics_and_clamps`
8. `test_execution_policy_uses_slippage_model_offsets`
9. `test_borrow_service_denies_unavailable_short`
10. `test_financing_service_daily_carry_estimate`

Additional coverage tests:
- `test_liquidity_window_calculation`
- `test_close_auction_window`
- `test_research_market_data_allows_yahoo`
- `test_trade_gating_allows_large_drift`

---

## Key Design Decisions

### 1. Hard Live/Research Data Separation
- `LiveMarketData` module has NO yfinance imports
- Returns None if IBKR data unavailable - NO FALLBACK
- `ResearchMarketData` can use Yahoo for backtests only

### 2. Session-Aware Job Scheduling
- Jobs created at precompute time (06:00 UTC)
- Execution deferred to venue liquidity windows
- Persistent storage for restart recovery
- Idempotent job creation (same inputs = same job ID)

### 3. Cost-vs-Benefit Gating
- Requires `benefit_usd >= cost_multiplier * predicted_cost_usd`
- Stricter in ELEVATED/CRISIS regimes (1.5x, 2.5x multipliers)
- Overrides for: limit breaches, emergency de-risk, hedge corrections

### 4. Self-Calibrating Slippage
- Updates from `ExecutionAnalytics` daily
- Uses p70 implementation shortfall as baseline
- Clamps to [0.5, 25.0] bps to avoid extremes
- Falls back to asset class when instrument has < 15 samples

### 5. Conservative Carry Defaults
- Borrow: 150 bps annual default
- Short dividend buffer: 5 bps near ex-div
- Cash rates: USD 4.5%, EUR 3%, GBP 4.5%

---

## Critical Invariants

1. **Live trading NEVER uses Yahoo** - `LiveMarketData` returns None
2. **No market orders** - All orders are marketable limits with collars
3. **Job idempotency** - Same inputs generate same job ID
4. **Overlay tracking** - All overlays recorded and must be unwound
5. **Risk overrides** - Gating bypassed on limit breaches

---

## Remaining Work

### Scheduler Integration
- Wire `ExecutionJobStore` into scheduler loop
- Implement precompute phase at 06:00 UTC
- Add job polling and execution in windows

### Full Overlay Implementation
- Complete legging detection in `PairExecutor`
- Implement temporary hedge submission
- Add overlay unwind on fill completion

### Telemetry Updates
- Add Phase 2 metrics to Prometheus
- Update Telegram daily summary
- Add gating/carry statistics

---

## File Summary

| File | Lines | Description |
|------|-------|-------------|
| `src/execution/jobs.py` | ~380 | Job scheduling and persistence |
| `src/execution/gater.py` | ~340 | Cost-vs-benefit trade filtering |
| `src/execution/slippage_model.py` | ~340 | Self-calibrating slippage |
| `src/execution/calendars.py` | +350 | Venue liquidity windows |
| `src/marketdata/live.py` | ~250 | IBKR-only market data |
| `src/marketdata/research.py` | ~220 | Yahoo research data |
| `src/carry/borrow.py` | ~240 | Borrow service |
| `src/carry/corporate_actions.py` | ~220 | Dividend service |
| `src/carry/financing.py` | ~200 | Financing service |
| `tests/test_execution_phase2.py` | ~450 | Phase 2 tests |
| `config/settings.yaml` | +130 | Phase 2 configuration |

**Total new code:** ~3,120 lines

---

## Next Steps

1. Complete scheduler integration with job execution
2. Implement full overlay logic with unwind
3. Add comprehensive telemetry reporting
4. Production testing with paper trading
5. Monitor and tune slippage model parameters
