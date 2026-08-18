"""Manual check of the db.py + metrics.py plumbing on synthetic data, with no
network and no real yfinance. Not part of a pytest suite — just a quick run
during development. Usage: python tests/smoke_test.py"""

import os
import sys
from datetime import date as dt_date
from datetime import datetime, timedelta
from datetime import time as dt_time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testdb  # noqa: E402

testdb.configure()

from app import collector, config, db, metrics  # noqa: E402

EXPIRY = "2026-09-18"
STRIKES = [90, 95, 100, 105, 110]

def make_chain(iv_shift: float, strike_100_volume: int, strike_100_oi: int) -> pd.DataFrame:
    """strike_100_volume/oi varies per snapshot to exercise daily normalization;
    the other strikes are kept stable to avoid confusion with a general rise in activity.
    last_price is a flat placeholder everywhere — the IV outlier guard
    (metrics._mark_unreliable_iv) judges a contract against its own history, not an
    absolute Black-Scholes price, so it doesn't need last_price to be physically
    consistent with iv here; the 7-day iv_shift drift below (0.30..0.36) stays well
    inside the guard's default 50% outlier threshold either way."""
    rows = []
    for strike in STRIKES:
        for option_type, base_iv in [("call", 0.30), ("put", 0.35)]:
            volume = strike_100_volume if strike == 100 else (20 + strike % 3)
            oi = strike_100_oi if strike == 100 else 50
            rows.append({
                "expiry": EXPIRY,
                "strike": float(strike),
                "option_type": option_type,
                "last_price": 2.5,
                "bid": 2.4,
                "ask": 2.6,
                "volume": volume,
                "open_interest": oi,
                "implied_volatility": base_iv + iv_shift,
                "in_the_money": strike < 100 if option_type == "call" else strike > 100,
            })
    return pd.DataFrame(rows)


def check_scheduled_collection(conn):
    """The interval is a setting, the floor is a rule, and the growth figure is
    computed rather than guessed.

    The floor is checked against the database and not only against the
    dropdown, because a value can arrive by other routes — a hand-written
    UPDATE, a future import, a list of choices edited without noticing what it
    implies — and the data source that would be hit too often cannot defend
    itself. Zero has to survive untouched: "off" is not a frequency, and
    clamping it to the floor would silently switch collection on.
    """
    assert db.get_collector_interval(conn) == 0, "collection must be off until asked for"

    db.set_collector_interval(conn, 60)
    assert db.get_collector_interval(conn) == 60

    db.set_collector_interval(conn, 1)
    assert db.get_collector_interval(conn) == config.PROVIDER_MIN_INTERVAL_MINUTES, (
        "a value below the provider floor must be raised to it, not honoured"
    )

    db.set_collector_interval(conn, 0)
    assert db.get_collector_interval(conn) == 0, "off must stay off"

    # Every choice the interface offers has to survive the floor, or the list
    # is offering something the code will silently change.
    for label, minutes in config.COLLECTOR_INTERVAL_CHOICES.items():
        db.set_collector_interval(conn, minutes)
        stored = db.get_collector_interval(conn)
        assert stored == minutes, f"{label} ({minutes}) came back as {stored}"

    assert db.estimated_growth_mb_per_month(conn, 0) == 0.0, "off costs nothing"
    hourly = db.estimated_growth_mb_per_month(conn, 60)
    quarter_hourly = db.estimated_growth_mb_per_month(conn, 15)
    assert quarter_hourly > hourly > 0, (hourly, quarter_hourly)
    assert abs(quarter_hourly / hourly - 4) < 1e-9, "four times as often is four times the disk"

    db.set_setting(conn, "unit-probe", "value")
    assert db.get_setting(conn, "unit-probe") == "value"
    db.set_setting(conn, "unit-probe", "changed")
    assert db.get_setting(conn, "unit-probe") == "changed", "a setting must be updatable in place"
    assert db.get_setting(conn, "never-set", "fallback") == "fallback"

    db.set_collector_interval(conn, 0)
    print("Scheduled-collection checks passed\n")


def check_archiving_moves_and_keeps(conn):
    """Archiving moves rows; it never removes them, and history stays whole.

    The second half is what makes this worth a check of its own: a snapshot is
    not archived as a unit — only the contracts inside it that have expired —
    so every historical read has to union both tables. Reading one of them
    leaves a chain quietly missing contracts, which is a defect that shows up
    as a slightly wrong chart rather than as an error.
    """
    ticker = "ARCHTEST"
    long_gone = (datetime.utcnow().date() - timedelta(days=400)).isoformat()
    live = (datetime.utcnow().date() + timedelta(days=30)).isoformat()
    moment = datetime.utcnow().replace(microsecond=0) - timedelta(days=1)
    chain = pd.DataFrame([
        {"expiry": long_gone, "strike": 100.0, "option_type": "call", "last_price": 1.0,
         "bid": 0.9, "ask": 1.1, "volume": 10, "open_interest": 100,
         "implied_volatility": 0.3, "in_the_money": False},
        {"expiry": live, "strike": 105.0, "option_type": "put", "last_price": 2.0,
         "bid": 1.9, "ask": 2.1, "volume": 20, "open_interest": 200,
         "implied_volatility": 0.4, "in_the_money": False},
    ])
    db.insert_snapshot(conn, ticker, moment, 100.0, chain)

    before = db.get_snapshots(conn, ticker, days=None)
    assert len(before) == 2, before

    moved = db.archive_expired_contracts(conn, grace_days=30)
    assert moved == 1, f"exactly the long-expired contract should have moved, got {moved}"

    hot = conn.execute(
        "SELECT count(*) FROM option_snapshots WHERE ticker = %s", (ticker,)
    ).fetchone()[0]
    assert hot == 1, "the expired contract must be out of the table live queries read"

    after = db.get_snapshots(conn, ticker, days=None)
    assert len(after) == 2, (
        "history lost a row: a read that does not union the archive shows an incomplete chain"
    )
    assert len(db.get_snapshot_dates(conn, ticker)) == 1, "the moment itself must not be duplicated"
    ratio = db.get_put_call_ratio(conn, ticker, days=None)
    assert len(ratio) == 1 and not pd.isna(ratio.iloc[0]["pcr_volume"]), ratio.to_dict()

    with conn.cursor() as cur:
        cur.execute("DELETE FROM option_snapshots_archive WHERE ticker = %s", (ticker,))
        cur.execute("DELETE FROM option_snapshots WHERE ticker = %s", (ticker,))
    print("Archiving checks passed (moved, not deleted; history stays whole)\n")


def check_two_sources_never_mix(conn):
    """Two providers with data for the same ticker must never appear together.

    The failure this guards is not cosmetic. Implied volatility is *computed*
    by a provider rather than observed, so two of them differ on the same
    contract on the same day — and the views that show "the latest moment"
    took the newest timestamp regardless of source and then every row at it.
    Two providers collecting the same minute therefore handed GEX and Max Pain
    each contract twice, which is a wrong number rather than an ugly chart.

    The older provider's rows stay in the database throughout — hidden is not
    deleted, and this asserts it rather than assuming it.
    """
    ticker = "SRCTEST"
    yesterday = datetime.utcnow().replace(microsecond=0) - timedelta(days=1)
    only_old = yesterday - timedelta(hours=2)   # yahoo alone
    shared = yesterday - timedelta(hours=1)     # both, to the same instant
    newest = yesterday                          # premium alone

    def collected(moment, source, price, iv_shift, volume):
        db.insert_snapshot(
            conn, ticker, moment, price, make_chain(iv_shift, volume, 500), source=source
        )
        db.log_run(
            conn, moment, moment + timedelta(seconds=1), ticker, "success",
            rows_fetched=len(STRIKES) * 2, source=source,
        )

    per_moment = len(STRIKES) * 2
    collected(only_old, "yahoo", 100.0, 0.0, 100)
    collected(shared, "yahoo", 100.0, 0.0, 110)
    collected(shared, "premium", 101.0, 0.05, 700)
    collected(newest, "premium", 102.0, 0.06, 800)

    assert db.active_source(conn, ticker) == "premium", "the freshest provider is the active one"
    assert db.sources_for(conn, ticker) == ["premium", "yahoo"], db.sources_for(conn, ticker)

    # The filter has real work to do: the shared instant genuinely holds two
    # chains, and the scoped read must return one of them.
    both = conn.execute(
        "SELECT count(*) FROM option_snapshots WHERE ticker = %s AND collected_at = %s",
        (ticker, shared),
    ).fetchone()[0]
    assert both == per_moment * 2, both
    at_shared = db.get_snapshots_at(conn, ticker, [shared])
    assert len(at_shared) == per_moment, (
        f"a moment collected by two providers returned {len(at_shared)} rows — "
        "every contract twice is a doubled GEX and a moved Max Pain"
    )
    assert set(at_shared["source"]) == {"premium"}

    latest = db.get_latest_snapshot(conn, ticker)
    assert len(latest) == per_moment and set(latest["source"]) == {"premium"}, latest["source"].unique()
    assert latest["collected_at"].max() == pd.Timestamp(newest)

    history = db.get_snapshots(conn, ticker, days=None)
    assert set(history["source"]) == {"premium"}, "history must not reach back into another provider"
    assert len(history) == per_moment * 2, len(history)

    moments = db.get_collection_moments(conn, ticker, days=None)
    assert moments == [newest, shared], moments
    assert len(db.get_snapshot_dates(conn, ticker)) == 2

    # One row per moment, not one per moment per provider: the IV chart would
    # otherwise zigzag between two opinions of the same market.
    iv = db.get_iv_weighted_average(conn, ticker, days=None)
    assert len(iv) == 2 and iv["collected_at"].is_unique, iv.to_dict()

    ratio = db.get_put_call_ratio(conn, ticker, days=None)
    assert len(ratio) == 2, ratio.to_dict()

    contract = db.get_contract_history(conn, ticker, EXPIRY, 100.0, "call", days=None)
    assert set(contract["source"]) == {"premium"}, "one contract's IV history must be one provider's"

    # The Unusual Activity baseline is stored per contract per source and joined
    # on the contract alone, so an unscoped read reports every contract twice.
    db.rebuild_volume_stats(conn, ticker, days=30)
    stats = db.get_volume_stats(conn, ticker)
    assert len(stats) == per_moment, f"expected one row per contract, got {len(stats)}"

    kept = conn.execute(
        "SELECT count(*) FROM option_snapshots WHERE ticker = %s AND source = 'yahoo'",
        (ticker,),
    ).fetchone()[0]
    assert kept == per_moment * 2, "the inactive provider's rows are hidden, never removed"

    with conn.cursor() as cur:
        for table in ("option_snapshots", "option_snapshots_archive", "snapshot_iv_summary",
                      "contract_volume_stats", "collection_runs"):
            cur.execute(f"DELETE FROM {table} WHERE ticker = %s", (ticker,))  # noqa: S608
    print("Source-scoping checks passed (two providers never share a screen)\n")


def check_missing_numbers_survive_a_write(conn):
    """A chain with nothing quoted for a contract must store, not explode.

    Providers spell "not quoted" as NaN — yfinance does it throughout — and a
    real chain carries it wherever a contract never traded. SQLite accepted
    that; Postgres INTEGER has no NaN and rejects the statement, and since a
    chain is written in one transaction the whole snapshot is lost rather than
    one field. Found by collecting for real after the move: every ticker failed
    with a range error while nothing was out of range.
    """
    ticker = "NANTEST"
    moment = datetime.utcnow().replace(microsecond=0)
    chain = pd.DataFrame([
        {"expiry": "2026-09-18", "strike": 100.0, "option_type": "call",
         "last_price": 1.0, "bid": 0.9, "ask": 1.1, "volume": 10,
         "open_interest": 100, "implied_volatility": 0.3, "in_the_money": False},
        # Never traded: no volume, no open interest, no price, no IV.
        {"expiry": "2026-09-18", "strike": 250.0, "option_type": "call",
         "last_price": float("nan"), "bid": float("nan"), "ask": float("nan"),
         "volume": float("nan"), "open_interest": float("nan"),
         "implied_volatility": float("nan"), "in_the_money": False},
    ])
    db.insert_snapshot(conn, ticker, moment, 100.0, chain)

    stored = db.get_snapshots(conn, ticker, days=None)
    assert len(stored) == 2, "the whole chain must be stored, including the untraded contract"
    quiet = stored[stored["strike"] == 250.0].iloc[0]
    for column in ("volume", "open_interest", "last_price", "implied_volatility"):
        assert pd.isna(quiet[column]), f"{column} came back as {quiet[column]!r}"
    # NULL rather than NaN in the column itself: NaN outranks every number in
    # Postgres, so a stored NaN would pass `> 0` and win an ORDER BY.
    nans = conn.execute(
        "SELECT count(*) FROM option_snapshots WHERE ticker = %s AND implied_volatility = 'NaN'",
        (ticker,),
    ).fetchone()[0]
    assert nans == 0, "missing values must be stored as NULL, not as NaN"

    with conn.cursor() as cur:
        cur.execute("DELETE FROM option_snapshots WHERE ticker = %s", (ticker,))
    print("Missing-number checks passed (a chain with untraded contracts stores)\n")


def _recent_trading_days(count: int) -> list:
    """The most recent New York weekdays, oldest first.

    Relative rather than fixed dates so the fixture never rots, and trading days
    rather than calendar ones because that is what the daily metrics count.
    """
    found, day = [], datetime.utcnow().date()
    while len(found) < count:
        if day.isoweekday() <= 5:
            found.append(day)
        day -= timedelta(days=1)
    return list(reversed(found))


def check_narrow_reads_and_rollups(conn):
    """Every view asks for what it shows, and the two stored numbers agree with
    the functions that define them.

    This is the port of work done on the hosted product, where loading a
    ticker's whole history on every interaction reached 3.1M rows, 22 seconds
    and 0.8 GB of memory. Manual collection hid the problem here; a scheduler
    removes that cover, which is why it is worth checking rather than trusting.

    The equality checks are the part that matters over time. Moving an
    aggregate into SQL is only worth anything if the numbers are identical, and
    "identical" is exactly the sort of claim that rots quietly — so
    metrics.iv_weighted_average and metrics.volume_stats stay the definitions
    and the stored values are asserted against them.
    """
    ticker = "NARROWTEST"
    with conn.cursor() as cur:
        for table in ("option_snapshots", "option_snapshots_archive", "snapshot_iv_summary",
                      "contract_volume_stats", "collection_runs"):
            cur.execute(f"DELETE FROM {table} WHERE ticker = %s", (ticker,))  # noqa: S608

    expiry = (datetime.utcnow().date() + timedelta(days=30)).isoformat()
    # TRADING days, at hours that mean the same calendar date in UTC and in New
    # York. Both halves matter now that daily metrics count trading days: a run
    # of the suite on a Sunday would otherwise build its fixture out of weekend
    # copies, and `midnight + 1h` is 21:00 of the PREVIOUS day in New York, so
    # two moments meant as two days would collapse into one bucket.
    days = _recent_trading_days(4)

    def chain(volume_a, volume_b):
        return pd.DataFrame([
            {"expiry": expiry, "strike": 100.0, "option_type": "call", "last_price": 1.0,
             "bid": 0.9, "ask": 1.1, "volume": volume_a, "open_interest": 100,
             "implied_volatility": 0.20, "in_the_money": False},
            {"expiry": expiry, "strike": 105.0, "option_type": "put", "last_price": 2.0,
             "bid": 1.9, "ask": 2.1, "volume": volume_b, "open_interest": 200,
             "implied_volatility": 0.60, "in_the_money": False},
        ])

    # Three closed days, the middle one collected twice so that "one snapshot
    # per day" has something to choose between, plus today.
    plan = [
        (datetime.combine(days[0], dt_time(16, 0)), chain(10, 90)),
        (datetime.combine(days[1], dt_time(10, 0)), chain(999, 999)),   # superseded
        (datetime.combine(days[1], dt_time(16, 0)), chain(20, 80)),
        (datetime.combine(days[2], dt_time(16, 0)), chain(30, 70)),
        (datetime.combine(days[3], dt_time(14, 0)), chain(400, 40)),
    ]
    for moment, rows in plan:
        db.insert_snapshot(conn, ticker, moment, 100.0, rows)
        db.log_run(conn, moment, moment, ticker, "success", rows_fetched=len(rows))

    moments = db.get_collection_moments(conn, ticker, days=None)
    assert len(moments) == 5, moments
    assert moments == sorted(moments, reverse=True), "newest first, as the Replay list expects"

    per_day = db.get_collection_moments(conn, ticker, days=None, per_day=True)
    assert len(per_day) == 4, per_day
    assert per_day[1] == plan[3][0], "a day collected twice must be represented by its later pass"

    latest = db.get_latest_snapshot(conn, ticker)
    assert len(latest) == 2 and latest["collected_at"].nunique() == 1, latest
    assert latest["collected_at"].iloc[0] == plan[-1][0]

    at_two = db.get_snapshots_at(conn, ticker, per_day[:2])
    assert len(at_two) == 4, "two moments, two contracts each"
    # An empty request is not an error — it is a ticker in its first minutes —
    # and it must still come back with columns, or every caller dies indexing.
    empty = db.get_snapshots_at(conn, ticker, [])
    assert empty.empty and "collected_at" in empty.columns

    one = db.get_contract_history(conn, ticker, expiry, 100.0, "call", days=None)
    assert len(one) == 5, "one contract, every collection"
    assert set(one["strike"]) == {100.0} and set(one["option_type"]) == {"call"}

    # The IV rollup is written with the chain; it must equal the function that
    # defines the number, computed over the same rows.
    stored_iv = db.get_iv_weighted_average(conn, ticker, days=None)
    everything = db.get_snapshots(conn, ticker, days=None)
    expected_iv = metrics.iv_weighted_average(everything)
    merged = expected_iv.merge(stored_iv, on="collected_at", suffixes=("_metrics", "_stored"))
    assert len(merged) == len(expected_iv) == 5, (len(merged), len(expected_iv))
    assert np.allclose(
        merged["iv_weighted_avg_metrics"].to_numpy(dtype=float),
        merged["iv_weighted_avg_stored"].to_numpy(dtype=float),
        rtol=1e-12, equal_nan=True,
    ), merged.to_dict()
    # Pin the arithmetic itself, so both being wrong the same way still fails:
    # 0.20*400 + 0.60*40 over 440.
    latest_iv = float(stored_iv.iloc[-1]["iv_weighted_avg"])
    assert abs(latest_iv - (0.20 * 400 + 0.60 * 40) / 440) < 1e-12, latest_iv

    # The volume baseline: closed days only, and equal to the shared function
    # fed the same rows.
    assert not db.volume_stats_are_current(conn, ticker), "nothing computed yet"
    db.rebuild_volume_stats(conn, ticker, days=None)
    assert db.volume_stats_are_current(conn, ticker), "a rebuild must satisfy the worker"

    stored_stats = db.get_volume_stats(conn, ticker)
    closed_days = everything[
        everything["collected_at"].dt.normalize() < pd.Timestamp(plan[-1][0]).normalize()
    ]
    expected_stats = metrics.volume_stats(closed_days)
    keys = ["expiry", "strike", "option_type"]
    joined = expected_stats.merge(stored_stats, on=keys, suffixes=("_metrics", "_stored"))
    assert len(joined) == len(expected_stats) == 2, (len(joined), len(expected_stats))
    for column in ("avg_volume", "std_volume", "history_points"):
        assert np.allclose(
            joined[f"{column}_metrics"].to_numpy(dtype=float),
            joined[f"{column}_stored"].to_numpy(dtype=float),
            rtol=1e-9, equal_nan=True,
        ), f"{column}: {joined.to_dict()}"
    # Today is excluded and the superseded pass does not count: the call's
    # baseline is 10, 20, 30 rather than anything containing 999 or 400.
    call_stats = stored_stats[stored_stats["strike"] == 100.0].iloc[0]
    assert call_stats["history_points"] == 3, call_stats.to_dict()
    assert abs(call_stats["avg_volume"] - 20.0) < 1e-9, call_stats.to_dict()

    with conn.cursor() as cur:
        for table in ("option_snapshots", "option_snapshots_archive", "snapshot_iv_summary",
                      "contract_volume_stats", "collection_runs"):
            cur.execute(f"DELETE FROM {table} WHERE ticker = %s", (ticker,))  # noqa: S608
    print("Narrow-read and rollup checks passed (stored numbers match the shared functions)\n")

def check_greek_attribution():
    """The decomposition has to add up, and refuse what it cannot explain.

    The first assertion is the one that matters: start + every term + every
    residual == end, exactly. The residual is defined as the leftover, so this
    is true by construction — which is the point. It means the chart can never
    quietly omit a contribution, and a reader who adds the bars gets the price.
    """
    def day(index, price, spot, iv, greeks=(0.012, 0.0008, 0.03, -0.012)):
        delta, gamma, vega, theta = greeks
        return {
            "collected_at": pd.Timestamp("2026-08-10 20:00") + pd.Timedelta(days=index),
            "last_price": price, "underlying_price": spot, "implied_volatility": iv,
            "delta": delta, "gamma": gamma, "vega": vega, "theta": theta,
        }

    # Mon-Fri, a falling underlying and a rising IV: the shape of the GLD trade
    # this feature was built from.
    history = pd.DataFrame([
        day(0, 0.19, 500.0, 0.20), day(1, 0.16, 498.5, 0.21), day(2, 0.13, 497.0, 0.22),
        day(3, 0.10, 495.5, 0.23), day(4, 0.07, 494.0, 0.24),
    ])
    result = metrics.greek_attribution(history)
    assert len(result.by_day) == 4, result.by_day
    totals = result.totals
    assert abs(result.start_price + totals["actual"] - result.end_price) < 1e-12, (
        "start plus the actual moves must land exactly on the end price"
    )
    reconstructed = sum(totals[name] for name in (*metrics.ATTRIBUTION_TERMS, "residual"))
    assert abs(reconstructed - totals["actual"]) < 1e-12, (
        "the four terms plus the residual ARE the move — if this drifts, the chart is "
        "showing a decomposition of something else"
    )
    # Signs: a falling underlying with a positive delta loses money, time decay
    # always does, a rising IV on positive vega gains.
    assert totals["delta"] < 0 and totals["theta"] < 0 and totals["vega"] > 0, totals

    # Theta is per CALENDAR day, so a Friday-to-Monday interval charges three
    # days of decay. Verified against the arithmetic rather than trusted.
    friday_to_monday = pd.DataFrame([
        day(4, 0.20, 500.0, 0.20),   # Friday 14.08
        day(7, 0.17, 500.0, 0.20),   # Monday 17.08
    ])
    weekend = metrics.greek_attribution(friday_to_monday)
    assert abs(weekend.totals["theta"] - (-0.012 * 3)) < 1e-9, weekend.totals
    assert weekend.missing_trading_days == 0, "Saturday and Sunday are not missing days"

    # A trading day that was never collected is reported, not silently folded in.
    gap = pd.DataFrame([day(0, 0.20, 500.0, 0.20), day(2, 0.17, 500.0, 0.20)])
    assert metrics.greek_attribution(gap).missing_trading_days == 1

    # The cheap tail is refused: on a one-cent option the vega term outgrew the
    # option's entire price on the real trade.
    with_tail = pd.DataFrame([
        day(0, 0.19, 500.0, 0.20), day(1, 0.16, 498.5, 0.21), day(2, 0.13, 497.0, 0.22),
        day(3, 0.02, 495.0, 0.60), day(4, 0.01, 494.0, 1.41),
    ])
    trimmed = metrics.greek_attribution(with_tail)
    assert trimmed.dropped_cheap == 2, trimmed.dropped_cheap
    assert trimmed.end_price == 0.13, "the run has to stop where the price got meaningless"
    assert abs(trimmed.start_price + trimmed.totals["actual"] - trimmed.end_price) < 1e-12, (
        "dropping the tail must leave a CONTIGUOUS run, or the arithmetic stops closing"
    )

    # Rows the IV guard rejected arrive as NaN greeks: counted, never guessed at.
    unreliable = pd.DataFrame([
        day(0, 0.19, 500.0, 0.20), day(1, 0.16, 498.5, 0.21),
        {**day(2, 0.13, 497.0, 0.22), "delta": float("nan"), "vega": float("nan")},
        day(3, 0.10, 495.5, 0.23),
    ])
    guarded = metrics.greek_attribution(unreliable)
    assert guarded.dropped_unreliable == 1, guarded.dropped_unreliable
    # STITCHED, not dropped — the second of the two choices пункт 9 left open.
    # The rejected day stops being a boundary, so the interval spans it using
    # the greeks from its start: two intervals survive, not one, and the extra
    # day is reported through missing_trading_days. The cost is second-order
    # error (greeks held a day longer), and the interface has to say so instead
    # of implying the day was excluded.
    assert len(guarded.by_day) == 2, guarded.by_day
    assert guarded.missing_trading_days == 1, (
        "a bridged day has to surface somewhere, or the window silently covers less "
        "than it claims"
    )
    assert abs(guarded.start_price + guarded.totals["actual"] - guarded.end_price) < 1e-12

    # Nothing to say is said as nothing, not as zeros.
    assert metrics.greek_attribution(pd.DataFrame()).by_day.empty
    assert metrics.greek_attribution(history.head(1)).by_day.empty
    assert metrics.greek_attribution(history.head(1)).totals["actual"] == 0.0

    # The spot the greeks were computed against has to travel with them, or ΔS
    # would be paired with the wrong snapshot.
    assert "underlying_price" in metrics.contract_greeks_history(
        pd.DataFrame(columns=["strike", "expiry", "option_type", "collected_at"]),
        100.0, pd.Timestamp("2026-09-18"), "call",
    ).columns
    print("Greek-attribution checks passed")


def check_expired_contracts_stay_reachable(conn):
    """A contract that expired must still be findable, with its history.

    The failure this guards is not a lost row — nothing is ever deleted — it is
    a lost ROUTE: the Contract tab builds its lists from the latest snapshot, and
    an expired contract is not in it. The registry is what puts it back on the
    screen, and it has to be maintained by the same write that stores the chain,
    or it lists contracts with no data and hides contracts that have some.
    """
    ticker = "EXPTEST"
    with conn.cursor() as cur:
        for table in ("option_snapshots", "option_snapshots_archive", "snapshot_iv_summary",
                      "contract_volume_stats", "collection_runs", "contract_registry"):
            cur.execute(f"DELETE FROM {table} WHERE ticker = %s", (ticker,))  # noqa: S608

    gone = (datetime.utcnow().date() - timedelta(days=40)).isoformat()
    live = (datetime.utcnow().date() + timedelta(days=30)).isoformat()
    rows = pd.DataFrame([
        {"expiry": gone, "strike": 100.0, "option_type": "call", "last_price": 1.0,
         "bid": 0.9, "ask": 1.1, "volume": 10, "open_interest": 100,
         "implied_volatility": 0.3, "in_the_money": False},
        {"expiry": live, "strike": 105.0, "option_type": "put", "last_price": 2.0,
         "bid": 1.9, "ask": 2.1, "volume": 20, "open_interest": 200,
         "implied_volatility": 0.4, "in_the_money": False},
    ])
    moment = datetime.utcnow().replace(microsecond=0) - timedelta(days=1)
    db.insert_snapshot(conn, ticker, moment, 100.0, rows)

    expired = db.get_expired_contracts(conn, ticker)
    assert len(expired) == 1, expired.to_dict()
    assert expired.iloc[0]["strike"] == 100.0
    assert expired.iloc[0]["snapshots"] == 1, "the registry counts what was written"

    # Collecting again must update the row rather than duplicate it: the
    # registry is one row per contract, and that is the whole reason it is
    # cheaper than a DISTINCT over the snapshots.
    db.insert_snapshot(conn, ticker, moment + timedelta(hours=1), 100.0, rows)
    expired = db.get_expired_contracts(conn, ticker)
    assert len(expired) == 1 and expired.iloc[0]["snapshots"] == 2, expired.to_dict()

    # And the history is genuinely reachable — including with the day window
    # off, which is what the interface does for an expired contract, because
    # the window is measured back from now() and would trim a past contract to
    # nothing.
    history = db.get_contract_history(conn, ticker, gone, 100.0, "call", days=None)
    assert len(history) == 2, history

    with conn.cursor() as cur:
        for table in ("option_snapshots", "option_snapshots_archive", "snapshot_iv_summary",
                      "contract_volume_stats", "collection_runs", "contract_registry"):
            cur.execute(f"DELETE FROM {table} WHERE ticker = %s", (ticker,))  # noqa: S608
    print("Expired-contract checks passed (registry keeps them reachable)\n")


def main():
    # A clean slate, without dropping the database itself: the checks assert
    # counts, and a leftover row from a previous run makes them fail in a way
    # that looks like a code defect.
    reset = db.get_connection()
    testdb.truncate_all(reset)
    reset.close()

    conn = db.get_connection()
    db.add_ticker(conn, "TEST")
    assert db.get_watchlist(conn) == ["TEST"]

    # Six CONSECUTIVE TRADING days starting Monday 06.07.2026, not six calendar
    # days. Daily metrics count trading days — the chain does not move while the
    # market is shut, so a Saturday is a copy of Friday rather than a day — and a
    # fixture that steps over a weekend shifts every day-over-day assertion below
    # by one, for a reason that has nothing to do with the code under test.
    #
    # Hours are 13:00 and 20:00 UTC, which are 09:00 and 16:00 in New York: the
    # same calendar date in both zones, so the fixture says what it looks like it
    # says.
    trading_days = [dt_date(2026, 7, d) for d in (6, 7, 8, 9, 10, 13)]
    # Days 1-5: one collection per day, OI grows a little day over day (95..100, OI 50..61).
    # Day 6: TWO collections (morning and evening) — the exact scenario from the bug: OI
    # doesn't change intraday (65->65) while volume grows as cumulative daily volume (200->500).
    daily_snapshots = [
        # (day index, hour UTC, volume, open_interest)
        (0, 20, 95, 50),
        (1, 20, 105, 52),
        (2, 20, 90, 55),
        (3, 20, 110, 58),
        (4, 20, 100, 61),
        (5, 13, 200, 65),  # day 6, morning
        (5, 20, 500, 65),  # day 6, evening — same OI as the morning
    ]
    base_day = datetime.combine(trading_days[0], dt_time(20, 0))

    for i, (day_index, hour, volume, oi) in enumerate(daily_snapshots):
        moment = datetime.combine(trading_days[day_index], dt_time(hour, 0))
        db.insert_snapshot(
            conn, "TEST", moment, underlying_price=100.0 + i * 0.1,
            chain_df=make_chain(iv_shift=i * 0.01, strike_100_volume=volume, strike_100_oi=oi),
        )
    last_day_index, last_hour = daily_snapshots[-1][0], daily_snapshots[-1][1]
    latest_moment = datetime.combine(trading_days[last_day_index], dt_time(last_hour, 0))
    db.log_run(conn, latest_moment, latest_moment + timedelta(seconds=1), "TEST", "success")

    df = db.get_snapshots(conn, "TEST")
    assert len(df) == 10 * len(daily_snapshots), f"unexpected row count: {len(df)}"

    pcr = metrics.put_call_ratio(df)
    print("Put/Call Ratio:\n", pcr, "\n")
    assert len(pcr) == len(daily_snapshots)

    expiry = pd.Timestamp(EXPIRY)

    mp = metrics.max_pain(df, expiry)
    print("Max Pain:", mp, "\n")
    assert mp is not None

    gex = metrics.gamma_exposure_profile(df, expiry)
    print("GEX profile:\n", gex, "\n")
    assert not gex.empty

    net_gex = metrics.net_gamma_exposure(gex)
    print("Net GEX:", net_gex, "\n")
    assert isinstance(net_gex, float)

    unusual = metrics.unusual_activity(df)
    print("Unusual activity (z-score based, daily-collapsed history):\n", unusual, "\n")
    assert (unusual["strike"] == 100.0).any(), "strike 100 had a clear volume spike on day 6 evening, should be flagged"
    assert len(unusual) <= 4, f"expected only strike-100 call/put flagged (z-score), got {len(unusual)} rows"
    flagged_call = unusual[(unusual["strike"] == 100.0) & (unusual["option_type"] == "call")].iloc[0]
    # day 6 morning (volume=200) must not enter the history twice — the history
    # must collapse to 6 calendar days (1-5 + day 6 morning), not 6 raw rows with a dupe
    assert flagged_call["avg_volume"] < 200, (
        f"history should include day 6 (morning, volume=200) only once, "
        f"avg_volume={flagged_call['avg_volume']} looks untouched by the collapse"
    )

    iv_avg = metrics.iv_weighted_average(df)
    print("IV weighted average:\n", iv_avg, "\n")
    assert len(iv_avg) == len(daily_snapshots)

    greeks_history = metrics.contract_greeks_history(df, 100.0, expiry, "call")
    print("Greeks history:\n", greeks_history, "\n")
    assert len(greeks_history) == len(daily_snapshots)
    for col in ("delta", "gamma", "theta", "vega", "rho", "vanna", "charm"):
        assert col in greeks_history.columns

    notes = metrics.interpret_greeks(greeks_history)
    print("Greeks interpretation:")
    for note in notes:
        print(" -", note)
    print()
    assert len(notes) == 7

    # IV outlier guard (found live on the SaaS sibling repo, real user
    # report on real MO LEAPS data, 2026-07-28/29): a reported IV that's a
    # strong outlier vs. this contract's own history, uncorroborated by any
    # matching move in last_price, must not silently produce garbage
    # greeks. Deliberately NOT an absolute Black-Scholes price check (that
    # version shipped and broke immediately on real data: no dividend
    # yield tracked -> badly overprices long-dated calls on high-yield
    # names; also assumed last_price is live, false for thin strikes where
    # it's often just stale). This one only ever compares a contract to
    # itself.
    guard_expiry = pd.Timestamp("2026-12-18")
    guard_base_iv, guard_base_price = 0.30, 3.0
    guard_rows = [
        {  # five stable days — nothing here should ever be suppressed,
           # regardless of what an absolute pricing model would say
            "collected_at": pd.Timestamp("2026-07-01") + timedelta(days=day),
            "expiry": guard_expiry, "strike": 100.0, "option_type": "call",
            "underlying_price": 100.0, "last_price": guard_base_price, "implied_volatility": guard_base_iv,
        }
        for day in range(5)
    ]
    guard_rows.append(
        {  # glitch day: IV far off this contract's own norm, last_price
           # completely unmoved — the exact live pattern (flat option
           # price, jagged IV/greeks)
            "collected_at": pd.Timestamp("2026-07-06"), "expiry": guard_expiry,
            "strike": 100.0, "option_type": "call",
            "underlying_price": 100.0, "last_price": guard_base_price, "implied_volatility": 0.06,
        }
    )
    guard_rows.append(
        {  # genuine repricing: IV AND price both move together — must
           # survive the guard even though IV is just as far from the
           # contract's norm as the glitch day above
            "collected_at": pd.Timestamp("2026-07-07"), "expiry": guard_expiry,
            "strike": 100.0, "option_type": "call",
            "underlying_price": 100.0, "last_price": guard_base_price * 1.5, "implied_volatility": 0.55,
        }
    )
    guard_history = metrics.contract_greeks_history(pd.DataFrame(guard_rows), 100.0, guard_expiry, "call")
    assert (guard_history.iloc[:5]["implied_volatility"] == guard_base_iv).all(), "stable history must pass through unchanged"
    assert not guard_history.iloc[:5]["delta"].isna().any(), "stable history's greeks must not be suppressed"
    assert pd.isna(guard_history.iloc[5]["implied_volatility"]), "uncorroborated outlier IV must be suppressed to NaN"
    assert pd.isna(guard_history.iloc[5]["delta"]), "greeks derived from a suppressed IV must also be NaN"
    assert guard_history.iloc[6]["implied_volatility"] == 0.55, "a real move corroborated by price must not be suppressed"
    assert not pd.isna(guard_history.iloc[6]["delta"]), "a real move's greeks must not be suppressed"
    guard_notes = metrics.interpret_greeks(guard_history)
    assert "not enough reliable data" in guard_notes[0], guard_notes[0]
    print("IV outlier guard checks passed\n")

    # Fail-open on a near-even split (the exact real incident found live on
    # THIS repo: a contract with only 4 locally-collected snapshots — an
    # old stuck IV and the current one, split 2-2 — has a median sitting
    # almost exactly between the two clusters, so BOTH looked like ~60%
    # outliers from it and every point in the contract's history got
    # suppressed, blanking out the whole chart instead of just the glitch.
    # Once "unreliable" would cover too large a share of the history, the
    # guard must back off entirely rather than trust a median that's
    # clearly not representative of anything.
    split_expiry = pd.Timestamp("2028-01-21")
    split_rows = [
        {"collected_at": pd.Timestamp("2026-07-23 12:03:12"), "expiry": split_expiry,
         "strike": 105.0, "option_type": "call",
         "underlying_price": 72.17, "last_price": 1.39, "implied_volatility": 0.0625},
        {"collected_at": pd.Timestamp("2026-07-23 12:03:51"), "expiry": split_expiry,
         "strike": 105.0, "option_type": "call",
         "underlying_price": 72.17, "last_price": 1.39, "implied_volatility": 0.0625},
        {"collected_at": pd.Timestamp("2026-07-28 21:39:51"), "expiry": split_expiry,
         "strike": 105.0, "option_type": "call",
         "underlying_price": 72.08, "last_price": 1.55, "implied_volatility": 0.2576},
        {"collected_at": pd.Timestamp("2026-07-28 21:41:07"), "expiry": split_expiry,
         "strike": 105.0, "option_type": "call",
         "underlying_price": 72.08, "last_price": 1.55, "implied_volatility": 0.2576},
    ]
    split_history = metrics.contract_greeks_history(pd.DataFrame(split_rows), 105.0, split_expiry, "call")
    assert not split_history["implied_volatility"].isna().any(), "a near-even split must fail open, not blank the whole contract"
    assert not split_history["delta"].isna().any(), "greeks must survive the fail-open path too"
    print("IV outlier guard fail-open checks passed\n")

    delta = metrics.oi_delta(df)
    print("OI delta (day-over-day, collapsed):\n", delta, "\n")
    assert not delta.empty
    strike_100_row = delta[(delta["strike"] == 100.0) & (delta["option_type"] == "call")].iloc[0]
    # Without the collapse, the old code would compare today's two collections (OI 65 vs 65) => 0.
    # With the fix, day 6 (65) is compared against day 5 (61) => +4.
    assert strike_100_row["oi_delta"] == 4, f"expected day-over-day OI delta of +4 (65-61), got {strike_100_row['oi_delta']}"
    assert strike_100_row["open_interest"] == 65, f"expected latest OI of 65, got {strike_100_row['open_interest']}"
    expected_pct = 4 / 61 * 100
    assert abs(strike_100_row["oi_delta_pct"] - expected_pct) < 1e-9, (
        f"expected oi_delta_pct ~{expected_pct:.3f}, got {strike_100_row['oi_delta_pct']}"
    )

    matrix = metrics.gex_matrix(df)
    print("GEX matrix (strike x expiry):\n", matrix, "\n")
    assert not matrix.empty
    assert expiry in matrix.columns
    assert (matrix.index == sorted(matrix.index, reverse=True)).all(), "strikes should be sorted descending"
    assert not matrix.isna().any().any(), "missing strike/expiry combos should fill with 0, not NaN"

    net_by_expiry = metrics.net_gex_by_expiry(df)
    print("Net GEX by expiry:\n", net_by_expiry, "\n")
    assert len(net_by_expiry) == 1  # the synthetic data contains only one expiry
    assert abs(net_by_expiry["net_gex"].iloc[0] - net_gex) < 1e-6, "should match net_gamma_exposure for the same expiry"

    walls = metrics.dealer_walls(df)
    print("Dealer walls:", walls, "\n")
    # strike 100 holds the highest OI (65) of all synthetic strikes (the rest have 50)
    assert walls["call_wall"] == 100.0, f"expected call wall at strike 100, got {walls['call_wall']}"
    assert walls["put_wall"] == 100.0, f"expected put wall at strike 100, got {walls['put_wall']}"

    flip = metrics.gamma_flip_price(df)
    print("Gamma flip price:", flip, "\n")
    # the synthetic data doesn't guarantee a sign change (the whole profile may
    # stay one sign) — what matters is the function doesn't crash and returns
    # either None or a float
    assert flip is None or isinstance(flip, float)

    # Replay: as_of set to an earlier date must reproduce the historical
    # snapshot, not silently fall back to the latest one
    earlier_date = pd.Timestamp(base_day)
    matrix_earlier = metrics.gex_matrix(df, as_of=earlier_date)
    assert not matrix_earlier.empty
    assert not matrix_earlier.equals(matrix), "historical snapshot should differ from the latest one"

    # iv_surface/iv_surface_grid — a separate small synthetic snapshot with two
    # expiries (the main synthetic data above uses only one, and a surface
    # needs at least 2x2 distinct strikes/expiries)
    iv_snapshot = pd.DataFrame([
        {"collected_at": latest_moment, "underlying_price": 100.0, "expiry": pd.Timestamp("2026-08-01"),
         "strike": 90.0, "option_type": "put", "implied_volatility": 0.35},
        {"collected_at": latest_moment, "underlying_price": 100.0, "expiry": pd.Timestamp("2026-08-01"),
         "strike": 110.0, "option_type": "call", "implied_volatility": 0.30},
        {"collected_at": latest_moment, "underlying_price": 100.0, "expiry": pd.Timestamp("2026-09-01"),
         "strike": 90.0, "option_type": "put", "implied_volatility": 0.38},
        {"collected_at": latest_moment, "underlying_price": 100.0, "expiry": pd.Timestamp("2026-09-01"),
         "strike": 110.0, "option_type": "call", "implied_volatility": 0.33},
    ])
    iv_surface_points = metrics.iv_surface(iv_snapshot)
    print("IV surface points:\n", iv_surface_points, "\n")
    assert len(iv_surface_points) == 4
    assert set(iv_surface_points["strike"]) == {90.0, 110.0}

    iv_grid = metrics.iv_surface_grid(iv_surface_points, strike_points=5, expiry_points=5)
    assert iv_grid is not None
    grid_strikes, grid_years, grid_iv = iv_grid
    assert grid_iv.shape == (5, 5)
    assert not np.isnan(grid_iv).any(), "gaps at the grid edges should be filled by nearest-neighbor fallback"

    # fewer than 2 distinct strikes or expiries — a line, not a surface; must return None, not crash
    assert metrics.iv_surface_grid(iv_surface_points[iv_surface_points["strike"] == 90.0]) is None

    # screener_table (spec FR25) — flat table of the latest snapshot with greeks
    screener = metrics.screener_table(df)
    assert len(screener) == 10, f"latest snapshot has 5 strikes x 2 types = 10 rows, got {len(screener)}"
    assert set(screener.columns) == {
        "expiry", "strike", "option_type", "dte", "last_price", "open_interest",
        "implied_volatility", "delta", "gamma", "theta", "vega", "rho", "vanna", "charm",
    }
    screener_call_100 = screener[(screener["strike"] == 100.0) & (screener["option_type"] == "call")].iloc[0]
    # the same contract on the same date must yield the same greeks as the Contract tab
    matching_history_row = greeks_history[greeks_history["collected_at"] == latest_moment].iloc[0]
    assert abs(screener_call_100["delta"] - matching_history_row["delta"]) < 1e-9
    assert abs(screener_call_100["gamma"] - matching_history_row["gamma"]) < 1e-9
    assert screener_call_100["dte"] == (expiry - latest_moment).days

    # realized_volatility (spec FR24) — synthetic daily history with a constant
    # per-step log return so the annualized vol computes predictably
    rng = np.random.default_rng(42)
    daily_log_returns = rng.normal(loc=0.0, scale=0.02, size=40)
    closes = 100.0 * np.exp(np.cumsum(daily_log_returns))
    price_history = pd.DataFrame({"close": closes})
    rv = metrics.realized_volatility(price_history, windows=(10, 20, 30))
    assert set(rv.keys()) == {10, 20, 30}, f"all three windows should fit in 40 days, got {rv.keys()}"
    for value in rv.values():
        assert 0 < value < 2, f"annualized RV out of sane range: {value}"
    # a window larger than the available history simply doesn't appear in the result, no crash
    rv_short = metrics.realized_volatility(price_history.head(15), windows=(10, 20, 30))
    assert set(rv_short.keys()) == {10}, f"only the 10d window fits in 15 days, got {rv_short.keys()}"

    # collector._oi_zero_fraction + collection log (spec FR23) — real incident
    # 2026-07-17: the data source returned a "successful" response with working
    # volume/prices but open_interest almost entirely zero. The zero fraction
    # must compute correctly and reach the log regardless of the outcome.
    healthy_chain = pd.DataFrame({"open_interest": [10, 0, 50, 200, 0]})
    corrupted_chain = pd.DataFrame({"open_interest": [0, 0, 0, 0, 12]})
    assert collector._oi_zero_fraction(healthy_chain) == 0.4
    assert collector._oi_zero_fraction(corrupted_chain) == 0.8
    assert collector._oi_zero_fraction(pd.DataFrame({"open_interest": []})) == 1.0

    # deliberately later than every other log_run in this pass — otherwise with
    # equal started_at their order under ORDER BY ... DESC is not guaranteed
    log_moment = latest_moment + timedelta(hours=1)
    db.log_run(
        conn, log_moment, log_moment + timedelta(seconds=1), "TEST", "failed",
        error_message="80% of contracts have open_interest=0 (threshold 50%) — not saving",
        rows_fetched=5, oi_zero_fraction=0.8,
    )
    recent_runs = db.get_recent_runs(conn, limit=1)
    print("Recent collection run log:\n", recent_runs, "\n")
    assert recent_runs.iloc[0]["rows_fetched"] == 5
    assert abs(recent_runs.iloc[0]["oi_zero_fraction"] - 0.8) < 1e-9
    assert recent_runs.iloc[0]["status"] == "failed"

    db.add_tracked_contract(conn, "TEST", expiry, 100.0, "call")
    tracked = db.get_tracked_contracts(conn, "TEST")
    print("Tracked contracts:\n", tracked, "\n")
    assert len(tracked) == 1
    db.remove_tracked_contract(conn, int(tracked.iloc[0]["id"]))
    assert db.get_tracked_contracts(conn, "TEST").empty

    check_missing_numbers_survive_a_write(conn)
    check_narrow_reads_and_rollups(conn)
    check_scheduled_collection(conn)
    check_archiving_moves_and_keeps(conn)
    check_two_sources_never_mix(conn)
    check_expired_contracts_stay_reachable(conn)
    check_greek_attribution()

    print("ALL SMOKE CHECKS PASSED")

if __name__ == "__main__":
    main()
