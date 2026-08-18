"""Scheduled collection: a small loop in its own container.

WHY A SEPARATE PROCESS, when the obvious place for a timer is the app that is
already running. Streamlit re-executes the whole script on every interaction —
a scheduler started inside it would be started again on every rerun, and the
result is not "collection is late" but "collection happens far more often than
anybody asked", which is how a free data source stops answering. There is no
version of an in-process timer that does not have to defend against that.

WHAT IT DOES NOT DO. It does not decide how often to collect: it asks the
database every cycle, so changing the interval in the dashboard takes effect on
the next one with nothing restarted. And it does not run at all until somebody
turns it on — the default is off, because a tool that starts hitting an API the
moment it is installed has made a decision that was not its to make.

Run it: `docker compose up` starts it alongside the app. From source,
`python -m app.worker`, or `python -m app.worker once` for a single pass.
"""

from __future__ import annotations

import datetime as dt
import logging
import sys
import time

from app import collector, config, db, market_calendar, providers

log = logging.getLogger("worker")

# How long to wait before asking again when collection is switched off. Short
# enough that turning it on in the dashboard feels immediate, long enough that
# an idle worker is not a busy loop.
IDLE_POLL_SECONDS = 60

_ARCHIVE_MARKER = "last_archive_pass_date"


# Which trading day the single closed-market collection has been spent on, and
# what the collector last believed about the market. Both live in app_settings
# rather than in memory: the worker restarts on every `docker compose up`, and a
# marker that forgets on restart would collect again on each one.
CLOSED_DAY_KEY = "last_closed_market_collection"
MARKET_STATE_KEY = "market_state"
MARKET_STATE_AT_KEY = "market_state_at"

# How long to wait when our clock says the session is running and the provider
# still says it is not. Short, because this is the one window where the two
# genuinely disagree and the answer changes within a minute. Confirmed on the
# sibling product: the first collection of the day landed at 09:31, exactly one
# poll after the bell.
DISAGREEMENT_POLL_SECONDS = 60


def closed_market_slot_taken(conn, day) -> bool:
    return db.get_setting(conn, CLOSED_DAY_KEY) == day.isoformat()


def take_closed_market_slot(conn, day) -> None:
    db.set_setting(conn, CLOSED_DAY_KEY, day.isoformat())


def sleep_seconds(interval_minutes: int, now: dt.datetime | None = None,
                  provider_state: str | None = None) -> float:
    """How long to wait before the next cycle.

    Normally one interval; never longer than the time left until the market
    opens. Written as a `min` on purpose: working out when the market opens is
    a calculation that can be wrong, and capped by the interval a wrong answer
    can only make the worker wake EARLIER than it does today. There is no input
    for which this waits longer than before.

    The disagreement case is the one measured in anger. Waking exactly at the
    bell means asking the provider at the worst possible instant — it has not
    flipped yet, says "closed", and a full interval of sleep follows. So when
    our clock says the session is running and the provider still says it is
    not, the wait drops to a minute. The provider stays authoritative: on a
    holiday it keeps saying closed and nothing is collected, at the price of one
    status call a minute for one day.
    """
    full = interval_minutes * 60
    if provider_state == market_calendar.CLOSED and \
            market_calendar.state_from_clock(now) == market_calendar.OPEN:
        return min(full, DISAGREEMENT_POLL_SECONDS)
    return min(full, max(30.0, market_calendar.seconds_until_open(now) + 5))


def collect_once(conn) -> int:
    """One pass over the watchlist. Returns how many tickers were collected."""
    tickers = db.get_watchlist(conn)
    if not tickers:
        log.info("Watchlist is empty — nothing to collect.")
        return 0
    provider = providers.get_provider()
    collector.collect_watchlist(conn, tickers, provider=provider)
    return len(tickers)


def archive_if_due(conn) -> int:
    """Move long-expired contracts out of the hot table, at most once a day.

    Guarded by a date written to app_settings rather than by a timer, so that a
    machine which is switched off overnight — which is most machines this runs
    on — still archives on its next start instead of only when it happens to be
    awake at the right moment.
    """
    today = dt.date.today().isoformat()
    if db.get_setting(conn, _ARCHIVE_MARKER) == today:
        return 0
    moved = db.archive_expired_contracts(conn)
    db.set_setting(conn, _ARCHIVE_MARKER, today)
    if moved:
        log.info(
            "Archived %s row(s) for contracts expired more than %s day(s) ago.",
            moved, config.CONTRACT_ARCHIVE_GRACE_DAYS,
        )
    return moved


def rebuild_stale_volume_stats(conn) -> None:
    """Recompute the Unusual Activity baseline for any ticker whose stored one
    no longer covers the closed days.

    Here rather than on a page view because it is an aggregate over one
    snapshot per day per contract — work that belongs off the path where
    somebody is waiting. Asked per ticker and skipped when already current, so
    a machine that was off when the day rolled over catches up on its next pass
    instead of waiting for a scheduler nobody watches.
    """
    for ticker in db.get_watchlist(conn):
        if db.volume_stats_are_current(conn, ticker):
            continue
        written = db.rebuild_volume_stats(conn, ticker)
        log.info("Rebuilt volume statistics for %s: %s contract(s).", ticker, written)


def run_forever() -> None:
    log.info("Collector worker started. Interval is read from the app on every cycle.")
    while True:
        conn = db.get_connection()
        try:
            interval = db.get_collector_interval(conn)
            if interval <= 0:
                # Not an error and not worth logging every minute: this is the
                # default state, and the dashboard is where it gets changed.
                time.sleep(IDLE_POLL_SECONDS)
                continue
            # The market calendar. Recorded before it is acted on, so the
            # dashboard can say "market closed" without making its own network
            # call and without the two processes ever disagreeing in front of
            # the reader.
            state, how = market_calendar.state(providers.get_provider())
            db.set_setting(conn, MARKET_STATE_KEY, state)
            db.set_setting(conn, MARKET_STATE_AT_KEY, dt.datetime.now(dt.timezone.utc).isoformat())

            # WHY THE COLLECTOR SLEEPS THROUGH A CLOSED MARKET. Measured on a
            # year of collected data: between two adjacent weekend snapshots not
            # one contract of 14,230 changed its price, volume or open interest,
            # while implied volatility moved on 13,727 of them — the provider
            # recomputing from a frozen price as the clock ticks. Those
            # snapshots cost disk and quota to record nothing, and they break
            # three views: OI Delta compares two copies of Friday, the Unusual
            # Activity baseline fills two sevenths of its sample with
            # duplicates, and the volatility chart draws a rise on a shut
            # market. One snapshot per closed day keeps a safety net without any
            # of that.
            closed = state == market_calendar.CLOSED
            today = market_calendar.market_day()
            if closed and closed_market_slot_taken(conn, today):
                log.info("Market closed (%s); %s already collected — skipping.", how, today)
                collected = 0
            else:
                if closed:
                    log.info("Market closed (%s); taking the single snapshot for %s.", how, today)
                archive_if_due(conn)
                collected = collect_once(conn)
                rebuild_stale_volume_stats(conn)
                # The day's slot is spent only once something was stored: a
                # provider that was briefly unreachable would otherwise cost the
                # whole day's snapshot, and a closed day has no second chance.
                if closed and collected:
                    take_closed_market_slot(conn, today)
                log.info("Collected %s ticker(s).", collected)
        except Exception:
            # A failed pass must not end the worker: the usual causes are a
            # source that is briefly unreachable and a machine that just woke
            # up. Individual ticker failures are already recorded in
            # collection_runs by the collector; this is the outer net.
            log.exception("Collection cycle failed — retrying on the next interval.")
            interval = db.get_collector_interval(conn) or IDLE_POLL_SECONDS / 60
            state = None
        finally:
            conn.close()
        time.sleep(sleep_seconds(interval, provider_state=state))


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s"
    )
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "once":
        conn = db.get_connection()
        try:
            archive_if_due(conn)
            print(f"Collected {collect_once(conn)} ticker(s).")
        finally:
            conn.close()
        return 0
    run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
