"""Manual check of the db.py + metrics.py plumbing on synthetic data, with no
network and no real yfinance. Not part of a pytest suite — just a quick run
during development. Usage: python tests/smoke_test.py"""

import os
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["OPTIONS_TRACKER_DB"] = "/tmp/options_tracker_smoke_test.db"

from app import collector, db, metrics  # noqa: E402

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


def main():
    if os.path.exists(os.environ["OPTIONS_TRACKER_DB"]):
        os.remove(os.environ["OPTIONS_TRACKER_DB"])

    conn = db.get_connection()
    db.add_ticker(conn, "TEST")
    assert db.get_watchlist(conn) == ["TEST"]

    base_day = datetime(2026, 7, 1, 21, 0)
    # Days 1-5: one collection per day, OI grows a little day over day (95..100, OI 50..61).
    # Day 6: TWO collections (morning and evening) — the exact scenario from the bug: OI
    # doesn't change intraday (65->65) while volume grows as cumulative daily volume (200->500).
    daily_snapshots = [
        # (offset_hours_from_base, volume, open_interest)
        (0, 95, 50),
        (24, 105, 52),
        (48, 90, 55),
        (72, 110, 58),
        (96, 100, 61),
        (120 - 12, 200, 65),  # day 6, morning
        (120, 500, 65),       # day 6, evening — same OI as the morning
    ]

    for i, (offset_hours, volume, oi) in enumerate(daily_snapshots):
        moment = base_day + timedelta(hours=offset_hours)
        db.insert_snapshot(
            conn, "TEST", moment, underlying_price=100.0 + i * 0.1,
            chain_df=make_chain(iv_shift=i * 0.01, strike_100_volume=volume, strike_100_oi=oi),
        )
    latest_moment = base_day + timedelta(hours=daily_snapshots[-1][0])
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

    print("ALL SMOKE CHECKS PASSED")


if __name__ == "__main__":
    main()
