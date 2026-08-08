"""Unit checks for the pure functions in metrics.py — no database, no network.

Separate from smoke_test.py on purpose. That one exercises the db.py +
metrics.py plumbing on synthetic data: it answers "does this work against a
real SQLite file". These answer "is the number right", which needs no database
at all and so should not pay for one.

Not a pytest suite — plain asserts and a main(), matching smoke_test.py and the
project's preference for small and readable over frameworks.

Usage: python tests/unit_tests.py
"""

import os
import sys
import tempfile
from datetime import datetime

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# BEFORE importing anything from app: config.DB_PATH is read at import time, so
# setting this later has no effect and the checks below would run against the
# developer's real database. Found the hard way — an earlier version of this
# file set it inside the function that needed it and wrote its fixtures into
# data/options.db. smoke_test.py sets it at the top for the same reason.
# setdefault for the same reason smoke_test.py uses it: when coverage_report.py
# runs both in one process, only the first assignment can take effect.
os.environ.setdefault(
    "OPTIONS_TRACKER_DB",
    os.path.join(tempfile.mkdtemp(prefix="gammagrid-unit-"), "unit.db"),
)

from app import db, metrics  # noqa: E402


def check_years_to_expiry():
    """Time to expiry is measured to the 16:00 ET close, not to midnight.

    This is the arithmetic behind every greek on the screener, the GEX tab and
    the Contract tab, and it was wrong in two ways at once for a long time:
    `(expiry - as_of).days / 365` measured to midnight of the expiration date,
    and truncated the fraction. Both are invisible on LEAPS and dominant on the
    near-dated contracts gamma cares about most."""
    expiry = "2026-08-21"

    # A contract expiring TODAY still trades until the close. The old formula
    # gave exactly zero from midnight onward, which zeroed every greek for the
    # whole session — the single most visible symptom of the bug.
    morning = datetime(2026, 8, 21, 13, 0)  # 09:00 ET, market open
    remaining = metrics.years_to_expiry(expiry, morning)
    assert remaining > 0, f"a contract trading until 16:00 ET is not expired at 09:00 ET ({remaining})"
    hours_left = remaining * 365 * 24
    assert abs(hours_left - 7) < 0.01, f"expected ~7 hours to the close, got {hours_left:.2f}"

    # Past the close it must go negative, not clamp: callers tell a finished
    # contract from a live one by the sign.
    after = datetime(2026, 8, 21, 21, 0)  # 17:00 ET
    assert metrics.years_to_expiry(expiry, after) < 0

    # The fraction is kept. The exact pair from the live measurement: a
    # snapshot at 21:44 UTC against an expiry two calendar days later. The old
    # `(expiry - as_of).days / 365` scored this as exactly 2.00 days — the
    # midnight target and the truncation cancelling into a round number that
    # looked entirely plausible.
    as_of = datetime(2026, 8, 19, 21, 44)
    later = "2026-08-22"
    old_formula_days = (pd.Timestamp(later) - pd.Timestamp(as_of)).days
    assert old_formula_days == 2, "the pair being reproduced is the one that scored 2.00"
    days = metrics.years_to_expiry(later, as_of) * 365
    assert 2.9 < days < 2.95, f"expected ~2.93 days, got {days:.3f}"
    assert days > old_formula_days, "truncating to whole days understates the remaining life"
    two_days = metrics.years_to_expiry(expiry, as_of)

    # Daylight saving: the same clock time on the expiry date is a different
    # UTC instant in August (EDT, UTC-4) and December (EST, UTC-5). A fixed
    # offset would pass in summer and be an hour wrong all winter.
    summer = metrics._expiry_moment("2026-08-21")
    winter = metrics._expiry_moment("2026-12-18")
    assert summer.hour == 20, f"16:00 EDT is 20:00 UTC, got {summer}"
    assert winter.hour == 21, f"16:00 EST is 21:00 UTC, got {winter}"

    # tz-aware input must not raise: stored timestamps are naive UTC, but
    # callers reach for pd.Timestamp.utcnow(), which is aware.
    aware = pd.Timestamp("2026-08-19 21:44", tz="UTC")
    assert abs(metrics.years_to_expiry(expiry, aware) - two_days) < 1e-12

    # The vectorized form must agree with the scalar one, element for element —
    # the screener uses one and the GEX tab the other, and a divergence between
    # them would show up as two tabs disagreeing about the same contract.
    expiries = pd.Series(["2026-08-21", "2026-09-18", "2027-01-15"])
    as_of = datetime(2026, 8, 19, 21, 44)
    vector = metrics.years_to_expiry_series(expiries, as_of).to_numpy()
    scalar = np.array([metrics.years_to_expiry(e, as_of) for e in expiries])
    assert np.allclose(vector, scalar, rtol=1e-12), (vector, scalar)
    print("years_to_expiry checks passed")


def check_greeks_respond_to_time():
    """Gamma scales with 1/sqrt(T), which is why the time-to-expiry error
    mattered: understating T overstates gamma, and GEX is gamma × open
    interest. Asserted as a property rather than a fixed number so the check
    survives a change of units."""
    at_the_money = dict(spot=100.0, strike=100.0, iv=0.30, risk_free_rate=0.05, option_type="call")
    near = metrics._black_scholes_greeks(years_to_expiry=2 / 365, **at_the_money)
    far = metrics._black_scholes_greeks(years_to_expiry=2.92 / 365, **at_the_money)
    assert near["gamma"] > far["gamma"], "less time left must mean more gamma at the money"
    overstatement = near["gamma"] / far["gamma"] - 1
    assert 0.1 < overstatement < 0.3, (
        f"a 2.00 vs 2.92 day error should overstate gamma by roughly 15%, got {overstatement:.1%}"
    )

    # A contract past its close has no greeks rather than nonsense ones.
    expired = metrics._black_scholes_greeks(years_to_expiry=-0.5 / 365, **at_the_money)
    assert all(value == 0.0 for value in expired.values()), expired
    print("greeks/time checks passed")


def check_put_call_ratio_matches_sql():
    """db.get_put_call_ratio must return exactly what grouping the raw rows
    returned. metrics.put_call_ratio stays the definition of the ratio and the
    oracle here: moving an aggregation into SQL is only worth anything if the
    numbers are identical, and "identical" is precisely the sort of claim that
    rots quietly — change the date filter, or how a side with no rows is
    handled, and the chart shifts without anything failing.

    This one needs a database — the throwaway one this module points at from
    its very first line."""
    conn = db.get_connection()
    db.add_ticker(conn, "UNIT")
    base = datetime(2026, 7, 1, 21, 0)
    for step in range(3):
        chain = pd.DataFrame([
            # Deliberately lopsided: puts outweigh calls, so a ratio
            # computed the wrong way round could not coincidentally match.
            {"expiry": "2026-09-18", "strike": 100.0, "option_type": "call",
             "last_price": 1.0, "bid": 0.9, "ask": 1.1, "volume": 10 + step,
             "open_interest": 100 + step, "implied_volatility": 0.3,
             "in_the_money": False},
            {"expiry": "2026-09-18", "strike": 100.0, "option_type": "put",
             "last_price": 1.0, "bid": 0.9, "ask": 1.1, "volume": 30 + step * 2,
             "open_interest": 250 + step * 5, "implied_volatility": 0.35,
             "in_the_money": False},
        ])
        db.insert_snapshot(conn, "UNIT", base + pd.Timedelta(hours=step), 100.0, chain)

    expected = metrics.put_call_ratio(db.get_snapshots(conn, "UNIT", days=None))
    actual = db.get_put_call_ratio(conn, "UNIT", days=None)
    assert len(actual) == len(expected) == 3, (len(actual), len(expected))
    merged = expected.merge(actual, on="collected_at", suffixes=("_expected", "_actual"))
    assert len(merged) == 3, "timestamps did not line up between the two implementations"
    for column in ("pcr_volume", "pcr_oi"):
        left = merged[f"{column}_expected"].to_numpy(dtype=float)
        right = merged[f"{column}_actual"].to_numpy(dtype=float)
        assert np.allclose(left, right, rtol=1e-9, equal_nan=True), (column, left, right)
    assert (merged["pcr_volume_actual"] > 1).all(), "puts outweigh calls here; a ratio below 1 is inverted"

    # A ticker with no rows must give the columns the chart expects, not a
    # KeyError on an absent column.
    empty = db.get_put_call_ratio(conn, "NOSUCH", days=None)
    assert empty.empty and list(empty.columns) == ["collected_at", "pcr_volume", "pcr_oi"], empty

    # The default bound must exclude old rows while days=None keeps them.
    old_moment = datetime.utcnow() - pd.Timedelta(days=400)
    db.insert_snapshot(conn, "UNIT", old_moment, 100.0, chain)
    assert len(db.get_snapshots(conn, "UNIT", days=None)) > len(db.get_snapshots(conn, "UNIT"))
    conn.close()
    print("put/call ratio and history bound checks passed")


def check_collector_isolation():
    """One ticker failing must not stop the others (spec FR12), and a chain
    that arrives corrupted must not reach the database (spec FR23).

    `fetch_ticker_snapshot` is replaced rather than called: these are the two
    rules the collector exists to enforce, and neither should need Yahoo to be
    up — or to be reproduced by waiting for a bad day on the real feed."""
    from app import collector

    conn = db.get_connection()
    expiry = (datetime.utcnow() + pd.Timedelta(days=30)).date().isoformat()

    def chain(zero_oi: bool) -> pd.DataFrame:
        return pd.DataFrame([
            {"expiry": expiry, "strike": 100.0, "option_type": option_type,
             "last_price": 1.0, "bid": 0.9, "ask": 1.1, "volume": 5,
             "open_interest": 0 if zero_oi else 50,
             "implied_volatility": 0.25, "in_the_money": False}
            for option_type in ("call", "put")
        ])

    original = collector.fetch_ticker_snapshot
    try:
        def fake(ticker):
            if ticker == "BOOM":
                raise RuntimeError("provider exploded")
            if ticker == "ZEROOI":
                return 100.0, chain(zero_oi=True)
            return 100.0, chain(zero_oi=False)

        collector.fetch_ticker_snapshot = fake
        results = collector.collect_watchlist(conn, ["GOOD", "BOOM", "ZEROOI"])
    finally:
        collector.fetch_ticker_snapshot = original

    assert results["GOOD"] == "success", results
    assert results["BOOM"].startswith("failed"), results
    assert "exploded" in results["BOOM"], results["BOOM"]
    # A chain where open interest came back empty is refused, not stored: it
    # looks ordinary but silently breaks the GEX heatmap and OI delta.
    assert results["ZEROOI"].startswith("failed"), results
    assert "open_interest=0" in results["ZEROOI"], results["ZEROOI"]
    assert len(db.get_snapshots(conn, "GOOD", days=None)) == 2
    assert db.get_snapshots(conn, "ZEROOI", days=None).empty, "a refused snapshot must not be stored"
    # Every attempt is logged, successful or not — the collection log is the
    # only place a user can see that a ticker is silently failing.
    logged = db.get_recent_runs(conn, limit=10)["ticker"].tolist()
    assert {"GOOD", "BOOM", "ZEROOI"} <= set(logged), logged

    # Naive UTC by convention: a tz-aware stamp here would raise on every
    # comparison against an expiry date later on.
    assert collector._now_utc().tzinfo is None
    conn.close()
    print("collector isolation checks passed")


def check_watchlist_and_snapshot_dates():
    """Removing a ticker takes it off the list without touching what was
    already collected — the one rule this project will not break — and
    get_snapshot_dates backs the Replay selector."""
    conn = db.get_connection()
    db.add_ticker(conn, "WATCH")
    assert "WATCH" in db.get_watchlist(conn)

    chain = pd.DataFrame([{
        "expiry": "2026-09-18", "strike": 100.0, "option_type": "call",
        "last_price": 1.0, "bid": 0.9, "ask": 1.1, "volume": 5,
        "open_interest": 50, "implied_volatility": 0.25, "in_the_money": False,
    }])
    base = datetime(2026, 7, 1, 21, 0)
    for step in range(2):
        db.insert_snapshot(conn, "WATCH", base + pd.Timedelta(hours=step), 100.0, chain)

    dates = db.get_snapshot_dates(conn, "WATCH")
    assert len(dates) == 2 and dates == sorted(dates), dates

    db.remove_ticker(conn, "WATCH")
    assert "WATCH" not in db.get_watchlist(conn)
    assert len(db.get_snapshots(conn, "WATCH", days=None)) == 2, (
        "removing a ticker from the watchlist must never delete its collected history"
    )
    conn.close()
    print("watchlist and snapshot-date checks passed")


def check_screener_expiry_awareness():
    """A snapshot ages. Its greeks stay correct as of the collection — that is
    the only honest way to price them — but contracts that were live then may
    have expired since, and a screener that lists them with a cheerful positive
    DTE is misleading.

    The dashboard hides those by default, and this pins the arithmetic it uses
    to decide: measured against NOW, not against the snapshot."""
    snapshot_date = pd.Timestamp("2026-07-28 21:40")
    now = pd.Timestamp("2026-08-08 20:00")

    # Alive when collected, gone by now.
    assert metrics.years_to_expiry("2026-08-05", snapshot_date) > 0
    assert metrics.years_to_expiry("2026-08-05", now) <= 0

    # Still alive on both counts.
    assert metrics.years_to_expiry("2026-08-14", snapshot_date) > 0
    assert metrics.years_to_expiry("2026-08-14", now) > 0

    # The screener's own DTE stays relative to the snapshot: recomputing it
    # against today would misprice every greek in the table.
    chain = pd.DataFrame([
        {"collected_at": snapshot_date, "underlying_price": 100.0,
         "expiry": pd.Timestamp("2026-08-05"), "strike": 100.0, "option_type": option_type,
         "last_price": 1.0, "volume": 10, "open_interest": 50, "implied_volatility": 0.3}
        for option_type in ("call", "put")
    ])
    table = metrics.screener_table(chain)
    # Whole days to midnight of the expiry date: 2026-07-28 21:40 -> 2026-08-05
    # is 7 days and change. `dte` is the column users read and filter on, where
    # whole days are what they expect — the fractional, close-aware figure is
    # what prices the greeks beside it, and the two are deliberately different.
    assert (table["dte"] == 7).all(), table["dte"].tolist()
    print("screener expiry-awareness checks passed")


def main():
    check_years_to_expiry()
    check_greeks_respond_to_time()
    check_put_call_ratio_matches_sql()
    check_collector_isolation()
    check_watchlist_and_snapshot_dates()
    check_screener_expiry_awareness()
    print("\nALL UNIT CHECKS PASSED")


if __name__ == "__main__":
    main()
