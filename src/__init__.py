"""
AbstractFinance - European Decline Macro Fund
A multi-sleeve macro hedge fund strategy expressing a structural view
on US vs European economic performance.
"""

__version__ = "0.2.0"  # Added Risk Parity + Sovereign Overlay
__author__ = "AbstractFinance"

# Core modules
from .portfolio import PortfolioState, Position, Sleeve
from .risk_engine import RiskEngine, RiskDecision, RiskRegime

# Strategy modules
from .strategy_logic import Strategy, StrategyOutput, OrderSpec
from .tail_hedge import TailHedgeManager, HedgeType, HedgePosition

# Risk Parity + Sovereign Overlay (Phase 1-3)
from .risk_parity import RiskParityAllocator, RiskParityWeights, RiskParityConfig
from .sovereign_overlay import (
    SovereignCrisisOverlay,
    SovereignProxy,
    SOVEREIGN_PROXIES,
    StressLevel,
    OverlayConfig,
)
from .strategy_integration import (
    IntegratedStrategy,
    IntegratedStrategyOutput,
    IntegratedStrategyConfig,
    create_integrated_strategy,
)

__all__ = [
    # Core
    "PortfolioState",
    "Position",
    "Sleeve",
    "RiskEngine",
    "RiskDecision",
    "RiskRegime",
    # Strategy
    "Strategy",
    "StrategyOutput",
    "OrderSpec",
    "TailHedgeManager",
    "HedgeType",
    "HedgePosition",
    # Risk Parity + Sovereign Overlay
    "RiskParityAllocator",
    "RiskParityWeights",
    "RiskParityConfig",
    "SovereignCrisisOverlay",
    "SovereignProxy",
    "SOVEREIGN_PROXIES",
    "StressLevel",
    "OverlayConfig",
    "IntegratedStrategy",
    "IntegratedStrategyOutput",
    "IntegratedStrategyConfig",
    "create_integrated_strategy",
]
