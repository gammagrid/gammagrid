"""Metric calculations.

Every function here lives in app/metrics_core.py, which is byte-identical to
the copy in the hosted product's repository — see the rules at the top of that
file before editing it. The two products drifted apart once (the free one
computed greeks with no dividend yield for weeks while the paid one did not),
and a shared file with a checked hash is the fix for the cause rather than for
the symptom.

This module exists so that callers keep importing `app.metrics` and never have
to know where a function lives. Most of it is a re-export.

WHAT IS DEFINED HERE RATHER THAN IN THE CORE, and when that should change. The
weather and expected-move functions at the bottom compose core functions and
add no arithmetic of their own, so on merit they belong in the shared file.
They are here because putting them there means editing a byte-identical file in
two repositories in one movement, and that is a job with its own preparation
rather than a side effect of adding a view. The moment to move them is the
first time one of them has to change: a duplicated composition that nobody
edits costs nothing, and the same composition edited on one side only is
exactly the drift the shared file exists to prevent.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.metrics_core import (
    _GREEK_KEYS,
    _YEAR_SECONDS,
    ATTRIBUTION_TERMS,
    DEFAULT_PRICING,
    EXPIRY_CLOSE_ET,
    EXPIRY_TZ,
    EXTREME_IV_THRESHOLD,
    PROVIDER_GREEKS,
    GreekAttribution,
    PricingInputs,
    _as_naive_utc,
    _black_scholes_greeks,
    _black_scholes_greeks_batch,
    _expiry_moment,
    _is_priceable,
    _last_snapshot_per_day,
    _mark_unreliable_iv,
    contract_greeks_history,
    contract_price_for_iv,
    contracts_backing_expiry,
    dealer_walls,
    expiry_rollup,
    gamma_exposure_profile,
    gamma_flip_from_matrix,
    gamma_flip_price,
    gex_matrix,
    greek_attribution,
    interpret_greeks,
    iv_surface,
    iv_surface_grid,
    iv_weighted_average,
    max_pain,
    max_pain_series,
    merge_provider_greeks,
    net_gamma_exposure,
    net_gex_by_expiry,
    net_gex_from_matrix,
    net_gex_series,
    oi_delta,
    put_call_ratio,
    realized_volatility,
    risk_free_rate,
    screener_table,
    solve_implied_volatility,
    unusual_activity,
    volume_stats,
    with_solved_iv,
    years_to_expiry,
    years_to_expiry_by_moment,
    years_to_expiry_series,
)

# --- gamma weather -----------------------------------------------------------
# The five states a daily summary can be in. Names, not icons: which picture a
# state gets is a decision for the screen, and whatever stores these has no
# business knowing about pictures.
WEATHER_CLEAR = "clear"
WEATHER_FAIR = "fair"
WEATHER_UNSETTLED = "unsettled"
WEATHER_RAIN = "rain"
WEATHER_STORM = "storm"


def weather_state(
    net_gex: float,
    spot: float,
    gamma_flip: float | None,
    near_pct: float,
    far_pct: float,
) -> str:
    """Which weather a regime looks like, from two facts and nothing else.

    THE SIGN ALONE IS NOT ENOUGH, and that is the whole reason this is a
    function rather than a ternary at the call site. Positive net GEX means
    dealer hedging works against the move and dampens it; negative means it
    works with the move and amplifies it. But a spot sitting two tenths of a
    percent from the gamma flip and a spot sitting five percent from it are the
    same "positive" and completely different days: the first can change regime
    before lunch.

    So the second axis is the distance to the flip as a percentage of the
    underlying price — near enough to be reached in an ordinary session, or far
    enough that it takes an event. That axis is what separates settled weather
    from unsettled, in both directions.

    NO FLIP IN RANGE IS NOT THE SAME AS A FLIP FAR AWAY, even though both end
    up settled. A chain can have no sign change at all — an index does, since
    below its lowest listed strike the cumulative profile is flat zero rather
    than negative. There is then no boundary to be near, so the sign decides
    alone, and the summary says as much in words rather than implying a
    distance nobody measured.
    """
    positive = net_gex >= 0
    if gamma_flip is None or not np.isfinite(spot) or spot <= 0:
        return WEATHER_CLEAR if positive else WEATHER_STORM

    distance = abs(gamma_flip - spot) / spot * 100
    if distance <= near_pct:
        # Close enough that the regime is the thing least worth relying on
        # today, whichever side it currently falls.
        return WEATHER_UNSETTLED
    if positive:
        return WEATHER_CLEAR if distance >= far_pct else WEATHER_FAIR
    return WEATHER_STORM if distance >= far_pct else WEATHER_RAIN


def weather_expiries(
    expiries: list,
    moment: pd.Timestamp,
    horizon_days: int,
    min_expiries: int,
) -> list:
    """Which expiries a daily summary looks at: everything inside a time
    window, never fewer than `min_expiries`.

    A WINDOW IN DAYS, NOT A COUNT OF EXPIRIES. "The nearest three" sounds like
    a horizon and is not one: on a liquid index it can mean two days, and on a
    thinly listed commodity fund over four months, because a count measures how
    densely the exchange listed expiries rather than how far ahead anybody is
    looking. One label cannot honestly cover both.

    THE FLOOR IS FOR SPARSE CALENDARS. Some symbols list a single expiry inside
    thirty days, and a summary built on one expiry is not a summary of
    anything. It then takes the nearest two even though the second falls
    outside the window — and says the range it actually used, rather than the
    one it asked for.
    """
    if not expiries:
        return []
    ordered = sorted(expiries)
    base = pd.Timestamp(moment).normalize()
    inside = [e for e in ordered if (pd.Timestamp(e) - base).days <= horizon_days]
    return inside if len(inside) >= min_expiries else ordered[:min_expiries]


def gamma_weather(
    df: pd.DataFrame,
    pricing: PricingInputs = DEFAULT_PRICING,
    as_of: pd.Timestamp | None = None,
    near_pct: float = 1.0,
    far_pct: float = 3.0,
    horizon_days: int = 30,
    min_expiries: int = 2,
) -> dict | None:
    """Everything a one-line summary of the day needs, from one snapshot, in
    one pass.

    WHY IT IS ASSEMBLED IN ONE PLACE. The summary sits above the view switcher,
    which means it is drawn on every view — so computing it piecemeal would put
    a full `gex_matrix` into views that do not otherwise need one. The matrix is
    built here exactly once and three numbers are read off it, which is the same
    lesson the heatmap already paid for.

    IT LOOKS AT THE NEAR TERM, deliberately. Reading the whole chain makes it
    disagree with the GEX Heatmap directly below it — same labels, different
    numbers, no explanation — on any symbol whose open interest sits mostly in
    far expiries.

    Returns None when the snapshot cannot support a summary at all — no rows,
    or no underlying price to measure a distance against. None is a normal
    answer: a ticker collected minutes ago has no weather yet, and inventing one
    would be worse than the line not appearing.
    """
    if df.empty:
        return None

    moment = as_of if as_of is not None else df["collected_at"].max()
    snapshot = df[df["collected_at"] == moment]
    if snapshot.empty:
        return None

    spot = pd.to_numeric(snapshot["underlying_price"].iloc[0], errors="coerce")
    if not np.isfinite(spot) or spot <= 0:
        return None

    chosen = weather_expiries(
        list(snapshot["expiry"].unique()), moment, horizon_days, min_expiries
    )
    if not chosen:
        return None

    matrix = gex_matrix(df, moment, pricing, expiries=chosen)
    if matrix.empty:
        return None

    net_gex = float(matrix.to_numpy(dtype=float).sum())
    flip = gamma_flip_from_matrix(matrix, spot)
    walls = dealer_walls(df, moment, expiries=chosen)
    # The same matrix folded by strike — the profile the flip and the net figure
    # are read off — handed out so that a caller drawing it does not build the
    # matrix a second time. No new arithmetic: a sum over what is already here.
    by_strike = matrix.sum(axis=1)
    profile = [
        [float(strike), float(gex)]
        for strike, gex in zip(by_strike.index, by_strike.to_numpy(dtype=float))
        if np.isfinite(gex)
    ]

    # The range actually used, so the summary can state it rather than repeat
    # the setting it asked for. These differ whenever the floor kicked in.
    horizon_dte = int((pd.Timestamp(max(chosen)) - pd.Timestamp(moment).normalize()).days)

    return {
        "collected_at": moment,
        "underlying_price": float(spot),
        "net_gex": net_gex,
        "call_wall": walls["call_wall"],
        "put_wall": walls["put_wall"],
        "gamma_flip": flip,
        "flip_distance_pct": None if flip is None else abs(flip - spot) / spot * 100,
        "state": weather_state(net_gex, spot, flip, near_pct, far_pct),
        "expiry_count": len(chosen),
        "horizon_dte": horizon_dte,
        "expiries": [pd.Timestamp(e).strftime("%Y-%m-%d") for e in sorted(chosen)],
        "gex_by_strike": profile,
    }


# --- expected move -----------------------------------------------------------
def atm_implied_volatility(
    df: pd.DataFrame, expiry, as_of: pd.Timestamp | None = None
) -> float | None:
    """Implied volatility at the money for one expiry, or None.

    THE STRIKE NEAREST THE SPOT, CALL AND PUT AVERAGED — not the chain's
    volume-weighted average, and the difference is the whole reason this exists
    rather than reusing `iv_weighted_average`. That average is taken across
    every strike, so it carries the skew: deep puts on an index trade far above
    at-the-money volatility, and a weighted number therefore describes the price
    of crash protection as much as it describes the expected move. The move to
    an expiry is a statement about the middle of the distribution, so it takes
    the volatility priced in the middle.

    Both sides averaged because put-call parity says they should agree and, on a
    real chain, they disagree slightly — quoting one is picking a side of a
    spread for no reason.
    """
    moment = as_of if as_of is not None else df["collected_at"].max()
    chain = df[(df["collected_at"] == moment) & (df["expiry"] == expiry)]
    if chain.empty:
        return None

    spot = pd.to_numeric(chain["underlying_price"].iloc[0], errors="coerce")
    strikes = pd.to_numeric(chain["strike"], errors="coerce")
    if not np.isfinite(spot) or spot <= 0 or strikes.dropna().empty:
        return None

    nearest = strikes.iloc[(strikes - spot).abs().argsort().iloc[0]]
    at_money = chain[strikes == nearest]
    iv = pd.to_numeric(at_money["implied_volatility"], errors="coerce").dropna()
    iv = iv[iv > 0]
    return float(iv.mean()) if not iv.empty else None


def expected_move(
    df: pd.DataFrame, expiry, as_of: pd.Timestamp | None = None
) -> dict | None:
    """One standard deviation of price movement between now and this expiry.

    `S x IV x sqrt(T)`, the number retail reads first — and on its own the
    number it reads without context. What makes it worth showing here is the
    company it keeps: the same screen already carries the call wall and the put
    wall, so the question it answers stops being "how far might it go" and
    becomes "does the move it prices reach the level where dealer hedging
    concentrates". A figure that lands short of both walls says something quite
    different from one that clears them.

    ONE SIGMA, SAID PLAINLY EVERYWHERE IT IS SHOWN. About a two-in-three chance
    of finishing inside the band, which means a third of the time it does not —
    and a band presented without that number is read as a promise.

    Returns None where the volatility is unknown; a move computed from a missing
    IV would be a confident zero.
    """
    moment = as_of if as_of is not None else df["collected_at"].max()
    chain = df[(df["collected_at"] == moment) & (df["expiry"] == expiry)]
    if chain.empty:
        return None

    spot = pd.to_numeric(chain["underlying_price"].iloc[0], errors="coerce")
    if not np.isfinite(spot) or spot <= 0:
        return None

    iv = atm_implied_volatility(df, expiry, moment)
    if iv is None:
        return None

    years = years_to_expiry(expiry, pd.Timestamp(moment))
    if years is None or years <= 0:
        # An expiry that has already passed prices no future move. Not an error:
        # the chain keeps expired contracts until they are archived.
        return None

    move = float(spot) * float(iv) * float(np.sqrt(years))
    return {
        "expiry": expiry,
        "underlying_price": float(spot),
        "implied_volatility": float(iv),
        "years": float(years),
        "move": move,
        "move_pct": move / float(spot) * 100,
        "lower": float(spot) - move,
        "upper": float(spot) + move,
    }


# Re-exports, including the underscore-prefixed helpers: `app.metrics` stays the
# single entry point for callers and tests, so moving a function into the core
# must not force every call site to learn where it went. Listing them here is
# also what tells the linter these imports are the point of the module.
__all__ = [
    "DEFAULT_PRICING",
    "WEATHER_CLEAR",
    "WEATHER_FAIR",
    "WEATHER_RAIN",
    "WEATHER_STORM",
    "WEATHER_UNSETTLED",
    "atm_implied_volatility",
    "expected_move",
    "gamma_weather",
    "weather_expiries",
    "weather_state",
    "EXPIRY_CLOSE_ET",
    "EXPIRY_TZ",
    "ATTRIBUTION_TERMS",
    "EXTREME_IV_THRESHOLD",
    "GreekAttribution",
    "PROVIDER_GREEKS",
    "PricingInputs",
    "_GREEK_KEYS",
    "_YEAR_SECONDS",
    "_as_naive_utc",
    "_black_scholes_greeks",
    "_black_scholes_greeks_batch",
    "_expiry_moment",
    "_is_priceable",
    "_last_snapshot_per_day",
    "_mark_unreliable_iv",
    "contract_greeks_history",
    "contract_price_for_iv",
    "contracts_backing_expiry",
    "dealer_walls",
    "expiry_rollup",
    "gamma_exposure_profile",
    "gamma_flip_from_matrix",
    "gamma_flip_price",
    "gex_matrix",
    "greek_attribution",
    "interpret_greeks",
    "iv_surface",
    "iv_surface_grid",
    "iv_weighted_average",
    "max_pain",
    "max_pain_series",
    "merge_provider_greeks",
    "net_gamma_exposure",
    "net_gex_by_expiry",
    "net_gex_from_matrix",
    "net_gex_series",
    "oi_delta",
    "put_call_ratio",
    "realized_volatility",
    "risk_free_rate",
    "screener_table",
    "solve_implied_volatility",
    "unusual_activity",
    "volume_stats",
    "with_solved_iv",
    "years_to_expiry",
    "years_to_expiry_by_moment",
    "years_to_expiry_series",
]
