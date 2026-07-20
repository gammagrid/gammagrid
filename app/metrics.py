"""Pure metric-calculation functions. Input — a DataFrame from
db.get_snapshots(), output — a DataFrame/number. No side effects, no DB or
network access — which is why they are testable without a database or network."""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.interpolate import griddata
from scipy.stats import norm

from app import config


def _last_snapshot_per_day(df: pd.DataFrame) -> pd.DataFrame:
    """Collapses multiple intraday collections down to one (the latest) per
    calendar day. Needed wherever values that aren't comparable within a day
    get compared: Yahoo's volume is cumulative since session open (grows until
    the close), while open_interest barely updates intraday — without this
    collapse, comparing "the last two snapshots" may compare two snapshots of
    the same day instead of day over day."""
    daily_latest = df.groupby(df["collected_at"].dt.date)["collected_at"].transform("max")
    return df[df["collected_at"] == daily_latest]


def put_call_ratio(df: pd.DataFrame) -> pd.DataFrame:
    """Per collection date — the put/call ratio by volume and by OI (spec FR4)."""
    grouped = (
        df.groupby(["collected_at", "option_type"])
        .agg(volume=("volume", "sum"), open_interest=("open_interest", "sum"))
        .unstack("option_type")
    )
    result = pd.DataFrame({
        "pcr_volume": grouped["volume"]["put"] / grouped["volume"]["call"],
        "pcr_oi": grouped["open_interest"]["put"] / grouped["open_interest"]["call"],
    })
    return result.reset_index()


def max_pain(df: pd.DataFrame, expiry: pd.Timestamp) -> float | None:
    """The strike with the minimal total payout by option sellers for the given
    expiry, based on the latest available snapshot (spec FR5)."""
    latest_date = df["collected_at"].max()
    snapshot = df[(df["collected_at"] == latest_date) & (df["expiry"] == expiry)]
    if snapshot.empty:
        return None

    strikes = sorted(snapshot["strike"].unique())
    calls = snapshot[snapshot["option_type"] == "call"].set_index("strike")["open_interest"]
    puts = snapshot[snapshot["option_type"] == "put"].set_index("strike")["open_interest"]

    def payout_at(settle: float) -> float:
        call_payout = sum(max(settle - k, 0) * calls.get(k, 0) for k in strikes)
        put_payout = sum(max(k - settle, 0) * puts.get(k, 0) for k in strikes)
        return call_payout + put_payout

    payouts = {settle: payout_at(settle) for settle in strikes}
    return min(payouts, key=payouts.get)


_GREEK_KEYS = ("delta", "gamma", "theta", "vega", "rho", "vanna", "charm")


def _black_scholes_greeks(
    spot: float, strike: float, years_to_expiry: float, iv: float, risk_free_rate: float, option_type: str
) -> dict[str, float]:
    """All greeks via Black-Scholes formulas ignoring dividend yield (q=0) —
    the same simplifying assumption already used for gamma in GEX (spec FR6,
    FR14). With q=0, charm is identical for calls and puts (delta_put =
    delta_call - const, so their time derivatives are equal) — deliberately
    not computed twice. Theta is per calendar day; vega/rho are per 1 pp
    change in IV/rate (the units traders actually use, not "raw" per-year
    partial derivatives)."""
    if years_to_expiry <= 0 or iv <= 0 or spot <= 0:
        return {k: 0.0 for k in _GREEK_KEYS}

    sqrt_t = np.sqrt(years_to_expiry)
    d1 = (np.log(spot / strike) + (risk_free_rate + iv ** 2 / 2) * years_to_expiry) / (iv * sqrt_t)
    d2 = d1 - iv * sqrt_t
    pdf_d1 = norm.pdf(d1)
    discount = np.exp(-risk_free_rate * years_to_expiry)

    gamma = pdf_d1 / (spot * iv * sqrt_t)
    vega = spot * pdf_d1 * sqrt_t / 100
    vanna = -pdf_d1 * d2 / iv
    charm = -pdf_d1 * (2 * risk_free_rate * years_to_expiry - d2 * iv * sqrt_t) / (2 * years_to_expiry * iv * sqrt_t)

    if option_type == "call":
        delta = norm.cdf(d1)
        theta = (-(spot * pdf_d1 * iv) / (2 * sqrt_t) - risk_free_rate * strike * discount * norm.cdf(d2)) / 365
        rho = strike * years_to_expiry * discount * norm.cdf(d2) / 100
    else:
        delta = norm.cdf(d1) - 1
        theta = (-(spot * pdf_d1 * iv) / (2 * sqrt_t) + risk_free_rate * strike * discount * norm.cdf(-d2)) / 365
        rho = -strike * years_to_expiry * discount * norm.cdf(-d2) / 100

    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega, "rho": rho, "vanna": vanna, "charm": charm}


def _black_scholes_greeks_batch(
    spot: pd.Series, strike: pd.Series, years_to_expiry: pd.Series, iv: pd.Series,
    risk_free_rate: float, option_type: pd.Series,
) -> pd.DataFrame:
    """Vectorized counterpart of `_black_scholes_greeks` — computes greeks for
    all rows at once (screener, spec FR25) instead of a Python loop over
    contracts. The formulas are identical, just on numpy arrays instead of
    scalars."""
    valid = (years_to_expiry > 0) & (iv > 0) & (spot > 0)
    # compute invalid rows on placeholder values (1.0) to avoid log(0)/division
    # by zero — the resulting values are zeroed out below via .where(valid)
    safe_t = years_to_expiry.where(valid, 1.0)
    safe_iv = iv.where(valid, 1.0)
    safe_spot = spot.where(valid, 1.0)

    sqrt_t = np.sqrt(safe_t)
    d1 = (np.log(safe_spot / strike) + (risk_free_rate + safe_iv ** 2 / 2) * safe_t) / (safe_iv * sqrt_t)
    d2 = d1 - safe_iv * sqrt_t
    pdf_d1 = norm.pdf(d1)
    discount = np.exp(-risk_free_rate * safe_t)

    gamma = pdf_d1 / (safe_spot * safe_iv * sqrt_t)
    vega = safe_spot * pdf_d1 * sqrt_t / 100
    vanna = -pdf_d1 * d2 / safe_iv
    charm = -pdf_d1 * (2 * risk_free_rate * safe_t - d2 * safe_iv * sqrt_t) / (2 * safe_t * safe_iv * sqrt_t)

    is_call = (option_type == "call").to_numpy()
    delta = np.where(is_call, norm.cdf(d1), norm.cdf(d1) - 1)
    theta = np.where(
        is_call,
        (-(safe_spot * pdf_d1 * safe_iv) / (2 * sqrt_t) - risk_free_rate * strike * discount * norm.cdf(d2)) / 365,
        (-(safe_spot * pdf_d1 * safe_iv) / (2 * sqrt_t) + risk_free_rate * strike * discount * norm.cdf(-d2)) / 365,
    )
    rho = np.where(
        is_call,
        strike * safe_t * discount * norm.cdf(d2) / 100,
        -strike * safe_t * discount * norm.cdf(-d2) / 100,
    )

    result = pd.DataFrame(
        {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega, "rho": rho, "vanna": vanna, "charm": charm},
        index=spot.index,
    )
    return result.where(valid, 0.0)


def screener_table(df: pd.DataFrame, risk_free_rate: float = config.RISK_FREE_RATE) -> pd.DataFrame:
    """Flat table of every contract in the ticker's latest snapshot with DTE
    and the full set of greeks (spec FR25) — the basis for the range-filter
    screener. Latest snapshot only, not the full history: the screener is a
    "what's out there right now" cross-section, not a time series (per-contract
    history lives in the Contract tab)."""
    snapshot_date = df["collected_at"].max()
    snapshot = df[df["collected_at"] == snapshot_date].copy()
    if snapshot.empty:
        return pd.DataFrame()

    snapshot["dte"] = (snapshot["expiry"] - snapshot_date).dt.days
    years_to_expiry = snapshot["dte"] / 365

    greeks = _black_scholes_greeks_batch(
        snapshot["underlying_price"], snapshot["strike"], years_to_expiry,
        snapshot["implied_volatility"], risk_free_rate, snapshot["option_type"],
    )
    table = pd.concat([snapshot.reset_index(drop=True), greeks.reset_index(drop=True)], axis=1)
    columns = [
        "expiry", "strike", "option_type", "dte", "last_price", "open_interest",
        "implied_volatility", *_GREEK_KEYS,
    ]
    return table[columns].sort_values(["expiry", "strike", "option_type"]).reset_index(drop=True)


def gamma_exposure_profile(
    df: pd.DataFrame,
    expiry: pd.Timestamp,
    risk_free_rate: float = config.RISK_FREE_RATE,
    as_of: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Approximate dealer GEX profile by strike for the given expiry (spec FR6).
    `as_of=None` — the latest snapshot (current state); otherwise a specific
    collection date from history (Replay, spec section 12, GEX Heatmap). Sign
    convention: puts contribute negatively — the "dealers are net long puts /
    net short calls versus retail flow" heuristic, which does NOT reflect
    actual market-maker positioning (see the disclaimer in the UI)."""
    snapshot_date = as_of if as_of is not None else df["collected_at"].max()
    snapshot = df[(df["collected_at"] == snapshot_date) & (df["expiry"] == expiry)].copy()
    if snapshot.empty:
        return pd.DataFrame(columns=["strike", "gex"])

    spot = snapshot["underlying_price"].iloc[0]
    years_to_expiry = (expiry - snapshot_date).days / 365

    snapshot["gamma"] = snapshot.apply(
        lambda row: _black_scholes_greeks(
            spot, row["strike"], years_to_expiry, row["implied_volatility"], risk_free_rate, row["option_type"]
        )["gamma"],
        axis=1,
    )
    snapshot["contract_gex"] = snapshot["gamma"] * snapshot["open_interest"].fillna(0) * 100 * spot
    snapshot.loc[snapshot["option_type"] == "put", "contract_gex"] *= -1

    profile = snapshot.groupby("strike", as_index=False)["contract_gex"].sum()
    return profile.rename(columns={"contract_gex": "gex"})


def net_gamma_exposure(gex_profile: pd.DataFrame) -> float:
    """Total net GEX for an expiry (spec FR15) — the sign defines the regime:
    positive — dealers dampen price moves, negative — they amplify them."""
    if gex_profile.empty:
        return 0.0
    return float(gex_profile["gex"].sum())


def gex_matrix(
    df: pd.DataFrame,
    as_of: pd.Timestamp | None = None,
    risk_free_rate: float = config.RISK_FREE_RATE,
    expiries: list | None = None,
) -> pd.DataFrame:
    """GEX matrix strike × expiry for a single snapshot (spec section 12, GEX
    Heatmap). Built by reusing `gamma_exposure_profile` in a loop over
    expiries; needs no new data — one snapshot already contains the full chain.
    `expiries=None` — every expiry in the snapshot; the UI usually passes only
    the visible subset (a ticker can have 30+ expiries including far-dated
    LEAPS — computing a profile for all of them is wasteful when only part is
    shown). Index — strike descending (top to bottom, as in a conventional
    heatmap), columns — expiry."""
    snapshot_date = as_of if as_of is not None else df["collected_at"].max()
    if expiries is None:
        expiries = sorted(df[df["collected_at"] == snapshot_date]["expiry"].unique())

    profiles = []
    for expiry in expiries:
        profile = gamma_exposure_profile(df, expiry, risk_free_rate, as_of=snapshot_date)
        if not profile.empty:
            profiles.append(profile.assign(expiry=expiry))

    if not profiles:
        return pd.DataFrame()

    combined = pd.concat(profiles, ignore_index=True)
    # fill_value=0, not NaN: if an expiry has no listing at a given strike,
    # dealer exposure there is genuinely zero (not "unknown") — this removes
    # visual "holes" in the matrix rather than merely masking them.
    matrix = combined.pivot_table(index="strike", columns="expiry", values="gex", aggfunc="sum", fill_value=0)
    return matrix.sort_index(ascending=False)


def net_gex_by_expiry(
    df: pd.DataFrame,
    as_of: pd.Timestamp | None = None,
    risk_free_rate: float = config.RISK_FREE_RATE,
    expiries: list | None = None,
) -> pd.DataFrame:
    """Net GEX per expiry of the snapshot (GEX Heatmap sidebar) — same as
    `net_gamma_exposure`, but for all expiries at once (or for the given
    subset — see `gex_matrix`)."""
    snapshot_date = as_of if as_of is not None else df["collected_at"].max()
    if expiries is None:
        expiries = sorted(df[df["collected_at"] == snapshot_date]["expiry"].unique())
    rows = [
        {
            "expiry": expiry,
            "net_gex": net_gamma_exposure(gamma_exposure_profile(df, expiry, risk_free_rate, as_of=snapshot_date)),
        }
        for expiry in expiries
    ]
    return pd.DataFrame(rows)


def dealer_walls(
    df: pd.DataFrame, as_of: pd.Timestamp | None = None, expiries: list | None = None
) -> dict[str, float | None]:
    """Call Wall / Put Wall — the strike with maximum open interest in
    calls/puts, aggregated across the snapshot's expiries — all by default, or
    the given subset (a common proxy for support/resistance levels created by
    dealer hedging flows, spec section 12)."""
    snapshot_date = as_of if as_of is not None else df["collected_at"].max()
    snapshot = df[df["collected_at"] == snapshot_date]
    if expiries is not None:
        snapshot = snapshot[snapshot["expiry"].isin(expiries)]
    oi_by_strike = snapshot.groupby(["option_type", "strike"])["open_interest"].sum()

    def wall(option_type: str) -> float | None:
        if option_type not in oi_by_strike.index.get_level_values("option_type"):
            return None
        series = oi_by_strike.loc[option_type]
        return float(series.idxmax()) if not series.empty else None

    return {"call_wall": wall("call"), "put_wall": wall("put")}


def gamma_flip_price(
    df: pd.DataFrame,
    as_of: pd.Timestamp | None = None,
    risk_free_rate: float = config.RISK_FREE_RATE,
    expiries: list | None = None,
) -> float | None:
    """Approximate underlying price at which total dealer GEX (across all
    expiries) flips sign — a proxy for the "gamma flip" level (spec section
    12). Simplification: take the already-computed per-strike GEX profile (at
    the actual current underlying price, as everywhere in the app), sum across
    expiries, walk strikes in ascending order and find where the cumulative
    sum changes sign — with linear interpolation between the two nearest
    strikes so the result is a price level, not just a strike. Does not
    re-price greeks on a grid of hypothetical underlying prices (more accurate
    but substantially more expensive) — the same class of assumption already
    used for net GEX (spec FR6/FR15)."""
    matrix = gex_matrix(df, as_of, risk_free_rate, expiries=expiries)
    if matrix.empty:
        return None

    combined = matrix.sum(axis=1).sort_index()
    cumulative = combined.cumsum()
    strikes = cumulative.index.to_numpy(dtype=float)
    values = cumulative.to_numpy(dtype=float)

    sign_changes = np.where(np.diff(np.sign(values)) != 0)[0]
    if len(sign_changes) == 0:
        return None

    i = int(sign_changes[0])
    x0, x1 = strikes[i], strikes[i + 1]
    y0, y1 = values[i], values[i + 1]
    if y1 == y0:
        return float(x0)
    return float(x0 + (0 - y0) * (x1 - x0) / (y1 - y0))


def unusual_activity(
    df: pd.DataFrame,
    z_threshold: float = config.UNUSUAL_Z_THRESHOLD,
    min_volume: int = config.UNUSUAL_MIN_VOLUME,
    min_history_points: int = config.UNUSUAL_MIN_HISTORY_POINTS,
) -> pd.DataFrame:
    """Contracts in the latest snapshot with anomalous volume (spec FR16). The
    flag is a volume z-score above `z_threshold` relative to the contract's
    own history (not a flat multiplier — that would flag thousands of rows on
    liquid tickers). Contracts with history shorter than `min_history_points`
    get no z-score trust — a crude fallback is used instead (volume > 2×OI).
    `min_volume` cuts noise from illiquid far strikes regardless of the stats.

    History for the mean/std is collapsed to one snapshot per calendar day
    (`_last_snapshot_per_day`) — Yahoo's volume is cumulative since session
    open, and without the collapse several same-day collections would distort
    the mean/variance by mixing different moments within the trading day."""
    latest_date = df["collected_at"].max()
    latest = df[df["collected_at"] == latest_date].copy()
    history = _last_snapshot_per_day(df[df["collected_at"] < latest_date])

    contract_keys = ["expiry", "strike", "option_type"]
    stats = history.groupby(contract_keys)["volume"].agg(avg_volume="mean", std_volume="std", history_points="count")
    latest = latest.merge(stats, on=contract_keys, how="left")
    latest["history_points"] = latest["history_points"].fillna(0)

    latest["volume_zscore"] = (latest["volume"] - latest["avg_volume"]) / latest["std_volume"].replace(0, np.nan)

    has_enough_history = latest["history_points"] >= min_history_points
    zscore_flag = has_enough_history & (latest["volume_zscore"] > z_threshold)
    fallback_flag = ~has_enough_history & (latest["volume"] > 2 * latest["open_interest"].clip(lower=1))
    passes_floor = latest["volume"] >= min_volume

    flagged = latest[(zscore_flag | fallback_flag) & passes_floor]
    columns = ["expiry", "strike", "option_type", "volume", "open_interest", "avg_volume", "volume_zscore"]
    return flagged[columns].sort_values("volume_zscore", ascending=False, na_position="last")


def iv_weighted_average(df: pd.DataFrame) -> pd.DataFrame:
    """Level 1 (spec FR8a): volume-weighted average IV across the whole chain, per collection date."""
    def weighted_avg(group: pd.DataFrame) -> float:
        weights = group["volume"].fillna(0)
        if weights.sum() == 0:
            return np.nan
        return np.average(group["implied_volatility"], weights=weights)

    result = df.groupby("collected_at").apply(weighted_avg, include_groups=False)
    return result.reset_index(name="iv_weighted_avg")


def realized_volatility(
    price_history: pd.DataFrame, windows: tuple[int, ...] = (10, 20, 30)
) -> dict[int, float]:
    """Realized (historical) close-to-close volatility of the underlying,
    annualized (spec FR24). `price_history` is daily closes with a "close"
    column — NOT our own snapshot history: that one is too short and sparse
    (a few days, scattered intraday points) for an honest calculation — a
    20-30 trading-day window would take months of real collection to fill.
    Instead, a separate deep daily price history (yfinance) is used,
    independent of how long we've been collecting option chains.

    Returns {window_days: annualized volatility} only for windows with enough
    history; missing windows simply don't appear in the result."""
    closes = price_history["close"].dropna()
    log_returns = np.log(closes / closes.shift(1)).dropna()

    result = {}
    for window in windows:
        if len(log_returns) < window:
            continue
        result[window] = float(log_returns.tail(window).std() * np.sqrt(252))
    return result


def contract_greeks_history(
    df: pd.DataFrame,
    strike: float,
    expiry: pd.Timestamp,
    option_type: str,
    risk_free_rate: float = config.RISK_FREE_RATE,
) -> pd.DataFrame:
    """Price, IV, and full-greeks history of a specific contract across
    collection dates (spec FR14). Replaces the former iv_by_contract — the
    same drill-down, plus the full set of greeks."""
    contract = df[
        (df["strike"] == strike) & (df["expiry"] == expiry) & (df["option_type"] == option_type)
    ].sort_values("collected_at")
    if contract.empty:
        return pd.DataFrame(columns=["collected_at", "last_price", "implied_volatility", *_GREEK_KEYS])

    records = []
    for row in contract.itertuples():
        years_to_expiry = (expiry - row.collected_at).days / 365
        greeks = _black_scholes_greeks(
            row.underlying_price, strike, years_to_expiry, row.implied_volatility, risk_free_rate, option_type
        )
        records.append({
            "collected_at": row.collected_at,
            "last_price": row.last_price,
            "implied_volatility": row.implied_volatility,
            **greeks,
        })
    return pd.DataFrame(records)


def interpret_greeks(history: pd.DataFrame) -> list[str]:
    """Plain-language interpretation of the latest greek values and their change
    versus the previous snapshot (spec FR14) — template generation over the
    numbers, not an LLM call."""
    if history.empty:
        return []

    latest = history.iloc[-1]
    prior = history.iloc[-2] if len(history) >= 2 else None

    def trend(col: str) -> str:
        if prior is None:
            return ""
        diff = latest[col] - prior[col]
        if abs(diff) < 1e-6:
            return " (unchanged since the previous snapshot)"
        return f" ({'up' if diff > 0 else 'down'} since the previous snapshot)"

    return [
        f"Delta {latest['delta']:.2f}{trend('delta')} — for a $1 move in the underlying, "
        f"the contract price changes by roughly ${abs(latest['delta']):.2f}.",
        f"Gamma {latest['gamma']:.4f}{trend('gamma')} — how fast delta itself changes per $1 "
        f"move in the underlying; the higher it is, the sharper dealers' hedging needs shift.",
        f"Theta {latest['theta']:.2f}{trend('theta')} — the contract loses roughly "
        f"${abs(latest['theta']):.2f} per day from the passage of time alone, all else equal.",
        f"Vega {latest['vega']:.2f}{trend('vega')} — a 1 pp rise in implied volatility "
        f"changes the contract price by roughly ${latest['vega']:.2f}.",
        f"Rho {latest['rho']:.2f}{trend('rho')} — sensitivity to the interest rate, usually "
        f"a secondary factor for options under a one-year horizon.",
        f"Vanna {latest['vanna']:.4f}{trend('vanna')} — how delta responds to a change in "
        f"volatility (symmetrically: how vega responds to a price move).",
        f"Charm {latest['charm']:.4f}{trend('charm')} — how much delta \"ages\" over one day "
        f"at an unchanged price (time decay of delta itself, not of the contract price).",
    ]


def oi_delta(df: pd.DataFrame) -> pd.DataFrame:
    """Open interest difference between the last and the previous calendar day
    (not snapshot — multiple same-day collections are collapsed to the latest
    via `_last_snapshot_per_day`). Yahoo's open interest barely updates
    intraday, so comparing two snapshots of the same day almost always yields
    a delta of 0 and does not reflect the real day-over-day change (spec FR9)."""
    daily = _last_snapshot_per_day(df)
    dates = sorted(daily["collected_at"].unique())
    if len(dates) < 2:
        return pd.DataFrame(columns=["expiry", "strike", "option_type", "open_interest", "oi_delta", "oi_delta_pct"])

    latest_date, previous_date = dates[-1], dates[-2]
    contract_keys = ["expiry", "strike", "option_type"]

    latest = daily[daily["collected_at"] == latest_date].set_index(contract_keys)["open_interest"]
    previous = daily[daily["collected_at"] == previous_date].set_index(contract_keys)["open_interest"]

    result = pd.DataFrame({"open_interest": latest, "oi_delta": latest - previous}).dropna()
    # % of the previous value — the absolute delta alone doesn't convey scale
    # (a 1000-contract increase is a lot at OI=2000 and almost nothing at
    # OI=200000). previous=0 yields NaN (opening "from zero" has no % form).
    result["oi_delta_pct"] = (result["oi_delta"] / previous.replace(0, np.nan)) * 100
    result = result.reset_index()

    # Sort by absolute delta magnitude — otherwise large NEGATIVE changes
    # (position closing) sink to the bottom of the table, though they matter
    # no less than increases. The sign is preserved in the value itself.
    return result.sort_values("oi_delta", key=abs, ascending=False)


def iv_surface(df: pd.DataFrame) -> pd.DataFrame:
    """Volatility surface points from the latest snapshot: for each strike, the
    OTM contract's IV is taken (put below spot, call above — standard vol
    surface practice, since ITM quotes are usually less liquid and noisier).
    Returns long format (strike, expiry, years_to_expiry, implied_volatility)
    ready for interpolation/plotting. Rows with zero/missing IV are dropped —
    they are left for interpolation to fill in (`iv_surface_grid`)."""
    latest_date = df["collected_at"].max()
    snapshot = df[df["collected_at"] == latest_date].copy()
    if snapshot.empty:
        return pd.DataFrame(columns=["strike", "expiry", "years_to_expiry", "implied_volatility"])

    spot = snapshot["underlying_price"].iloc[0]
    is_otm = np.where(
        snapshot["strike"] < spot, snapshot["option_type"] == "put", snapshot["option_type"] == "call"
    )
    otm = snapshot[is_otm & snapshot["implied_volatility"].notna() & (snapshot["implied_volatility"] > 0)].copy()
    otm["years_to_expiry"] = (otm["expiry"] - latest_date).dt.days / 365
    return otm[["strike", "expiry", "years_to_expiry", "implied_volatility"]].sort_values(["expiry", "strike"])


def iv_surface_grid(
    surface: pd.DataFrame, strike_points: int = 40, expiry_points: int = 40
) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """Builds a regular strike × years_to_expiry grid over the (usually uneven
    — not all expiries share the same strikes, far dates are coarser) points
    from `iv_surface` via linear interpolation (`griddata`). Linear
    interpolation is undefined outside the convex hull of the points (grid
    edges) — those are filled with nearest-neighbor so the surface has no
    holes/NaNs. Returns None when there aren't enough points to interpolate
    (needs ≥2 distinct strikes and ≥2 distinct expiries — otherwise the points
    lie on a line, not a surface)."""
    if surface.empty or surface["strike"].nunique() < 2 or surface["years_to_expiry"].nunique() < 2:
        return None

    strikes = np.linspace(surface["strike"].min(), surface["strike"].max(), strike_points)
    years = np.linspace(surface["years_to_expiry"].min(), surface["years_to_expiry"].max(), expiry_points)
    grid_x, grid_y = np.meshgrid(strikes, years)
    points = (surface["strike"].to_numpy(), surface["years_to_expiry"].to_numpy())
    values = surface["implied_volatility"].to_numpy()

    grid_z = griddata(points, values, (grid_x, grid_y), method="linear")
    nan_mask = np.isnan(grid_z)
    if nan_mask.any():
        grid_z_nearest = griddata(points, values, (grid_x, grid_y), method="nearest")
        grid_z[nan_mask] = grid_z_nearest[nan_mask]

    return strikes, years, grid_z
