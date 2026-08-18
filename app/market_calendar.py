"""Is the US options market open right now, and which trading day is it.

WHY THIS EXISTS (epic С-21). Measured on collected data: between two adjacent
weekend snapshots of SPY, not one of 14,230 contracts changed its price, bid,
volume or open interest — while implied volatility changed on 13,727 of them.
That IV movement is the provider recomputing from a frozen price and a
shrinking time to expiry: arithmetic that follows from the clock ticking, not
an observation of a market. Writing 96 such snapshots a day costs disk and
provider quota to record nothing, and it actively breaks three views — OI Delta
compares two copies of Friday, the Unusual Activity baseline takes two sevenths
of its days from duplicates, and the volatility chart draws a rise on a closed
market.

TWO SOURCES, IN THIS ORDER. The provider is asked first because it is
authoritative: Massive's own status endpoint knows about holidays and half-days,
which a hardcoded calendar would not and which would go stale once a year
without anybody noticing. The clock is the fallback, for Yahoo (no such
endpoint) and for the minutes when the provider is unreachable.

EXTENDED HOURS COUNT AS CLOSED, and this is a measurement rather than an
opinion: in the pre-market window the underlying price inside the chain
snapshot is frozen (one snapshot of 83 moved), options do not trade, and on
Friday 14.08 volume stopped moving at 20:31 UTC — one minute after the close.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

MARKET_TZ = ZoneInfo("America/New_York")
OPEN_TIME = dt.time(9, 30)
CLOSE_TIME = dt.time(16, 0)

OPEN = "open"
CLOSED = "closed"

# Long enough that the worker and the app asking in the same cycle produce one
# request, short enough to be irrelevant next to a 15-minute collection cycle.
CACHE_SECONDS = 60

_cache: dict[str, tuple[float, str, str]] = {}


def to_market_time(moment: dt.datetime | None = None) -> dt.datetime:
    """A UTC (or naive-UTC) instant as New York wall time.

    Naive input is read as UTC, because that is what this application stores
    everywhere — `collected_at`, `started_at` and everything derived from them.
    """
    moment = moment or dt.datetime.now(dt.timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(MARKET_TZ)


def market_day(moment: dt.datetime | None = None) -> dt.date:
    """The trading date an instant belongs to.

    New York's date, never UTC's. A snapshot taken at 01:15 UTC on Saturday is
    Friday 21:15 in New York — the evening of the same trading day — and
    counting it as Saturday would spend the weekend's single collection slot
    on Friday night and then miss Saturday entirely.
    """
    return to_market_time(moment).date()


def last_completed_trading_day(moment: dt.datetime | None = None) -> dt.date:
    """The most recent trading day that is fully behind us.

    Used by anything whose input is "whole days only": the Unusual Activity
    baseline deliberately excludes today, because volume accumulates within a
    session and a partial figure is not comparable with completed ones.

    Calendar days are the wrong unit for that question. On a Monday
    "yesterday" is Sunday, which was never collected — so a baseline built
    through Friday looks stale, gets rebuilt on every pass, and produces the
    same numbers each time. Weekends made it every cycle for two days.
    """
    day = market_day(moment) - dt.timedelta(days=1)
    while day.isoweekday() > 5:
        day -= dt.timedelta(days=1)
    return day


def state_from_clock(moment: dt.datetime | None = None) -> str:
    """Regular session only: weekdays, 09:30–16:00 New York.

    Via zoneinfo rather than a fixed offset: the US and Europe change to summer
    time on different dates, so "UTC minus four hours" is wrong twice a year
    for a week at a time — and wrong in the direction of thinking the market is
    already open when it is not.

    KNOWN GAP: this path does not know about holidays. On Thanksgiving it says
    "open" and we collect one day of duplicates. Accepted deliberately — a
    handful of days a year is cheaper than a holiday table somebody has to
    remember to update, and providers that answer the status endpoint never
    reach this code.
    """
    local = to_market_time(moment)
    if local.isoweekday() > 5:
        return CLOSED
    return OPEN if OPEN_TIME <= local.time() < CLOSE_TIME else CLOSED


def seconds_until_open(moment: dt.datetime | None = None) -> float:
    """Seconds from now to the next regular-session open in New York.

    Built by walking forward to the next weekday 09:30 rather than by
    arithmetic on offsets: constructing the wall time and letting zoneinfo
    resolve it is what makes this correct across a daylight-saving change,
    where the gap between "now" and "09:30 tomorrow" is 23 or 25 hours rather
    than 24.

    Holidays are not consulted — this only shortens a sleep, and waking at
    09:30 on a holiday costs one idle cycle. The provider still has the last
    word on whether anything is collected.
    """
    local = to_market_time(moment)
    # EVERY comparison and subtraction below happens in UTC, and that is the
    # whole correctness argument. Python subtracts two aware datetimes by
    # wall clock — ignoring their offsets entirely — when they share the same
    # tzinfo OBJECT, and zoneinfo caches its instances, so every datetime in
    # this module carries literally the same ZoneInfo. Across a daylight-saving
    # change that silently drops the hour: measured 17.08, Saturday 07.03 to
    # the following Monday's open came back as 45.5 hours instead of 44.5, and
    # the worker would have woken an hour after the opening bell rather than at
    # it.
    now_utc = local.astimezone(dt.timezone.utc)
    day = local.date()
    for _ in range(8):  # a week plus one, so the loop cannot run away
        # Each candidate is BUILT from a date and a wall-clock time, never
        # derived from the previous one: adding a day to an aware datetime
        # keeps the offset it already had, which is the same trap from the
        # other end.
        candidate = dt.datetime.combine(day, OPEN_TIME, tzinfo=MARKET_TZ)
        candidate_utc = candidate.astimezone(dt.timezone.utc)
        if candidate_utc > now_utc and day.isoweekday() <= 5:
            return (candidate_utc - now_utc).total_seconds()
        day += dt.timedelta(days=1)
    raise RuntimeError("no market open found within a week")  # unreachable


def time_to_open_phrase(now: dt.datetime | None = None) -> str:
    """"opens in 3h 20m" — a DURATION, deliberately, not a time of day.

    A duration needs no timezone and is read identically in Prague, Lisbon and
    Chicago; a wall-clock time would be right for one of them and quietly wrong
    for the others. Most of what a European user feels about this product is
    exactly this arithmetic, done in their head, several times a day.
    """
    minutes = int(seconds_until_open(now) // 60)
    if minutes < 1:
        return "opening now."
    hours, minutes = divmod(minutes, 60)
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"opens in {days}d {hours}h."
    if hours:
        return f"opens in {hours}h {minutes:02d}m."
    return f"opens in {minutes}m."


def status_note(stored_state: str | None, stored_at: str | None,
                interval_minutes: int, now: dt.datetime | None = None) -> str | None:
    """What to tell the reader about the market, from what the collector recorded.

    Three answers, and the middle one exists because the first version of this
    got caught contradicting itself on production: it printed "Market closed —
    opens in 23h 55m" at 09:35 New York, five minutes AFTER the bell. The state
    came from the collector (which had asked the provider at 09:30:05 and been
    told "closed"), while the duration was computed live — so the line announced
    a market that opens tomorrow and a market that is shut, at a moment when it
    was neither.

    When our own clock says the session is running, we do not repeat a stale
    "closed" no matter who said it. We say what is actually true: the market is
    open and the day's first collection has not landed yet.
    """
    if stored_state != CLOSED or not stored_at:
        return None
    try:
        seen = dt.datetime.fromisoformat(stored_at)
    except ValueError:
        return None
    reference = now or dt.datetime.now(dt.timezone.utc)
    if reference - seen > dt.timedelta(minutes=2 * interval_minutes):
        # An unattended "market closed" is how a stopped collector disguises
        # itself as a calm Sunday.
        return None
    if state_from_clock(reference) == OPEN:
        return "Market open — the first collection of the session has not landed yet."
    return f"Market closed — {time_to_open_phrase(reference)} The chain does not change until then."


def _from_provider(provider) -> str | None:
    """The provider's own answer, or None if it has none or could not give it.

    None means "ask someone else", never "closed": treating an unreachable
    provider as a closed market would stop collection for as long as the
    outage lasts, and those snapshots cannot be fetched afterwards.
    """
    ask = getattr(provider, "market_status", None)
    if ask is None:
        return None
    try:
        answer = ask()
    except Exception as exc:  # noqa: BLE001 — any failure means "fall back"
        log.debug("Provider could not report market status (%s); using the clock.", exc)
        return None
    return answer if answer in (OPEN, CLOSED) else None


def state(provider=None, moment: dt.datetime | None = None) -> tuple[str, str]:
    """(state, how it was decided). `how` goes into the log, so that a surprising
    skip can be explained without reproducing it."""
    key = getattr(provider, "name", "-")
    cached = _cache.get(key)
    if moment is None and cached and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1], cached[2]

    try:
        answer = _from_provider(provider) if provider is not None else None
        if answer is not None:
            result = (answer, f"{key} status endpoint")
        else:
            result = (state_from_clock(moment), "clock, New York")
    except Exception:  # noqa: BLE001
        # Deciding is not allowed to be the thing that stops collection. An
        # unexplained failure here resolves to "open", because a duplicate
        # snapshot is recoverable and a missed one is not.
        log.exception("Could not determine market state — assuming open.")
        result = (OPEN, "failed, assumed open")

    if moment is None:
        _cache[key] = (time.monotonic(), result[0], result[1])
    return result


def clear_cache() -> None:
    """Drops the memoised state. For the checks, and for a process that has
    just been told the provider changed."""
    _cache.clear()
