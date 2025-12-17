"""
Market Calendars - Session timing and trading hours.

Provides:
- Market open/close times by exchange
- Session phase detection (pre-open, auction, regular, etc.)
- DST-aware time handling
- Holiday handling (basic)
- Venue-based liquidity windows (Phase 2)
- FX/Futures avoid windows
"""

from dataclasses import dataclass, field
from datetime import datetime, date, time, timedelta
from typing import Dict, Optional, Tuple, List, Any
from enum import Enum
import pytz


class SessionPhase(Enum):
    """Trading session phases."""
    PRE_OPEN = "pre_open"
    OPEN_AUCTION = "open_auction"
    REGULAR = "regular"
    CLOSE_AUCTION = "close_auction"
    POST_CLOSE = "post_close"
    CLOSED = "closed"


@dataclass
class MarketSession:
    """Definition of a market session."""
    exchange: str
    timezone: str
    pre_open_time: time      # When pre-market starts
    open_auction_start: time  # When opening auction starts
    open_time: time          # Regular session start
    close_auction_start: time  # When closing auction starts
    close_time: time         # Market close
    post_close_end: time     # When after-hours ends

    # Days market is open (0=Monday, 6=Sunday)
    trading_days: List[int] = None

    def __post_init__(self):
        if self.trading_days is None:
            self.trading_days = [0, 1, 2, 3, 4]  # Mon-Fri default


# Standard market definitions
MARKET_SESSIONS: Dict[str, MarketSession] = {
    "NYSE": MarketSession(
        exchange="NYSE",
        timezone="America/New_York",
        pre_open_time=time(4, 0),      # 4:00 AM
        open_auction_start=time(9, 28),  # 9:28 AM
        open_time=time(9, 30),          # 9:30 AM
        close_auction_start=time(15, 50),  # 3:50 PM
        close_time=time(16, 0),          # 4:00 PM
        post_close_end=time(20, 0),      # 8:00 PM
    ),
    "NASDAQ": MarketSession(
        exchange="NASDAQ",
        timezone="America/New_York",
        pre_open_time=time(4, 0),
        open_auction_start=time(9, 28),
        open_time=time(9, 30),
        close_auction_start=time(15, 55),
        close_time=time(16, 0),
        post_close_end=time(20, 0),
    ),
    "LSE": MarketSession(
        exchange="LSE",
        timezone="Europe/London",
        pre_open_time=time(7, 0),       # 7:00 AM
        open_auction_start=time(7, 50),  # 7:50 AM
        open_time=time(8, 0),           # 8:00 AM
        close_auction_start=time(16, 30),  # 4:30 PM
        close_time=time(16, 35),         # 4:35 PM
        post_close_end=time(17, 15),     # 5:15 PM
    ),
    "XETRA": MarketSession(
        exchange="XETRA",
        timezone="Europe/Berlin",
        pre_open_time=time(7, 0),       # 7:00 AM
        open_auction_start=time(8, 50),  # 8:50 AM
        open_time=time(9, 0),           # 9:00 AM
        close_auction_start=time(17, 30),  # 5:30 PM
        close_time=time(17, 35),         # 5:35 PM
        post_close_end=time(20, 0),
    ),
    "CME": MarketSession(
        exchange="CME",
        timezone="America/Chicago",
        pre_open_time=time(17, 0),      # 5:00 PM (previous day)
        open_auction_start=time(17, 0),
        open_time=time(17, 0),          # 23-hour trading
        close_auction_start=time(16, 0),
        close_time=time(16, 0),          # 4:00 PM
        post_close_end=time(17, 0),
        trading_days=[0, 1, 2, 3, 4, 6],  # Sun-Fri (electronic)
    ),
}

# Map exchange aliases
EXCHANGE_ALIASES = {
    "US": "NYSE",
    "SMART": "NYSE",
    "ARCA": "NYSE",
    "BATS": "NYSE",
    "IEX": "NYSE",
    "ISLAND": "NASDAQ",
    "LSE": "LSE",
    "LSEETF": "LSE",
    "IBIS": "XETRA",
    "XETRA": "XETRA",
    "GLOBEX": "CME",
    "CME": "CME",
}


class MarketCalendar:
    """
    Provides market session information.

    Note: This is a simplified implementation. For production,
    consider using exchange_calendars or pandas_market_calendars.
    """

    def __init__(self):
        self.sessions = MARKET_SESSIONS
        self.holidays: Dict[str, List[date]] = {}

    def get_session(self, exchange: str) -> Optional[MarketSession]:
        """Get session definition for an exchange."""
        # Resolve alias
        exchange = EXCHANGE_ALIASES.get(exchange.upper(), exchange.upper())
        return self.sessions.get(exchange)

    def get_session_phase(
        self,
        exchange: str,
        at_time: Optional[datetime] = None,
    ) -> SessionPhase:
        """
        Get current session phase for an exchange.

        Args:
            exchange: Exchange code
            at_time: Time to check (default: now)

        Returns:
            Current SessionPhase
        """
        session = self.get_session(exchange)
        if session is None:
            return SessionPhase.CLOSED

        # Get current time in exchange timezone
        tz = pytz.timezone(session.timezone)
        if at_time is None:
            at_time = datetime.now(pytz.UTC)
        local_time = at_time.astimezone(tz)

        # Check if trading day
        if local_time.weekday() not in session.trading_days:
            return SessionPhase.CLOSED

        # Check if holiday
        if self._is_holiday(exchange, local_time.date()):
            return SessionPhase.CLOSED

        # Get current time of day
        current = local_time.time()

        # Determine phase
        if current < session.pre_open_time:
            return SessionPhase.CLOSED
        elif current < session.open_auction_start:
            return SessionPhase.PRE_OPEN
        elif current < session.open_time:
            return SessionPhase.OPEN_AUCTION
        elif current < session.close_auction_start:
            return SessionPhase.REGULAR
        elif current < session.close_time:
            return SessionPhase.CLOSE_AUCTION
        elif current < session.post_close_end:
            return SessionPhase.POST_CLOSE
        else:
            return SessionPhase.CLOSED

    def is_market_open(
        self,
        exchange: str,
        at_time: Optional[datetime] = None,
    ) -> bool:
        """Check if market is open for regular trading."""
        phase = self.get_session_phase(exchange, at_time)
        return phase in (SessionPhase.REGULAR, SessionPhase.OPEN_AUCTION, SessionPhase.CLOSE_AUCTION)

    def time_until_open(
        self,
        exchange: str,
        from_time: Optional[datetime] = None,
    ) -> Optional[timedelta]:
        """Get time until market opens."""
        session = self.get_session(exchange)
        if session is None:
            return None

        tz = pytz.timezone(session.timezone)
        if from_time is None:
            from_time = datetime.now(pytz.UTC)
        local_time = from_time.astimezone(tz)

        # If market is open, return 0
        if self.is_market_open(exchange, from_time):
            return timedelta(0)

        # Find next open
        current_date = local_time.date()
        for days_ahead in range(7):
            check_date = current_date + timedelta(days=days_ahead)
            if check_date.weekday() not in session.trading_days:
                continue
            if self._is_holiday(exchange, check_date):
                continue

            # Check if we can open today
            open_dt = datetime.combine(check_date, session.open_time)
            open_dt = tz.localize(open_dt)

            if open_dt > local_time:
                return open_dt - local_time

        return None

    def time_until_close(
        self,
        exchange: str,
        from_time: Optional[datetime] = None,
    ) -> Optional[timedelta]:
        """Get time until market closes."""
        session = self.get_session(exchange)
        if session is None:
            return None

        if not self.is_market_open(exchange, from_time):
            return None

        tz = pytz.timezone(session.timezone)
        if from_time is None:
            from_time = datetime.now(pytz.UTC)
        local_time = from_time.astimezone(tz)

        close_dt = datetime.combine(local_time.date(), session.close_time)
        close_dt = tz.localize(close_dt)

        if close_dt > local_time:
            return close_dt - local_time
        return timedelta(0)

    def minutes_since_open(
        self,
        exchange: str,
        at_time: Optional[datetime] = None,
    ) -> Optional[int]:
        """Get minutes since market opened."""
        session = self.get_session(exchange)
        if session is None:
            return None

        phase = self.get_session_phase(exchange, at_time)
        if phase not in (SessionPhase.REGULAR, SessionPhase.CLOSE_AUCTION):
            return None

        tz = pytz.timezone(session.timezone)
        if at_time is None:
            at_time = datetime.now(pytz.UTC)
        local_time = at_time.astimezone(tz)

        open_dt = datetime.combine(local_time.date(), session.open_time)
        open_dt = tz.localize(open_dt)

        diff = local_time - open_dt
        return int(diff.total_seconds() / 60)

    def minutes_until_close(
        self,
        exchange: str,
        at_time: Optional[datetime] = None,
    ) -> Optional[int]:
        """Get minutes until market closes."""
        time_remaining = self.time_until_close(exchange, at_time)
        if time_remaining is None:
            return None
        return int(time_remaining.total_seconds() / 60)

    def add_holiday(self, exchange: str, holiday_date: date) -> None:
        """Add a holiday for an exchange."""
        exchange = EXCHANGE_ALIASES.get(exchange.upper(), exchange.upper())
        if exchange not in self.holidays:
            self.holidays[exchange] = []
        self.holidays[exchange].append(holiday_date)

    def _is_holiday(self, exchange: str, check_date: date) -> bool:
        """Check if date is a holiday."""
        exchange = EXCHANGE_ALIASES.get(exchange.upper(), exchange.upper())
        return check_date in self.holidays.get(exchange, [])


# Singleton calendar instance
_calendar_instance: Optional[MarketCalendar] = None

# Global flag to force execution regardless of market hours
# Set to True to bypass all trading time checks (use for testing/manual runs)
FORCE_EXECUTION: bool = False


def get_market_calendar() -> MarketCalendar:
    """Get singleton MarketCalendar instance."""
    global _calendar_instance
    if _calendar_instance is None:
        _calendar_instance = MarketCalendar()
    return _calendar_instance


def is_market_open(
    exchange: str,
    at_time: Optional[datetime] = None,
) -> bool:
    """Convenience function to check if market is open."""
    return get_market_calendar().is_market_open(exchange, at_time)


def get_session_phase(
    exchange: str,
    at_time: Optional[datetime] = None,
) -> str:
    """Convenience function to get session phase as string."""
    phase = get_market_calendar().get_session_phase(exchange, at_time)
    return phase.value


def should_avoid_trading(
    exchange: str,
    avoid_first_minutes: int = 15,
    avoid_last_minutes: int = 10,
    at_time: Optional[datetime] = None,
) -> Tuple[bool, str]:
    """
    Check if trading should be avoided at current time.

    Args:
        exchange: Exchange code
        avoid_first_minutes: Minutes after open to avoid
        avoid_last_minutes: Minutes before close to avoid
        at_time: Time to check

    Returns:
        Tuple of (should_avoid, reason)
    """
    # Check global force flag first
    if FORCE_EXECUTION:
        return False, "FORCED"

    calendar = get_market_calendar()

    phase = calendar.get_session_phase(exchange, at_time)

    if phase == SessionPhase.CLOSED:
        return True, "Market closed"

    if phase == SessionPhase.PRE_OPEN:
        return True, "Pre-market"

    if phase == SessionPhase.POST_CLOSE:
        return True, "After-hours"

    if phase == SessionPhase.OPEN_AUCTION:
        return True, "Opening auction in progress"

    if phase == SessionPhase.CLOSE_AUCTION:
        return True, "Closing auction in progress"

    # Check proximity to open/close
    minutes_since = calendar.minutes_since_open(exchange, at_time)
    if minutes_since is not None and minutes_since < avoid_first_minutes:
        return True, f"Too close to open ({minutes_since} min)"

    minutes_until = calendar.minutes_until_close(exchange, at_time)
    if minutes_until is not None and minutes_until < avoid_last_minutes:
        return True, f"Too close to close ({minutes_until} min)"

    return False, "OK"


# =============================================================================
# PHASE 2: Venue-based Liquidity Windows
# =============================================================================

@dataclass
class LiquidityWindow:
    """Time window when trading is preferred for a venue."""
    venue: str
    start_utc: datetime
    end_utc: datetime
    style: str = "MIDDAY"  # MIDDAY, CLOSE_AUCTION, OPEN_AUCTION

    def is_active(self, now_utc: datetime) -> bool:
        """Check if window is currently active."""
        return self.start_utc <= now_utc <= self.end_utc

    def time_until_start(self, now_utc: datetime) -> Optional[timedelta]:
        """Get time until window starts, or None if past."""
        if now_utc >= self.end_utc:
            return None
        if now_utc >= self.start_utc:
            return timedelta(0)
        return self.start_utc - now_utc


@dataclass
class VenueConfig:
    """Configuration for a trading venue."""
    venue: str
    timezone: str
    primary_exchange: str
    open_offset_minutes: int = 15
    close_offset_minutes: int = 10
    close_auction_minutes: int = 5
    allow_open_auction: bool = False
    allow_close_auction: bool = True
    avoid_windows_utc: List[Tuple[str, str]] = field(default_factory=list)


# Default venue configurations
DEFAULT_VENUE_CONFIGS: Dict[str, VenueConfig] = {
    "EU": VenueConfig(
        venue="EU",
        timezone="Europe/Berlin",
        primary_exchange="XETRA",
        open_offset_minutes=15,
        close_offset_minutes=10,
        close_auction_minutes=5,
        allow_open_auction=False,
        allow_close_auction=True,
    ),
    "US": VenueConfig(
        venue="US",
        timezone="America/New_York",
        primary_exchange="NYSE",
        open_offset_minutes=15,
        close_offset_minutes=10,
        close_auction_minutes=5,
        allow_open_auction=False,
        allow_close_auction=True,
    ),
    "FX": VenueConfig(
        venue="FX",
        timezone="UTC",
        primary_exchange="IDEALPRO",
        open_offset_minutes=0,
        close_offset_minutes=0,
        allow_open_auction=False,
        allow_close_auction=False,
        avoid_windows_utc=[("21:55", "22:10")],  # FX roll window
    ),
    "FUT": VenueConfig(
        venue="FUT",
        timezone="UTC",
        primary_exchange="GLOBEX",
        open_offset_minutes=0,
        close_offset_minutes=0,
        allow_open_auction=False,
        allow_close_auction=False,
        avoid_windows_utc=[("21:55", "22:10")],  # Maintenance window
    ),
}


class VenueLiquidityManager:
    """
    Manages liquidity windows for different venues.

    Phase 2 Enhancement: Provides venue-specific trading windows
    with configurable offsets and avoid periods.
    """

    def __init__(self, venue_configs: Optional[Dict[str, VenueConfig]] = None):
        self.venue_configs = venue_configs or DEFAULT_VENUE_CONFIGS
        self.calendar = get_market_calendar()

    def get_liquidity_window(
        self,
        venue: str,
        for_date: date,
        style: str = "MIDDAY",
    ) -> Optional[LiquidityWindow]:
        """
        Get the liquidity window for a venue on a given date.

        Args:
            venue: Venue code (EU, US, FX, FUT)
            for_date: Date to get window for
            style: MIDDAY, CLOSE_AUCTION, or OPEN_AUCTION

        Returns:
            LiquidityWindow or None if no valid window
        """
        config = self.venue_configs.get(venue)
        if config is None:
            return None

        # Get exchange session
        session = self.calendar.get_session(config.primary_exchange)
        if session is None:
            return None

        tz = pytz.timezone(config.timezone)

        # Check if trading day
        if for_date.weekday() not in session.trading_days:
            return None

        if style == "CLOSE_AUCTION" and config.allow_close_auction:
            # Close auction window
            auction_start = datetime.combine(for_date, session.close_auction_start)
            auction_end = datetime.combine(for_date, session.close_time)
            auction_start = tz.localize(auction_start)
            auction_end = tz.localize(auction_end)

            return LiquidityWindow(
                venue=venue,
                start_utc=auction_start.astimezone(pytz.UTC),
                end_utc=auction_end.astimezone(pytz.UTC),
                style="CLOSE_AUCTION",
            )

        elif style == "OPEN_AUCTION" and config.allow_open_auction:
            # Open auction window
            auction_start = datetime.combine(for_date, session.open_auction_start)
            auction_end = datetime.combine(for_date, session.open_time)
            auction_start = tz.localize(auction_start)
            auction_end = tz.localize(auction_end)

            return LiquidityWindow(
                venue=venue,
                start_utc=auction_start.astimezone(pytz.UTC),
                end_utc=auction_end.astimezone(pytz.UTC),
                style="OPEN_AUCTION",
            )

        else:
            # MIDDAY - regular session with offsets
            open_time = datetime.combine(for_date, session.open_time)
            close_time = datetime.combine(for_date, session.close_auction_start)

            open_time = tz.localize(open_time)
            close_time = tz.localize(close_time)

            # Apply offsets
            start_utc = open_time + timedelta(minutes=config.open_offset_minutes)
            end_utc = close_time - timedelta(minutes=config.close_offset_minutes)

            return LiquidityWindow(
                venue=venue,
                start_utc=start_utc.astimezone(pytz.UTC),
                end_utc=end_utc.astimezone(pytz.UTC),
                style="MIDDAY",
            )

    def get_close_auction_window(
        self,
        venue: str,
        for_date: date,
    ) -> Optional[LiquidityWindow]:
        """Get the close auction window for a venue."""
        return self.get_liquidity_window(venue, for_date, style="CLOSE_AUCTION")

    def is_within_window(
        self,
        now_utc: datetime,
        window: LiquidityWindow,
    ) -> bool:
        """Check if current time is within a window."""
        return window.is_active(now_utc)

    def next_window_start(
        self,
        venue: str,
        from_utc: datetime,
        style: str = "MIDDAY",
        max_days_ahead: int = 5,
    ) -> Optional[datetime]:
        """
        Find the next window start time for a venue.

        Args:
            venue: Venue code
            from_utc: Start searching from this time
            style: Window style
            max_days_ahead: Max days to search

        Returns:
            Next window start time (UTC) or None
        """
        current_date = from_utc.date()

        for days_ahead in range(max_days_ahead):
            check_date = current_date + timedelta(days=days_ahead)
            window = self.get_liquidity_window(venue, check_date, style)

            if window is None:
                continue

            # If window is in the future, return its start
            if window.start_utc > from_utc:
                return window.start_utc

            # If we're currently in the window, return now
            if window.is_active(from_utc):
                return from_utc

        return None

    def should_avoid_venue(
        self,
        venue: str,
        at_time: datetime,
    ) -> Tuple[bool, str]:
        """
        Check if trading should be avoided for a venue at given time.

        Args:
            venue: Venue code
            at_time: Time to check (UTC)

        Returns:
            Tuple of (should_avoid, reason)
        """
        # Check global force flag first
        if FORCE_EXECUTION:
            return False, "FORCED"

        config = self.venue_configs.get(venue)
        if config is None:
            return True, f"Unknown venue: {venue}"

        # Check avoid windows (FX/FUT specific)
        for avoid_start_str, avoid_end_str in config.avoid_windows_utc:
            avoid_start = time.fromisoformat(avoid_start_str)
            avoid_end = time.fromisoformat(avoid_end_str)
            current_time = at_time.time()

            # Handle overnight windows
            if avoid_start > avoid_end:
                # Overnight window (e.g., 21:55 to 22:10)
                if current_time >= avoid_start or current_time <= avoid_end:
                    return True, f"Within avoid window ({avoid_start_str}-{avoid_end_str})"
            else:
                if avoid_start <= current_time <= avoid_end:
                    return True, f"Within avoid window ({avoid_start_str}-{avoid_end_str})"

        # For equity venues, check exchange status
        if venue in ("EU", "US"):
            return should_avoid_trading(
                config.primary_exchange,
                avoid_first_minutes=config.open_offset_minutes,
                avoid_last_minutes=config.close_offset_minutes,
                at_time=at_time,
            )

        return False, "OK"

    def get_executable_venues(
        self,
        at_time: datetime,
    ) -> List[str]:
        """Get list of venues that can be traded right now."""
        executable = []
        for venue in self.venue_configs:
            should_avoid, reason = self.should_avoid_venue(venue, at_time)
            if not should_avoid:
                executable.append(venue)
        return executable

    def configure_venue(
        self,
        venue: str,
        settings: Dict[str, Any],
    ) -> None:
        """
        Configure a venue from settings dictionary.

        Args:
            venue: Venue code
            settings: Configuration dictionary from settings.yaml
        """
        existing = self.venue_configs.get(venue, DEFAULT_VENUE_CONFIGS.get(venue))
        if existing is None:
            return

        # Update from settings
        self.venue_configs[venue] = VenueConfig(
            venue=venue,
            timezone=settings.get("timezone", existing.timezone),
            primary_exchange=existing.primary_exchange,
            open_offset_minutes=settings.get("open_offset_minutes", existing.open_offset_minutes),
            close_offset_minutes=settings.get("close_offset_minutes", existing.close_offset_minutes),
            close_auction_minutes=settings.get("close_auction_minutes", existing.close_auction_minutes),
            allow_open_auction=settings.get("allow_open_auction", existing.allow_open_auction),
            allow_close_auction=settings.get("allow_close_auction", existing.allow_close_auction),
            avoid_windows_utc=[
                (w[0], w[1]) for w in settings.get("avoid_window_utc", [])
            ] if "avoid_window_utc" in settings else existing.avoid_windows_utc,
        )


# Singleton instance
_venue_manager: Optional[VenueLiquidityManager] = None


def get_venue_manager() -> VenueLiquidityManager:
    """Get singleton VenueLiquidityManager instance."""
    global _venue_manager
    if _venue_manager is None:
        _venue_manager = VenueLiquidityManager()
    return _venue_manager


def get_liquidity_window(
    venue: str,
    for_date: date,
    style: str = "MIDDAY",
) -> Optional[LiquidityWindow]:
    """Convenience function to get liquidity window."""
    return get_venue_manager().get_liquidity_window(venue, for_date, style)


def is_within_liquidity_window(
    venue: str,
    at_time: datetime,
    style: str = "MIDDAY",
) -> bool:
    """Check if current time is within a venue's liquidity window."""
    window = get_liquidity_window(venue, at_time.date(), style)
    if window is None:
        return False
    return window.is_active(at_time)
