"""Shared calculation core — BYTE-IDENTICAL in gammagrid (GitHub, self-hosted)
and options-flow (GitLab, hosted). Do not edit one copy alone.

Everything here is pure: a DataFrame from db.get_snapshots() plus `config`
goes in, a DataFrame or a number comes out. No database, no network, no
Streamlit — which is why it is testable without any of them, and why the same
file can serve two products with different storage engines.

WHY THIS FILE EXISTS. The two products drifted, and the audit (docs/specs,
OSS-1) found the cause was not the database engine but the fact that fixes
were carried across by hand and sometimes were not. The free product spent
weeks computing greeks with no dividend yield while the paid one did not — a
wrong number in the free product, which is the worst place to have one.

THE RULES.
  1. A change here lands in BOTH repositories, in the same shape.
  2. After changing it, update app/metrics_core.sha256 (the checks print the
     new value when they fail).
  3. Anything that needs data one product cannot get — a live rate curve, a
     dividend yield — does NOT belong here. It goes in that product's own
     app/metrics.py, which imports from this module.
  4. Only `app.config` may be imported from the application. The eight
     constants read here must exist in both repositories' config.py; a check
     asserts that.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

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


def _is_priceable(spot, strike, years_to_expiry, iv) -> bool:
    return (
        spot is not None and strike is not None and years_to_expiry is not None and iv is not None
        and not pd.isna(spot) and not pd.isna(strike) and not pd.isna(years_to_expiry) and not pd.isna(iv)
        and spot > 0 and strike > 0 and years_to_expiry > 0 and iv > 0
    )


EXTREME_IV_THRESHOLD = 3.0


EXPIRY_CLOSE_ET = dt.time(16, 0)


EXPIRY_TZ = ZoneInfo("America/New_York")


_YEAR_SECONDS = 365 * 24 * 3600


def _expiry_moment(expiry) -> pd.Timestamp:
    """The instant a contract actually stops trading, as naive UTC."""
    date = pd.Timestamp(expiry).date()
    local = dt.datetime.combine(date, EXPIRY_CLOSE_ET, tzinfo=EXPIRY_TZ)
    return pd.Timestamp(local.astimezone(dt.timezone.utc)).tz_localize(None)


def _as_naive_utc(moment) -> pd.Timestamp:
    """Everything stored is naive UTC, but callers reach for whatever is handy
    — `pd.Timestamp.utcnow()` is tz-aware and subtracting it raises. Normalize
    here rather than at each call site, where the mistake is invisible until
    it throws."""
    stamp = pd.Timestamp(moment)
    return stamp.tz_convert("UTC").tz_localize(None) if stamp.tzinfo is not None else stamp


def years_to_expiry(expiry, as_of) -> float:
    """Time to expiry in years, measured to the 16:00 ET close.

    Replaces `(expiry - as_of).days / 365`, which was wrong twice over
    (reported live): it compared against MIDNIGHT of the expiration date, so a
    contract still trading through the session already counted as expired and
    every greek collapsed to zero for the whole day; and truncating to whole
    days understated the remaining life by up to 24 hours. Both errors are
    negligible on LEAPS and dominant on the near-dated contracts where gamma
    is largest — which is exactly what the GEX tab is for.

    Negative once trading has stopped, which callers rely on to tell a
    finished contract from a live one."""
    return (_expiry_moment(expiry) - _as_naive_utc(as_of)).total_seconds() / _YEAR_SECONDS


def years_to_expiry_series(expiry: pd.Series, as_of) -> pd.Series:
    """Vectorized counterpart — the screener and GEX price whole chains."""
    moments = pd.to_datetime(pd.Series(expiry).map(_expiry_moment))
    return (moments - _as_naive_utc(as_of)).dt.total_seconds() / _YEAR_SECONDS


def risk_free_rate(curve: dict[float, float], years_to_expiry: float) -> float | None:
    """Continuously-compounded risk-free rate for this maturity, from a stored
    Treasury par yield curve ({tenor_years: yield_pct}).

    Two conversions, both mandatory and both easy to forget:
      1. the feed publishes percent (4.06), the maths needs a fraction;
      2. it publishes a PAR yield with semi-annual coupons, while
         Black-Scholes needs a continuously-compounded rate.
    Beyond either end of the curve the nearest tenor is used — extrapolating a
    yield curve is a modelling decision we have no reason to make here."""
    if not curve or years_to_expiry is None or years_to_expiry <= 0:
        return None
    tenors = sorted(curve)
    par = float(np.interp(years_to_expiry, tenors, [curve[t] for t in tenors])) / 100.0
    if par <= -1.99:  # guards log of a non-positive number on absurd input
        return None
    return 2.0 * float(np.log1p(par / 2.0))


class PricingInputs:
    """The (r, q) pair every greek in this app is computed with (epic С-16).

    Passed around as one object rather than two floats because they must move
    together: mixing a curve rate with a zero dividend yield is precisely the
    inconsistency this epic exists to remove. The rate depends on time to
    expiry, so it is resolved per contract, not once per call.

    The default instance keeps the pre-С-16 behaviour — flat config rate, no
    dividend — so any caller that has no reference data (tests, a database
    that has never fetched a curve) still works and is visibly using the old
    model rather than silently producing NaNs."""

    __slots__ = ("curve", "dividend_yield")

    def __init__(self, curve: dict[float, float] | None = None, dividend_yield: float = 0.0):
        self.curve = curve or {}
        self.dividend_yield = float(dividend_yield or 0.0)

    def rate_for(self, years_to_expiry: float) -> float:
        rate = risk_free_rate(self.curve, years_to_expiry) if self.curve else None
        return config.RISK_FREE_RATE if rate is None else rate

    def rate_series(self, years: pd.Series) -> np.ndarray:
        """Vectorized counterpart — the screener and GEX price a whole chain at
        once, and every row has its own maturity."""
        if not self.curve:
            return np.full(len(years), config.RISK_FREE_RATE, dtype=float)
        tenors = sorted(self.curve)
        par = np.interp(
            np.asarray(years, dtype=float), tenors, [self.curve[t] for t in tenors]
        ) / 100.0
        return 2.0 * np.log1p(np.clip(par, -1.9, None) / 2.0)

    def __repr__(self) -> str:  # keeps cache keys and debug output readable
        return f"PricingInputs(tenors={len(self.curve)}, q={self.dividend_yield:.4f})"


DEFAULT_PRICING = PricingInputs()


PROVIDER_GREEKS = ("delta", "gamma", "theta", "vega")


def merge_provider_greeks(computed: pd.DataFrame, snapshot: pd.DataFrame) -> pd.DataFrame:
    """Provider greeks win where present, ours fill the gaps (epic С-16).

    The provider's greeks are internally consistent with the IV it derived
    them from, which ours can only approximate — so where it supplies one, use
    it. It supplies them for part of the chain only (180/300 on SPY, 247/666
    on MO) and Yahoo supplies none, so the computed set is not a fallback for
    edge cases but the normal path for most rows.

    Both frames must share the row order; the caller resets indexes first."""
    merged = computed.copy()
    for greek in PROVIDER_GREEKS:
        if greek not in snapshot.columns:
            continue
        supplied = pd.to_numeric(snapshot[greek], errors="coerce")
        merged[greek] = supplied.where(supplied.notna(), merged[greek])
    return merged


def _black_scholes_greeks(
    spot: float, strike: float, years_to_expiry: float, iv: float, risk_free_rate: float,
    option_type: str, dividend_yield: float = 0.0,
) -> dict[str, float]:
    """All greeks via generalized Black-Scholes with a continuous dividend
    yield. Theta and charm are per calendar day and per year respectively;
    vega/rho are per 1 pp change in IV/rate (the units traders actually use,
    not raw per-year partial derivatives).

    `dividend_yield` was added in epic С-16. It defaults to 0 only so that
    callers that genuinely have no yield keep working — every caller that
    prices a real contract must pass the real one. Ignoring it was measured to
    misprice the MO forward by 11% and to put our delta 0.05 away from the
    provider's on long-dated contracts.

    With q != 0, charm is NO LONGER the same for calls and puts: the earlier
    version computed it once and shared it, which was correct only because
    delta_put - delta_call was a constant at q = 0. It is now computed per
    option type."""
    # `iv` and `spot` come from collected data and can simply be absent: a
    # provider that quotes no volatility for a strike writes SQL NULL, and a
    # deep out-of-the-money contract is exactly where that happens. Which
    # Python value arrives depends on rows this contract has nothing to do
    # with: pandas keeps the column as `object` (so `None` survives) when the
    # WHOLE ticker's column is null, and as `float64` (so it becomes `NaN`)
    # when any other contract of that ticker has a value. `None <= 0` raises,
    # and that took the Contract tab down entirely — found live 14.08.2026 on
    # MO 2026-08-21 500 call, whose 245 snapshots all carry a last_price, no
    # implied volatility and no provider greeks.
    #
    # NaN rather than 0.0, and the distinction is the point: zero below means
    # "this contract has no optionality left", which is a statement about an
    # expired contract and is true. Missing input means "there is nothing to
    # compute from", which is not a value at all — returning 0.0 would draw a
    # flat delta of zero and make an absence of data indistinguishable from a
    # real number.
    # `_is_priceable` performs exactly this rejection and has done since С-16 —
    # it just is not called from here, which is the whole story of this bug: the
    # knowledge existed in the file and was not applied at the one call site
    # that takes its inputs straight from a provider's rows. It cannot simply be
    # called here either, because it also rejects an expired contract, and that
    # case must keep returning zeros (below) rather than NaN.
    if iv is None or spot is None or pd.isna(iv) or pd.isna(spot):
        return dict.fromkeys(_GREEK_KEYS, float("nan"))

    if years_to_expiry <= 0 or iv <= 0 or spot <= 0:
        return {k: 0.0 for k in _GREEK_KEYS}

    q = dividend_yield
    sqrt_t = np.sqrt(years_to_expiry)
    d1 = (np.log(spot / strike) + (risk_free_rate - q + iv ** 2 / 2) * years_to_expiry) / (iv * sqrt_t)
    d2 = d1 - iv * sqrt_t
    pdf_d1 = norm.pdf(d1)
    discount = np.exp(-risk_free_rate * years_to_expiry)
    carry_discount = np.exp(-q * years_to_expiry)

    gamma = carry_discount * pdf_d1 / (spot * iv * sqrt_t)
    vega = spot * carry_discount * pdf_d1 * sqrt_t / 100
    vanna = -carry_discount * pdf_d1 * d2 / iv
    shared_charm = carry_discount * pdf_d1 * (
        2 * (risk_free_rate - q) * years_to_expiry - d2 * iv * sqrt_t
    ) / (2 * years_to_expiry * iv * sqrt_t)

    if option_type == "call":
        delta = carry_discount * norm.cdf(d1)
        theta = (
            -(spot * carry_discount * pdf_d1 * iv) / (2 * sqrt_t)
            - risk_free_rate * strike * discount * norm.cdf(d2)
            + q * spot * carry_discount * norm.cdf(d1)
        ) / 365
        rho = strike * years_to_expiry * discount * norm.cdf(d2) / 100
        charm = q * carry_discount * norm.cdf(d1) - shared_charm
    else:
        delta = carry_discount * (norm.cdf(d1) - 1)
        theta = (
            -(spot * carry_discount * pdf_d1 * iv) / (2 * sqrt_t)
            + risk_free_rate * strike * discount * norm.cdf(-d2)
            - q * spot * carry_discount * norm.cdf(-d1)
        ) / 365
        rho = -strike * years_to_expiry * discount * norm.cdf(-d2) / 100
        charm = -q * carry_discount * norm.cdf(-d1) - shared_charm

    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega, "rho": rho, "vanna": vanna, "charm": charm}


def _mark_unreliable_iv(contract: pd.DataFrame) -> pd.Series:
    """For one contract's full snapshot history (implied_volatility,
    last_price columns, any order), returns implied_volatility with
    unreliable points replaced by NaN.

    First cut of this guard compared each row's IV to an absolute
    Black-Scholes price and flagged it against the reported last_price —
    reverted (found live: real MO LEAPS data) because that needs a
    dividend yield the app doesn't track. Ignoring dividends (q=0, the same
    simplification _black_scholes_greeks already uses) is fine for greeks,
    which are directional/relative, but badly overprices long-dated calls
    on high-yield names in absolute terms — it flagged perfectly good IV
    as "inconsistent" purely because the model itself was wrong, not the
    data. The same real contract also exposed a second issue: for a thin,
    rarely-traded strike, last_price is often just stale (no new trade),
    while implied_volatility keeps updating from live quotes — so even a
    "correct" pricing model has no reliable last_price to check against.

    This version sidesteps both problems by never pricing anything: a
    snapshot's IV is untrusted only if it's a strong outlier versus the
    CONTRACT'S OWN median IV *and* last_price does not corroborate a real
    move of comparable size. Since option price is monotonic in IV (higher
    vol -> higher price, all else equal, for any dividend yield), a genuine
    large IV move always shows up as a real price move too; an IV move with
    no matching price move at all is what the live incident actually looked
    like — a single snapshot where last_price never budged."""
    iv = contract["implied_volatility"]
    price = contract["last_price"]
    valid = iv.notna() & (iv > 0)
    if valid.sum() < config.IV_OUTLIER_MIN_HISTORY_POINTS:
        return iv  # not enough history yet to know what's "typical"

    reference_iv = iv[valid].median()
    reference_price = price[valid].median()
    if not (reference_iv > 0) or not (reference_price > 0):
        return iv

    iv_deviation = (iv - reference_iv).abs() / reference_iv
    price_deviation = (price - reference_price).abs() / reference_price

    is_outlier = iv_deviation > config.IV_OUTLIER_REL_THRESHOLD
    is_corroborated = price_deviation > config.IV_OUTLIER_PRICE_COROBORATION_THRESHOLD
    unreliable = valid & is_outlier & ~is_corroborated

    # A median is only a trustworthy "typical" value while outliers are a
    # small minority of the sample — found live on a short real history (4
    # snapshots, an old stuck IV and the current one split 2-2): the median
    # landed almost exactly between the two clusters, so BOTH looked like
    # outliers from it, and every point in the contract's history would
    # have been suppressed. If suppression would hit too large a share of
    # the valid history, the reference itself can't be trusted — fail open
    # (trust the raw data) rather than blank out a whole contract.
    if unreliable.sum() > config.IV_OUTLIER_MAX_UNRELIABLE_FRACTION * valid.sum():
        return iv

    return iv.where(~unreliable, np.nan)


def _black_scholes_greeks_batch(
    spot: pd.Series, strike: pd.Series, years_to_expiry: pd.Series, iv: pd.Series,
    rate: float | np.ndarray, option_type: pd.Series, dividend_yield: float = 0.0,
) -> pd.DataFrame:
    """Vectorized counterpart of `_black_scholes_greeks` — computes greeks for
    all rows at once (screener, spec FR25) instead of a Python loop over
    contracts. The formulas are identical, just on numpy arrays.

    `rate` may be an array: the risk-free rate depends on time to expiry, and
    a chain priced in one call spans everything from days to years (epic С-16).
    `dividend_yield` must match the one used everywhere else for this ticker —
    the point of the epic is that all greeks in the app share one model."""
    valid = (years_to_expiry > 0) & (iv > 0) & (spot > 0)
    # compute invalid rows on placeholder values (1.0) to avoid log(0)/division
    # by zero — the resulting values are zeroed out below via .where(valid)
    safe_t = years_to_expiry.where(valid, 1.0)
    safe_iv = iv.where(valid, 1.0)
    safe_spot = spot.where(valid, 1.0)

    q = dividend_yield
    sqrt_t = np.sqrt(safe_t)
    d1 = (np.log(safe_spot / strike) + (rate - q + safe_iv ** 2 / 2) * safe_t) / (safe_iv * sqrt_t)
    d2 = d1 - safe_iv * sqrt_t
    pdf_d1 = norm.pdf(d1)
    discount = np.exp(-rate * safe_t)
    carry_discount = np.exp(-q * safe_t)

    gamma = carry_discount * pdf_d1 / (safe_spot * safe_iv * sqrt_t)
    vega = safe_spot * carry_discount * pdf_d1 * sqrt_t / 100
    vanna = -carry_discount * pdf_d1 * d2 / safe_iv
    shared_charm = carry_discount * pdf_d1 * (
        2 * (rate - q) * safe_t - d2 * safe_iv * sqrt_t
    ) / (2 * safe_t * safe_iv * sqrt_t)

    is_call = (option_type == "call").to_numpy()
    delta = np.where(is_call, carry_discount * norm.cdf(d1), carry_discount * (norm.cdf(d1) - 1))
    theta = np.where(
        is_call,
        (-(safe_spot * carry_discount * pdf_d1 * safe_iv) / (2 * sqrt_t)
         - rate * strike * discount * norm.cdf(d2)
         + q * safe_spot * carry_discount * norm.cdf(d1)) / 365,
        (-(safe_spot * carry_discount * pdf_d1 * safe_iv) / (2 * sqrt_t)
         + rate * strike * discount * norm.cdf(-d2)
         - q * safe_spot * carry_discount * norm.cdf(-d1)) / 365,
    )
    rho = np.where(
        is_call,
        strike * safe_t * discount * norm.cdf(d2) / 100,
        -strike * safe_t * discount * norm.cdf(-d2) / 100,
    )
    # With q != 0 charm no longer coincides for calls and puts (see the scalar
    # version) — it must be split here too, or the screener and the Contract
    # tab would disagree on the same contract.
    charm = np.where(
        is_call,
        q * carry_discount * norm.cdf(d1) - shared_charm,
        -q * carry_discount * norm.cdf(-d1) - shared_charm,
    )

    result = pd.DataFrame(
        {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega, "rho": rho, "vanna": vanna, "charm": charm},
        index=spot.index,
    )
    return result.where(valid, 0.0)


def screener_table(df: pd.DataFrame, pricing: PricingInputs = DEFAULT_PRICING) -> pd.DataFrame:
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
    years = years_to_expiry_series(snapshot["expiry"], snapshot_date)

    greeks = _black_scholes_greeks_batch(
        snapshot["underlying_price"], snapshot["strike"], years,
        snapshot["implied_volatility"], pricing.rate_series(years),
        snapshot["option_type"], dividend_yield=pricing.dividend_yield,
    )
    snapshot = snapshot.reset_index(drop=True)
    greeks = merge_provider_greeks(greeks.reset_index(drop=True), snapshot)
    # Drop the raw provider columns before concat: they carry the same names as
    # the merged ones, and duplicate column labels turn every later `table[col]`
    # into a DataFrame instead of a Series (caught by the screener smoke test).
    table = pd.concat(
        [snapshot.drop(columns=[g for g in PROVIDER_GREEKS if g in snapshot.columns]), greeks],
        axis=1,
    )
    columns = [
        "expiry", "strike", "option_type", "dte", "last_price", "open_interest",
        "implied_volatility", *_GREEK_KEYS,
    ]
    return table[columns].sort_values(["expiry", "strike", "option_type"]).reset_index(drop=True)


def gamma_exposure_profile(
    df: pd.DataFrame,
    expiry: pd.Timestamp,
    pricing: PricingInputs = DEFAULT_PRICING,
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
    years = years_to_expiry(expiry, snapshot_date)

    snapshot["gamma"] = snapshot.apply(
        lambda row: _black_scholes_greeks(
            spot, row["strike"], years, row["implied_volatility"],
            pricing.rate_for(years), row["option_type"],
            dividend_yield=pricing.dividend_yield,
        )["gamma"],
        axis=1,
    )
    snapshot["contract_gex"] = snapshot["gamma"] * snapshot["open_interest"].fillna(0) * 100 * spot
    snapshot.loc[snapshot["option_type"] == "put", "contract_gex"] *= -1

    profile = snapshot.groupby("strike", as_index=False)["contract_gex"].sum()
    return profile.rename(columns={"contract_gex": "gex"})


def contracts_backing_expiry(
    df: pd.DataFrame, expiry: pd.Timestamp, as_of: pd.Timestamp | None = None
) -> int:
    """How many contracts of this expiry carry open interest in the snapshot.

    Max Pain and GEX are both weighted by open interest, which makes them immune
    to junk in the chain — a contract with zero OI contributes exactly zero — but
    also means the answer can rest on a handful of contracts while looking
    identical to one resting on hundreds. This is that count, so the UI can say
    which it is.

    Measured live on CPER, 11.08: three newly listed 2027 expiries arrived
    carrying 80, 80 and 60 contracts of which 3, 1 and 1 had any open interest.
    Each produced a confident "Max Pain 21.0" next to a 33.0 built from 56
    contracts, with nothing on screen to tell them apart. Both numbers were
    arithmetically correct; one of them was noise.

    Counts CONTRACTS rather than summing open interest on purpose. Max Pain is
    the shape of a payout curve across strikes, so what it needs is enough
    populated strikes for a shape to exist; total open interest describes how big
    the positions are, not how well the curve is resolved."""
    snapshot_date = as_of if as_of is not None else df["collected_at"].max()
    snapshot = df[(df["collected_at"] == snapshot_date) & (df["expiry"] == expiry)]
    if snapshot.empty:
        return 0
    return int((snapshot["open_interest"].fillna(0) > 0).sum())


def net_gamma_exposure(gex_profile: pd.DataFrame) -> float:
    """Total net GEX for an expiry (spec FR15) — the sign defines the regime:
    positive — dealers dampen price moves, negative — they amplify them."""
    if gex_profile.empty:
        return 0.0
    return float(gex_profile["gex"].sum())


def gex_matrix(
    df: pd.DataFrame,
    as_of: pd.Timestamp | None = None,
    pricing: PricingInputs = DEFAULT_PRICING,
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
        profile = gamma_exposure_profile(df, expiry, pricing, as_of=snapshot_date)
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
    pricing: PricingInputs = DEFAULT_PRICING,
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
            "net_gex": net_gamma_exposure(gamma_exposure_profile(df, expiry, pricing, as_of=snapshot_date)),
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
    pricing: PricingInputs = DEFAULT_PRICING,
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
    matrix = gex_matrix(df, as_of, pricing, expiries=expiries)
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


_CONTRACT_KEYS = ["expiry", "strike", "option_type"]


def volume_stats(history: pd.DataFrame) -> pd.DataFrame:
    """Per-contract volume mean, sample deviation and point count over daily
    history — the only part of `unusual_activity` whose cost grows with how
    long the installation has been collecting.

    Split out so that a caller who can compute the same three numbers more
    cheaply may hand them in (see `unusual_activity(stats=...)`), while the
    rules that decide what counts as unusual stay here, in one place, for both
    products. Aggregates are cheap to reimplement and easy to verify against
    each other; product rules are neither.

    Counting rules that any other implementation has to match: a contract with
    no volume recorded for a day does not contribute a point (the count is over
    non-null volumes, not over days), and the deviation is the SAMPLE one — one
    observation therefore yields NaN rather than 0, which is what stops a
    single-point history from producing an infinite z-score."""
    daily = _last_snapshot_per_day(history)
    if daily.empty:
        return pd.DataFrame(columns=[*_CONTRACT_KEYS, "avg_volume", "std_volume", "history_points"])
    return (
        daily.groupby(_CONTRACT_KEYS)["volume"]
        .agg(avg_volume="mean", std_volume="std", history_points="count")
        .reset_index()
    )


def unusual_activity(
    df: pd.DataFrame,
    z_threshold: float = config.UNUSUAL_Z_THRESHOLD,
    min_volume: int = config.UNUSUAL_MIN_VOLUME,
    min_history_points: int = config.UNUSUAL_MIN_HISTORY_POINTS,
    stats: pd.DataFrame | None = None,
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

    # `stats` supplied means the caller aggregated the history itself — the
    # hosted product does it in SQL, because in pandas it needs every daily
    # snapshot of the window loaded first. `df` then only has to carry the
    # latest snapshot. Everything below is unchanged either way, which is the
    # point: what is unusual is decided here for both products.
    if stats is None:
        # Whole calendar days only, not "everything before the latest moment".
        # Volume is cumulative within a session, so an earlier collection of
        # today is a PARTIAL day, and letting it into the baseline compares
        # today against a fraction of itself — the same mixing of moments
        # within a trading day that the daily collapse exists to prevent. The
        # hosted product's stored statistics are built the same way; the rule
        # has to match, or the two products call different things unusual.
        stats = volume_stats(df[df["collected_at"].dt.normalize() < latest_date.normalize()])
    latest = latest.merge(stats, on=_CONTRACT_KEYS, how="left")
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
        # Rows without an implied volatility are dropped, not zero-weighted.
        # np.average computes sum(a*w)/sum(w), and NaN*0 is still NaN — so a
        # single contract with a missing IV poisons the whole day's average
        # even when its weight is zero. Harmless while the data source filled
        # IV on every contract (Yahoo did); the moment Massive became an option
        # it emptied the chart completely, since it legitimately reports no IV
        # for ~15% of a chain (deep ITM/OTM, nothing traded).
        usable = group[["implied_volatility", "volume"]].dropna(subset=["implied_volatility"])
        weights = usable["volume"].fillna(0)
        if usable.empty or weights.sum() == 0:
            return np.nan
        return np.average(usable["implied_volatility"], weights=weights)

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
    pricing: PricingInputs = DEFAULT_PRICING,
) -> pd.DataFrame:
    """Price, IV, and full-greeks history of a specific contract across
    collection dates (spec FR14). Replaces the former iv_by_contract — the
    same drill-down, plus the full set of greeks."""
    contract = df[
        (df["strike"] == strike) & (df["expiry"] == expiry) & (df["option_type"] == option_type)
    ].sort_values("collected_at")
    if contract.empty:
        return pd.DataFrame(columns=["collected_at", "last_price", "implied_volatility", *_GREEK_KEYS])

    # Data-quality guard (see _mark_unreliable_iv): an IV that's a strong
    # outlier vs. this contract's own history, uncorroborated by any real
    # move in last_price, is untrustworthy for that one snapshot — every
    # greek below is derived from `iv`, not the raw row value, so the whole
    # row's worth of derived numbers gaps out together rather than one field
    # silently lying.
    reliable_iv = _mark_unreliable_iv(contract)

    records = []
    for row in contract.itertuples():
        years = years_to_expiry(expiry, row.collected_at)
        iv = reliable_iv[row.Index]
        greeks = _black_scholes_greeks(
            row.underlying_price, strike, years, iv,
            pricing.rate_for(years), option_type,
            dividend_yield=pricing.dividend_yield,
        )
        # Provider greeks win where this snapshot carries them (epic С-16);
        # rho/vanna/charm are always ours, the provider never supplies them.
        for greek in PROVIDER_GREEKS:
            supplied = getattr(row, greek, None)
            if supplied is not None and not pd.isna(supplied):
                greeks[greek] = float(supplied)
        records.append({
            "collected_at": row.collected_at,
            "last_price": row.last_price,
            "implied_volatility": iv,
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
        # An untrustworthy IV (see contract_greeks_history's data-quality
        # guard) makes this and/or the prior row's greeks NaN — diff is then
        # NaN too, and NaN comparisons are always False, which would silently
        # fall through to claiming "down" for what's actually just missing
        # data. Say so plainly instead of guessing a direction.
        if pd.isna(diff):
            return " (not enough reliable data to compare with the previous snapshot)"
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
    otm["years_to_expiry"] = years_to_expiry_series(otm["expiry"], latest_date)
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
