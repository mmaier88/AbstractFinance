"""
Option Contract Factory for AbstractFinance.

Resolves abstract option hedge instruments (vix_call, vstoxx_call, etc.)
to actual tradeable IBKR option contracts with specific strikes and expiries.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class OptionContractSpec:
    """Specification for an option contract."""
    underlying: str
    option_type: str  # "CALL" or "PUT"
    exchange: str
    currency: str
    multiplier: float
    sec_type: str = "OPT"  # OPT for index options, FOP for futures options
    min_dte: int = 20
    max_dte: int = 90
    preferred_dte: int = 45
    strike_offset_pct: float = 0.05  # 5% OTM by default
    description: str = ""


@dataclass
class OptionSelection:
    """Selected option contract for trading."""
    con_id: Optional[int]
    underlying: str
    symbol: str
    option_type: str  # "C" or "P"
    strike: float
    expiry: str  # YYYYMMDD format
    exchange: str
    currency: str
    multiplier: float
    dte: int
    last_price: Optional[float] = None
    selected_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for persistence."""
        return {
            "con_id": self.con_id,
            "underlying": self.underlying,
            "symbol": self.symbol,
            "option_type": self.option_type,
            "strike": self.strike,
            "expiry": self.expiry,
            "exchange": self.exchange,
            "currency": self.currency,
            "multiplier": self.multiplier,
            "dte": self.dte,
            "last_price": self.last_price,
            "selected_at": self.selected_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OptionSelection":
        """Create from dictionary."""
        selected_at = data.get("selected_at")
        if isinstance(selected_at, str):
            selected_at = datetime.fromisoformat(selected_at)
        else:
            selected_at = datetime.now()

        return cls(
            con_id=data.get("con_id"),
            underlying=data["underlying"],
            symbol=data["symbol"],
            option_type=data["option_type"],
            strike=data["strike"],
            expiry=data["expiry"],
            exchange=data["exchange"],
            currency=data["currency"],
            multiplier=data["multiplier"],
            dte=data.get("dte", 0),
            last_price=data.get("last_price"),
            selected_at=selected_at,
        )


class OptionContractFactory:
    """
    Factory for resolving abstract option instruments to concrete contracts.

    Given abstract instrument IDs like 'vix_call' or 'vstoxx_call', selects
    an appropriate option contract from IBKR's option chain based on:
    - Target DTE (days to expiration)
    - Strike selection rules (OTM percentage)
    - Liquidity considerations

    Persists selections to avoid re-selection on every run.
    """

    # Abstract instrument -> contract specification mapping
    OPTION_SPECS: Dict[str, OptionContractSpec] = {
        "vix_call": OptionContractSpec(
            underlying="VIX",
            option_type="CALL",
            exchange="CBOE",
            currency="USD",
            multiplier=100.0,
            sec_type="OPT",
            min_dte=20,
            max_dte=60,
            preferred_dte=30,
            strike_offset_pct=0.15,  # 15% OTM for VIX calls
            description="VIX Index Call Option",
        ),
        "vstoxx_call": OptionContractSpec(
            underlying="V2X",
            option_type="CALL",
            exchange="EUREX",
            currency="EUR",
            multiplier=100.0,
            sec_type="FOP",  # Futures option
            min_dte=20,
            max_dte=90,
            preferred_dte=45,
            strike_offset_pct=0.20,  # 20% OTM for VSTOXX
            description="VSTOXX Futures Call Option",
        ),
        "sx5e_put": OptionContractSpec(
            underlying="ESTX50",
            option_type="PUT",
            exchange="EUREX",
            currency="EUR",
            multiplier=10.0,
            sec_type="OPT",
            min_dte=30,
            max_dte=90,
            preferred_dte=60,
            strike_offset_pct=0.10,  # 10% OTM for SX5E puts
            description="Euro STOXX 50 Index Put Option",
        ),
        "eu_bank_put": OptionContractSpec(
            underlying="SX7E",
            option_type="PUT",
            exchange="EUREX",
            currency="EUR",
            multiplier=10.0,
            sec_type="OPT",
            min_dte=30,
            max_dte=90,
            preferred_dte=45,
            strike_offset_pct=0.15,  # 15% OTM for EU bank puts
            description="Euro STOXX Banks Index Put Option",
        ),
        "hyg_put": OptionContractSpec(
            underlying="HYG",
            option_type="PUT",
            exchange="ARCA",
            currency="USD",
            multiplier=100.0,
            sec_type="OPT",
            min_dte=20,
            max_dte=60,
            preferred_dte=30,
            strike_offset_pct=0.05,  # 5% OTM for HYG puts
            description="HYG High Yield ETF Put Option",
        ),
    }

    CACHE_FILE = "state/option_selections.json"

    def __init__(
        self,
        ib_client: Optional[Any] = None,
        reference_price_resolver: Optional[Any] = None,
        cache_file: Optional[str] = None,
    ):
        """
        Initialize option contract factory.

        Args:
            ib_client: IBClient instance for IBKR option chain queries
            reference_price_resolver: Resolver for underlying prices
            cache_file: Path to selection cache file
        """
        self.ib_client = ib_client
        self.price_resolver = reference_price_resolver
        self.cache_file = Path(cache_file or self.CACHE_FILE)

        # Cache of selected contracts
        self._selections: Dict[str, OptionSelection] = {}

        # Load persisted selections
        self._load_selections()

    def _load_selections(self) -> None:
        """Load persisted option selections."""
        try:
            if self.cache_file.exists():
                with open(self.cache_file, 'r') as f:
                    data = json.load(f)
                    for inst_id, selection_data in data.get("selections", {}).items():
                        self._selections[inst_id] = OptionSelection.from_dict(selection_data)
                logger.info(f"Loaded {len(self._selections)} option selections from cache")
        except Exception as e:
            logger.warning(f"Failed to load option selections: {e}")
            self._selections = {}

    def _save_selections(self) -> None:
        """Persist option selections."""
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)

            data = {
                "selections": {
                    inst_id: sel.to_dict()
                    for inst_id, sel in self._selections.items()
                },
                "last_updated": datetime.now().isoformat(),
            }

            with open(self.cache_file, 'w') as f:
                json.dump(data, f, indent=2)

        except Exception as e:
            logger.warning(f"Failed to save option selections: {e}")

    def get_contract(
        self,
        instrument_id: str,
        force_refresh: bool = False,
    ) -> Optional[OptionSelection]:
        """
        Get option contract for abstract instrument.

        Args:
            instrument_id: Abstract instrument ID (e.g., 'vix_call')
            force_refresh: Force re-selection even if cached

        Returns:
            OptionSelection if found, None otherwise
        """
        # Check if we have a valid cached selection
        if not force_refresh and instrument_id in self._selections:
            selection = self._selections[instrument_id]

            # Validate selection is still valid (not expired, DTE > 5)
            if self._is_selection_valid(selection):
                logger.debug(f"Using cached selection for {instrument_id}: {selection.symbol}")
                return selection

        # Get spec for this instrument
        spec = self.OPTION_SPECS.get(instrument_id)
        if not spec:
            logger.warning(f"No option spec found for {instrument_id}")
            return None

        # Select new contract
        selection = self._select_contract(instrument_id, spec)

        if selection:
            self._selections[instrument_id] = selection
            self._save_selections()

        return selection

    def _is_selection_valid(self, selection: OptionSelection) -> bool:
        """Check if a selection is still valid for trading."""
        if not selection.expiry:
            return False

        try:
            expiry_date = datetime.strptime(selection.expiry, "%Y%m%d")
            dte = (expiry_date - datetime.now()).days

            # Need at least 5 DTE
            if dte < 5:
                logger.info(f"Selection {selection.symbol} expired (DTE={dte})")
                return False

            return True

        except (ValueError, TypeError):
            return False

    def _select_contract(
        self,
        instrument_id: str,
        spec: OptionContractSpec,
    ) -> Optional[OptionSelection]:
        """
        Select optimal option contract based on spec.

        Args:
            instrument_id: Abstract instrument ID
            spec: Option specification

        Returns:
            OptionSelection if successful, None otherwise
        """
        # Get underlying price
        underlying_price = self._get_underlying_price(spec.underlying)
        if not underlying_price:
            logger.warning(f"Cannot get underlying price for {spec.underlying}")
            return self._create_fallback_selection(instrument_id, spec)

        # Calculate target strike
        if spec.option_type == "CALL":
            # OTM call = above current price
            target_strike = underlying_price * (1 + spec.strike_offset_pct)
        else:
            # OTM put = below current price
            target_strike = underlying_price * (1 - spec.strike_offset_pct)

        # Calculate target expiry
        target_expiry = datetime.now() + timedelta(days=spec.preferred_dte)

        # If we have IBKR connection, query option chain
        if self.ib_client and self.ib_client.is_connected():
            selection = self._query_option_chain(
                instrument_id, spec, underlying_price, target_strike, target_expiry
            )
            if selection:
                return selection

        # Fallback: create synthetic selection
        return self._create_fallback_selection(
            instrument_id, spec, underlying_price, target_strike, target_expiry
        )

    def _get_underlying_price(self, underlying: str) -> Optional[float]:
        """Get current price of underlying."""
        if self.price_resolver:
            result = self.price_resolver.get_reference_price(underlying)
            if result and result.is_valid:
                return result.price

        # Use default guardrails
        defaults = {
            "VIX": 18.0,
            "V2X": 20.0,
            "ESTX50": 4800.0,
            "SX5E": 4800.0,
            "SX7E": 100.0,
            "HYG": 75.0,
            "FVS": 20.0,
        }

        return defaults.get(underlying)

    def _query_option_chain(
        self,
        instrument_id: str,
        spec: OptionContractSpec,
        underlying_price: float,
        target_strike: float,
        target_expiry: datetime,
    ) -> Optional[OptionSelection]:
        """Query IBKR for option chain and select best contract."""
        try:
            from ib_insync import Option, Index, Future

            # Build underlying contract
            if spec.sec_type == "FOP":
                # Futures option - need underlying future
                underlying_contract = Future(
                    symbol=spec.underlying,
                    exchange=spec.exchange,
                    currency=spec.currency,
                )
            else:
                # Index option
                underlying_contract = Index(
                    symbol=spec.underlying,
                    exchange=spec.exchange,
                    currency=spec.currency,
                )

            # Qualify underlying
            self.ib_client.ib.qualifyContracts(underlying_contract)

            # Request option chain
            chains = self.ib_client.ib.reqSecDefOptParams(
                underlying_contract.symbol,
                "",  # futFopExchange
                underlying_contract.secType,
                underlying_contract.conId,
            )

            if not chains:
                logger.warning(f"No option chains returned for {spec.underlying}")
                return None

            # Find best matching contract
            best_option = self._find_best_option(
                chains, spec, target_strike, target_expiry
            )

            if best_option:
                # Build and qualify the option contract
                option_contract = Option(
                    symbol=spec.underlying,
                    lastTradeDateOrContractMonth=best_option["expiry"],
                    strike=best_option["strike"],
                    right=spec.option_type[0],  # "C" or "P"
                    exchange=spec.exchange,
                    currency=spec.currency,
                )

                qualified = self.ib_client.ib.qualifyContracts(option_contract)

                if qualified:
                    contract = qualified[0]
                    expiry_date = datetime.strptime(best_option["expiry"], "%Y%m%d")
                    dte = (expiry_date - datetime.now()).days

                    return OptionSelection(
                        con_id=contract.conId,
                        underlying=spec.underlying,
                        symbol=contract.localSymbol or f"{spec.underlying}{best_option['expiry']}{spec.option_type[0]}{best_option['strike']}",
                        option_type=spec.option_type[0],
                        strike=best_option["strike"],
                        expiry=best_option["expiry"],
                        exchange=spec.exchange,
                        currency=spec.currency,
                        multiplier=spec.multiplier,
                        dte=dte,
                    )

        except Exception as e:
            logger.warning(f"Option chain query failed for {instrument_id}: {e}")

        return None

    def _find_best_option(
        self,
        chains: List[Any],
        spec: OptionContractSpec,
        target_strike: float,
        target_expiry: datetime,
    ) -> Optional[Dict[str, Any]]:
        """Find best matching option from chain data."""
        best_score = float('inf')
        best_option = None

        for chain in chains:
            # Filter by exchange if needed
            if chain.exchange != spec.exchange:
                continue

            # Check available expirations
            for expiry in chain.expirations:
                try:
                    expiry_date = datetime.strptime(expiry, "%Y%m%d")
                    dte = (expiry_date - datetime.now()).days

                    # Check DTE bounds
                    if dte < spec.min_dte or dte > spec.max_dte:
                        continue

                    # Check strikes
                    for strike in chain.strikes:
                        # Score based on distance from targets
                        dte_diff = abs(dte - spec.preferred_dte)
                        strike_diff = abs(strike - target_strike) / target_strike * 100

                        # Combined score (lower is better)
                        score = dte_diff * 2 + strike_diff * 10

                        if score < best_score:
                            best_score = score
                            best_option = {
                                "expiry": expiry,
                                "strike": strike,
                                "dte": dte,
                            }

                except (ValueError, TypeError):
                    continue

        return best_option

    def _create_fallback_selection(
        self,
        instrument_id: str,
        spec: OptionContractSpec,
        underlying_price: Optional[float] = None,
        target_strike: Optional[float] = None,
        target_expiry: Optional[datetime] = None,
    ) -> OptionSelection:
        """Create synthetic selection when IBKR query fails."""
        # Use defaults if not provided
        if underlying_price is None:
            underlying_price = self._get_underlying_price(spec.underlying) or 100.0

        if target_strike is None:
            if spec.option_type == "CALL":
                target_strike = underlying_price * (1 + spec.strike_offset_pct)
            else:
                target_strike = underlying_price * (1 - spec.strike_offset_pct)

        if target_expiry is None:
            target_expiry = datetime.now() + timedelta(days=spec.preferred_dte)

        # Round strike to sensible increment
        if underlying_price > 1000:
            strike = round(target_strike / 25) * 25
        elif underlying_price > 100:
            strike = round(target_strike / 5) * 5
        else:
            strike = round(target_strike, 1)

        expiry = target_expiry.strftime("%Y%m%d")
        dte = (target_expiry - datetime.now()).days

        logger.info(
            f"Created fallback selection for {instrument_id}: "
            f"{spec.underlying} {spec.option_type} {strike} exp {expiry}"
        )

        return OptionSelection(
            con_id=None,  # No conId for fallback
            underlying=spec.underlying,
            symbol=f"{spec.underlying}_{expiry}_{spec.option_type[0]}_{strike}",
            option_type=spec.option_type[0],
            strike=strike,
            expiry=expiry,
            exchange=spec.exchange,
            currency=spec.currency,
            multiplier=spec.multiplier,
            dte=dte,
        )

    def refresh_all(self) -> Dict[str, OptionSelection]:
        """Refresh all option selections."""
        results = {}

        for instrument_id in self.OPTION_SPECS:
            selection = self.get_contract(instrument_id, force_refresh=True)
            if selection:
                results[instrument_id] = selection

        return results

    def get_all_selections(self) -> Dict[str, OptionSelection]:
        """Get all current selections."""
        return self._selections.copy()

    def clear_cache(self) -> None:
        """Clear all cached selections."""
        self._selections = {}
        if self.cache_file.exists():
            self.cache_file.unlink()
