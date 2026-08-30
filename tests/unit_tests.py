"""Unit checks for the pure functions in metrics.py — no database, no network.

Separate from smoke_test.py on purpose. That one exercises the db.py +
metrics.py plumbing on synthetic data: it answers "does this work against a
real SQLite file". These answer "is the number right", which needs no database
at all and so should not pay for one.

Not a pytest suite — plain asserts and a main(), matching smoke_test.py and the
project's preference for small and readable over frameworks.

Usage: python tests/unit_tests.py
"""

import datetime as dt
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from scipy.stats import norm

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testdb  # noqa: E402

testdb.configure()

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

    # A fake PROVIDER, not a patched module function. Before providers existed
    # this test replaced collector.fetch_ticker_snapshot, and when collection
    # moved behind the provider interface that patch silently stopped applying:
    # the suite kept passing names like "BOOM" to the real Yahoo API and took
    # 17 seconds to decide the network disagreed with it. Injection is the
    # supported way in, and it is what keeps these checks offline.
    class Fake:
        name = "yahoo"
        price_history_source = "yahoo"
        requires_token = False

        def fetch_ticker_snapshot(self, ticker):
            if ticker == "BOOM":
                raise RuntimeError("provider exploded")
            return 100.0, chain(zero_oi=(ticker == "ZEROOI"))

    results = collector.collect_watchlist(conn, ["GOOD", "BOOM", "ZEROOI"], provider=Fake())

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

def check_metrics_core_is_pristine():
    """app/metrics_core.py is byte-identical to the copy in the hosted product's
    repository. This check is what makes that claim enforceable here.

    It cannot compare the two repositories — this one is public and has no
    business reaching into a private one. What it catches is the likelier
    accident, and the one that matters most in an open-source repo: a
    contributor edits the shared core without noticing the header saying it is
    shared. The failure prints the new hash, so a DELIBERATE change costs one
    paste while an accidental one stops the suite.

    Also asserts the config contract: the core may import `app.config` and
    nothing else from the application, and every constant it reads has to exist
    here. A name that exists in one product and not the other turns a shared
    file into an AttributeError on someone's first page load."""
    import hashlib
    import re

    root = os.path.join(os.path.dirname(__file__), "..")
    with open(os.path.join(root, "app", "metrics_core.py"), "rb") as handle:
        body = handle.read()
    actual = hashlib.sha256(body).hexdigest()
    with open(os.path.join(root, "app", "metrics_core.sha256")) as handle:
        recorded = handle.read().split()[0]
    assert actual == recorded, (
        "app/metrics_core.py changed but app/metrics_core.sha256 did not.\n"
        "  This file is shared with the hosted product. If the change is\n"
        "  deliberate, it has to land there too — then write this line into\n"
        "  app/metrics_core.sha256:\n"
        f"    {actual}  app/metrics_core.py"
    )

    source = body.decode()
    app_imports = set(re.findall(r"^from app(?:\.([a-z_]+))? import", source, re.M))
    assert app_imports <= {""}, (
        f"metrics_core.py imports {sorted(app_imports)} from the application. Only "
        "`from app import config` may be shared — anything else does not exist in "
        "the same shape in both products."
    )
    from app import config
    used = set(re.findall(r"\bconfig\.([A-Z_][A-Z0-9_]*)", source))
    assert used, "no config constants found — the extraction regex is probably wrong"
    for name in sorted(used):
        assert hasattr(config, name), f"metrics_core.py reads config.{name}, which does not exist here"
    print(f"shared-core checks passed ({len(used)} config constants, hash matches)")

def check_dividend_yield_defaults_to_the_old_model():
    """Greeks gained a dividend yield when the core became shared. At q=0 the
    generalized formulas must reduce EXACTLY to the ones this project used
    before — otherwise the port silently changed every number on every chart.

    Checked against the previous implementation itself, reproduced below from
    the pre-shared-core source, rather than against pasted golden numbers: a
    literal only records what the code did on the day someone ran it, and gets
    "corrected" to whatever it prints next time it fails. The reduction is
    exact algebra — every carry term is e^0 = 1 and every q-term is multiplied
    by zero — so the assertion is exact equality, with no tolerance to tune.

    Charm is the one worth naming: it used to be computed once and shared
    between calls and puts, which is correct only at q=0, and is now computed
    per option type."""
    def previous_implementation(spot, strike, t, iv, r, option_type):
        sqrt_t = np.sqrt(t)
        d1 = (np.log(spot / strike) + (r + iv ** 2 / 2) * t) / (iv * sqrt_t)
        d2 = d1 - iv * sqrt_t
        pdf_d1 = norm.pdf(d1)
        discount = np.exp(-r * t)
        greeks = {
            "gamma": pdf_d1 / (spot * iv * sqrt_t),
            "vega": spot * pdf_d1 * sqrt_t / 100,
            "vanna": -pdf_d1 * d2 / iv,
            "charm": -pdf_d1 * (2 * r * t - d2 * iv * sqrt_t) / (2 * t * iv * sqrt_t),
        }
        if option_type == "call":
            greeks["delta"] = norm.cdf(d1)
            greeks["theta"] = (-(spot * pdf_d1 * iv) / (2 * sqrt_t)
                               - r * strike * discount * norm.cdf(d2)) / 365
            greeks["rho"] = strike * t * discount * norm.cdf(d2) / 100
        else:
            greeks["delta"] = norm.cdf(d1) - 1
            greeks["theta"] = (-(spot * pdf_d1 * iv) / (2 * sqrt_t)
                               + r * strike * discount * norm.cdf(-d2)) / 365
            greeks["rho"] = -strike * t * discount * norm.cdf(-d2) / 100
        return greeks

    checked = 0
    for spot in (50.0, 100.0, 431.7):
        for strike in (45.0, 100.0, 460.0):
            for t in (0.002, 0.08, 1.0, 3.0):      # from one day out to a LEAP
                for iv in (0.09, 0.35, 1.2):
                    for r in (0.0, 0.05):
                        for option_type in ("call", "put"):
                            now = metrics._black_scholes_greeks(spot, strike, t, iv, r, option_type)
                            then = previous_implementation(spot, strike, t, iv, r, option_type)
                            for greek, expected in then.items():
                                assert now[greek] == expected, (
                                    f"{greek} changed at q=0: spot={spot} strike={strike} "
                                    f"t={t} iv={iv} r={r} {option_type}: {now[greek]} != {expected}"
                                )
                            checked += 1

    # And the parameter is not decorative: a real yield has to move delta.
    flat = metrics._black_scholes_greeks(100.0, 100.0, 1.0, 0.30, 0.05, "call")
    with_q = metrics._black_scholes_greeks(100.0, 100.0, 1.0, 0.30, 0.05, "call",
                                           dividend_yield=0.03)
    assert with_q["delta"] < flat["delta"] - 0.01, (with_q["delta"], flat["delta"])

    # Put charm is no longer a copy of call charm once q != 0.
    put_q = metrics._black_scholes_greeks(100.0, 100.0, 1.0, 0.30, 0.05, "put",
                                          dividend_yield=0.03)
    assert abs(put_q["charm"] - with_q["charm"]) > 1e-9, "charm must differ by side when q != 0"
    print(f"dividend-yield reduction checks passed ({checked} contracts, exact equality)")

def check_iv_average_survives_a_contract_without_iv():
    """One contract with no implied volatility must not empty the whole day's
    volume-weighted average.

    `np.average` computes sum(a*w)/sum(w), and NaN*0 is still NaN — so a
    zero-weighted row with a missing IV poisons the result. This was invisible
    while the only data source filled IV on every contract; the hosted product
    hit it the day a second provider arrived, because a real provider
    legitimately reports no IV for part of a chain. The provider abstraction
    added here is exactly what makes that possible in this repo too, so the
    check comes with it rather than after it."""
    moment = datetime(2026, 8, 12, 20, 0)
    live = pd.Timestamp("2026-09-18")
    rows = [
        {"collected_at": moment, "expiry": live, "implied_volatility": 0.30, "volume": 100},
        {"collected_at": moment, "expiry": live, "implied_volatility": 0.50, "volume": 300},
        # Deep OTM, never traded, no IV quoted — the shape that broke it.
        {"collected_at": moment, "expiry": live, "implied_volatility": np.nan, "volume": 0},
    ]
    result = metrics.iv_weighted_average(pd.DataFrame(rows))
    assert len(result) == 1, result
    value = float(result.iloc[0]["iv_weighted_avg"])
    assert abs(value - (0.30 * 100 + 0.50 * 300) / 400) < 1e-12, value

    # A contract whose expiry has passed carries no volatility, whatever the
    # source reports for it. Found on the hosted side, where the provider
    # returns a sentinel 20.0 for such contracts and they still arrive with the
    # whole session's volume: on SPY that turned a ticker average of 0.163 into
    # 13.53, once a day, every day. The comparison is by date and inclusive —
    # a contract expiring today is real trading during today's session.
    with_expired = pd.DataFrame([
        {"collected_at": moment, "expiry": live, "implied_volatility": 0.30, "volume": 100},
        {"collected_at": moment, "expiry": pd.Timestamp("2026-08-11"),
         "implied_volatility": 20.0, "volume": 900_000},
        {"collected_at": moment, "expiry": pd.Timestamp("2026-08-12"),
         "implied_volatility": 0.30, "volume": 100},
    ])
    expired_out = float(metrics.iv_weighted_average(with_expired).iloc[0]["iv_weighted_avg"])
    assert abs(expired_out - 0.30) < 1e-12, (
        f"an expired contract's sentinel volatility reached the average: {expired_out}"
    )

    # A day with no usable IV at all returns NaN rather than raising.
    only_nan = pd.DataFrame([
        {"collected_at": moment, "expiry": live, "implied_volatility": np.nan, "volume": 5},
    ])
    assert np.isnan(float(metrics.iv_weighted_average(only_nan).iloc[0]["iv_weighted_avg"]))
    print("IV-average checks passed")

def check_pricing_inputs():
    """PricingInputs is the (r, q) pair every greek is computed with. The
    default instance must reproduce the flat-rate model this project used
    before it existed — that is what makes it safe to thread through every
    signature without changing a number."""
    default = metrics.DEFAULT_PRICING
    from app import config
    assert default.rate_for(0.5) == config.RISK_FREE_RATE
    assert default.dividend_yield == 0.0
    rates = default.rate_series(pd.Series([0.1, 1.0, 5.0]))
    assert len(rates) == 3 and all(r == config.RISK_FREE_RATE for r in rates)

    # With a curve, the rate depends on maturity — par yields in percent,
    # converted to a continuously-compounded rate.
    curve = {0.5: 4.0, 2.0: 4.4, 10.0: 4.8}
    priced = metrics.PricingInputs(curve=curve, dividend_yield=0.02)
    short, long_ = priced.rate_for(0.5), priced.rate_for(10.0)
    assert 0.03 < short < long_ < 0.05, (short, long_)
    assert abs(priced.rate_for(1.25) - (short + long_) / 2) < 0.01 or short < priced.rate_for(1.25) < long_
    # Off the ends of the curve it clamps rather than extrapolating to nonsense.
    assert priced.rate_for(0.01) == priced.rate_for(0.5)
    assert priced.rate_for(40.0) == priced.rate_for(10.0)
    assert "tenors=3" in repr(priced) and "q=0.0200" in repr(priced)

    # risk_free_rate() on an empty curve has nothing to interpolate.
    assert metrics.risk_free_rate({}, 1.0) is None
    print("pricing-input checks passed")

def check_unpriceable_contracts_are_skipped():
    """A contract Black-Scholes cannot price must produce no greeks rather than
    a plausible-looking number. Zero or negative time, zero IV, zero spot — and
    an IV so extreme it is a data error, not a market."""
    assert metrics._is_priceable(100.0, 100.0, 0.5, 0.3) is True
    assert metrics._is_priceable(100.0, 100.0, 0.0, 0.3) is False, "expired"
    assert metrics._is_priceable(100.0, 100.0, 0.5, 0.0) is False, "no IV"
    assert metrics._is_priceable(0.0, 100.0, 0.5, 0.3) is False, "no spot"
    # Missing values, not just zeroes: a chain routinely carries None/NaN IV on
    # strikes that never traded, and `None > 0` raises rather than returning
    # False, so the guard has to reject them before comparing.
    assert metrics._is_priceable(100.0, 100.0, 0.5, None) is False
    assert metrics._is_priceable(100.0, 100.0, np.nan, 0.3) is False

    # A genuinely expired contract prices to zero greeks rather than raising on
    # log(0) or sqrt of a negative. Zero and not NaN is deliberate: a contract
    # past its close really does have no delta left. What made this look like a
    # bug for months was the DATE arithmetic in front of it — measuring to
    # midnight meant a contract still trading through the session arrived here
    # with t <= 0 and every greek collapsed at midnight (see
    # check_years_to_expiry, which is what guards that).
    expired = metrics._black_scholes_greeks(100.0, 100.0, 0.0, 0.3, 0.05, "call")
    assert set(expired) == set(metrics._GREEK_KEYS)
    assert all(value == 0.0 for value in expired.values()), expired

    # A missing IV is not the same statement as a zero one, and the difference
    # is visible on a chart: zero above means an expired contract genuinely has
    # no optionality left, while a source that quotes no volatility for a strike
    # tells us nothing about its delta. Returning 0.0 for the second would draw
    # a flat line where there is no data at all.
    #
    # This crashed in the paid sibling on 14.08.2026 (`None <= 0` raises), and
    # the free product is exposed by the same file: Yahoo quotes an IV on
    # everything, but the provider interface added in v0.3.0 exists precisely so
    # that other sources can be plugged in, and a source with no IV is ordinary.
    for missing in (None, np.nan):
        unknown = metrics._black_scholes_greeks(100.0, 100.0, 0.5, missing, 0.05, "call")
        assert set(unknown) == set(metrics._GREEK_KEYS)
        assert all(np.isnan(value) for value in unknown.values()), (missing, unknown)
        no_spot = metrics._black_scholes_greeks(missing, 100.0, 0.5, 0.3, 0.05, "call")
        assert all(np.isnan(value) for value in no_spot.values()), (missing, no_spot)
    print("unpriceable-contract checks passed")

def check_contracts_backing_expiry():
    """How many contracts with open interest stand behind an expiry's numbers.

    Max Pain and GEX are both weighted by open interest, so an expiry where
    almost nothing is open produces a confident-looking number resting on two
    or three strikes — arithmetically correct and meaningless. This is the
    count that lets the interface say which kind it is showing."""
    collected = pd.Timestamp("2026-08-12 20:00:00")
    expiry = pd.Timestamp("2026-09-18")
    other = pd.Timestamp("2026-10-16")
    df = pd.DataFrame([
        {"collected_at": collected, "expiry": expiry, "strike": 100.0,
         "option_type": "call", "open_interest": 500},
        {"collected_at": collected, "expiry": expiry, "strike": 105.0,
         "option_type": "put", "open_interest": 0},
        # Newly listed, quoted but never traded — open_interest arrives null.
        {"collected_at": collected, "expiry": expiry, "strike": 110.0,
         "option_type": "call", "open_interest": None},
        {"collected_at": collected, "expiry": other, "strike": 100.0,
         "option_type": "call", "open_interest": 900},
        # An older snapshot of the same expiry must not be counted alongside
        # the latest one — the count describes one snapshot, not the history.
        {"collected_at": pd.Timestamp("2026-08-11 20:00:00"), "expiry": expiry,
         "strike": 115.0, "option_type": "call", "open_interest": 700},
    ])
    assert metrics.contracts_backing_expiry(df, expiry) == 1, "only the strike with OI counts"
    assert metrics.contracts_backing_expiry(df, other) == 1
    assert metrics.contracts_backing_expiry(df, pd.Timestamp("2027-01-15")) == 0, "unknown expiry"
    print("expiry-backing checks passed")

def check_provider_registry():
    """get_provider() is the single place a name becomes a working object.

    An unknown name has to raise. Falling back to the default would mean
    someone configures a source, sees data appear, and never learns the numbers
    came from somewhere else — the failure would surface weeks later as "these
    figures look wrong" with nothing pointing at the cause.

    Also checks that YahooProvider actually satisfies the Protocol. It is
    runtime-checkable precisely so that "I wrote a provider, does it fit?" is a
    one-line answer for anyone adding their own."""
    from app import providers

    assert providers.known_providers() == ("yahoo",), providers.known_providers()
    assert providers.DEFAULT_PROVIDER in providers.known_providers()

    default = providers.get_provider()
    assert default.name == "yahoo" and default.requires_token is False
    assert providers.get_provider("YAHOO").name == "yahoo", "the name is case-insensitive"
    assert providers.get_provider("  yahoo  ").name == "yahoo", "and surrounded by whitespace"
    assert isinstance(default, providers.DataProvider), (
        "YahooProvider must satisfy the DataProvider protocol"
    )

    try:
        providers.get_provider("definitely-not-a-provider")
    except ValueError as exc:
        assert "definitely-not-a-provider" in str(exc), exc
        assert "yahoo" in str(exc), "the error should say what IS available"
    else:
        raise AssertionError("an unknown provider name must raise, not fall back")

    # Every column the reader expects, in the order db.insert_snapshot writes.
    assert providers.CHAIN_COLUMNS[:3] == ["expiry", "strike", "option_type"]
    for greek in ("delta", "gamma", "theta", "vega"):
        assert greek in providers.CHAIN_COLUMNS, f"{greek} missing from the provider contract"
    print("provider registry checks passed")


def check_timezone_database_is_available():
    """Both daylight-saving transitions, resolved by the actual database.

    This is a check on the ENVIRONMENT as much as on the code. Every market-hours
    decision and every timestamp shown to a reader goes through zoneinfo, which
    reads the system timezone database — `python:3.12-slim` ships one, an image
    built on something slimmer does not, and the failure is ZoneInfo raising at
    import time on somebody else's machine. `tzdata` is in requirements.txt so
    the product carries its own copy; this asserts that whatever is being used
    actually knows when the clocks move.
    """
    market = ZoneInfo("America/New_York")

    def offset(day, hour=9, minute=30):
        return dt.datetime(*day, hour, minute, tzinfo=market).utcoffset()

    # Spring forward 2026 is 8 March, autumn back is 1 November.
    assert offset((2026, 3, 7)) == dt.timedelta(hours=-5), "before the spring change: EST"
    assert offset((2026, 3, 9)) == dt.timedelta(hours=-4), "after it: EDT"
    assert offset((2026, 10, 31)) == dt.timedelta(hours=-4), "before the autumn change: EDT"
    assert offset((2026, 11, 2)) == dt.timedelta(hours=-5), "after it: EST"

    # And the consequence that matters: 09:30 in New York is a DIFFERENT UTC
    # instant either side of a transition. A fixed offset would put the open an
    # hour out for weeks at a time, in the direction of thinking the market is
    # already trading when it is not.
    from app import market_calendar as calendar

    winter = dt.datetime(2026, 1, 5, 14, 30, tzinfo=dt.timezone.utc)   # 09:30 EST
    summer = dt.datetime(2026, 8, 17, 13, 30, tzinfo=dt.timezone.utc)  # 09:30 EDT
    assert calendar.state_from_clock(winter) == calendar.OPEN
    assert calendar.state_from_clock(summer) == calendar.OPEN
    assert calendar.state_from_clock(winter - dt.timedelta(hours=1)) == calendar.CLOSED
    assert calendar.state_from_clock(summer - dt.timedelta(hours=1)) == calendar.CLOSED
    print("Timezone-database checks passed (both DST transitions)")


def _chain_row(expiry, strike, option_type, **extra):
    row = {
        "expiry": expiry, "strike": strike, "option_type": option_type,
        "last_price": 1.0, "bid": 0.9, "ask": 1.1, "volume": 5,
        "open_interest": 50, "implied_volatility": 0.25, "in_the_money": False,
    }
    row.update(extra)
    return row


def check_adjusted_contracts_do_not_break_collection():
    """A chain carrying two contracts at one strike must still store.

    THE FAILURE. After a split or a special dividend an adjusted series trades
    alongside the standard one at the same strike, expiry and type — which is
    exactly the key contract_registry is built on. The upsert then tries to
    update one registry row twice in one statement and Postgres refuses the
    whole thing:

        ON CONFLICT DO UPDATE command cannot affect row a second time

    Chain and registry are written in one transaction, so the snapshot dies
    with it: not a degraded collection, none at all, on every cycle, for as
    long as the adjusted series exists. Silent from the outside.

    Run this against the code before the fix and it raises here rather than
    asserting — which is the point of writing it with a real database.
    """
    conn = db.get_connection()
    expiry = "2027-01-15"
    # TSLL1 is the adjusted root: the plain ticker plus a digit. The standard
    # series must be the one that survives — it is where the volume is, and it
    # is what a reader means by "the 100 strike".
    chain = pd.DataFrame([
        _chain_row(expiry, 100.0, "call", contract_symbol="TSLL1270115C00100000",
                   open_interest=3),
        _chain_row(expiry, 100.0, "call", contract_symbol="TSLL270115C00100000",
                   open_interest=4321),
        _chain_row(expiry, 105.0, "put", contract_symbol="TSLL270115P00105000"),
    ])
    moment = datetime(2026, 8, 24, 15, 0)
    db.insert_snapshot(conn, "TSLL", moment, 100.0, chain)

    stored = db.get_snapshots(conn, "TSLL", days=None)
    assert len(stored) == 2, f"one row per contract identity, got {len(stored)}"
    survivor = stored[(stored["strike"] == 100.0) & (stored["option_type"] == "call")]
    assert len(survivor) == 1, survivor
    assert int(survivor["open_interest"].iloc[0]) == 4321, (
        "the standard series must win, not whichever row came first"
    )

    # Without a symbol there is nothing to choose by, and losing one contract
    # still beats losing the ticker.
    blind = pd.DataFrame([
        _chain_row(expiry, 200.0, "call"),
        _chain_row(expiry, 200.0, "call"),
    ])
    db.insert_snapshot(conn, "BLIND", moment, 200.0, blind)
    assert len(db.get_snapshots(conn, "BLIND", days=None)) == 1

    assert db._contract_root("TSLL270115C00100000") == "TSLL"
    assert db._contract_root("TSLL1270115C00100000") == "TSLL1"
    assert db._contract_root("short") is None
    conn.close()
    print("adjusted-contract checks passed")


def check_archiving_goes_through_the_registry():
    """Expired contracts move, and the statements never filter on `expiry`.

    WHY THE SHAPE IS ASSERTED AND NOT THE PLAN. option_snapshots has no index
    on `expiry` alone — it sits third in a composite, behind two equalities,
    where it cannot be seeked — so `WHERE expiry < …` can only be answered by
    scanning the entire hot table, twice per pass, on every pass, including
    days with nothing to archive. Measured on a real 18.5M-row database at
    4,912 ms and 333,025 blocks per scan.

    A check on the query plan would be the direct test and is not honest on a
    database this size: with a few hundred rows the planner picks a sequential
    scan for everything, correctly, and the assertion would fail on code that
    is right. So this asserts the two things that survive being small — the
    statements reach snapshots through contract_registry, and the move loses
    nothing.
    """
    conn = db.get_connection()
    move, remove = db.archive_statements()
    for statement in (move, remove):
        assert "contract_registry" in statement, statement
        assert "o.expiry <" not in statement, (
            "filtering the hot table by expiry is the scan this exists to avoid"
        )
        assert "r.expiry <" in statement, statement

    old = (dt.date.today() - dt.timedelta(days=400)).isoformat()
    live = (dt.date.today() + dt.timedelta(days=90)).isoformat()
    moment = datetime(2026, 8, 24, 16, 0)
    db.insert_snapshot(conn, "ARCH", moment, 50.0, pd.DataFrame([
        _chain_row(old, 10.0, "call"),
        _chain_row(live, 20.0, "call"),
    ]))
    moved = db.archive_expired_contracts(conn, grace_days=30)
    assert moved == 1, f"exactly the expired contract moves, got {moved}"

    # The hot table specifically, not db.get_snapshots — that reads both tables
    # by design, so it would show the same two rows whether archiving worked or
    # did nothing at all.
    hot = conn.execute(
        "SELECT expiry FROM option_snapshots WHERE ticker = 'ARCH'"
    ).fetchall()
    assert [str(row[0]) for row in hot] == [live], hot
    archived = conn.execute(
        "SELECT count(*) FROM option_snapshots_archive WHERE ticker = 'ARCH'"
    ).fetchone()[0]
    assert archived == 1, "the row is moved, never deleted"
    # And the reader still sees both halves — which is the point of moving
    # rather than deleting.
    assert len(db.get_snapshots(conn, "ARCH", days=None)) == 2
    conn.close()
    print("archiving checks passed (registry join, nothing lost)")


def check_disk_estimate_respects_the_market_calendar():
    """The growth estimate must not assume collection runs around the clock.

    It did, and was too high by roughly five: the collector has slept through a
    closed market since v0.5.0, and the formula still multiplied by 24 hours
    and 30 days. The error was in the safe direction, which is why it could
    have lived a long time — nobody complains that the disk filled slower than
    promised — but the number is shown at the one moment somebody decides
    whether to switch collection on at all.
    """
    from app import db as database

    naive_15 = (60 / 15) * 24 * 30  # what the old formula gave: 2,880
    actual_15 = database._passes_per_month(15)
    assert 500 < actual_15 < 620, actual_15
    ratio = naive_15 / actual_15
    assert 4.5 < ratio < 5.5, f"expected the old figure to be ~5x too high, got {ratio:.1f}"

    # 21 trading days plus one snapshot on each of the nine closed ones.
    assert actual_15 == 21 * (390 / 15) + 9, actual_15

    # A four-hour interval gets a fraction of a pass per session, and rounding
    # it up would bring back a smaller version of the same overstatement.
    assert database._passes_per_month(240) < 21 * 2 + 9

    # Never below one pass per trading day, however long the interval.
    assert database._passes_per_month(100_000) == 21 + 9
    print("disk-estimate checks passed")


def check_version_comes_from_the_changelog():
    """The app must be able to say which revision it is.

    Every self-hosted installation runs a different one, updated whenever its
    owner felt like it, so a screenshot without a version starts every support
    conversation with a round of correspondence. Read from CHANGELOG.md rather
    than a constant because the changelog cannot be forgotten at release time
    and a constant can — and a version that lies is worse than none.
    """
    from app import config as settings

    assert settings.APP_VERSION.startswith("v"), settings.APP_VERSION
    assert settings._read_version() == settings.APP_VERSION
    assert settings.APP_VERSION[1].isdigit(), settings.APP_VERSION

    # AN EMPTY [Unreleased] HEADING IS NOT UNRELEASED WORK, and the first
    # release cut after this function was written proved it the hard way: Keep
    # a Changelog leaves that heading in the file forever, so the tagged build
    # called itself "+unreleased". The section counts only when something is
    # written under it.
    import pathlib
    lines = pathlib.Path(settings._CHANGELOG).read_text(encoding="utf-8").splitlines()
    headings = [i for i, line in enumerate(lines) if line.startswith("## [")]
    first, second = headings[0], headings[1]
    has_unreleased_content = any(line.strip() for line in lines[first + 1:second])
    assert ("+unreleased" in settings.APP_VERSION) == has_unreleased_content, (
        settings.APP_VERSION, has_unreleased_content
    )
    print(f"version check passed ({settings.APP_VERSION})")


def check_hopeless_symbols_stop_being_asked_for():
    """A symbol that has never worked is suspended; one with history never is.

    That second half is the whole safeguard. A data source having a bad
    afternoon fails everything at once, including tickers that have collected
    happily for months — suspending those would delete a working watchlist over
    an outage.
    """
    from app import config as settings

    conn = db.get_connection()
    limit = settings.UNRESOLVABLE_AFTER_FAILURES
    started = datetime(2026, 8, 24, 9, 0)

    for step in range(limit):
        db.log_run(conn, started + dt.timedelta(minutes=step),
                   started + dt.timedelta(minutes=step), "NOPE", "failed", "no such symbol")
    suspended = db.unresolvable_tickers(conn)
    assert "NOPE" in suspended, suspended
    # The moment it was suspended is the moment of the failure that crossed the
    # threshold — the sixth, not the first. "Failing since" and "given up on"
    # are different dates, and the second is the one worth showing.
    assert suspended["NOPE"] == started + dt.timedelta(minutes=limit - 1), suspended

    # One short of the threshold is not suspended.
    for step in range(limit - 1):
        db.log_run(conn, started + dt.timedelta(minutes=step),
                   started + dt.timedelta(minutes=step), "ALMOST", "failed", "no such symbol")
    assert "ALMOST" not in db.unresolvable_tickers(conn)

    # A symbol with a single success in its whole history is never suspended,
    # however many times it has failed since.
    db.log_run(conn, started, started, "REAL", "success", rows_fetched=10)
    for step in range(limit * 3):
        db.log_run(conn, started + dt.timedelta(minutes=step),
                   started + dt.timedelta(minutes=step), "REAL", "failed", "provider down")
    assert "REAL" not in db.unresolvable_tickers(conn), (
        "an outage must never suspend a ticker that has collected before"
    )

    # And the collector acts on it rather than merely reporting it.
    class Boom:
        name = "yahoo"
        price_history_source = "yahoo"
        requires_token = False

        def fetch_ticker_snapshot(self, ticker):
            raise AssertionError(f"{ticker} must not be requested")

    from app import collector

    results = collector.collect_watchlist(conn, ["NOPE"], provider=Boom())
    assert results["NOPE"].startswith("skipped"), results
    conn.close()
    print("suspension checks passed")


def check_history_depth_is_known_before_the_chart():
    """How much history a ticker has, without touching option_snapshots.

    A self-hosted database starts empty, so the first chart is a single point
    by construction. Saying so before somebody adds the ticker is the whole
    fix; reading it from the rollup rather than the hot table is what keeps it
    affordable enough to say on a text input.
    """
    conn = db.get_connection()
    first = datetime(2026, 8, 20, 15, 0)
    for step in range(3):
        db.insert_snapshot(conn, "DEEP", first + dt.timedelta(days=step), 100.0,
                           pd.DataFrame([_chain_row("2027-01-15", 100.0, "call")]))
    depth = db.collection_depth(conn)
    assert depth["DEEP"] == first.date(), depth
    assert "NEVERCOLLECTED" not in depth
    conn.close()
    print("history-depth checks passed")


def check_being_throttled_stops_the_whole_pass():
    """Rate limiting is the one failure that must not be retried.

    Retrying a throttled source is what extends the throttling, and with_retry
    did it three times per call for every expiry of every ticker. One mistyped
    symbol was enough to burn half a dozen requests before the first valid
    ticker was reached, after which the source refused that one too — and the
    log blamed the ticker that was fine.
    """
    from app import collector, providers

    assert providers.is_rate_limited("HTTP Error 429: Too Many Requests")
    assert providers.is_rate_limited(RuntimeError("Yahoo error 999"))

    class YFRateLimitError(Exception):
        pass

    assert providers.is_rate_limited(YFRateLimitError("slow down"))
    # A strike or a row count that happens to contain 999 is not throttling.
    assert not providers.is_rate_limited("collected 999 rows")
    assert not providers.is_rate_limited(ValueError("Empty option chain"))

    calls = []

    def counted():
        calls.append(1)
        raise RuntimeError("HTTP Error 429: Too Many Requests")

    try:
        providers.with_retry(counted)
    except RuntimeError:
        pass
    assert len(calls) == 1, f"a throttled call must not be retried, made {len(calls)}"

    conn = db.get_connection()
    db.set_setting(conn, db.COOLDOWN_UNTIL_KEY, "")

    class Throttled:
        name = "yahoo"
        price_history_source = "yahoo"
        requires_token = False

        def fetch_ticker_snapshot(self, ticker):
            raise RuntimeError("HTTP Error 429: Too Many Requests")

    results = collector.collect_watchlist(conn, ["ONE", "TWO", "THREE"], provider=Throttled())
    assert results["ONE"].startswith("failed"), results
    assert "limiting requests" in results["ONE"], results["ONE"]
    # The rest of the watchlist is not attempted at all — and is not blamed.
    for ticker in ("TWO", "THREE"):
        assert results[ticker].startswith("skipped"), results[ticker]
    assert db.provider_cooldown_until(conn) is not None

    # And while the cooldown holds, nothing goes near the source.
    class Boom:
        name = "yahoo"
        price_history_source = "yahoo"
        requires_token = False

        def fetch_ticker_snapshot(self, ticker):
            raise AssertionError("the source must not be touched during a cooldown")

    during = collector.collect_watchlist(conn, ["ONE"], provider=Boom())
    assert during["ONE"].startswith("skipped"), during

    db.set_setting(conn, db.COOLDOWN_UNTIL_KEY, "")
    conn.commit()
    assert db.provider_cooldown_until(conn) is None
    conn.close()
    print("rate-limit checks passed")


def check_suggestions_name_a_way_forward():
    """A refusal that only says no leaves the person where they were."""
    from app import suggestions

    assert suggestions.suggest("APPL", ["AAPL", "SPY"]) == "AAPL"
    assert suggestions.suggest("NVIDIA", ["NVDA", "SPY"]) == "NVDA"
    # Not a spelling problem at all: no amount of similarity reaches IBIT.
    assert suggestions.suggest("BTCUSD", []) == "IBIT"
    assert suggestions.suggest("NDX", []) == "QQQ"
    # Nothing honest to offer is None, not a guess.
    assert suggestions.suggest("EURUSD", []) is None
    assert suggestions.suggest("ZZZZ", ["AAPL", "SPY"]) is None
    assert suggestions.suggest("", ["AAPL"]) is None

    assert "AAPL" in suggestions.refusal("APPL", ["AAPL"])
    assert "options exchanges" in suggestions.refusal("EURUSD", [])
    assert "as it trades" in suggestions.refusal("ZZZZ", [])
    print("suggestion checks passed")


def check_rollup_fixture_does_not_depend_on_the_weekday():
    """The rollup fixture means the same thing on every day of the year.

    A CLASS OF DEFECT RATHER THAN AN INCIDENT. Three checks in this repository
    built their data from "today" or "yesterday" while the code under them
    counted New York trading days, and each one failed on a specific weekday
    inside code that was correct. The last of them was live: `main` was red
    from Sunday 23.08 because the stored volume baseline saw four days of
    history and the reference saw three — 115 against 20 — and it went green on
    Monday by itself, which is the worst way for a check to be fixed.

    So the fixture builder is checked directly, over a year and a week of
    anchors: that covers every weekday, both daylight-saving changes and a leap
    of the year boundary, and it costs a millisecond.

    THE INVARIANT IS THE ONE `db.rebuild_volume_stats` READS: the newest day of
    the fixture is the anchor itself, and every completed day is strictly
    before it. That is what makes "exclude today" and "exclude the last day of
    the fixture" the same sentence no matter when the suite runs.
    """
    for offset in range(371):
        anchor = dt.date(2026, 1, 1) + dt.timedelta(days=offset)
        days = testdb.rollup_fixture_days(closed=3, today=anchor)
        assert len(days) == 4, (anchor, days)
        assert days[-1] == anchor, (anchor, days)
        completed = days[:-1]
        assert completed == sorted(completed), (anchor, days)
        assert len(set(completed)) == 3, (anchor, days)
        assert all(day < anchor for day in completed), (anchor, days)
        assert all(day.isoweekday() <= 5 for day in completed), (anchor, days)

    # The anchor defaults to the same clock the rule under check reads. Not
    # `market_calendar.last_completed_trading_day()`, and not "yesterday":
    # `db.rebuild_volume_stats` cuts on `dt.date.today()`, so anything else
    # here is a second definition of today waiting to disagree with the first.
    assert testdb.rollup_fixture_days()[-1] == dt.date.today()
    print("rollup fixture checks passed (371 anchors, every weekday)")


def check_the_batched_gex_path_returns_the_old_numbers():
    """One matrix per render, and the numbers are the ones from three passes.

    THE DEFECT THIS CLOSES. The heatmap drew a matrix, then asked for the gamma
    flip, then asked for per-expiry net GEX — and the last two each rebuilt the
    matrix internally. Three evaluations of the same greeks for one screen, on
    a screen whose two sliders rerun the whole script on every nudge. Measured
    on the sibling product, whose matrix is this same function: 854.6 ms to
    66.7 ms on a 13,160-contract chain.

    WHICH MAKES THIS CHECK THE POINT OF THE WHOLE CHANGE. A faster path that
    answers differently is not an optimisation, it is a silent change to the
    numbers people read. So the two paths are held against each other here:
    exactly equal for the flip, and to a floating-point tolerance for the
    per-expiry sums, where the difference is summation ORDER — down the strikes
    rather than over the raw rows — and nothing else.

    `expiry_rollup` is checked against the same reference in the same breath.
    Nothing in this product calls it yet; it is part of the shared core, so it
    is here, and an untested function in a file two products read from is worse
    than an unused one.
    """
    moment = pd.Timestamp("2026-08-10 20:00")
    expiries = ["2026-09-18", "2026-10-16"]
    rows = []
    for expiry in expiries:
        for strike in (90.0, 95.0, 100.0, 105.0, 110.0):
            for option_type in ("call", "put"):
                rows.append({
                    "collected_at": moment,
                    "expiry": pd.Timestamp(expiry),
                    "strike": strike,
                    "option_type": option_type,
                    "underlying_price": 100.0,
                    "last_price": 2.0,
                    "bid": 1.9,
                    "ask": 2.1,
                    "volume": 100,
                    "open_interest": int(400 - abs(strike - 100) * 20),
                    "implied_volatility": 0.25,
                    "in_the_money": False,
                    "delta": None, "gamma": None, "theta": None, "vega": None,
                })
    chain = pd.DataFrame(rows)

    matrix = metrics.gex_matrix(chain, as_of=moment, expiries=[pd.Timestamp(e) for e in expiries])
    assert not matrix.empty and len(matrix.columns) == 2, matrix

    # The flip: the batched read and the function that builds its own matrix.
    from_matrix = metrics.gamma_flip_from_matrix(matrix)
    rebuilt = metrics.gamma_flip_price(chain, as_of=moment, expiries=[pd.Timestamp(e) for e in expiries])
    assert (from_matrix is None) == (rebuilt is None), (from_matrix, rebuilt)
    if from_matrix is not None:
        assert abs(from_matrix - rebuilt) < 1e-9, (from_matrix, rebuilt)

    # Per-expiry net GEX, read off the matrix against the path that priced the
    # chain again. Relative, because the two sum in different orders.
    read_off = metrics.net_gex_from_matrix(matrix, expiries=[pd.Timestamp(e) for e in expiries])
    priced_again = metrics.net_gex_by_expiry(
        chain, as_of=moment, expiries=[pd.Timestamp(e) for e in expiries]
    )
    assert list(read_off["expiry"]) == list(priced_again["expiry"]), (read_off, priced_again)
    assert np.allclose(
        read_off["net_gex"].to_numpy(dtype=float),
        priced_again["net_gex"].to_numpy(dtype=float),
        rtol=1e-6,
    ), (read_off.to_dict(), priced_again.to_dict())
    # An expiry asked for and absent from the matrix comes back as 0.0, which is
    # what the sidebar has always shown for it — not as a missing row.
    padded = metrics.net_gex_from_matrix(matrix, expiries=[*matrix.columns, pd.Timestamp("2027-01-15")])
    assert float(padded.iloc[-1]["net_gex"]) == 0.0, padded.to_dict()
    assert metrics.net_gex_from_matrix(pd.DataFrame(), expiries=["x"]).iloc[0]["net_gex"] == 0.0

    # Both numbers of the rollup, on one row per (moment, expiry).
    rollup = metrics.expiry_rollup(chain)
    assert list(rollup.columns) == ["collected_at", "expiry", "max_pain", "net_gex"], rollup.columns
    assert len(rollup) == 2, rollup.to_dict()
    assert np.allclose(
        rollup.sort_values("expiry")["net_gex"].to_numpy(dtype=float),
        priced_again.sort_values("expiry")["net_gex"].to_numpy(dtype=float),
        rtol=1e-6,
    ), rollup.to_dict()
    assert metrics.expiry_rollup(pd.DataFrame()).empty
    print("batched GEX checks passed (one matrix, the same numbers)")


def check_the_solver_stands_aside_where_it_should():
    """VIX, and a frame with no spot, keep the source's volatility — and say so.

    NOT AN EDGE CASE, A CORRECTNESS ONE. VIX options are written on VIX
    FUTURES, not on the index this product would price them against, so
    inverting a Black-Scholes price for them produces a confident number that
    means nothing. The futures curve is not something this product collects, so
    the honest answer is to have no volatility of our own for VIX rather than an
    invented one.

    THE TWO MARKER COLUMNS ARE PRESENT EITHER WAY, and that is the part worth
    pinning. A caller forced to write `if "iv_is_ours" in frame` before every
    use is a caller that will one day forget — and one did, in the sibling
    product, dying on VIX after twenty minutes of work.
    """
    chain = pd.DataFrame([{
        "collected_at": pd.Timestamp("2026-08-10 20:00"),
        "expiry": pd.Timestamp("2026-09-18"), "strike": 20.0, "option_type": "call",
        "underlying_price": 18.0, "last_price": 1.5, "bid": 1.4, "ask": 1.6,
        "volume": 50, "open_interest": 100, "implied_volatility": 0.85,
    }])

    refused = metrics.with_solved_iv(chain, metrics.DEFAULT_PRICING, ticker="VIX")
    assert not refused["iv_is_ours"].any(), refused.to_dict()
    assert float(refused.iloc[0]["implied_volatility"]) == 0.85, "the source's number stands"
    assert float(refused.iloc[0]["provider_implied_volatility"]) == 0.85

    # No spot to invert against — an imported database, or a provider that
    # serves chains without the underlying's price.
    spotless = metrics.with_solved_iv(chain.drop(columns=["underlying_price"]))
    assert not spotless["iv_is_ours"].any()
    assert "provider_implied_volatility" in spotless.columns

    # And where it does run, it runs: the same chain under its own ticker.
    solved = metrics.with_solved_iv(chain, metrics.DEFAULT_PRICING, ticker="VXX")
    assert bool(solved.iloc[0]["iv_is_ours"]), solved.to_dict()
    assert abs(float(solved.iloc[0]["implied_volatility"]) - 0.85) > 1e-6, \
        "the solved volatility is not the source's, or nothing was solved"

    # An empty frame is a ticker in its first minutes, not an error.
    assert metrics.with_solved_iv(pd.DataFrame()).empty
    print("IV solver checks passed (VIX and a missing spot keep the source's number)")


def main():
    # Start from an empty database, like the other two suites already do. This
    # one did not, and got away with it only because nothing it left behind
    # collided with itself — until a check inserted a snapshot at a moment a
    # leftover registry row already described, and the failure surfaced as a
    # cardinality violation inside application code that was working correctly.
    reset = db.get_connection()
    testdb.truncate_all(reset)
    reset.close()

    check_timezone_database_is_available()
    check_years_to_expiry()
    check_greeks_respond_to_time()
    check_put_call_ratio_matches_sql()
    check_collector_isolation()
    check_watchlist_and_snapshot_dates()
    check_screener_expiry_awareness()
    check_provider_registry()
    check_metrics_core_is_pristine()
    check_dividend_yield_defaults_to_the_old_model()
    check_iv_average_survives_a_contract_without_iv()
    check_pricing_inputs()
    check_unpriceable_contracts_are_skipped()
    check_contracts_backing_expiry()
    check_adjusted_contracts_do_not_break_collection()
    check_archiving_goes_through_the_registry()
    check_disk_estimate_respects_the_market_calendar()
    check_version_comes_from_the_changelog()
    check_hopeless_symbols_stop_being_asked_for()
    check_history_depth_is_known_before_the_chart()
    check_being_throttled_stops_the_whole_pass()
    check_suggestions_name_a_way_forward()
    check_rollup_fixture_does_not_depend_on_the_weekday()
    check_the_batched_gex_path_returns_the_old_numbers()
    check_the_solver_stands_aside_where_it_should()
    print("\nALL UNIT CHECKS PASSED")

if __name__ == "__main__":
    main()
