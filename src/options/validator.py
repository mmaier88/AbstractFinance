"""
Option Lifecycle Validator for Tail Hedges.

Phase D: Prevents execution of illiquid or overpriced option hedges.

Key features:
- Bid/ask spread validation
- Volume/open interest thresholds
- Premium budget constraints
- Slippage collar enforcement
- Alternative strike suggestions
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Dict, List, Optional, Tuple, Any
from enum import Enum

logger = logging.getLogger(__name__)


class ValidationFailure(Enum):
    """Types of validation failures."""
    SPREAD_TOO_WIDE = "spread_too_wide"
    LOW_VOLUME = "low_volume"
    LOW_OPEN_INTEREST = "low_open_interest"
    PREMIUM_TOO_HIGH = "premium_too_high"
    PREMIUM_EXCEEDS_BUDGET = "premium_exceeds_budget"
    NO_QUOTES = "no_quotes"
    INVALID_MULTIPLIER = "invalid_multiplier"
    EXPIRY_TOO_CLOSE = "expiry_too_close"
    CONTRACT_NOT_FOUND = "contract_not_found"


@dataclass
class OptionValidationConfig:
    """Configuration for option validation thresholds."""
    # Spread thresholds (percentage of mid)
    max_spread_pct_equity_puts: float = 0.08  # 8%
    max_spread_pct_vix_calls: float = 0.12    # 12% (VIX options often wider)
    max_spread_pct_credit_puts: float = 0.10  # 10%

    # Volume thresholds
    min_volume_equity_puts: int = 100
    min_volume_vix_calls: int = 50
    min_volume_credit_puts: int = 50

    # Open interest thresholds
    min_open_interest_equity_puts: int = 500
    min_open_interest_vix_calls: int = 200
    min_open_interest_credit_puts: int = 200

    # Premium limits
    max_premium_per_leg_usd: float = 50000.0    # Max $50k per leg
    max_premium_pct_budget: float = 0.25        # Max 25% of hedge budget per leg

    # Slippage collar (how much worse than mid we'll accept)
    max_slippage_bps: float = 50.0  # 50 bps (0.5%)

    # Minimum DTE
    min_dte: int = 14  # Don't buy options with < 14 days to expiry


@dataclass
class OptionQuote:
    """Market data for an option."""
    symbol: str
    underlying: str
    strike: float
    expiry: date
    option_type: str  # "call" or "put"
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    volume: int = 0
    open_interest: int = 0
    multiplier: float = 100.0
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def mid(self) -> Optional[float]:
        """Calculate mid price."""
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2
        return self.last

    @property
    def spread(self) -> Optional[float]:
        """Calculate bid-ask spread."""
        if self.bid is not None and self.ask is not None:
            return self.ask - self.bid
        return None

    @property
    def spread_pct(self) -> Optional[float]:
        """Calculate spread as percentage of mid."""
        if self.spread is not None and self.mid and self.mid > 0:
            return self.spread / self.mid
        return None

    @property
    def dte(self) -> int:
        """Days to expiry."""
        return (self.expiry - date.today()).days


@dataclass
class OptionValidationResult:
    """Result of option validation."""
    is_valid: bool
    symbol: str
    failures: List[ValidationFailure] = field(default_factory=list)
    failure_details: Dict[str, Any] = field(default_factory=dict)
    alternative_strikes: List[float] = field(default_factory=list)
    recommended_action: str = ""
    premium_estimate: Optional[float] = None

    def add_failure(self, failure: ValidationFailure, detail: Any = None) -> None:
        """Add a validation failure."""
        self.is_valid = False
        self.failures.append(failure)
        if detail:
            self.failure_details[failure.value] = detail


class OptionValidator:
    """
    Validates option orders before submission.

    Usage:
        validator = OptionValidator(config)

        # Validate a single option
        quote = get_option_quote(symbol)
        result = validator.validate(quote, hedge_type="equity_put", budget=10000)

        if not result.is_valid:
            logger.warning(f"Option rejected: {result.failures}")
            # Consider alternatives
            for strike in result.alternative_strikes:
                ...
    """

    def __init__(self, config: Optional[OptionValidationConfig] = None):
        """Initialize validator with configuration."""
        self.config = config or OptionValidationConfig()

        # Track rejected orders for metrics
        self.rejected_count = 0
        self.rejection_reasons: Dict[str, int] = {}

    def validate(
        self,
        quote: OptionQuote,
        hedge_type: str,
        budget_remaining: float,
        quantity: int = 1
    ) -> OptionValidationResult:
        """
        Validate an option order.

        Args:
            quote: Option quote data
            hedge_type: Type of hedge ("equity_put", "vix_call", "credit_put")
            budget_remaining: Remaining hedge budget
            quantity: Number of contracts

        Returns:
            OptionValidationResult with pass/fail and details
        """
        result = OptionValidationResult(
            is_valid=True,
            symbol=quote.symbol
        )

        # 1. Check quotes exist
        if quote.bid is None or quote.ask is None:
            result.add_failure(ValidationFailure.NO_QUOTES)
            result.recommended_action = "Wait for quotes or use limit order at theoretical value"
            return result

        # 2. Check spread
        self._validate_spread(quote, hedge_type, result)

        # 3. Check volume
        self._validate_volume(quote, hedge_type, result)

        # 4. Check open interest
        self._validate_open_interest(quote, hedge_type, result)

        # 5. Check premium limits
        self._validate_premium(quote, quantity, budget_remaining, result)

        # 6. Check DTE
        self._validate_dte(quote, result)

        # 7. Check multiplier sanity
        self._validate_multiplier(quote, result)

        # If invalid, suggest alternatives and track metrics
        if not result.is_valid:
            self._suggest_alternatives(quote, result)
            self._track_rejection(result)
        else:
            # Calculate premium estimate
            result.premium_estimate = (quote.ask or quote.mid or 0) * quantity * quote.multiplier
            result.recommended_action = "Order approved - use marketable limit at ask"

        return result

    def validate_batch(
        self,
        quotes: List[OptionQuote],
        hedge_type: str,
        budget_remaining: float,
        quantities: Optional[List[int]] = None
    ) -> List[OptionValidationResult]:
        """Validate multiple options."""
        quantities = quantities or [1] * len(quotes)
        results = []
        remaining = budget_remaining

        for quote, qty in zip(quotes, quantities):
            result = self.validate(quote, hedge_type, remaining, qty)
            results.append(result)

            # Deduct from remaining budget if valid
            if result.is_valid and result.premium_estimate:
                remaining -= result.premium_estimate

        return results

    def _validate_spread(
        self,
        quote: OptionQuote,
        hedge_type: str,
        result: OptionValidationResult
    ) -> None:
        """Validate bid-ask spread."""
        spread_pct = quote.spread_pct
        if spread_pct is None:
            return

        # Get threshold for hedge type
        thresholds = {
            "equity_put": self.config.max_spread_pct_equity_puts,
            "vix_call": self.config.max_spread_pct_vix_calls,
            "credit_put": self.config.max_spread_pct_credit_puts,
            "bank_put": self.config.max_spread_pct_equity_puts,
        }
        max_spread = thresholds.get(hedge_type, 0.08)

        if spread_pct > max_spread:
            result.add_failure(
                ValidationFailure.SPREAD_TOO_WIDE,
                {"spread_pct": spread_pct, "max_pct": max_spread}
            )

    def _validate_volume(
        self,
        quote: OptionQuote,
        hedge_type: str,
        result: OptionValidationResult
    ) -> None:
        """Validate trading volume."""
        thresholds = {
            "equity_put": self.config.min_volume_equity_puts,
            "vix_call": self.config.min_volume_vix_calls,
            "credit_put": self.config.min_volume_credit_puts,
            "bank_put": self.config.min_volume_credit_puts,
        }
        min_volume = thresholds.get(hedge_type, 50)

        if quote.volume < min_volume:
            result.add_failure(
                ValidationFailure.LOW_VOLUME,
                {"volume": quote.volume, "min_volume": min_volume}
            )

    def _validate_open_interest(
        self,
        quote: OptionQuote,
        hedge_type: str,
        result: OptionValidationResult
    ) -> None:
        """Validate open interest."""
        thresholds = {
            "equity_put": self.config.min_open_interest_equity_puts,
            "vix_call": self.config.min_open_interest_vix_calls,
            "credit_put": self.config.min_open_interest_credit_puts,
            "bank_put": self.config.min_open_interest_credit_puts,
        }
        min_oi = thresholds.get(hedge_type, 200)

        if quote.open_interest < min_oi:
            result.add_failure(
                ValidationFailure.LOW_OPEN_INTEREST,
                {"open_interest": quote.open_interest, "min_oi": min_oi}
            )

    def _validate_premium(
        self,
        quote: OptionQuote,
        quantity: int,
        budget_remaining: float,
        result: OptionValidationResult
    ) -> None:
        """Validate premium against limits."""
        if quote.ask is None:
            return

        total_premium = quote.ask * quantity * quote.multiplier

        # Check absolute limit per leg
        if total_premium > self.config.max_premium_per_leg_usd:
            result.add_failure(
                ValidationFailure.PREMIUM_TOO_HIGH,
                {
                    "premium": total_premium,
                    "max_premium": self.config.max_premium_per_leg_usd
                }
            )

        # Check budget constraint
        max_from_budget = budget_remaining * self.config.max_premium_pct_budget
        if total_premium > max_from_budget:
            result.add_failure(
                ValidationFailure.PREMIUM_EXCEEDS_BUDGET,
                {
                    "premium": total_premium,
                    "budget_remaining": budget_remaining,
                    "max_pct": self.config.max_premium_pct_budget
                }
            )

    def _validate_dte(self, quote: OptionQuote, result: OptionValidationResult) -> None:
        """Validate days to expiry."""
        if quote.dte < self.config.min_dte:
            result.add_failure(
                ValidationFailure.EXPIRY_TOO_CLOSE,
                {"dte": quote.dte, "min_dte": self.config.min_dte}
            )

    def _validate_multiplier(self, quote: OptionQuote, result: OptionValidationResult) -> None:
        """Validate contract multiplier is sane."""
        expected_multipliers = {
            "SPY": 100, "SPX": 100, "VIX": 100,
            "HYG": 100, "EUFN": 100, "FEZ": 100
        }
        expected = expected_multipliers.get(quote.underlying, 100)

        if quote.multiplier != expected:
            result.add_failure(
                ValidationFailure.INVALID_MULTIPLIER,
                {"multiplier": quote.multiplier, "expected": expected}
            )

    def _suggest_alternatives(
        self,
        quote: OptionQuote,
        result: OptionValidationResult
    ) -> None:
        """Suggest alternative strikes for invalid options."""
        # Suggest nearby strikes (5% increments)
        strike = quote.strike
        alternatives = []

        if quote.option_type == "put":
            # For puts, suggest lower strikes (cheaper)
            for pct in [0.95, 0.90, 0.85]:
                alternatives.append(round(strike * pct))
        else:
            # For calls, suggest higher strikes (cheaper)
            for pct in [1.05, 1.10, 1.15]:
                alternatives.append(round(strike * pct))

        result.alternative_strikes = alternatives

        # Build recommended action
        actions = []
        if ValidationFailure.SPREAD_TOO_WIDE in result.failures:
            actions.append("use limit order at mid or better")
        if ValidationFailure.LOW_VOLUME in result.failures:
            actions.append("try alternative strike with more liquidity")
        if ValidationFailure.PREMIUM_TOO_HIGH in result.failures:
            actions.append("reduce quantity or choose further OTM strike")
        if ValidationFailure.PREMIUM_EXCEEDS_BUDGET in result.failures:
            actions.append("wait for budget refresh or reduce size")

        result.recommended_action = "; ".join(actions) if actions else "Skip this option"

    def _track_rejection(self, result: OptionValidationResult) -> None:
        """Track rejection metrics."""
        self.rejected_count += 1
        for failure in result.failures:
            self.rejection_reasons[failure.value] = \
                self.rejection_reasons.get(failure.value, 0) + 1

        logger.warning(
            f"Option rejected: {result.symbol} - {[f.value for f in result.failures]}"
        )

    def get_metrics(self) -> Dict[str, Any]:
        """Get validation metrics for monitoring."""
        return {
            "total_rejected": self.rejected_count,
            "rejection_reasons": self.rejection_reasons.copy()
        }

    def reset_metrics(self) -> None:
        """Reset metrics (e.g., daily)."""
        self.rejected_count = 0
        self.rejection_reasons.clear()


def create_option_quote_from_ibkr(
    ticker: Any,  # ib_insync.Ticker
    underlying: str,
    strike: float,
    expiry: date,
    option_type: str
) -> OptionQuote:
    """
    Create OptionQuote from IBKR ticker data.

    Args:
        ticker: ib_insync Ticker object
        underlying: Underlying symbol
        strike: Strike price
        expiry: Expiration date
        option_type: "call" or "put"

    Returns:
        OptionQuote object
    """
    return OptionQuote(
        symbol=f"{underlying}{strike}{option_type[0].upper()}{expiry.strftime('%y%m%d')}",
        underlying=underlying,
        strike=strike,
        expiry=expiry,
        option_type=option_type,
        bid=ticker.bid if hasattr(ticker, 'bid') and ticker.bid > 0 else None,
        ask=ticker.ask if hasattr(ticker, 'ask') and ticker.ask > 0 else None,
        last=ticker.last if hasattr(ticker, 'last') and ticker.last > 0 else None,
        volume=int(ticker.volume) if hasattr(ticker, 'volume') and ticker.volume else 0,
        open_interest=int(ticker.openInterest) if hasattr(ticker, 'openInterest') else 0,
        multiplier=100.0,
        timestamp=datetime.now()
    )
