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


def check_contract_greeks_history_is_the_scalar_path_in_one_batch():
    """`contract_greeks_history` computes a contract's history in one batch
    instead of one row at a time; row for row it must say what the scalar functions it
    replaced said, on every kind of row a real history has: a live one, one
    with no volatility (NaN greeks, not zeros), one observed after the close
    of expiry (zeros), one with no spot, one whose provider greeks override
    ours, and one the IV guard blanks. Built here from the scalar kernels, so
    a drift in either direction fails with the greek and the row named."""
    from app import metrics
    from app import metrics_core as core

    expiry = pd.Timestamp("2026-10-16")
    curve = {0.08: 4.2, 0.5: 4.0, 1.0: 3.9, 2.0: 3.8}
    pricing = metrics.PricingInputs(curve=curve, dividend_yield=0.012)
    stamp = pd.Timestamp("2026-09-01 19:30")
    rows = []
    for i in range(12):
        rows.append({
            "collected_at": stamp + pd.Timedelta(days=i), "expiry": expiry, "strike": 100.0,
            "option_type": "put", "last_price": 3.0 + 0.05 * i, "underlying_price": 101.0 - 0.2 * i,
            "implied_volatility": 0.22 + 0.002 * i, "delta": None, "gamma": None, "theta": None, "vega": None,
        })
    rows[3]["implied_volatility"] = None                       # no volatility: NaN greeks
    rows[5]["underlying_price"] = None                         # no spot: NaN greeks
    rows[7].update({"delta": -0.44, "gamma": 0.03})            # the provider's delta and gamma win
    rows[9]["implied_volatility"] = 2.5                        # the guard's outlier: blanked
    rows.append({**rows[0], "collected_at": pd.Timestamp("2026-10-17 12:00")})  # after expiry: zeros
    frame = pd.DataFrame(rows)
    # A second contract in the frame, to prove the filter still selects one.
    frame = pd.concat([frame, frame.assign(strike=105.0)], ignore_index=True)

    history = metrics.contract_greeks_history(frame, 100.0, expiry, "put", pricing=pricing)
    assert len(history) == 13 and list(history["collected_at"]) == sorted(history["collected_at"])
    contract = frame[frame["strike"] == 100.0].sort_values("collected_at")
    guarded = core._mark_unreliable_iv(contract)
    assert pd.isna(guarded.loc[contract.index[9]]), "the fixture's outlier must trip the guard"
    for position, row in enumerate(contract.itertuples()):
        years = core.years_to_expiry(expiry, row.collected_at)
        expected = core._black_scholes_greeks(
            row.underlying_price, 100.0, years, guarded[row.Index], pricing.rate_for(years), "put",
            dividend_yield=pricing.dividend_yield,
        )
        for greek in core.PROVIDER_GREEKS:
            supplied = getattr(row, greek)
            if supplied is not None and not pd.isna(supplied):
                expected[greek] = float(supplied)
        got = history.iloc[position]
        for greek in core._GREEK_KEYS:
            same_gap = pd.isna(expected[greek]) and pd.isna(got[greek])
            assert same_gap or abs(expected[greek] - got[greek]) < 1e-12, (
                f"row {position} {greek}: scalar {expected[greek]} batch {got[greek]}"
            )
        assert (pd.isna(guarded[row.Index]) and pd.isna(got["implied_volatility"])) or (
            got["implied_volatility"] == guarded[row.Index]
        )
    assert all(pd.isna(history.iloc[3][list(core._GREEK_KEYS)])), "no volatility is NaN, not zero"
    assert history.iloc[7]["delta"] == -0.44 and history.iloc[7]["gamma"] == 0.03
    assert history.iloc[7]["vega"] != 0 and not pd.isna(history.iloc[7]["vega"]), "ours where the provider has none"
    assert (history.iloc[12][list(core._GREEK_KEYS)] == 0).all(), "past the close of expiry every greek is zero"
    assert history["delta"].dtype == float, history.dtypes
    print("Contract greeks history batch/scalar parity checks passed")


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
    """A refusal that only says no leaves the person where they were.

    FOUR KINDS OF WRONG, ONE ANSWER. `propose` is the single entry point, and
    the order it asks in is the order the reasons rule each other out: what the
    source refuses (a fact about us), what has no US-listed options at all (a
    fact about the instrument), a non-US listing of something we do have (a fact
    about form), and only then a near miss (a guess). Before it, three
    mechanisms answered in three voices and left holes between them — SAP.DE,
    BTCUSDT and 9988.HK got nothing at all while the help text under the box
    named ADRs as the answer.
    """
    from app import providers, suggestions

    refused = providers.get_provider().unsupported_symbols

    def propose(typed, known=(), catalogue=()):
        return suggestions.propose(
            typed, list(known),
            in_catalogue=set(catalogue).__contains__ if catalogue else None,
            unsupported=refused, provider="yahoo",
        )

    # 1. The source's own refusal, and it is the ONLY one that blocks: the rest
    # is a reading of what somebody meant, and being wrong about that must cost
    # them a sentence rather than the ability to try.
    spx = propose("SPX")
    assert spx.reason == "source" and spx.substitute == "SPY", spx
    assert spx.blocking, "only the source's refusal takes the button away"
    assert not propose("BTCUSD").blocking

    # 2. No US-listed options at all — and the pair as three terminals write it.
    # One form is stored and the spelling is folded before the lookup, so the
    # next spelling somebody pastes is not a new gap.
    for spelling in ("BTCUSD", "BTCUSDT", "BTC-USD", "BTC/USD"):
        answer = propose(spelling)
        assert answer.reason == "instrument" and answer.substitute == "IBIT", (spelling, answer)
        # Quoted back as it was typed: correcting somebody's spelling while
        # answering their question is two conversations at once.
        assert spelling in answer.message, answer.message
    assert propose("EURUSD").substitute is None, "no honest substitute is said, not invented"

    # 3. A non-US listing. The ordinary case needs no map — strip the suffix and
    # ask the directory — and the exceptions are there because stripping gets
    # them wrong: SAN.PA stripped is Banco Santander, and SAN.PA is Sanofi.
    sap = propose("SAP.DE", catalogue=["SAP"])
    assert sap.reason == "foreign" and sap.substitute == "SAP", sap
    assert propose("9988.HK", catalogue=["BABA"]).substitute == "BABA"
    assert propose("SAN.PA", catalogue=["SAN", "SNY"]).substitute == "SNY", \
        "stripping the suffix here names a different company entirely"
    # A dot is not enough to call something foreign: US symbols have them too.
    assert propose("BRK.B", catalogue=["BRK.B"]) is None
    # Foreign and nothing to offer is still a better answer than silence.
    unhelpable = propose("SIE.DE", catalogue=["SAP"])
    assert unhelpable.reason == "foreign" and unhelpable.substitute is None, unhelpable

    # 4. The guess, and it comes last for that reason.
    typo = propose("APPL", known=["AAPL", "SPY"])
    assert typo.reason == "typo" and typo.substitute == "AAPL", typo
    assert propose("NVIDIA", known=["NVDA", "SPY"]).substitute == "NVDA"

    # A SYMBOL IN THE DIRECTORY IS NEVER "A TYPO". Correcting AAPX to AAPL when
    # AAPX is a real security somebody deliberately typed would be the product
    # arguing with its own data.
    assert propose("AAPX", known=["AAPL"], catalogue=["AAPX", "AAPL"]) is None
    # Nor is anything said about a symbol already being collected.
    assert propose("AAPL", known=["AAPL"]) is None
    assert propose("") is None

    # The plain helpers still answer for callers that have no catalogue.
    assert suggestions.suggest("APPL", ["AAPL", "SPY"]) == "AAPL"
    assert suggestions.suggest("BTCUSD", []) == "IBIT"
    assert suggestions.suggest("EURUSD", []) is None
    assert suggestions.suggest("ZZZZ", ["AAPL", "SPY"]) is None
    assert "AAPL" in suggestions.refusal("APPL", ["AAPL"])
    assert "US options exchanges" in suggestions.refusal("EURUSD", [])
    assert "as it trades" in suggestions.refusal("ZZZZ", [])

    # THE DROPDOWN ALIASES ONLY POINT AT REAL SYMBOLS. Each is a row the browser
    # matches literally, so every spelling needs its own entry — and a row
    # offering something the directory does not hold would be the box promising
    # what the Add button then refuses.
    aliases = suggestions.dropdown_aliases()
    assert aliases["BTCUSDT"] == "IBIT" and aliases["BTC/USD"] == "IBIT", aliases
    assert aliases["9988.HK"] == "BABA"
    assert "EURUSD" not in aliases, "an alias with nothing to point at is not a row"
    print("suggestion checks passed (one entry point, four kinds of wrong)")

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


def check_the_collector_knows_about_holidays():
    """Thanksgiving is not a trading day, and the calendar is what says so.

    WHY THIS IS NOT A SMALL THING HERE. The clock path used to carry the gap in
    writing — "on Thanksgiving it says open" — and the reason it was acceptable
    was that a provider with a status endpoint answered first and never reached
    it. This product ships one source, Yahoo, and Yahoo has no such endpoint:
    the path with the gap in it was the only path. The cost of a wrong answer
    is a full collection pass against a source that limits requests, plus one
    more "trading day" in the history that OI Delta and the Unusual Activity
    baseline stand on.

    HALF-DAYS ARE THE HALF A HAND-WRITTEN LIST WOULD HAVE GOT WRONG. The day
    after Thanksgiving is a session, and it closes at 13:00 New York.

    THE LAST BLOCK IS THE ROLLBACK. With the package hidden, the same call
    returns "open" for the same instant — so this check is verifying the
    calendar rather than the calendar's absence, and the documented degradation
    is exercised rather than asserted about.
    """
    from app import market_calendar as mc

    # 2026: Thanksgiving falls on 26 November, the half-day on the 27th.
    # 15:00 UTC is 10:00 in New York, inside any regular session.
    assert mc.state_from_clock(dt.datetime(2026, 11, 26, 15, 0)) == mc.CLOSED
    assert mc.state_from_clock(dt.datetime(2026, 11, 18, 15, 0)) == mc.OPEN
    # The half-day: open at 12:00 New York, shut at 13:30.
    assert mc.state_from_clock(dt.datetime(2026, 11, 27, 17, 0)) == mc.OPEN
    assert mc.state_from_clock(dt.datetime(2026, 11, 27, 18, 30)) == mc.CLOSED

    # What the reader is shown, on the evening before a holiday: the next open
    # is Friday morning, not Thursday morning. Seventeen hours against forty.
    hours = mc.seconds_until_open(dt.datetime(2026, 11, 25, 22, 0)) / 3600
    assert 40 < hours < 42, hours
    assert "1d" in mc.time_to_open_phrase(dt.datetime(2026, 11, 25, 22, 0))

    calendar, package = mc._XNYS, mc._xcals
    try:
        mc._XNYS, mc._xcals = None, None
        assert mc.state_from_clock(dt.datetime(2026, 11, 26, 15, 0)) == mc.OPEN, \
            "without the package this must degrade to weekends and hours, not fail"
        assert mc.seconds_until_open(dt.datetime(2026, 11, 25, 22, 0)) / 3600 < 20
    finally:
        mc._XNYS, mc._xcals = calendar, package
    print("market-calendar checks passed (holidays, half-days, and the fallback)")


def check_symbols_the_source_will_not_serve_are_explained():
    """SPX is real, and the message about it has to say something true.

    THE FAILURE THIS REPLACES. Yahoo serves no chain and no spot for a
    cash-settled index, so a watchlist containing SPX collected nothing and the
    log said "either the symbol does not exist, or the source is limiting
    requests". The first half is false and the person who typed SPX knows it is
    false — they trade it — and a message somebody knows to be wrong takes the
    credibility of every other message with it.

    THREE PLACES, ONE MAP, and the map belongs to the provider rather than to
    this module: a licensed feed serves SPX perfectly well, so which symbols are
    refused is a fact about the source. The three are the refusal when adding,
    the banner on the ticker's page, and the filter in the collector — checked
    here through the collector, which is the one with a consequence: every
    cycle would otherwise spend a request per refused symbol against a source
    that limits requests, and log a failure against a symbol that is fine.
    """
    from app import collector, providers, suggestions

    provider = providers.get_provider()
    refused = provider.unsupported_symbols
    assert refused["SPX"] == "SPY" and refused["NDX"] == "QQQ", refused
    # VIX maps to nothing on purpose: an ETF on VIX futures is a different
    # instrument, and offering it would be worse than offering nothing.
    assert refused["VIX"] is None, refused

    message = suggestions.source_refusal("SPX", refused["SPX"], provider.name)
    assert "real symbol with listed options" in message, message
    assert "SPY" in message, message
    # The claim that was false, and the reason this exists.
    assert "no options" not in message, message
    no_substitute = suggestions.source_refusal("VIX", None, provider.name)
    assert "no honest substitute" in no_substitute, no_substitute

    # The other half of the same map: what these instruments are called at
    # Yahoo, where the index QUOTE exists even though the chain does not. Used
    # by the price-history lookup behind realized volatility, and by the spot
    # lookup — one translation, in one place, because a translation applied in
    # one of two places is the same bug twice.
    from app.providers import yahoo as yahoo_provider
    assert yahoo_provider._yahoo_symbol("SPX") == "^GSPC"
    assert yahoo_provider._yahoo_symbol("spx") == "^GSPC", "the caller's case is not its problem"
    assert yahoo_provider._yahoo_symbol("AAPL") == "AAPL", "anything else is passed through"

    # A provider written before this attribute existed refuses nothing, rather
    # than crashing every caller that asks.
    class OldProvider:
        name = "old"
    assert getattr(OldProvider(), "unsupported_symbols", {}) == {}

    # The collector skips them without a request and without a run-log entry.
    # THE FAKE PROVIDER RAISES: if the filter ever stops working, this check
    # fails with the request that should never have been made, rather than
    # quietly passing on a mocked success.
    class RefusingProvider:
        name = "refusing"
        unsupported_symbols = {"SPX": "SPY"}

        def fetch_ticker_snapshot(self, ticker):
            raise AssertionError(f"asked the source for {ticker}, which it refuses")

    conn = db.get_connection()
    try:
        before = len(db.get_recent_runs(conn, limit=200))
        outcome = collector.collect_watchlist(conn, ["SPX"], provider=RefusingProvider())
        assert outcome["SPX"].startswith("skipped: "), outcome
        assert "SPY" in outcome["SPX"], outcome
        assert len(db.get_recent_runs(conn, limit=200)) == before, \
            "a decision not to attempt is not an attempt, and does not belong in the log"
    finally:
        conn.close()
    print("unsupported-symbol checks passed (three places, one map, no requests)")


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
    from_matrix = metrics.gamma_flip_from_matrix(matrix, 100.0)
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


def check_the_flip_is_the_crossing_by_the_money():
    """The flip must be the zero crossing nearest the underlying price.

    The profile is walked over the WHOLE strike range so
    the flip does not move when the display band moves, and that range reaches
    far below the traded part of the chain, where the cumulative sum is still
    hovering around nothing. Taking the first crossing therefore reported the
    deepest one: AAPL on prod showed 137.24 against an underlying of 319.70
    while the visible matrix changed sign between 305 and 307.5. Measured on the
    local base afterwards, 161 of 200 snapshots had more than one crossing.

    The profile below is built to tell the three candidate rules apart rather
    than merely to have a flip: it crosses zero three times, once far below the
    money on values of ±1 (the deep-tail wobble), once just under it, and once
    far above. "First" answers 135.0, "last" answers 608.57, and only "nearest
    to the spot" answers 304.0. Own data throughout — a matrix this test builds
    itself, no chain, no database, no reliance on any other check having run.
    """
    expiry = pd.Timestamp("2026-09-18")
    spot = 320.0

    def matrix_with(cumulative: dict[float, float]) -> pd.DataFrame:
        """A one-column matrix whose cumulative profile is exactly `cumulative`.

        Written the other way round on purpose: the argument is the shape the
        function under test walks over, so the assertions below can be read
        against it without anyone summing five numbers in their head.
        """
        strikes = sorted(cumulative)
        running = 0.0
        per_strike = []
        for strike in strikes:
            per_strike.append(cumulative[strike] - running)
            running = cumulative[strike]
        return pd.DataFrame({expiry: per_strike}, index=pd.Index(strikes, name="strike"))

    three = matrix_with({130.0: 1.0, 140.0: -1.0, 300.0: -8000.0,
                         310.0: 12000.0, 600.0: 3000.0, 610.0: -500.0})
    got = metrics.gamma_flip_from_matrix(three, spot)
    assert got is not None and abs(got - 304.0) < 1e-9, (
        f"the flip against a spot of {spot} came back {got}, not the crossing at "
        f"304.0 next to the money. 135.0 means the lowest crossing is still being "
        f"taken (the AAPL 137.24 defect); 608.57 means the highest one is"
    )

    # A STRIKE CARRYING NOTHING IS NOT A CROSSING. `np.sign` maps an untouched
    # strike to 0, so a 0 → -1 step reads as a change of sign and the profile
    # was reported as crossing zero at the point where it merely left it. This
    # is SPX, which reported 2,800 against a spot of 7,711.76 — and nearest-to-
    # the-money does not fix it on its own, because that artefact was the only
    # candidate in the whole profile.
    below_the_chain = matrix_with({50.0: 0.0, 60.0: 0.0, 300.0: -8000.0, 310.0: 12000.0})
    got = metrics.gamma_flip_from_matrix(below_the_chain, spot)
    assert got is not None and abs(got - 304.0) < 1e-9, (
        f"a run of strikes with no exposure at all produced {got} instead of 304.0 — "
        f"60.0 means leaving zero is still counted as crossing it"
    )

    # Same artefact with nothing real after it: the profile never changes sign,
    # and saying so is the honest answer. Reporting 60.0 here is how SPX got a
    # flip 4,900 points below the money.
    never_crosses = matrix_with({50.0: 0.0, 60.0: 0.0, 300.0: -8000.0, 310.0: -12000.0})
    assert metrics.gamma_flip_from_matrix(never_crosses, spot) is None, (
        "a profile that only ever leaves zero and stays negative has no flip level, "
        f"but one was reported: {metrics.gamma_flip_from_matrix(never_crosses, spot)}"
    )

    # A profile that passes exactly THROUGH zero on its way over still crosses:
    # the two strictly signed values on either side of the flat part bracket it.
    through_zero = matrix_with({300.0: -8000.0, 305.0: 0.0, 310.0: 12000.0})
    got = metrics.gamma_flip_from_matrix(through_zero, spot)
    assert got is not None and abs(got - 304.0) < 1e-9, (
        f"a profile touching 0.0 between two opposite signs lost its crossing: {got}"
    )

    # Without a reference price there is no "nearest", and every distance would
    # be NaN — which compares false and quietly leaves the lowest crossing.
    assert metrics.gamma_flip_from_matrix(three, float("nan")) is None, (
        "a NaN underlying price must not select a crossing"
    )

    # And the two entries stay one function: the chain-side one reads the spot
    # off the snapshot rather than being told it.
    moment = dt.datetime(2026, 8, 20, 15, 0)
    chain = pd.DataFrame([
        {"collected_at": moment, "expiry": expiry, "strike": strike,
         "option_type": option_type, "underlying_price": 100.0,
         "implied_volatility": 0.25,
         "open_interest": 300 if option_type == "call" else 900,
         "volume": 5}
        for strike in (80.0, 90.0, 100.0, 110.0, 120.0)
        for option_type in ("call", "put")
    ])
    pricing = metrics.PricingInputs()
    by_hand = metrics.gamma_flip_from_matrix(
        metrics.gex_matrix(chain, pricing=pricing), 100.0
    )
    by_chain = metrics.gamma_flip_price(chain, pricing=pricing)
    assert (by_hand is None) == (by_chain is None) and (
        by_hand is None or abs(by_hand - by_chain) < 1e-9
    ), f"the chain-side entry stopped reading the spot off the snapshot: {by_chain} vs {by_hand}"

    print("Gamma flip picks the crossing by the money (3 crossings, deep wobble ignored)")


def check_the_strike_count_actually_decides_what_is_drawn():
    """Asking for more strikes has to return more strikes.

    THE DEFECT THIS IS WRITTEN FOR SHIPPED TO PRODUCTION AND WAS REPORTED FROM
    IT. The control asked for a percentage band, the band was computed
    correctly, and then the page trimmed it to a 45-row window around the money
    — so on any symbol with more than 45 strikes inside the narrowest band the
    slider did nothing at all across its whole range. Measured on live data: SPY
    has 77 strikes within ±5% of 769.35, SPX has 154 within ±5% of 7711.76.
    Both are among the most-watched symbols there are, which is why it was
    reported as "the slider is broken" rather than as an edge case.

    So the assertion is not "the filter filters" — that always worked — but that
    the row count RESPONDS. Reintroducing the trim makes the first assertion
    fail with the production symptom: the same number at every position.

    THE MONEY IS ALWAYS IN THE MIDDLE, and that is the property the percentage
    version could not hold: n rows either side is the same request on every
    chain, while ±5% is 8 strikes on MO and 154 on SPX.
    """
    from app.viewtime import strikes_around_money

    # 600 strikes at $1 around 500, so even 100 each side stays inside the grid
    # and the counts can be required to keep rising. The index runs high to low,
    # as the heatmap draws it.
    spot = 500.0
    matrix = pd.DataFrame(
        {"2026-09-18": range(600)},
        index=[float(strike) for strike in range(799, 199, -1)],
    )

    counts = {n: len(strikes_around_money(matrix, spot, n)) for n in (5, 10, 25, 100)}
    assert counts[5] < counts[10] < counts[25] < counts[100], (
        f"the control does not change what is drawn: {counts} — this is the defect "
        "where a 45-row window silently overrode it"
    )
    # n each side means 2n+1 rows: n above, n below, and the money itself.
    assert counts == {5: 11, 10: 21, 25: 51, 100: 201}, counts
    assert counts[10] > 0 and counts[25] > 45, (
        "the fixture is too small to catch the defect it exists for"
    )

    ten = strikes_around_money(matrix, spot, 10)
    assert 500.0 in ten.index, "the money must always be drawn"
    assert list(ten.index).index(500.0) == 10, (
        f"the money is not in the middle: position {list(ten.index).index(500.0)} of {len(ten)}"
    )
    assert ten.index.max() == 510.0 and ten.index.min() == 490.0, (ten.index.max(), ten.index.min())

    # Clamped at the ends rather than backfilled from the other side: a chain
    # with three strikes above the money shows three, not ten borrowed from
    # below.
    near_top = strikes_around_money(matrix, 797.0, 10)
    assert len(near_top) == 13, len(near_top)
    assert near_top.index.max() == 799.0

    # A chain shorter than the request keeps working — the case that looked
    # perfectly fine for the whole time the defect was live, because fewer than
    # 45 strikes never met the window.
    sparse = pd.DataFrame({"2026-09-18": range(5)}, index=[110.0, 105.0, 100.0, 95.0, 90.0])
    assert len(strikes_around_money(sparse, 100.0, 10)) == 5
    assert list(strikes_around_money(sparse, 100.0, 1).index) == [105.0, 100.0, 95.0]
    assert strikes_around_money(pd.DataFrame(), 100.0, 10).empty
    print(f"Strike-count checks passed (5/10/25/100 each side -> {list(counts.values())} rows)")


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


def check_the_directory_parse_survives_the_real_header():
    """The header is what breaks this, and the failure is silent.

    Cboe's file starts `Company Name, Stock Symbol, DPM Name, Post/Station` —
    with a space after each comma, so the raw field names carry a leading
    blank. Read without stripping, the file parses cleanly into ZERO symbols:
    nothing raises, the catalogue empties, and the search box goes quiet with
    no error anywhere. That is why the parse is a separate function from the
    download, and why it is checked against the real header rather than a tidy
    one.
    """
    from app import catalogue

    payload = (
        "Company Name, Stock Symbol, DPM Name, Post/Station\n"
        "APPLE INC, AAPL, Citadel Securities, 5/1\n"
        "STATE STR SPDR S&P 500 ETF TR TR UNIT, SPY, Citadel Securities, 6/2\n"
    )
    rows = catalogue.parse_directory(payload)
    assert rows == [("AAPL", "APPLE INC"), ("SPY", "STATE STR SPDR S&P 500 ETF TR TR UNIT")], rows

    # A row the file truncates is skipped, not guessed at.
    assert catalogue.parse_directory(
        "Company Name, Stock Symbol\nAPPLE INC, AAPL\nBROKEN\n"
    ) == [("AAPL", "APPLE INC")]

    # EVERY FAILURE RAISES, because the caller's answer to a raise is "keep the
    # catalogue we already have". A parse that returned an empty list instead
    # would be indistinguishable from a market with no listed options.
    for broken, why in (
        ("", "no header at all"),
        ("Company Name, Ticker\nAPPLE INC, AAPL\n", "the symbol column renamed"),
        ("Company Name, Stock Symbol\n", "a header and nothing under it"),
    ):
        try:
            catalogue.parse_directory(broken)
        except ValueError:
            continue
        raise AssertionError(f"parsed {why!r} without complaining")
    print("directory parse checks passed (the real header, and every way it fails)")


def check_a_stopped_collection_does_not_look_healthy():
    """Four states, and the two that are easy to confuse kept apart.

    THE CASE THAT MADE THIS WORTH WRITING is the quiet one: collection stops
    and nothing on screen changes. Every chart still draws, every number still
    reads as today's, and the only clue is a timestamp in a caption on one of
    eight tabs. Suspension does not help here — it only ever fires for symbols
    that have NEVER collected, so the ticker that worked for a month and then
    stopped is precisely the one nothing was watching.

    THE REGRESSION IN THE MIDDLE BLOCK is worth naming because the sibling
    product shipped it and lost a night to it. "The market was shut for the
    whole gap" cannot be written as `traded == 0`: the last cycle before the
    bell lands wherever the interval falls, commonly minutes before it, and
    those minutes stay in the total forever. Written that way, a screen sat
    green and silent all night. It has to ask whether the market is shut NOW.

    Dates are fixed and in the past, and every instant is explicit — the states
    this is about are weekends, holidays and outages, none of which can be
    waited for, and none of which may depend on the day the suite happens to
    run.
    """
    from app import freshness

    # Friday 11 September 2026. 19:54 UTC is 15:54 New York — six minutes
    # before the close, which is where an ordinary 15-minute cycle lands.
    last = dt.datetime(2026, 9, 11, 19, 54, tzinfo=dt.timezone.utc)

    # Ten minutes later, mid-session: nothing to say at all.
    assert freshness.assess(last, 0, 15, now=last + dt.timedelta(minutes=10)) is None

    # THE REGRESSION. Two, six and fourteen hours after that collection the
    # market is shut, the gap is real, and the product must say so calmly —
    # every time, not only when the snapshot landed exactly at the bell.
    for hours in (2, 6, 14):
        answer = freshness.assess(last, 0, 15, now=last + dt.timedelta(hours=hours))
        assert answer is not None, f"{hours}h after the close said nothing"
        state, note = answer
        assert state == freshness.RESTING, f"{hours}h after the close reported {state}"
        assert "Market closed" in note

    # Sunday evening is still resting: two days later the market has not been
    # open for a single second in between.
    state, _ = freshness.assess(last, 0, 15, now=dt.datetime(2026, 9, 13, 22, 0, tzinfo=dt.timezone.utc))
    assert state == freshness.RESTING

    # Monday lunchtime with nothing collected since Friday is NOT resting: the
    # market has been open for hours and nothing arrived.
    monday = dt.datetime(2026, 9, 14, 16, 0, tzinfo=dt.timezone.utc)  # 12:00 New York
    state, note = freshness.assess(last, 0, 15, now=monday)
    assert state == freshness.STALE, state
    assert "not the market being closed" in note
    # And it counts open hours, not elapsed ones: from 15:54 Friday that is the
    # six minutes left of Friday plus 2.5 hours of Monday.
    traded = freshness.open_seconds_between(last, monday)
    assert 2.5 * 3600 < traded < 2.75 * 3600, traded

    # A holiday does not make data stale. Thanksgiving 2026 is 26 November;
    # collected the evening before, read on the holiday itself.
    before_holiday = dt.datetime(2026, 11, 25, 20, 55, tzinfo=dt.timezone.utc)
    holiday_noon = dt.datetime(2026, 11, 26, 17, 0, tzinfo=dt.timezone.utc)
    # 15:55 New York: the five minutes left of that session, and nothing at all
    # from the holiday itself.
    assert freshness.open_seconds_between(before_holiday, holiday_noon) == 5 * 60
    state, _ = freshness.assess(before_holiday, 0, 15, now=holiday_noon)
    assert state == freshness.RESTING, state

    # Refusals outrank age and are named while the data is still fresh.
    state, note = freshness.assess(last, 3, 15, now=last + dt.timedelta(minutes=10))
    assert state == freshness.FAILING, state
    assert "3" in note and "failing" in note.lower()

    # One or two failures are not a story; a symbol that has never collected
    # and is not being refused either has nothing to report yet.
    assert freshness.assess(last, 2, 15, now=last + dt.timedelta(minutes=10)) is None
    assert freshness.assess(None, 1, 15, now=monday) is None
    state, note = freshness.assess(None, 4, 15, now=monday)
    assert state == freshness.FAILING and "Nothing has been collected" in note

    # Durations are read at a glance, never more than two units.
    assert freshness.age_phrase(dt.timedelta(minutes=7)) == "7m"
    assert freshness.age_phrase(dt.timedelta(hours=3, minutes=4, seconds=59)) == "3h 4m"
    assert freshness.age_phrase(dt.timedelta(days=2, hours=4, minutes=30)) == "2d 4h"
    assert freshness.age_phrase(dt.timedelta(days=2)) == "2d"

    print("freshness checks passed (resting, stale, failing, and the bell-minute trap)")


def check_the_run_log_answers_how_collection_is_going():
    """Last success and failures SINCE it, read in one query and per source.

    TWO QUERIES WOULD LIE. Counting failures separately counts the ones that
    happened before the success, so a symbol that recovered an hour ago would
    be reported as failing until the log was trimmed — which it never is.

    PER SOURCE, because the screen is per source. A ticker one provider serves
    and another refuses is healthy on the screen drawing the provider that
    serves it, and the reader cannot act on a warning about the other one.
    """
    conn = db.get_connection()
    ticker = "FRESHCHK"
    # Naive UTC, which is what the collector writes and what the column holds —
    # asserting on aware values here would be testing a convention this product
    # does not use.
    base = dt.datetime(2026, 9, 11, 14, 0)

    # Nothing logged at all: no success, no failures.
    assert db.collection_state(conn, ticker) == (None, 0)

    def run(status, minutes, source="yahoo"):
        at = base + dt.timedelta(minutes=minutes)
        db.log_run(conn, at, at + dt.timedelta(seconds=30), ticker, status, source=source)

    run("failed", 0)
    run("failed", 15)
    run("success", 30)
    run("failed", 45)
    run("failed", 60)

    last, failures = db.collection_state(conn, ticker)
    assert last == base + dt.timedelta(minutes=30), last
    assert failures == 2, failures  # the two before the success do not count

    # A second source with its own story does not leak into the first.
    run("success", 75, source="other")
    last_yahoo, failures_yahoo = db.collection_state(conn, ticker, source="yahoo")
    assert last_yahoo == base + dt.timedelta(minutes=30)
    assert failures_yahoo == 2
    last_other, failures_other = db.collection_state(conn, ticker, source="other")
    assert last_other == base + dt.timedelta(minutes=75)
    assert failures_other == 0

    conn.execute("DELETE FROM collection_runs WHERE ticker = %s", (ticker,))
    conn.commit()
    conn.close()
    print("collection-state checks passed (one query, failures since the last success, per source)")


def check_realized_volatility_is_stored_once_a_day_and_read_back():
    """The underlying's own volatility is a row, not a download on a page load.

    WHAT THIS IS GUARDING. The figures beside a contract's implied volatility
    come from six months of daily closes, and they used to be fetched from the
    source while the Contract tab was being drawn — behind a thirty-minute
    cache, which is not a fix but a way of choosing who waits. Now the
    collection pass stores them and the screen reads them, so the assertions
    that matter are: one request per ticker per day and not one more, the read
    gives back exactly what was written, and a source that fails costs a
    caption rather than the collection somebody asked for.

    The fetch is injected, so none of this touches the network.
    """
    from app import collector

    conn = db.get_connection()
    ticker = "RVCHECK"
    day = dt.date(2026, 9, 18)
    calls = []

    def fake_fetch(symbol, *args, **kwargs):
        calls.append(symbol)
        # 120 closes that actually move, so all three windows have enough data.
        rng = np.random.default_rng(7)
        steps = rng.normal(0, 0.01, 120)
        return pd.DataFrame({"close": 100 * np.exp(np.cumsum(steps))})

    written = collector.refresh_realized_volatility(conn, [ticker], today=day, fetch=fake_fetch)
    assert written == 3, written               # one row per window
    assert calls == [ticker], calls

    # The second pass of the same day asks the source for nothing at all. This
    # is the whole guard: at a fifteen-minute interval it is the difference
    # between one request and ninety-six identical ones.
    again = collector.refresh_realized_volatility(conn, [ticker], today=day, fetch=fake_fetch)
    assert again == 0, again
    assert calls == [ticker], calls

    as_of, values, source = db.get_realized_volatility(conn, ticker)
    assert as_of == day, as_of
    assert sorted(values) == [10, 20, 30], values
    assert all(0 < v < 2 for v in values.values()), values   # annualised fractions
    assert source

    # A later day supersedes it, and the older row stays on the record.
    collector.refresh_realized_volatility(
        conn, [ticker], today=day + dt.timedelta(days=1), fetch=fake_fetch
    )
    as_of, _, _ = db.get_realized_volatility(conn, ticker)
    assert as_of == day + dt.timedelta(days=1), as_of
    kept = conn.execute(
        "SELECT count(DISTINCT as_of_date) FROM realized_volatility WHERE ticker = %s",
        (ticker,),
    ).fetchone()[0]
    assert kept == 2, kept

    # A source that raises costs a caption, not the pass: nothing is written
    # for the new ticker and nothing propagates out.
    def angry_fetch(symbol, *args, **kwargs):
        raise OSError("the price source is unreachable")

    assert collector.refresh_realized_volatility(
        conn, ["RVANGRY"], today=day, fetch=angry_fetch
    ) == 0
    assert db.get_realized_volatility(conn, "RVANGRY") is None

    # And a symbol whose history is too short to fill any window stores nothing
    # rather than a confident zero.
    assert collector.refresh_realized_volatility(
        conn, ["RVSHORT"], today=day,
        fetch=lambda symbol, *a, **k: pd.DataFrame({"close": [100.0, 101.0, 100.5]}),
    ) == 0
    assert db.get_realized_volatility(conn, "RVSHORT") is None

    conn.execute("DELETE FROM realized_volatility WHERE ticker IN ('RVCHECK','RVANGRY','RVSHORT')")
    conn.commit()
    conn.close()
    print("realized-volatility checks passed (one fetch a day, read back, failures are captions)")


def _future_friday(days_ahead: int = 21) -> dt.date:
    """A Friday at least `days_ahead` away, computed rather than written down.

    A literal expiry in a fixture is a date that arrives: it passes, the
    contract it describes becomes worthless, and checks that were green for
    months turn red on a Tuesday evening with no code having changed. The
    fixture asks for "a normal monthly expiry from here", which is what it
    actually means.
    """
    day = dt.date.today() + dt.timedelta(days=days_ahead)
    return day + dt.timedelta(days=(4 - day.weekday()) % 7)


def _day_chain(moment: dt.datetime, spot: float, expiry: dt.date, oi_scale: float = 1.0):
    """A small but honest chain: five strikes, calls and puts, priced so the
    solver has something to solve and the GEX profile has a shape."""
    rows = []
    for strike in (spot - 10, spot - 5, spot, spot + 5, spot + 10):
        for option_type in ("call", "put"):
            intrinsic = max(0.0, (spot - strike) if option_type == "call" else (strike - spot))
            rows.append({
                "collected_at": moment,
                "expiry": pd.Timestamp(expiry),
                "strike": float(strike),
                "option_type": option_type,
                "underlying_price": spot,
                "last_price": intrinsic + 2.0,
                "bid": intrinsic + 1.9,
                "ask": intrinsic + 2.1,
                "volume": int(100 * oi_scale),
                "open_interest": int(1000 * oi_scale) + int(strike) % 7,
                "implied_volatility": 0.25,
                "delta": None, "gamma": None, "theta": None, "vega": None, "rho": None,
                "in_the_money": strike < spot if option_type == "call" else strike > spot,
            })
    return pd.DataFrame(rows)


def check_the_day_row_and_the_ladder():
    """A day row from a chain, and what two of them say happened.

    WHAT THIS IS REALLY CHECKING. The view built on this exists to answer
    "what changed since yesterday", and the ways that answer can be wrong are
    all in this function rather than in the drawing: a level compared against a
    different expiry, a first day dressed up as a screenful of events, a regime
    that moved reported in the same ink as one that did not.
    """
    from app import day_summary, weather

    expiry = _future_friday()
    monday = dt.datetime(2026, 9, 14, 19, 45)
    tuesday = dt.datetime(2026, 9, 15, 19, 45)

    before_chain = _day_chain(monday, 100.0, expiry)
    after_chain = _day_chain(tuesday, 102.0, expiry, oi_scale=1.2)
    before = day_summary.summarize(
        before_chain, metrics.DEFAULT_PRICING, metrics.gamma_weather(before_chain)
    )
    after = day_summary.summarize(
        after_chain, metrics.DEFAULT_PRICING, metrics.gamma_weather(after_chain)
    )
    assert before is not None and after is not None

    # The day is New York's date of the collection, not UTC's: 19:45 UTC is
    # 15:45 in New York, the same day — and the rule has to be the one the rest
    # of the product uses, or two views disagree about "yesterday".
    assert after["day"] == dt.date(2026, 9, 15), after["day"]
    assert after["state"] in dict(weather.WEATHER_WORDS)
    assert after["contracts"] == len(after_chain)
    assert after["expiries"] == [expiry.strftime("%Y-%m-%d")]
    assert after["gex_by_strike"], "the profile the overlay chart draws must be in the row"
    # The flow by side is stored with the day, because nothing else stores it.
    assert after["call_volume"] == after["put_volume"] == 5 * int(100 * 1.2)
    assert after["call_oi"] > before["call_oi"], "open interest grew between the two days"

    # A chain with no expiry worth reporting on has no max pain and no expected
    # move, and says so with None rather than with a confident number.
    thin = _day_chain(tuesday, 102.0, expiry).head(4)
    thin_row = day_summary.summarize(thin, metrics.DEFAULT_PRICING, metrics.gamma_weather(thin))
    if thin_row is not None:
        assert thin_row["nearest_max_pain"] is None

    rollup_before = {"iv_weighted_avg": 0.24, "call_volume": 500, "put_volume": 500,
                     "call_oi": 5000, "put_oi": 5100}
    rollup_after = {"iv_weighted_avg": 0.29, "call_volume": 600, "put_volume": 900,
                    "call_oi": 6000, "put_oi": 5100}
    ladder = day_summary.changes(before, after, rollup_before, rollup_after)
    by_key = {line["key"]: line for line in ladder}

    assert by_key["price"]["kind"] == day_summary.MOVED
    assert abs(by_key["price"]["delta"] - 2.0) < 1e-9
    assert by_key["put_oi"]["kind"] == day_summary.SAME, "unchanged open interest is not a move"
    assert by_key["iv"]["kind"] == day_summary.MOVED
    assert day_summary.format_delta(by_key["iv"]) == "+5.0 pts"
    assert day_summary.format_delta(by_key["call_oi"]) == "+20.0%"
    # A CHANGE TOO SMALL TO PRINT IS NOT A CHANGE. A ratio that moved in the
    # fourth decimal used to read "+0.00" next to two identical numbers, which
    # asks the reader to find a difference that is not there.
    invisible = day_summary._number_change("pcr_oi", "Put/Call", 0.4612, 0.4613, unit="ratio")
    assert invisible["kind"] == day_summary.MOVED
    assert day_summary.format_delta(invisible) == "unchanged"
    assert by_key["pcr_volume"]["kind"] == day_summary.MOVED

    # Every line has a kind from the closed set — the view colours by it, and
    # an unexpected value would simply not be drawn.
    assert all(line["kind"] in day_summary.KINDS for line in ladder)

    # THE FIRST DAY ON RECORD IS NOT A SCREENFUL OF EVENTS. Nothing to compare
    # means nothing is claimed, and the headline says which case this is.
    first = day_summary.changes(None, after, None, rollup_after)
    assert {line["kind"] for line in first} == {day_summary.UNKNOWN}
    assert "First day" in day_summary.headline(first)
    assert all(day_summary.format_delta(line) == "—" for line in first)

    # MAX PAIN IS COMPARED FOR ONE EXPIRY. When the nearest expiry rolls over,
    # the line names the old one instead of reporting a level that never moved.
    rolled = dict(after)
    rolled["nearest_expiry"] = expiry + dt.timedelta(days=7)
    ladder_rolled = day_summary.changes(before, rolled, rollup_before, rollup_after)
    pain = {line["key"]: line for line in ladder_rolled}["max_pain"]
    assert pain.get("note", "").startswith("nearest expiry was")
    # Given yesterday's number FOR TODAY'S expiry, it is a comparison again.
    ladder_same = day_summary.changes(
        before, rolled, rollup_before, rollup_after, previous_same_expiry_pain=99.0
    )
    pain_same = {line["key"]: line for line in ladder_same}["max_pain"]
    assert pain_same["before"] == 99.0 and pain_same["kind"] in (day_summary.MOVED, day_summary.SAME)

    # A REGIME THAT MOVED IS THE HEADLINE; A LEVEL THAT MOVED IS NOT.
    stormy = dict(after)
    stormy["state"] = metrics.WEATHER_STORM
    calm = dict(before)
    calm["state"] = metrics.WEATHER_CLEAR
    turned = day_summary.changes(calm, stormy, rollup_before, rollup_after)
    state_line = {line["key"]: line for line in turned}["state"]
    assert state_line["kind"] == day_summary.AMPLIFYING
    assert day_summary.headline(turned).startswith("Gamma weather turned")
    assert day_summary.format_delta(state_line) == "amplifying"
    # And the other way round, which is the green one.
    back = day_summary.changes(stormy, calm, rollup_before, rollup_after)
    assert {line["key"]: line for line in back}["state"]["kind"] == day_summary.DAMPING

    # Expiries are named rather than counted: "31 -> 30" says less than which
    # one expired.
    rolled_off = dict(after)
    rolled_off["expiries"] = []
    shape = {line["key"]: line for line in day_summary.changes(
        before, rolled_off, rollup_before, rollup_after)}["expiries"]
    assert shape["rolled_off"] == before["expiries"]

    # Levels move in strikes, because that is the grid they live on.
    step_row = {"gex_by_strike": [[95.0, 1.0], [100.0, 2.0], [105.0, 1.0]]}
    assert day_summary.strike_step(step_row) == 5.0
    moved_wall = day_summary._number_change(
        "call_wall", "Call wall", 100.0, 105.0, unit="strikes", step=5.0
    )
    assert day_summary.format_delta(moved_wall) == "+1 strike · +5.00"
    assert day_summary.format_value(moved_wall, "before") == "100.00"
    # Net GEX takes the unit its own size asks for.
    assert weather.format_gex(-671_670_000) == "-671.67M"
    assert weather.format_gex(2_100_000_000) == "+2.10B"

    print("day-row and ladder checks passed (the day rule, one expiry, the first day, the regime)")


def check_day_summaries_are_written_and_filled_in():
    """The rows reach the database, and the gaps fill themselves in.

    THE GAP IS THE POINT. These machines are switched off overnight, and an
    upgrade brings the table in empty — a view about what changed, with days
    missing, is indistinguishable from nothing having changed. So the backfill
    is checked for what it does the second time as much as the first: nothing.
    """
    from app import day_summary

    conn = db.get_connection()
    ticker = "DAYCHK"
    source = "yahoo"
    expiry = _future_friday()
    moments = [dt.datetime(2026, 9, 14, 19, 45), dt.datetime(2026, 9, 15, 19, 45)]

    for index, moment in enumerate(moments):
        chain = _day_chain(moment, 100.0 + index, expiry, oi_scale=1 + index * 0.1)
        row = day_summary.summarize(chain, metrics.DEFAULT_PRICING, metrics.gamma_weather(chain))
        db.upsert_day_summary(conn, ticker, source, row, day_summary.CODE_SHA)

    stored = db.get_day_summary(conn, ticker, source)
    assert stored["day"] == dt.date(2026, 9, 15), stored["day"]
    assert stored["core_sha"] == day_summary.CODE_SHA
    assert isinstance(stored["expiries"], list) and stored["gex_by_strike"]

    earlier = db.previous_day_summary(conn, ticker, source, dt.date(2026, 9, 15))
    assert earlier["day"] == dt.date(2026, 9, 14)
    assert db.previous_day_summary(conn, ticker, source, dt.date(2026, 9, 14)) is None
    assert db.list_day_summaries(conn, ticker, source) == [dt.date(2026, 9, 15), dt.date(2026, 9, 14)]

    # The same day written twice keeps one row, describing the later snapshot.
    late = dt.datetime(2026, 9, 15, 20, 55)
    chain = _day_chain(late, 111.0, expiry)
    db.upsert_day_summary(
        conn, ticker, source,
        day_summary.summarize(chain, metrics.DEFAULT_PRICING, metrics.gamma_weather(chain)),
        day_summary.CODE_SHA,
    )
    assert db.list_day_summaries(conn, ticker, source) == [dt.date(2026, 9, 15), dt.date(2026, 9, 14)]
    assert db.get_day_summary(conn, ticker, source)["underlying_price"] == 111.0

    # A row built by other formulas is asked to be rebuilt, exactly as a
    # missing one is: an upgrade that changed a formula must not show its own
    # release as a market event.
    conn.execute(
        "UPDATE ticker_day_summary SET core_sha = 'older' WHERE ticker = %s AND day = %s",
        (ticker, dt.date(2026, 9, 15)),
    )
    conn.commit()

    # Now the real path: snapshots plus a run log, and the backfill reading
    # them. A day nothing was collected on simply has no row to build.
    backfill_ticker = "DAYFILL"
    today = dt.datetime.utcnow().replace(microsecond=0)
    at = today - dt.timedelta(days=1)
    while at.isoweekday() > 5:
        at -= dt.timedelta(days=1)
    chain = _day_chain(at, 100.0, expiry)
    db.insert_snapshot(
        conn, backfill_ticker, at, 100.0,
        chain.drop(columns=["collected_at", "underlying_price"]), source=source,
    )
    db.log_run(conn, at, at + dt.timedelta(seconds=20), backfill_ticker, "success",
               rows_fetched=len(chain), source=source)

    # The rollup of that same moment: the volatility average this table does
    # hold, read by key. The flow numbers are not here, and the day row is why.
    rollup = db.get_moment_rollup(conn, backfill_ticker, source, at)
    assert rollup is not None and "iv_weighted_avg" in rollup
    assert db.get_moment_rollup(conn, backfill_ticker, source, at - dt.timedelta(days=9)) is None

    missing = db.missing_day_summaries(conn, backfill_ticker, source, 7, day_summary.CODE_SHA)
    assert missing, "a collected day with no row must be offered for building"
    counted = day_summary.backfill(conn, source, [backfill_ticker], 7, dry_run=True)
    assert counted == len(missing)
    assert db.get_day_summary(conn, backfill_ticker, source) is None, "dry run must write nothing"

    written = day_summary.backfill(conn, source, [backfill_ticker], 7)
    assert written == len(missing), written
    built = db.get_day_summary(conn, backfill_ticker, source)
    assert built is not None and built["core_sha"] == day_summary.CODE_SHA

    # Run again: everything is current, so nothing is built and nothing is read
    # beyond the one query that says so.
    assert day_summary.backfill(conn, source, [backfill_ticker], 7) == 0
    assert db.missing_day_summaries(conn, backfill_ticker, source, 7, day_summary.CODE_SHA) == []

    conn.execute("DELETE FROM ticker_day_summary WHERE ticker IN (%s, %s)", (ticker, backfill_ticker))
    conn.execute("DELETE FROM collection_runs WHERE ticker = %s", (backfill_ticker,))
    conn.execute("DELETE FROM option_snapshots WHERE ticker = %s", (backfill_ticker,))
    conn.commit()
    conn.close()
    print("day-summary storage checks passed (upsert, the previous day, backfill, and its second run)")


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
    check_contract_greeks_history_is_the_scalar_path_in_one_batch()
    check_contracts_backing_expiry()
    check_adjusted_contracts_do_not_break_collection()
    check_archiving_goes_through_the_registry()
    check_disk_estimate_respects_the_market_calendar()
    check_version_comes_from_the_changelog()
    check_hopeless_symbols_stop_being_asked_for()
    check_history_depth_is_known_before_the_chart()
    check_being_throttled_stops_the_whole_pass()
    check_suggestions_name_a_way_forward()
    check_the_directory_parse_survives_the_real_header()
    check_rollup_fixture_does_not_depend_on_the_weekday()
    check_the_collector_knows_about_holidays()
    check_symbols_the_source_will_not_serve_are_explained()
    check_the_batched_gex_path_returns_the_old_numbers()
    check_the_flip_is_the_crossing_by_the_money()
    check_the_strike_count_actually_decides_what_is_drawn()
    check_the_solver_stands_aside_where_it_should()
    check_a_stopped_collection_does_not_look_healthy()
    check_the_run_log_answers_how_collection_is_going()
    check_realized_volatility_is_stored_once_a_day_and_read_back()
    check_the_day_row_and_the_ladder()
    check_day_summaries_are_written_and_filled_in()
    print("\nALL UNIT CHECKS PASSED")

if __name__ == "__main__":
    main()
