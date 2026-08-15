#!/usr/bin/env python3
"""Copy a pre-Postgres database into the new one.

Everything collected before this release lives in a SQLite file — by default
`data/options.db` — and the application no longer opens it. That history is the
most valuable thing the application holds, because it is time and cannot be
re-fetched, so moving it is a supported step rather than an exercise left to
the reader.

    python scripts/import_sqlite.py                     # data/options.db
    python scripts/import_sqlite.py path/to/options.db
    python scripts/import_sqlite.py --dry-run           # count, change nothing

Inside Docker, with the stack already running:

    docker compose run --rm -v "$PWD/data:/import" app \\
        python scripts/import_sqlite.py /import/options.db

THE OLD FILE IS NEVER TOUCHED. It is opened read-only and left exactly where it
was; delete it yourself once you have seen your charts. If this is interrupted,
run it again — every table is keyed so that a second run adds nothing it
already added.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402

# Column lists are written out rather than taken from the file, because the two
# schemas are not identical: the SQLite one grew `delta`/`gamma`/`theta`/`vega`
# and `source` late, and a database that predates them has neither the columns
# nor any way to invent them. Missing ones are filled with the same defaults
# the SQLite migrations used, which is what those rows actually mean.
SNAPSHOT_COLUMNS = [
    "ticker", "collected_at", "underlying_price", "expiry", "strike", "option_type",
    "last_price", "bid", "ask", "volume", "open_interest", "implied_volatility",
    "in_the_money", "delta", "gamma", "theta", "vega", "source",
]

RUN_COLUMNS = [
    "started_at", "finished_at", "ticker", "status", "error_message",
    "rows_fetched", "oi_zero_fraction",
]

TRACKED_COLUMNS = ["ticker", "expiry", "strike", "option_type"]

BATCH = 5_000


def sqlite_columns(source: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in source.execute(f"PRAGMA table_info({table})")}  # noqa: S608


def read_rows(source: sqlite3.Connection, table: str, columns: list[str]):
    """Rows in the requested column order, substituting for what the file lacks.

    `source` defaults to 'yahoo' and the greeks to NULL — the same values the
    SQLite-era migration used when it added those columns, and both are simply
    true: Yahoo was the only provider that ever wrote to these files, and it
    serves no greeks.
    """
    # SQLite has no boolean type and stores 0/1; Postgres will not take an
    # integer for a boolean column. This is the only type that needs help —
    # timestamps and dates arrive as ISO strings, which Postgres parses.
    def coerce(column: str, value):
        if column == "in_the_money" and value is not None:
            return bool(value)
        return value

    present = sqlite_columns(source, table)
    selected = [column for column in columns if column in present]
    cursor = source.execute(f"SELECT {', '.join(selected)} FROM {table}")  # noqa: S608
    for row in cursor:
        by_name = dict(zip(selected, row))
        yield tuple(
            coerce(column, by_name.get(column, "yahoo" if column == "source" else None))
            for column in columns
        )


def copy_table(source, target, table: str, columns: list[str], conflict: str, dry_run: bool) -> int:
    total = source.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608
    if dry_run or not total:
        return total
    placeholders = ", ".join(["%s"] * len(columns))
    statement = (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) {conflict}"  # noqa: S608
    )
    written, batch = 0, []
    with target.cursor() as cur:
        for row in read_rows(source, table, columns):
            batch.append(row)
            if len(batch) >= BATCH:
                cur.executemany(statement, batch)
                written += len(batch)
                batch = []
                print(f"  {table}: {written}/{total}", end="\r", flush=True)
        if batch:
            cur.executemany(statement, batch)
            written += len(batch)
    print(f"  {table}: {written}/{total}      ")
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default="data/options.db")
    parser.add_argument("--dry-run", action="store_true", help="count rows, write nothing")
    args = parser.parse_args()

    if not os.path.exists(args.path):
        print(f"No such file: {args.path}")
        return 1

    # Read-only, and stated in the URI rather than merely intended: this script
    # runs against the only copy of somebody's history.
    source = sqlite3.connect(f"file:{args.path}?mode=ro", uri=True)
    target = db.get_connection()
    try:
        print(f"Reading {args.path}")
        # ON CONFLICT DO NOTHING everywhere, so an interrupted run can simply be
        # repeated. option_snapshots has no natural key, so it is guarded by
        # emptiness instead — importing twice into a table that already holds
        # rows would duplicate history rather than restore it.
        existing = target.execute("SELECT count(*) FROM option_snapshots").fetchone()[0]
        if existing and not args.dry_run:
            print(
                f"The target already holds {existing} snapshot rows. Import into an empty "
                "database — running this twice would duplicate the history, not merge it."
            )
            return 1

        copied = {
            "watchlist": copy_table(
                source, target, "watchlist", ["ticker"], "ON CONFLICT DO NOTHING", args.dry_run
            ),
            "option_snapshots": copy_table(
                source, target, "option_snapshots", SNAPSHOT_COLUMNS, "", args.dry_run
            ),
            "collection_runs": copy_table(
                source, target, "collection_runs", RUN_COLUMNS, "", args.dry_run
            ),
            "tracked_contracts": copy_table(
                source, target, "tracked_contracts", TRACKED_COLUMNS,
                "ON CONFLICT DO NOTHING", args.dry_run,
            ),
        }
    finally:
        source.close()
        target.close()

    verb = "would copy" if args.dry_run else "copied"
    for table, count in copied.items():
        print(f"{verb} {count} row(s) into {table}")
    if not args.dry_run:
        print(f"\nDone. {args.path} was not modified — keep it until you have seen your charts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
