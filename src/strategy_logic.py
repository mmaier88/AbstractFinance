"""
Multi-sleeve strategy construction for AbstractFinance.
Implements the European Decline Macro strategy across all sleeves.
"""

import pandas as pd
import numpy as np
from datetime import datetime, date
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from enum import Enum

from .portfolio import PortfolioState, Sleeve, Position
from .risk_engine import RiskEngine, RiskDecision, RiskRegime
from .data_feeds import DataFeed
from .stock_screener import run_screening, get_default_us_universe, get_default_eu_universe
from .logging_utils import get_trading_logger


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
        risk_decision: RiskDecision
    ) -> StrategyOutput:
        """
        Compute target positions for all sleeves.

        Args:
            portfolio: Current portfolio state
            data_feed: Data feed for prices
            risk_decision: Risk engine decision

        Returns:
            StrategyOutput with all targets and orders
        """
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

        # 1. Core Index RV Sleeve
        core_targets = self._build_core_index_targets(
            nav, scaling, data_feed, risk_decision
        )
        sleeve_targets[Sleeve.CORE_INDEX_RV] = core_targets
        all_positions.update(core_targets.target_positions)

        # 2. Sector RV Sleeve
        sector_targets = self._build_sector_rv_targets(
            nav, scaling, data_feed, risk_decision
        )
        sleeve_targets[Sleeve.SECTOR_RV] = sector_targets
        all_positions.update(sector_targets.target_positions)

        # 3. Single Name Sleeve
        single_targets = self._build_single_name_targets(
            nav, scaling, data_feed, risk_decision
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

    def _build_core_index_targets(
        self,
        nav: float,
        scaling: float,
        data_feed: DataFeed,
        risk_decision: RiskDecision
    ) -> SleeveTargets:
        """
        Build Core Index RV sleeve targets.
        Long US (CSPX) vs Short EU (CS51), FX-hedged.
        Using UCITS ETFs for EU PRIIPs/KID compliance.
        """
        target_weight = self.sleeve_weights[Sleeve.CORE_INDEX_RV]
        target_notional = nav * target_weight * scaling

        # Split between long and short legs
        notional_per_leg = target_notional / 2
        targets = {}

        try:
            # US Long Leg - use UCITS ETF (CSPX on LSE)
            cspx_price = data_feed.get_last_price('CSPX')
            cspx_qty = int(notional_per_leg / cspx_price)
            if cspx_qty > 0:
                targets['us_index_etf'] = cspx_qty

            # EU Short Leg - use UCITS ETF (CS51 on XETRA)
            cs51_price = data_feed.get_last_price('CS51')
            cs51_qty = int(notional_per_leg / cs51_price)
            if cs51_qty > 0:
                targets['eu_index_etf'] = -cs51_qty  # Negative for short

            # FX Hedge - hedge EUR exposure from short EU leg
            # Short EUR to hedge the EUR notional of the short EU leg
            eur_notional = abs(cs51_qty * cs51_price) if cs51_qty else 0

            # Use micro futures for better sizing
            # M6E = 12,500 EUR per contract
            m6e_multiplier = 12500
            fx_contracts = int(eur_notional / m6e_multiplier)
            if fx_contracts > 0:
                targets['eurusd_micro'] = -fx_contracts  # Short EUR

        except Exception as e:
            # Fallback to smaller position
            targets['us_index_etf'] = int(notional_per_leg / 500)  # Assume ~$500 CSPX
            targets['eu_index_etf'] = -int(notional_per_leg / 50)  # Assume ~$50 CS51

        return SleeveTargets(
            sleeve=Sleeve.CORE_INDEX_RV,
            target_positions=targets,
            target_notional=target_notional,
            target_weight=target_weight,
            long_notional=notional_per_leg,
            short_notional=notional_per_leg
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
