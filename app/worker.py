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

from app import collector, config, db, providers

log = logging.getLogger("worker")

# How long to wait before asking again when collection is switched off. Short
# enough that turning it on in the dashboard feels immediate, long enough that
# an idle worker is not a busy loop.
IDLE_POLL_SECONDS = 60

_ARCHIVE_MARKER = "last_archive_pass_date"


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
            archive_if_due(conn)
            collected = collect_once(conn)
            rebuild_stale_volume_stats(conn)
            log.info("Collected %s ticker(s); next pass in %s minute(s).", collected, interval)
        except Exception:
            # A failed pass must not end the worker: the usual causes are a
            # source that is briefly unreachable and a machine that just woke
            # up. Individual ticker failures are already recorded in
            # collection_runs by the collector; this is the outer net.
            log.exception("Collection cycle failed — retrying on the next interval.")
            interval = db.get_collector_interval(conn) or IDLE_POLL_SECONDS / 60
        finally:
            conn.close()
        time.sleep(interval * 60)


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
