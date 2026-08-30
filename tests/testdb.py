"""The one place that decides which database the checks are allowed to touch.

They TRUNCATE. Against a throwaway database that is housekeeping; against a
real one it is unrecoverable, and the only thing between the two used to be the
default value of an environment variable.

That stopped being enough on 2026-08-15. A refactor dropped the `setdefault`
out of one suite, the suite fell back to `app.config`'s default — which is the
same connection string somebody running from source uses for their own data —
and the first thing it would have done is empty every table. Nothing was lost:
an unrelated check happened to raise first. There is no reason to expect an
unrelated check next time.

So the target is verified rather than assumed. A database name that does not
say it is disposable is refused, loudly, before anything is opened.
"""

from __future__ import annotations

import datetime as dt
import os
from urllib.parse import urlparse

DEFAULT_TEST_URL = "postgresql://gammagrid:gammagrid@localhost:5432/gammagrid_test"

REQUIRED_SUFFIX = "_test"


def configure() -> str:
    """Point this process at the checks' database and confirm it is one.

    Call before importing anything from `app`: `app.config` reads the
    environment at import time, so a later assignment has no effect and the
    checks would run somewhere else entirely.

    setdefault rather than assignment, because `tests/coverage_report.py` runs
    several suites in one process — whoever imports first decides, and the rest
    agree rather than fighting over it.
    """
    os.environ.setdefault("DATABASE_URL", DEFAULT_TEST_URL)
    url = os.environ["DATABASE_URL"]
    name = urlparse(url).path.lstrip("/")
    if not name.endswith(REQUIRED_SUFFIX):
        raise SystemExit(
            f"refusing to run the checks against a database named {name!r}.\n"
            f"These scripts empty every table they use, so they only run against a database "
            f"whose name ends in {REQUIRED_SUFFIX!r} — create one and point DATABASE_URL at it:\n"
            f"    createdb gammagrid_test\n"
            f"    DATABASE_URL={DEFAULT_TEST_URL} python tests/coverage_report.py"
        )
    return url


def truncate_all(conn) -> None:
    """Empty the tables the checks write to, leaving the schema alone.

    TRUNCATE rather than DROP: the shape belongs to the migrations, and a copy
    of it here would be a second definition to keep in step. RESTART IDENTITY
    so that checks asserting on row ids see the same numbers on every run.

    THE DERIVED TABLES BELONG IN THIS LIST TOO, and leaving them out was a real
    trap. contract_registry and snapshot_iv_summary are written by
    insert_snapshot in the same transaction as the chain; emptying the chain
    and not them leaves a registry describing contracts that no longer exist.
    The next run then re-inserts a snapshot at a moment the registry already
    knows, and the upsert fails with a cardinality violation that points at the
    application code rather than at the leftovers that caused it.
    """
    with conn.cursor() as cur:
        for table in (
            "option_snapshots",
            "option_snapshots_archive",
            "contract_registry",
            "snapshot_iv_summary",
            "contract_volume_stats",
            "collection_runs",
            "tracked_contracts",
            "watchlist",
            "app_settings",
            # The downloaded symbol directory. Not collected data — a copy of a
            # public file that the next refresh rebuilds — but it is shared
            # state that a check can order its assertions against, so the
            # suites start from a known empty one.
            "option_symbols",
        ):
            cur.execute(f"TRUNCATE {table} RESTART IDENTITY")  # noqa: S608 — fixed list above


def rollup_fixture_days(closed: int = 3, today: dt.date | None = None) -> list[dt.date]:
    """`closed` completed trading days, oldest first, then TODAY itself.

    THE LAST DAY IS TODAY'S CALENDAR DATE, NOT THE LATEST TRADING DAY, and that
    is the entire point of this helper.

    Rollups that exclude the day being judged — the volume baseline in
    `db.rebuild_volume_stats` is the one that matters — cut on
    `dt.date.today()`: a calendar date, read from this same clock, because
    volume accumulates within a session and today's partial figure is not
    comparable with completed days. A fixture built only out of TRADING days
    disagrees with that rule every weekend. Run on a Sunday, its newest day is
    Friday, Friday is strictly before today, and the stored baseline counts four
    days while the reference the check compares it against counts three.
    Measured on Sunday 30.08.2026: 115 against 20, with the suite pointing at
    code that was working correctly.

    That is the expensive half of the failure. A check that breaks on Saturday
    and passes on Monday teaches people not to look at the checks, and in a
    public repository a red mark on `main` is read as a statement about the
    project rather than about the calendar.

    The completed days are trading days, because the daily metrics count
    trading days and a fixture of weekend copies would collapse into one
    bucket. They are strictly before today so that the two rules can never
    disagree about the newest one — whatever day of the week it is.
    """
    anchor = today or dt.date.today()
    completed, day = [], anchor - dt.timedelta(days=1)
    while len(completed) < closed:
        if day.isoweekday() <= 5:
            completed.append(day)
        day -= dt.timedelta(days=1)
    return [*reversed(completed), anchor]
