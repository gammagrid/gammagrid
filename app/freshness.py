"""How old the chain on screen is, and whether that is normal.

THE DEFECT THIS CLOSES. A screen draws whatever the last successful collection
left behind, and it draws it exactly the same way whether that was ten minutes
ago or ten days ago. The collector can be stopped, the machine can have been
rebooted without bringing the worker back, the interval can be set so that it
never lands inside a session, the source can be refusing this one symbol — and
every chart keeps rendering yesterday's numbers as though they were today's.

**A chart that looks alive and has quietly stopped is worse than an empty
one.** An empty screen is read correctly; a frozen one is not.

The two edges of this were already covered: a symbol that has never collected
at all is suspended and marked in the watchlist, and the market-status line
says when the chain is not supposed to be changing. What was missing is
everything in between, which is also everything that is worth a warning.

FOUR STATES, AND SEPARATING THEM IS THE POINT. If "the market is closed" and
"collection has stopped" look alike, a reader learns to ignore both, and then
the one that matters arrives and is ignored too.

    current   nothing is said at all
    resting   the market has been shut for the whole gap — Saturday showing
              Friday's chain is not a fault and must not be dressed as one
    stale     the market was open and nothing arrived
    failing   the collector is trying this symbol and being refused

The line between `resting` and `stale` is not drawn on elapsed time. It is
drawn on **how much of the gap the market was actually open for** — which is
what makes a weekend, a holiday and a half-day all come out right without any
of them being special-cased here.
"""

from __future__ import annotations

import datetime as dt

from app import market_calendar

CURRENT = "current"
RESTING = "resting"
STALE = "stale"
FAILING = "failing"

# How many collection cycles may be missed before the gap is worth a word. Two
# rather than one because the boundary case is ordinary: a cycle that started a
# minute late leaves a gap slightly over one interval, and a product that
# announces that has taught its reader to ignore the announcement by lunchtime.
_CYCLES_BEFORE_STALE = 2

# Consecutive failures before the collector's own trouble is worth naming.
# Deliberately lower than the threshold that suspends a symbol, and aimed at a
# different case: suspension only ever applies to a symbol that has NEVER
# collected, so an index that worked happily for a month and then stopped being
# served is precisely what suspension refuses to touch — and precisely what
# this has to catch instead.
_FAILURES_BEFORE_NAMING = 3


def open_seconds_between(start: dt.datetime, end: dt.datetime) -> float:
    """How many seconds the market was open between two instants.

    THE ONE NUMBER THAT SEPARATES "resting" FROM "stale". Elapsed time cannot:
    fourteen hours on a Tuesday night and fourteen hours on a Saturday morning
    are the same duration and opposite situations. This counts only the part
    the collector could have used, so a weekend, a holiday and a half-day are
    all handled by the same arithmetic rather than by three special cases.

    Walks day by day. The gaps this is asked about are hours to days, and a
    loop over a handful of dates is cheaper and far easier to read than
    interval arithmetic over session boundaries.
    """
    if start is None or end is None or end <= start:
        return 0.0
    start_local = market_calendar.to_market_time(start)
    end_local = market_calendar.to_market_time(end)
    total = 0.0
    day = start_local.date()
    while day <= end_local.date():
        opens, closes = market_calendar.session_times(day)
        if opens is not None:
            session_start = dt.datetime.combine(day, opens, tzinfo=market_calendar.MARKET_TZ)
            session_end = dt.datetime.combine(day, closes, tzinfo=market_calendar.MARKET_TZ)
            overlap_start = max(session_start, start_local)
            overlap_end = min(session_end, end_local)
            if overlap_end > overlap_start:
                total += (overlap_end - overlap_start).total_seconds()
        day += dt.timedelta(days=1)
    return total


def assess(
    last_success: dt.datetime | None,
    consecutive_failures: int,
    interval_minutes: int,
    now: dt.datetime | None = None,
) -> tuple[str, str] | None:
    """(state, what to tell the reader), or None when there is nothing to say.

    Pure: every input is a value, so this is testable without a database, a
    provider or a clock — which matters, because the cases worth checking are
    Saturdays, holidays and outages, none of which can be waited for.

    `interval_minutes` is the collector's real cycle as stored in the settings,
    not a constant: a threshold invented here would disagree with the collector
    the moment either changed. Pass the interval the worker is actually running
    on, and pass a sensible floor when collection is switched off entirely —
    "off" is not the same fact as "late", and this function is not the place
    that decides which one applies.
    """
    reference = now or dt.datetime.now(dt.timezone.utc)

    if last_success is None:
        # Never collected at all. Not "stale" — there is no age to report —
        # and saying so plainly is what keeps a brand-new ticker from looking
        # broken while its first pass runs.
        if consecutive_failures >= _FAILURES_BEFORE_NAMING:
            return FAILING, (
                f"**Nothing has been collected for this symbol yet.** The last "
                f"{consecutive_failures} attempts failed. It may not be listed with the "
                "current data source."
            )
        return None

    if last_success.tzinfo is None:
        last_success = last_success.replace(tzinfo=dt.timezone.utc)
    traded = open_seconds_between(last_success, reference)
    budget = _CYCLES_BEFORE_STALE * interval_minutes * 60

    # FAILURES OUTRANK AGE, and are reported even while the data is still
    # fresh. The gap opens gradually; the refusals start immediately, and the
    # early warning is the whole value of showing them.
    if consecutive_failures >= _FAILURES_BEFORE_NAMING:
        return FAILING, (
            f"**Collection for this symbol is failing.** The last {consecutive_failures} "
            f"attempts were refused; the newest data here is from "
            f"{age_phrase(reference - last_success)} ago. Other symbols are unaffected."
        )

    if traded <= budget:
        is_closed_now = market_calendar.state_from_clock(reference) == market_calendar.CLOSED
        if is_closed_now and reference - last_success > dt.timedelta(hours=1):
            # The gap is real and the market is shut right now. Said in a calm
            # voice on purpose: this is the market being shut, and a warning
            # here would be the product crying wolf every weekend.
            #
            # NOT "traded == 0": the last successful cycle almost never lands
            # exactly at the bell — it lands wherever the collector's regular
            # interval happened to fall, commonly a few minutes before close.
            # Those last few minutes of the session stay inside `traded`
            # forever, so requiring it to be exactly zero leaves this branch
            # unreachable for the ordinary case and reachable only for the rare
            # snapshot that fires exactly at the bell. The sibling product
            # shipped that version and spent a night showing a healthy green
            # screen with no note on it, after a collection at 15:54 New York.
            return RESTING, (
                f"Market closed since the last collection "
                f"({age_phrase(reference - last_success)} ago). The chain does not "
                "change until the next session."
            )
        return None

    return STALE, (
        f"**This data is {age_phrase(reference - last_success)} old.** The market has "
        f"been open for {age_phrase(dt.timedelta(seconds=traded))} since the last "
        "successful collection, so this is not the market being closed."
    )


def age_phrase(gap: dt.timedelta) -> str:
    """A duration a person reads without converting anything.

    Rounded down and never mixed beyond two units: "2d 4h" is read at a glance
    and "2 days, 4 hours, 17 minutes and 3 seconds" is read as noise, on a line
    whose whole job is to be noticed and understood in passing.
    """
    seconds = max(int(gap.total_seconds()), 0)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m"
