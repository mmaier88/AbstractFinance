"""
Multi-sleeve strategy construction for AbstractFinance.
Implements the European Decline Macro strategy across all sleeves.

ENGINE_FIX_PLAN Updates:
- Phase 4: Currency-correct position sizing
- Phase 5: Portfolio-level FX hedging (central FX book)
"""

import pandas as pd
import numpy as np
from datetime import datetime, date
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from enum import Enum

from .portfolio import PortfolioState, Sleeve, Position, InstrumentType
from .risk_engine import RiskEngine, RiskDecision, RiskRegime
from .data_feeds import DataFeed
from .stock_screener import run_screening, get_default_us_universe, get_default_eu_universe
from .logging_utils import get_trading_logger
from .fx_rates import (
    FXRates, BASE_CCY, get_fx_rates,
    compute_net_fx_exposure, compute_fx_hedge_quantities
)


# =============================================================================
# STRATEGY EVOLUTION: Trend Filter for Core RV Gating
# Prevents bleeding during EU outperformance periods
# =============================================================================

@dataclass
class TrendFilterConfig:
    """Configuration for US/EU relative trend filter."""
    enabled: bool = True
    short_lookback_days: int = 60       # 3-month momentum
    long_lookback_days: int = 252       # 12-month momentum
    positive_threshold: float = 0.02    # +2% = full size
    negative_threshold: float = -0.05   # -5% = cut to 25%
    options_only_threshold: float = -0.10  # -10% = switch to options only
    full_size_multiplier: float = 1.0
    reduced_size_multiplier: float = 0.25

    @classmethod
    def from_settings(cls, settings: Dict[str, Any]) -> "TrendFilterConfig":
        """Create TrendFilterConfig from settings dict."""
        tf_settings = settings.get('trend_filter', {})
        return cls(
            enabled=tf_settings.get('enabled', True),
            short_lookback_days=tf_settings.get('short_lookback_days', 60),
            long_lookback_days=tf_settings.get('long_lookback_days', 252),
            positive_threshold=tf_settings.get('positive_momentum_threshold', 0.02),
            negative_threshold=tf_settings.get('negative_momentum_threshold', -0.05),
            options_only_threshold=tf_settings.get('options_only_threshold', -0.10),
            full_size_multiplier=tf_settings.get('full_size_multiplier', 1.0),
            reduced_size_multiplier=tf_settings.get('reduced_size_multiplier', 0.25),
        )


@dataclass
class TrendFilterResult:
    """Result of trend filter analysis."""
    us_eu_momentum_short: float     # 3-month relative momentum
    us_eu_momentum_long: float      # 12-month relative momentum
    combined_momentum: float        # Weighted average
    sizing_multiplier: float        # 0.0 to 1.0
    use_options_only: bool          # Switch to options-only mode
    commentary: str


class TrendFilter:
    """
    US/EU Relative Trend Filter.

    Prevents the strategy from bleeding during periods when Europe
    outperforms the US (which does happen cyclically).

    When US/EU momentum turns negative:
    - Reduce core RV and single name sizing
    - At extreme negative, switch to options-only protection
    """

    def __init__(self, config: TrendFilterConfig):
        self.config = config

    def compute_momentum(
        self,
        us_prices: pd.Series,
        eu_prices: pd.Series,
        lookback_days: int
    ) -> float:
        """
        Compute US vs EU relative momentum.

        Returns:
            Positive = US outperforming (thesis working)
            Negative = EU outperforming (thesis not working)
        """
        if len(us_prices) < lookback_days or len(eu_prices) < lookback_days:
            return 0.0

        # Compute returns over lookback
        us_return = (us_prices.iloc[-1] / us_prices.iloc[-lookback_days]) - 1
        eu_return = (eu_prices.iloc[-1] / eu_prices.iloc[-lookback_days]) - 1

        # Relative momentum = US return - EU return
        return us_return - eu_return

    def analyze(
        self,
        data_feed: "DataFeed",
        us_symbol: str = "CSPX",
        eu_symbol: str = "CS51"
    ) -> TrendFilterResult:
        """
        Analyze US/EU trend and determine position sizing.

        Args:
            data_feed: Data feed for price history
            us_symbol: US index symbol
            eu_symbol: EU index symbol

        Returns:
            TrendFilterResult with sizing recommendation
        """
        if not self.config.enabled:
            return TrendFilterResult(
                us_eu_momentum_short=0.0,
                us_eu_momentum_long=0.0,
                combined_momentum=0.0,
                sizing_multiplier=1.0,
                use_options_only=False,
                commentary="Trend filter disabled"
            )

        try:
            # Get price history
            us_prices = data_feed.get_price_history(us_symbol, self.config.long_lookback_days + 10)
            eu_prices = data_feed.get_price_history(eu_symbol, self.config.long_lookback_days + 10)

            # Compute short and long momentum
            momentum_short = self.compute_momentum(
                us_prices, eu_prices, self.config.short_lookback_days
            )
            momentum_long = self.compute_momentum(
                us_prices, eu_prices, self.config.long_lookback_days
            )

            # Combined momentum (weight short-term more heavily)
            combined = 0.6 * momentum_short + 0.4 * momentum_long

        except Exception as e:
            # If data unavailable, assume neutral
            return TrendFilterResult(
                us_eu_momentum_short=0.0,
                us_eu_momentum_long=0.0,
                combined_momentum=0.0,
                sizing_multiplier=1.0,
                use_options_only=False,
                commentary=f"Trend filter data unavailable: {e}"
            )

        # Determine sizing multiplier
        if combined >= self.config.positive_threshold:
            # Thesis working well - full size
            multiplier = self.config.full_size_multiplier
            options_only = False
            commentary = f"Trend positive ({combined:+.1%}): full size"

        elif combined <= self.config.options_only_threshold:
            # Thesis very wrong - switch to options only
            multiplier = 0.0
            options_only = True
            commentary = f"Trend very negative ({combined:+.1%}): options only"

        elif combined <= self.config.negative_threshold:
            # Thesis not working - reduce size
            multiplier = self.config.reduced_size_multiplier
            options_only = False
            commentary = f"Trend negative ({combined:+.1%}): reduced to {multiplier:.0%}"

        else:
            # Neutral zone - interpolate
            # Map from [negative_threshold, positive_threshold] to [reduced, full]
            range_size = self.config.positive_threshold - self.config.negative_threshold
            position_in_range = (combined - self.config.negative_threshold) / range_size
            multiplier = (
                self.config.reduced_size_multiplier +
                position_in_range * (self.config.full_size_multiplier - self.config.reduced_size_multiplier)
            )
            options_only = False
            commentary = f"Trend neutral ({combined:+.1%}): size {multiplier:.0%}"

        return TrendFilterResult(
            us_eu_momentum_short=momentum_short,
            us_eu_momentum_long=momentum_long,
            combined_momentum=combined,
            sizing_multiplier=multiplier,
            use_options_only=options_only,
            commentary=commentary
        )


# =============================================================================
# ROADMAP Phase C: FX Hedge Policy Modes
# =============================================================================

class FXHedgeMode(Enum):
    """FX hedge policy modes."""
    FULL = "FULL"        # Hedge to < 2% residual FX exposure
    PARTIAL = "PARTIAL"  # Hedge to ~25% residual (let some USD exposure through)
    NONE = "NONE"        # No FX hedging (full USD exposure payoff)


@dataclass
class FXHedgePolicy:
    """
    FX hedge policy configuration.

    ROADMAP Phase C: Supports FULL/PARTIAL/NONE modes with regime overrides.
    """
    mode: FXHedgeMode = FXHedgeMode.PARTIAL

    # Target residual FX exposure as % of NAV
    target_residual_pct: Dict[FXHedgeMode, float] = field(default_factory=lambda: {
        FXHedgeMode.FULL: 0.02,     # 2% max residual
        FXHedgeMode.PARTIAL: 0.25,  # 25% max residual
        FXHedgeMode.NONE: 1.00,     # No hedge
    })

    # Regime-based mode overrides
    regime_overrides: Dict[str, FXHedgeMode] = field(default_factory=lambda: {
        "NORMAL": FXHedgeMode.PARTIAL,
        "ELEVATED": FXHedgeMode.PARTIAL,
        "CRISIS": FXHedgeMode.NONE,  # Let USD exposure pay off in crisis
    })

    @classmethod
    def from_settings(cls, settings: Dict[str, Any]) -> "FXHedgePolicy":
        """Create FXHedgePolicy from settings dict."""
        fx_settings = settings.get('fx_hedge', {})

        mode_str = fx_settings.get('mode', 'PARTIAL')
        mode = FXHedgeMode[mode_str] if isinstance(mode_str, str) else FXHedgeMode.PARTIAL

        target_residual = fx_settings.get('target_residual_pct_nav', {})
        target_residual_pct = {
            FXHedgeMode.FULL: target_residual.get('FULL', 0.02),
            FXHedgeMode.PARTIAL: target_residual.get('PARTIAL', 0.25),
            FXHedgeMode.NONE: target_residual.get('NONE', 1.00),
        }

        regime_override_strs = fx_settings.get('regime_overrides', {})
        regime_overrides = {}
        for regime, mode_str in regime_override_strs.items():
            try:
                regime_overrides[regime.upper()] = FXHedgeMode[mode_str.upper()]
            except (KeyError, AttributeError):
                pass

        # Fill defaults for missing regimes
        for regime in ["NORMAL", "ELEVATED", "CRISIS", "RECOVERY"]:
            if regime not in regime_overrides:
                regime_overrides[regime] = FXHedgeMode.PARTIAL

        return cls(
            mode=mode,
            target_residual_pct=target_residual_pct,
            regime_overrides=regime_overrides,
        )

    def get_effective_mode(self, regime: str) -> FXHedgeMode:
        """Get effective FX hedge mode considering regime override."""
        return self.regime_overrides.get(regime.upper(), self.mode)

    def get_hedge_ratio(self, regime: str) -> float:
        """
        Get FX hedge ratio for current regime.

        Returns:
            Hedge ratio (0.0 = no hedge, 1.0 = full hedge)
        """
        effective_mode = self.get_effective_mode(regime)
        target_residual = self.target_residual_pct.get(effective_mode, 0.25)

        # Convert residual target to hedge ratio
        # If target_residual = 0.02 (2%), we hedge 98% -> ratio = 0.98
        # If target_residual = 0.25 (25%), we hedge 75% -> ratio = 0.75
        # If target_residual = 1.00 (100%), we hedge 0% -> ratio = 0.0
        return max(0.0, 1.0 - target_residual)


@dataclass
class OrderSpec:
    """Specification for a trade order."""
    instrument_id: str
    side: str  # "BUY" or "SELL"
    quantity: float
    order_type: str = "MKT"  # "MKT", "LMT", "STP"
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    sleeve: Sleeve = Sleeve.CORE_INDEX_RV
    reason: str = ""
    urgency: str = "normal"  # "normal", "urgent", "immediate"


@dataclass
class SleeveTargets:
    """Target positions for a single sleeve."""
    sleeve: Sleeve
    target_positions: Dict[str, float]  # instrument_id -> target quantity
    target_notional: float
    target_weight: float
    long_notional: float = 0.0
    short_notional: float = 0.0


@dataclass
class StrategyOutput:
    """Complete strategy output with all sleeve targets."""
    sleeve_targets: Dict[Sleeve, SleeveTargets]
    total_target_positions: Dict[str, float]
    orders: List[OrderSpec]
    scaling_factor: float
    regime: RiskRegime
    commentary: str


class Strategy:
    """
    Main strategy class implementing multi-sleeve European Decline Macro.
    """

    def __init__(
        self,
        settings: Dict[str, Any],
        instruments_config: Dict[str, Any],
        risk_engine: RiskEngine,
        tail_hedge_manager: Optional[Any] = None
    ):
        """
        Initialize strategy.

        Args:
            settings: Application settings
            instruments_config: Instrument configurations
            risk_engine: Risk engine instance
            tail_hedge_manager: Optional tail hedge manager
        """
        self.settings = settings
        self.instruments = instruments_config
        self.risk_engine = risk_engine
        self.tail_hedge_manager = tail_hedge_manager

        # Extract sleeve weights from settings
        sleeve_settings = settings.get('sleeves', {})
        self.sleeve_weights = {
            Sleeve.CORE_INDEX_RV: sleeve_settings.get('core_index_rv', 0.35),
            Sleeve.SECTOR_RV: sleeve_settings.get('sector_rv', 0.25),
            Sleeve.SINGLE_NAME: sleeve_settings.get('single_name', 0.15),
            Sleeve.CREDIT_CARRY: sleeve_settings.get('credit_carry', 0.15),
            Sleeve.CRISIS_ALPHA: sleeve_settings.get('crisis_alpha', 0.05),
            Sleeve.CASH_BUFFER: sleeve_settings.get('cash_buffer', 0.05)
        }

        # ROADMAP Phase C: FX Hedge Policy
        self.fx_hedge_policy = FXHedgePolicy.from_settings(settings)

        # STRATEGY EVOLUTION: Trend Filter
        trend_config = TrendFilterConfig.from_settings(settings)
        self.trend_filter = TrendFilter(trend_config)

        # Track last trend filter result for reporting
        self.last_trend_result: Optional[TrendFilterResult] = None

        # Instrument mappings
        self._build_instrument_mappings()

    def _build_instrument_mappings(self) -> None:
        """Build mappings from config to instrument specs."""
        self.us_index = {
            'future': 'us_index_future',
            'etf': 'us_index_etf',
            'micro': 'us_index_micro'
        }
        self.eu_index = {
            'future': 'eu_index_future',
            'etf': 'eu_index_etf'
        }
        self.fx_hedge = {
            'future': 'eurusd_future',
            'micro': 'eurusd_micro',
            'spot': 'eurusd_spot'
        }

        # US sector ETFs for long
        self.us_sectors = ['tech_xlk', 'tech_qqq', 'tech_igv', 'tech_smh',
                          'health_xlv', 'health_xbi', 'health_ibb',
                          'factor_qual', 'factor_mtum']

        # EU sector ETFs for short
        self.eu_sectors = ['financials_eufn', 'broad_fez', 'broad_ieur',
                          'value_ewu', 'value_ewg']

        # US credit for long
        self.us_credit = ['ig_lqd', 'hy_hyg', 'hy_jnk', 'loans_bkln',
                         'loans_srln', 'bdc_arcc', 'bdc_main']

        # EU credit for short
        self.eu_credit = ['ig_ieac', 'hy_ihyg']

    def compute_all_sleeve_targets(
        self,
        portfolio: PortfolioState,
        data_feed: DataFeed,
        risk_decision: RiskDecision,
        fx_rates: Optional[FXRates] = None,
        fx_hedge_ratio: float = 1.0
    ) -> StrategyOutput:
        """
        Compute target positions for all sleeves.

        ENGINE_FIX_PLAN Updates:
        - Phase 4: Currency-correct position sizing
        - Phase 5: Portfolio-level FX hedging (replaces per-sleeve hedges)

        Args:
            portfolio: Current portfolio state
            data_feed: Data feed for prices
            risk_decision: Risk engine decision
            fx_rates: FX rates for currency conversion
            fx_hedge_ratio: FX hedge ratio (0.0 to 1.0, default 1.0 = full hedge)

        Returns:
            StrategyOutput with all targets and orders
        """
        fx_rates = fx_rates or get_fx_rates()
        sleeve_targets = {}
        all_positions = {}
        commentary_parts = []

        nav = portfolio.nav
        scaling = risk_decision.scaling_factor

        # Apply regime-based adjustments
        if risk_decision.reduce_core_exposure:
            scaling *= risk_decision.reduce_factor
            commentary_parts.append(
                f"Regime reduction applied: {risk_decision.reduce_factor:.2f}"
            )

        # STRATEGY EVOLUTION: Apply trend filter to equity L/S sleeves
        trend_result = self.trend_filter.analyze(data_feed)
        self.last_trend_result = trend_result
        trend_multiplier = trend_result.sizing_multiplier

        commentary_parts.append(f"Trend: {trend_result.commentary}")

        if trend_result.use_options_only:
            commentary_parts.append("WARNING: Options-only mode - equity L/S disabled")

        # 1. Core Index RV Sleeve (trend-gated)
        # Apply trend multiplier to equity L/S sleeves
        core_scaling = scaling * trend_multiplier if not trend_result.use_options_only else 0.0
        core_targets = self._build_core_index_targets(
            nav, core_scaling, data_feed, risk_decision, fx_rates
        )
        sleeve_targets[Sleeve.CORE_INDEX_RV] = core_targets
        all_positions.update(core_targets.target_positions)

        # 2. Sector RV Sleeve (trend-gated, factor-neutral)
        sector_scaling = scaling * trend_multiplier if not trend_result.use_options_only else 0.0
        sector_targets = self._build_sector_rv_targets(
            nav, sector_scaling, data_feed, risk_decision
        )
        sleeve_targets[Sleeve.SECTOR_RV] = sector_targets
        all_positions.update(sector_targets.target_positions)

        # 3. Single Name Sleeve (trend-gated)
        single_scaling = scaling * trend_multiplier if not trend_result.use_options_only else 0.0
        single_targets = self._build_single_name_targets(
            nav, single_scaling, data_feed, risk_decision
        )
        sleeve_targets[Sleeve.SINGLE_NAME] = single_targets
        all_positions.update(single_targets.target_positions)

        # 4. Credit & Carry Sleeve
        credit_targets = self._build_credit_carry_targets(
            nav, scaling, data_feed, risk_decision
        )
        sleeve_targets[Sleeve.CREDIT_CARRY] = credit_targets
        all_positions.update(credit_targets.target_positions)

        # 5. Crisis Alpha Sleeve (handled by TailHedgeManager)
        crisis_targets = self._build_crisis_alpha_targets(
            nav, portfolio, data_feed, risk_decision
        )
        sleeve_targets[Sleeve.CRISIS_ALPHA] = crisis_targets
        all_positions.update(crisis_targets.target_positions)

        # 6. Cash Buffer (no positions)
        sleeve_targets[Sleeve.CASH_BUFFER] = SleeveTargets(
            sleeve=Sleeve.CASH_BUFFER,
            target_positions={},
            target_notional=nav * self.sleeve_weights[Sleeve.CASH_BUFFER],
            target_weight=self.sleeve_weights[Sleeve.CASH_BUFFER]
        )

        # =====================================================
        # Phase 5 + Phase C: Portfolio-Level FX Hedging with Policy Modes
        # =====================================================
        # Compute net FX exposure across ALL positions and cash
        net_fx_exposure = compute_net_fx_exposure(
            portfolio.positions,
            portfolio.cash_by_ccy,
            fx_rates
        )

        # Add exposure from target positions (estimate based on targets)
        # This is an approximation - real exposure will be computed after fills
        for inst_id, target_qty in all_positions.items():
            try:
                spec = self._find_instrument_spec_by_id(inst_id)
                if spec and spec.get('currency', 'USD') != 'USD':
                    ccy = spec['currency']
                    price = data_feed.get_last_price(inst_id)
                    multiplier = spec.get('multiplier', 1.0)
                    exposure = target_qty * price * multiplier
                    net_fx_exposure[ccy] = net_fx_exposure.get(ccy, 0.0) + exposure
            except Exception:
                pass

        # ROADMAP Phase C: Get FX hedge ratio based on policy and regime
        regime_str = risk_decision.regime.value if risk_decision.regime else "NORMAL"
        effective_fx_mode = self.fx_hedge_policy.get_effective_mode(regime_str)
        policy_hedge_ratio = self.fx_hedge_policy.get_hedge_ratio(regime_str)

        # Use policy ratio unless explicitly overridden by fx_hedge_ratio param
        effective_hedge_ratio = fx_hedge_ratio if fx_hedge_ratio < 1.0 else policy_hedge_ratio

        commentary_parts.append(
            f"FX Mode: {effective_fx_mode.value} (regime={regime_str}, ratio={effective_hedge_ratio:.0%})"
        )

        # Compute FX hedge quantities
        fx_hedges = compute_fx_hedge_quantities(
            net_fx_exposure,
            fx_rates,
            hedge_ratio=effective_hedge_ratio
        )

        # Add FX hedges to all_positions
        fx_hedge_mapping = {
            "EUR": "eurusd_micro",  # M6E
            "GBP": "gbpusd_micro",  # M6B
            "CHF": "chfusd_micro",  # M6S
        }

        for ccy, contracts in fx_hedges.items():
            if abs(contracts) > 0 and ccy in fx_hedge_mapping:
                inst_id = fx_hedge_mapping[ccy]
                all_positions[inst_id] = contracts
                commentary_parts.append(
                    f"FX Hedge: {ccy} -> {contracts} contracts"
                )

        # Check residual FX exposure is within limits (< 2% of NAV)
        residual_fx_usd = sum(
            abs(fx_rates.to_base(exp, ccy))
            for ccy, exp in net_fx_exposure.items()
        )
        residual_fx_pct = residual_fx_usd / nav if nav > 0 else 0
        if residual_fx_pct > 0.02:
            commentary_parts.append(
                f"WARNING: Residual FX exposure {residual_fx_pct:.1%} > 2%"
            )

        # Generate orders
        orders = self._generate_orders(portfolio.positions, all_positions, sleeve_targets)

        # Build commentary
        commentary = self._build_commentary(
            portfolio, risk_decision, sleeve_targets, commentary_parts
        )

        return StrategyOutput(
            sleeve_targets=sleeve_targets,
            total_target_positions=all_positions,
            orders=orders,
            scaling_factor=scaling,
            regime=risk_decision.regime,
            commentary=commentary
        )

    def _find_instrument_spec_by_id(self, instrument_id: str) -> Optional[Dict]:
        """Find instrument spec by ID in config."""
        for category, instruments in self.instruments.items():
            if isinstance(instruments, dict):
                if instrument_id in instruments:
                    return instruments[instrument_id]
        return None

    def _build_core_index_targets(
        self,
        nav: float,
        scaling: float,
        data_feed: DataFeed,
        risk_decision: RiskDecision,
        fx_rates: Optional[FXRates] = None
    ) -> SleeveTargets:
        """
        Build Core Index RV sleeve targets.
        Long US (CSPX) vs Short EU (CS51).

        CRITICAL (ENGINE_FIX_PLAN Phase 4):
        - NAV is in BASE_CCY (USD)
        - Convert notional to instrument currency BEFORE dividing by price
        - Use round() not floor() for large positions

        NOTE: FX hedging is now done at portfolio level (Phase 5)
        """
        fx_rates = fx_rates or get_fx_rates()
        target_weight = self.sleeve_weights[Sleeve.CORE_INDEX_RV]
        target_notional_usd = nav * target_weight * scaling

        # Split between long and short legs (in USD)
        notional_per_leg_usd = target_notional_usd / 2
        targets = {}

        try:
            # US Long Leg - CSPX on LSE (trades in USD)
            # NAV (USD) -> notional (USD) -> divide by price (USD) -> qty
            cspx_price = data_feed.get_last_price('CSPX')
            cspx_qty = round(notional_per_leg_usd / cspx_price)
            if cspx_qty > 0:
                targets['us_index_etf'] = cspx_qty

            # EU Short Leg - CS51 on XETRA (trades in EUR)
            # NAV (USD) -> convert to EUR -> divide by price (EUR) -> qty
            cs51_price = data_feed.get_last_price('CS51')
            notional_eur = fx_rates.convert(notional_per_leg_usd, "USD", "EUR")
            cs51_qty = round(notional_eur / cs51_price)
            if cs51_qty > 0:
                targets['eu_index_etf'] = -cs51_qty  # Negative for short

            # NOTE: FX hedge is no longer per-sleeve (Phase 5)
            # Portfolio-level FX hedging is done in compute_all_sleeve_targets

        except Exception as e:
            # Fallback to smaller position
            targets['us_index_etf'] = round(notional_per_leg_usd / 500)
            targets['eu_index_etf'] = -round(notional_per_leg_usd / 50)

        return SleeveTargets(
            sleeve=Sleeve.CORE_INDEX_RV,
            target_positions=targets,
            target_notional=target_notional_usd,
            target_weight=target_weight,
            long_notional=notional_per_leg_usd,
            short_notional=notional_per_leg_usd
        )

    def _build_sector_rv_targets(
        self,
        nav: float,
        scaling: float,
        data_feed: DataFeed,
        risk_decision: RiskDecision
    ) -> SleeveTargets:
        """
        Build Sector RV sleeve targets.
        Long US innovation sectors vs Short EU old-economy sectors.
        """
        target_weight = self.sleeve_weights[Sleeve.SECTOR_RV]
        target_notional = nav * target_weight * scaling
        notional_per_leg = target_notional / 2

        targets = {}

        # US Long Basket - equal weight across tech/healthcare/quality
        # Using UCITS ETFs for EU PRIIPs/KID compliance
        us_etfs = ['IUIT', 'CNDX', 'SEMI', 'IUHC', 'IUQA']
        us_weight_each = notional_per_leg / len(us_etfs)

        for etf in us_etfs:
            try:
                price = data_feed.get_last_price(etf)
                qty = int(us_weight_each / price)
                if qty > 0:
                    # Map to instrument ID
                    inst_id = self._etf_to_instrument_id(etf)
                    if inst_id:
                        targets[inst_id] = qty
            except Exception:
                continue

        # EU Short Basket - financials and broad Europe
        # Using UCITS ETFs for EU PRIIPs/KID compliance
        eu_etfs = ['EXV1', 'EXS1', 'IUKD']
        eu_weight_each = notional_per_leg / len(eu_etfs)

        for etf in eu_etfs:
            try:
                price = data_feed.get_last_price(etf)
                qty = int(eu_weight_each / price)
                if qty > 0:
                    inst_id = self._etf_to_instrument_id(etf)
                    if inst_id:
                        targets[inst_id] = -qty  # Short
            except Exception:
                continue

        return SleeveTargets(
            sleeve=Sleeve.SECTOR_RV,
            target_positions=targets,
            target_notional=target_notional,
            target_weight=target_weight,
            long_notional=notional_per_leg,
            short_notional=notional_per_leg
        )

    def _build_single_name_targets(
        self,
        nav: float,
        scaling: float,
        data_feed: DataFeed,
        risk_decision: RiskDecision
    ) -> SleeveTargets:
        """
        Build Single Name L/S sleeve targets.
        US quality growth vs EU "zombies".

        Uses quantitative factor-based screening:
        - US Longs: Quality (50%) + Momentum (30%) + Size (20%)
        - EU Shorts: Zombie (50%) + Weakness (30%) + Sector (20%)

        See stock_screener.py for full methodology.
        """
        target_weight = self.sleeve_weights[Sleeve.SINGLE_NAME]
        target_notional = nav * target_weight * scaling
        notional_per_leg = target_notional / 2

        targets = {}
        logger = get_trading_logger()

        # Run quantitative screening to select stocks
        try:
            long_symbols, short_symbols, screening_metadata = run_screening(
                top_n=10,
                logger=logger
            )
            logger.logger.info(
                "stock_screening_complete",
                extra={
                    "long_count": len(long_symbols),
                    "short_count": len(short_symbols),
                    "screening_date": screening_metadata.get('screening_date'),
                    "methodology_version": screening_metadata.get('methodology_version')
                }
            )
        except Exception as e:
            # Fallback to default stocks if screening fails
            logger.logger.warning(f"Screening failed, using defaults: {e}")
            long_symbols = ['AAPL', 'MSFT', 'GOOGL', 'NVDA', 'AMZN']
            short_symbols = []
            screening_metadata = {}

        # US Long Positions - equal weight across screened stocks
        if long_symbols:
            us_weight_each = notional_per_leg / len(long_symbols)
            for stock in long_symbols:
                try:
                    price = data_feed.get_last_price(stock)
                    qty = int(us_weight_each / price)
                    if qty > 0:
                        targets[stock] = qty
                except Exception:
                    continue

        # EU Short Positions - equal weight across screened stocks
        if short_symbols:
            eu_weight_each = notional_per_leg / len(short_symbols)
            for stock in short_symbols:
                try:
                    price = data_feed.get_last_price(stock)
                    qty = int(eu_weight_each / price)
                    if qty > 0:
                        targets[stock] = -qty  # Negative for short
                except Exception:
                    continue
        else:
            # Fallback: Use EXV1 ETF (UCITS) as proxy for EU shorts if no individual shorts
            try:
                price = data_feed.get_last_price('EXV1')
                qty = int(notional_per_leg / price)
                if qty > 0:
                    targets['financials_eufn_single'] = -qty
            except Exception:
                pass

        return SleeveTargets(
            sleeve=Sleeve.SINGLE_NAME,
            target_positions=targets,
            target_notional=target_notional,
            target_weight=target_weight,
            long_notional=notional_per_leg,
            short_notional=notional_per_leg
        )

    def _build_credit_carry_targets(
        self,
        nav: float,
        scaling: float,
        data_feed: DataFeed,
        risk_decision: RiskDecision
    ) -> SleeveTargets:
        """
        Build Credit & Carry sleeve targets.
        Long US credit, underweight/short EU credit.
        Using UCITS ETFs for EU PRIIPs/KID compliance.
        """
        target_weight = self.sleeve_weights[Sleeve.CREDIT_CARRY]
        target_notional = nav * target_weight * scaling

        targets = {}

        # US Credit Long (70% of sleeve)
        # Using UCITS ETFs for EU PRIIPs/KID compliance
        us_notional = target_notional * 0.7
        us_credit_etfs = {
            'LQDE': 0.40,   # IG (UCITS)
            'IHYU': 0.25,   # HY (UCITS)
            'FLOT': 0.20,   # Floating Rate (UCITS)
            'ARCC': 0.15    # BDC (individual stock, no KID needed)
        }

        for etf, weight in us_credit_etfs.items():
            try:
                price = data_feed.get_last_price(etf)
                qty = int((us_notional * weight) / price)
                if qty > 0:
                    inst_id = self._etf_to_instrument_id(etf)
                    if inst_id:
                        targets[inst_id] = qty
            except Exception:
                continue

        # EU Credit Short (30% of sleeve)
        eu_notional = target_notional * 0.3
        # Using UCITS ETF for EU credit short exposure
        try:
            # Short EU HY via IHYG (UCITS)
            ihyg_price = data_feed.get_last_price('IHYG')
            eu_short_qty = int(eu_notional / ihyg_price)
            # Instead of actual EU credit short, reduce US credit exposure
            # This is a simplification - in production would short actual EU credit
        except Exception:
            pass

        return SleeveTargets(
            sleeve=Sleeve.CREDIT_CARRY,
            target_positions=targets,
            target_notional=target_notional,
            target_weight=target_weight,
            long_notional=us_notional,
            short_notional=eu_notional
        )

    def _build_crisis_alpha_targets(
        self,
        nav: float,
        portfolio: PortfolioState,
        data_feed: DataFeed,
        risk_decision: RiskDecision
    ) -> SleeveTargets:
        """
        Build Crisis Alpha sleeve targets.
        Managed by TailHedgeManager - this provides base structure.
        """
        target_weight = self.sleeve_weights[Sleeve.CRISIS_ALPHA]
        target_notional = nav * target_weight

        # Crisis sleeve is managed separately by TailHedgeManager
        # Here we just define the envelope
        targets = {}

        # In elevated/crisis regime, allocate more to hedges
        if risk_decision.regime in [RiskRegime.ELEVATED, RiskRegime.CRISIS]:
            target_notional *= 1.5  # 50% more hedge budget

        return SleeveTargets(
            sleeve=Sleeve.CRISIS_ALPHA,
            target_positions=targets,
            target_notional=target_notional,
            target_weight=target_weight
        )

    def _etf_to_instrument_id(self, etf_symbol: str) -> Optional[str]:
        """Map ETF symbol to instrument ID in config.
        Updated for EU PRIIPs/KID compliance using UCITS ETFs.
        """
        mapping = {
            # US Index - UCITS
            'CSPX': 'us_index_etf',
            # EU Index - UCITS
            'CS51': 'eu_index_etf',
            'SMEA': 'eu_broad_etf',
            # US Sectors - UCITS
            'IUIT': 'tech_xlk',
            'CNDX': 'tech_qqq',
            'WTCH': 'tech_igv',
            'SEMI': 'tech_smh',
            'IUHC': 'health_xlv',
            'SBIO': 'health_xbi',
            'BTEK': 'health_ibb',
            'IUQA': 'factor_qual',
            'IUMO': 'factor_mtum',
            # EU Sectors - UCITS
            'EXV1': 'financials_eufn',
            'IUKD': 'value_ewu',
            'EXS1': 'value_ewg',
            # Credit - UCITS
            'LQDE': 'ig_lqd',
            'IHYU': 'hy_hyg',
            'HYLD': 'hy_jnk',
            'FLOT': 'loans_bkln',
            'FLOA': 'loans_srln',
            # BDCs - Individual stocks (no KID required)
            'ARCC': 'bdc_arcc',
            'MAIN': 'bdc_main'
        }
        return mapping.get(etf_symbol)

    def _generate_orders(
        self,
        current_positions: Dict[str, Position],
        target_positions: Dict[str, float],
        sleeve_targets: Dict[Sleeve, SleeveTargets]
    ) -> List[OrderSpec]:
        """
        Generate orders to move from current to target positions.

        Args:
            current_positions: Current portfolio positions
            target_positions: Target positions by instrument
            sleeve_targets: Sleeve target information

        Returns:
            List of OrderSpec objects
        """
        orders = []

        # Get current quantities
        current_qty = {
            inst_id: pos.quantity
            for inst_id, pos in current_positions.items()
        }

        # Build instrument to sleeve mapping
        inst_to_sleeve = {}
        for sleeve, targets in sleeve_targets.items():
            for inst_id in targets.target_positions:
                inst_to_sleeve[inst_id] = sleeve

        # Generate orders for each target
        all_instruments = set(target_positions.keys()) | set(current_qty.keys())

        for inst_id in all_instruments:
            current = current_qty.get(inst_id, 0)
            target = target_positions.get(inst_id, 0)
            diff = target - current

            # Skip if no change needed (with some tolerance)
            if abs(diff) < 1:
                continue

            # Determine order side
            if diff > 0:
                side = "BUY"
            else:
                side = "SELL"
                diff = abs(diff)

            # Get sleeve
            sleeve = inst_to_sleeve.get(inst_id, Sleeve.CORE_INDEX_RV)

            # Create order
            order = OrderSpec(
                instrument_id=inst_id,
                side=side,
                quantity=diff,
                order_type="MKT",
                sleeve=sleeve,
                reason=f"Rebalance to target: {target:.0f} from {current:.0f}"
            )
            orders.append(order)

        return orders

    def _build_commentary(
        self,
        portfolio: PortfolioState,
        risk_decision: RiskDecision,
        sleeve_targets: Dict[Sleeve, SleeveTargets],
        additional_notes: List[str]
    ) -> str:
        """Build strategy commentary for logging/reporting."""
        lines = [
            f"Strategy Update - {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            f"NAV: ${portfolio.nav:,.2f}",
            f"Regime: {risk_decision.regime.value}",
            f"Scaling Factor: {risk_decision.scaling_factor:.2f}",
            "",
            "Sleeve Allocations:"
        ]

        for sleeve, targets in sleeve_targets.items():
            lines.append(
                f"  {sleeve.value}: ${targets.target_notional:,.0f} "
                f"({targets.target_weight:.1%})"
            )

        if risk_decision.warnings:
            lines.append("")
            lines.append("Warnings:")
            for warning in risk_decision.warnings:
                lines.append(f"  - {warning}")

        if additional_notes:
            lines.append("")
            lines.append("Notes:")
            for note in additional_notes:
                lines.append(f"  - {note}")

        return "\n".join(lines)


def generate_rebalance_orders(
    current_positions: Dict[str, float],
    target_positions: Dict[str, float],
    instruments_config: Dict[str, Any],
    min_trade_value: float = 1000.0
) -> List[OrderSpec]:
    """
    Generate rebalance orders to move from current to target positions.

    Args:
        current_positions: Dict of instrument_id -> current quantity
        target_positions: Dict of instrument_id -> target quantity
        instruments_config: Instrument configuration
        min_trade_value: Minimum trade value to generate order

    Returns:
        List of OrderSpec orders
    """
    orders = []

    all_instruments = set(current_positions.keys()) | set(target_positions.keys())

    for inst_id in all_instruments:
        current = current_positions.get(inst_id, 0)
        target = target_positions.get(inst_id, 0)
        diff = target - current

        if abs(diff) < 1:
            continue

        side = "BUY" if diff > 0 else "SELL"
        quantity = abs(diff)

        order = OrderSpec(
            instrument_id=inst_id,
            side=side,
            quantity=quantity,
            order_type="MKT",
            reason=f"Rebalance: {current:.0f} -> {target:.0f}"
        )
        orders.append(order)

    return orders
