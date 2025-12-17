"""
Contracts module for AbstractFinance.

Provides option chain resolution and contract factory for abstract instruments.
"""

from .option_factory import (
    OptionContractFactory,
    OptionContractSpec,
    OptionSelection,
)

__all__ = [
    "OptionContractFactory",
    "OptionContractSpec",
    "OptionSelection",
]
