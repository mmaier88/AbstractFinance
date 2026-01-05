"""
Sector Pairs Module - Factor-Neutral US vs EU Sector Matching.

Implements same-sector US vs EU pairs to isolate regional beta from:
- Growth vs value factor
- Duration sensitivity
- Sector-specific risk

The key insight: Long US Tech / Short EU Banks is NOT a clean regional bet.
It's a combination of: regional + growth/value + duration + sector.

Same-sector pairs (US Banks vs EU Banks, US Industrials vs EU Industrials)
isolate the regional component.
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple
import numpy as np

logger = logging.getLogger(__name__)


class Sector(Enum):
    """Sector classifications matching GICS."""
    FINANCIALS = "financials"
    TECHNOLOGY = "technology"
    INDUSTRIALS = "industrials"
    HEALTHCARE = "healthcare"
    CONSUMER_DISCRETIONARY = "consumer_discretionary"
    CONSUMER_STAPLES = "consumer_staples"
    ENERGY = "energy"
    MATERIALS = "materials"
    UTILITIES = "utilities"
    REAL_ESTATE = "real_estate"
    COMMUNICATION = "communication"


@dataclass
class SectorPair:
    """A matched US vs EU sector pair."""
    sector: Sector
    us_symbol: str
    eu_symbol: str
    us_name: str
    eu_name: str

    # Beta matching
    us_beta: float
    eu_beta: float
    beta_ratio: float  # eu_beta / us_beta for position sizing

    # Factor exposures (for neutralization)
    us_growth_exposure: float
    eu_growth_exposure: float
    us_value_exposure: float
    eu_value_exposure: float

    # Liquidity and cost estimates
    us_avg_spread_bps: float
    eu_avg_spread_bps: float
    us_avg_daily_volume: float
    eu_avg_daily_volume: float


@dataclass
class SectorPairPosition:
    """Target position for a sector pair."""
    pair: SectorPair
    us_notional: float  # Positive = long
    eu_notional: float  # Negative = short

    # Factor-neutral adjustments
    growth_adjustment: float
    value_adjustment: float

    # Effective exposures after adjustment
    net_regional_exposure: float
    net_growth_exposure: float
    net_value_exposure: float


# Predefined sector pairs with estimated parameters
# These should be calibrated periodically with actual data
#
# IMPORTANT: US sector ETFs (XLF, XLK, XLI, XLV) are NOT tradeable from EU accounts
# due to PRIIPs/KID regulations. We use UCITS alternatives where available:
# - XLK -> IUIT (iShares S&P 500 IT Sector UCITS ETF, LSE)
# - XLV -> IUHC (iShares S&P 500 Health Care UCITS ETF, LSE)
# - XLF -> No direct UCITS equivalent (pair DISABLED)
# - XLI -> No direct UCITS equivalent (pair DISABLED)
#
SECTOR_PAIRS = {
    # FINANCIALS: DISABLED - No UCITS equivalent for XLF available from EU accounts
    # US S&P 500 Financials sector ETFs require KID which isn't provided
    # Keeping definition for reference but marked as unavailable
    # Sector.FINANCIALS: SectorPair(...),  # DISABLED

    Sector.TECHNOLOGY: SectorPair(
        sector=Sector.TECHNOLOGY,
        us_symbol="IUIT",     # iShares S&P 500 IT Sector UCITS ETF (LSE)
        eu_symbol="EXV3",     # iShares STOXX Europe 600 Technology
        us_name="US Technology (UCITS)",
        eu_name="EU Technology",
        us_beta=1.2,
        eu_beta=1.1,
        beta_ratio=0.92,
        us_growth_exposure=0.6,
        eu_growth_exposure=0.4,
        us_value_exposure=-0.3,
        eu_value_exposure=-0.2,
        us_avg_spread_bps=5.0,   # UCITS typically wider spreads
        eu_avg_spread_bps=5.0,
        us_avg_daily_volume=5_000_000,  # Lower volume for UCITS
        eu_avg_daily_volume=2_000_000,
    ),

    # INDUSTRIALS: DISABLED - No UCITS equivalent for XLI available from EU accounts
    # Sector.INDUSTRIALS: SectorPair(...),  # DISABLED

    Sector.HEALTHCARE: SectorPair(
        sector=Sector.HEALTHCARE,
        us_symbol="IUHC",     # iShares S&P 500 Health Care UCITS ETF (LSE)
        eu_symbol="EXV4",     # iShares STOXX Europe 600 Health Care
        us_name="US Healthcare (UCITS)",
        eu_name="EU Healthcare",
        us_beta=0.85,
        eu_beta=0.8,
        beta_ratio=0.94,
        us_growth_exposure=0.2,
        eu_growth_exposure=0.1,
        us_value_exposure=0.1,
        eu_value_exposure=0.2,
        us_avg_spread_bps=2.0,
        eu_avg_spread_bps=4.0,
        us_avg_daily_volume=20_000_000,
        eu_avg_daily_volume=2_500_000,
    ),

    Sector.CONSUMER_DISCRETIONARY: SectorPair(
        sector=Sector.CONSUMER_DISCRETIONARY,
        us_symbol="XLY",      # Consumer Discretionary Select Sector SPDR
        eu_symbol="EXV2",     # iShares STOXX Europe 600 Retail
        us_name="US Consumer Disc",
        eu_name="EU Consumer Disc",
        us_beta=1.2,
        eu_beta=1.1,
        beta_ratio=0.92,
        us_growth_exposure=0.4,
        eu_growth_exposure=0.2,
        us_value_exposure=-0.1,
        eu_value_exposure=0.1,
        us_avg_spread_bps=2.0,
        eu_avg_spread_bps=5.0,
        us_avg_daily_volume=15_000_000,
        eu_avg_daily_volume=1_500_000,
    ),

    Sector.ENERGY: SectorPair(
        sector=Sector.ENERGY,
        us_symbol="XLE",      # Energy Select Sector SPDR
        eu_symbol="EXH2",     # iShares STOXX Europe 600 Oil & Gas
        us_name="US Energy",
        eu_name="EU Energy",
        us_beta=1.3,
        eu_beta=1.2,
        beta_ratio=0.92,
        us_growth_exposure=-0.3,
        eu_growth_exposure=-0.3,
        us_value_exposure=0.4,
        eu_value_exposure=0.4,
        us_avg_spread_bps=2.5,
        eu_avg_spread_bps=4.0,
        us_avg_daily_volume=20_000_000,
        eu_avg_daily_volume=3_000_000,
    ),

    Sector.UTILITIES: SectorPair(
        sector=Sector.UTILITIES,
        us_symbol="XLU",      # Utilities Select Sector SPDR
        eu_symbol="EXH9",     # iShares STOXX Europe 600 Utilities
        us_name="US Utilities",
        eu_name="EU Utilities",
        us_beta=0.6,
        eu_beta=0.7,
        beta_ratio=1.17,
        us_growth_exposure=-0.2,
        eu_growth_exposure=-0.2,
        us_value_exposure=0.3,
        eu_value_exposure=0.3,
        us_avg_spread_bps=2.0,
        eu_avg_spread_bps=4.0,
        us_avg_daily_volume=15_000_000,
        eu_avg_daily_volume=2_000_000,
    ),
}


class SectorPairEngine:
    """
    Engine for computing factor-neutral sector pair positions.

    The goal: Express "US outperforms EU" without hidden bets on:
    - Growth vs value
    - High beta vs low beta
    - Specific sectors vs others

    Method:
    1. Match US and EU by sector
    2. Beta-adjust position sizes
    3. Neutralize growth/value exposure across portfolio
    """

    DEFAULT_CONFIG = {
        # Sector selection
        # NOTE: Only Technology and Healthcare have UCITS equivalents for US sector ETFs
        # Financials (XLF) and Industrials (XLI) are blocked by PRIIPs for EU investors
        "included_sectors": [
            Sector.TECHNOLOGY,   # IUIT (US) vs EXV3 (EU) - 10% of sleeve
            Sector.HEALTHCARE,   # IUHC (US) vs EXV4 (EU) - 10% of sleeve
        ],

        # Position sizing
        "equal_weight_sectors": True,
        "beta_adjust": True,

        # Factor neutralization
        "neutralize_growth_value": True,
        "max_growth_exposure": 0.1,
        "max_value_exposure": 0.1,

        # Liquidity constraints
        "min_daily_volume_usd": 1_000_000,
        "max_spread_bps": 10.0,

        # Rebalance
        "rebalance_threshold": 0.05,  # 5% drift
    }

    def __init__(self, config: Optional[Dict] = None):
        """Initialize with optional config overrides."""
        self.config = {**self.DEFAULT_CONFIG, **(config or {})}
        self.sector_pairs = SECTOR_PAIRS

    def compute_positions(
        self,
        sleeve_nav: float,
        scaling: float = 1.0,
        current_prices: Optional[Dict[str, float]] = None
    ) -> List[SectorPairPosition]:
        """
        Compute target positions for all sector pairs.

        Args:
            sleeve_nav: NAV allocated to Sector RV sleeve
            scaling: Regime-based scaling factor (0.0 to 1.0)
            current_prices: Optional dict of symbol -> price

        Returns:
            List of SectorPairPosition targets
        """
        cfg = self.config
        positions = []

        # Filter to included sectors
        included = [
            self.sector_pairs[s]
            for s in cfg["included_sectors"]
            if s in self.sector_pairs
        ]

        if not included:
            return []

        # Compute weight per sector
        n_sectors = len(included)
        weight_per_sector = sleeve_nav * scaling / n_sectors

        # First pass: compute raw positions
        raw_positions = []
        for pair in included:
            # Beta-adjust if configured
            if cfg["beta_adjust"]:
                # Adjust EU notional by beta ratio to match market exposure
                us_notional = weight_per_sector / 2
                eu_notional = -(weight_per_sector / 2) * pair.beta_ratio
            else:
                us_notional = weight_per_sector / 2
                eu_notional = -weight_per_sector / 2

            raw_positions.append({
                "pair": pair,
                "us_notional": us_notional,
                "eu_notional": eu_notional,
            })

        # Second pass: factor neutralization
        if cfg["neutralize_growth_value"]:
            raw_positions = self._neutralize_factors(raw_positions)

        # Build final positions
        for raw in raw_positions:
            pair = raw["pair"]
            us_not = raw["us_notional"]
            eu_not = raw["eu_notional"]

            # Compute effective exposures
            net_regional = us_not - eu_not  # Positive = long US vs EU
            net_growth = (
                us_not * pair.us_growth_exposure +
                eu_not * pair.eu_growth_exposure
            )
            net_value = (
                us_not * pair.us_value_exposure +
                eu_not * pair.eu_value_exposure
            )

            positions.append(SectorPairPosition(
                pair=pair,
                us_notional=us_not,
                eu_notional=eu_not,
                growth_adjustment=raw.get("growth_adj", 0.0),
                value_adjustment=raw.get("value_adj", 0.0),
                net_regional_exposure=net_regional,
                net_growth_exposure=net_growth,
                net_value_exposure=net_value,
            ))

        return positions

    def _neutralize_factors(
        self,
        raw_positions: List[Dict]
    ) -> List[Dict]:
        """
        Adjust positions to neutralize growth/value exposure.

        Uses optimization to minimize factor exposure while
        maintaining regional exposure.
        """
        cfg = self.config

        # Compute portfolio-level factor exposures
        total_growth = 0.0
        total_value = 0.0
        total_regional = 0.0

        for raw in raw_positions:
            pair = raw["pair"]
            us_not = raw["us_notional"]
            eu_not = raw["eu_notional"]

            total_growth += (
                us_not * pair.us_growth_exposure +
                eu_not * pair.eu_growth_exposure
            )
            total_value += (
                us_not * pair.us_value_exposure +
                eu_not * pair.eu_value_exposure
            )
            total_regional += us_not - eu_not

        # If already neutral, no adjustment needed
        if (abs(total_growth) <= cfg["max_growth_exposure"] * total_regional and
            abs(total_value) <= cfg["max_value_exposure"] * total_regional):
            return raw_positions

        # Simple neutralization: scale sectors with opposing exposures
        # This is a simplified approach; production would use optimization

        # Find which sectors can offset
        growth_offsetters = []
        value_offsetters = []

        for raw in raw_positions:
            pair = raw["pair"]
            net_growth = (
                pair.us_growth_exposure - pair.eu_growth_exposure
            )
            net_value = (
                pair.us_value_exposure - pair.eu_value_exposure
            )

            if net_growth * total_growth < 0:  # Opposite sign
                growth_offsetters.append((raw, abs(net_growth)))
            if net_value * total_value < 0:
                value_offsetters.append((raw, abs(net_value)))

        # Scale up offsetters slightly, scale down contributors
        adjustment_factor = 0.1  # 10% adjustment

        for raw in raw_positions:
            pair = raw["pair"]
            net_growth = pair.us_growth_exposure - pair.eu_growth_exposure

            # If this sector contributes to unwanted growth exposure
            if net_growth * total_growth > 0:
                # Scale down slightly
                raw["us_notional"] *= (1 - adjustment_factor)
                raw["eu_notional"] *= (1 - adjustment_factor)
                raw["growth_adj"] = -adjustment_factor
            elif net_growth * total_growth < 0:
                # Scale up slightly
                raw["us_notional"] *= (1 + adjustment_factor)
                raw["eu_notional"] *= (1 + adjustment_factor)
                raw["growth_adj"] = adjustment_factor

        return raw_positions

    def compute_expected_return(
        self,
        positions: List[SectorPairPosition],
        us_market_return: float,
        eu_market_return: float,
        sector_returns: Optional[Dict[Sector, Tuple[float, float]]] = None
    ) -> float:
        """
        Compute expected return for sector pair positions.

        For backtesting with simplified assumptions.

        Args:
            positions: List of SectorPairPosition
            us_market_return: US market daily return
            eu_market_return: EU market daily return
            sector_returns: Optional dict of Sector -> (us_return, eu_return)

        Returns:
            Total daily return
        """
        total_return = 0.0
        total_notional = 0.0

        for pos in positions:
            pair = pos.pair

            # Use sector-specific returns if provided, else scale from market
            if sector_returns and pair.sector in sector_returns:
                us_ret, eu_ret = sector_returns[pair.sector]
            else:
                # Scale market return by beta
                us_ret = us_market_return * pair.us_beta
                eu_ret = eu_market_return * pair.eu_beta

            # Position return
            us_pnl = pos.us_notional * us_ret
            eu_pnl = pos.eu_notional * eu_ret  # Negative notional = short

            pair_return = us_pnl + eu_pnl
            total_return += pair_return
            total_notional += abs(pos.us_notional) + abs(pos.eu_notional)

        # Return as percentage of notional
        if total_notional > 0:
            return total_return / (total_notional / 2)  # Divide by 2 for net exposure
        return 0.0

    def get_tradeable_instruments(self) -> List[Dict]:
        """Get list of instruments needed for sector pairs."""
        instruments = []

        for sector in self.config["included_sectors"]:
            if sector not in self.sector_pairs:
                continue

            pair = self.sector_pairs[sector]
            instruments.append({
                "symbol": pair.us_symbol,
                "name": pair.us_name,
                "region": "US",
                "sector": sector.value,
                "exchange": "ARCA",  # Most sector ETFs trade on ARCA
            })
            instruments.append({
                "symbol": pair.eu_symbol,
                "name": pair.eu_name,
                "region": "EU",
                "sector": sector.value,
                "exchange": "XETR",  # Most EU ETFs on Xetra
            })

        return instruments


# Convenience function for backtest
def compute_sector_pairs_return(
    us_return: float,
    eu_return: float,
    sleeve_nav: float = 1.0,
    scaling: float = 1.0
) -> float:
    """
    Compute Sector RV return for backtest.

    Simplified interface using market returns (beta-adjusted).
    """
    engine = SectorPairEngine()
    positions = engine.compute_positions(sleeve_nav, scaling)

    return engine.compute_expected_return(
        positions, us_return, eu_return
    )
