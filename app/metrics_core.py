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
  5. English only, everywhere in this file, including comments and
     docstrings — no exceptions for internal item or epic references. One
     repository's internal numbering means nothing in the other, and
     `gammagrid` is a public, English-only project; a note that made sense
     in one repo's history reads as noise or as leaked internals in the
     other. Say what and why in plain prose instead of citing a ticket.
"""

from __future__ import annotations

import datetime as dt
import functools
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from scipy.interpolate import griddata
from scipy.special import ndtr

from app import config

MARKET_TZ = ZoneInfo("America/New_York")


def market_dates(collected_at: pd.Series) -> pd.Series:
    """`collected_at` as New York calendar dates.

    Timestamps are stored naive in UTC throughout both products, so a naive
    series is read as UTC and converted; an aware one is only converted. The
    distinction is not cosmetic — a collection at 01:15 UTC on Saturday is
    Friday 21:15 in New York, which is the evening of that trading day and the
    most complete snapshot it has.
    """
    values = pd.to_datetime(collected_at)
    if values.dt.tz is None:
        values = values.dt.tz_localize("UTC")
    return values.dt.tz_convert(MARKET_TZ)


def _last_snapshot_per_day(df: pd.DataFrame) -> pd.DataFrame:
    """Collapses multiple intraday collections down to one (the latest) per
    TRADING day. Needed wherever values that aren't comparable within a day
    get compared: Yahoo's volume is cumulative since session open (grows until
    the close), while open_interest barely updates intraday — without this
    collapse, comparing "the last two snapshots" may compare two snapshots of
    the same day instead of day over day.

    Two rules, both measured rather than assumed:

    A day is New York's, not UTC's. Grouping by the UTC date files Friday's
    post-close snapshot under Saturday and then treats Friday's 20:00 UTC
    collection as its last one.

    Days when the market never opened do not count. Between two weekend
    snapshots of SPY not one of 14,230 contracts changed its price, volume or
    open interest — Saturday and Sunday are copies of Friday. Counted as days
    they make day-over-day comparisons return "nothing changed" and give a
    volume baseline two sevenths made of repetitions, which pulls the mean
    toward Friday and understates the spread.
    """
    local = market_dates(df["collected_at"])
    trading = df[local.dt.dayofweek < 5]
    if trading.empty:
        return trading
    daily_latest = trading.groupby(market_dates(trading["collected_at"]).dt.date)[
        "collected_at"
    ].transform("max")
    return trading[trading["collected_at"] == daily_latest]


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


def max_pain_series(df: pd.DataFrame) -> pd.DataFrame:
    """Max pain for EVERY (moment, expiry) in the frame — spec FR5, generalised
    over time (tier 2 of R-01.1).

    THE DEFINITION LIVES HERE and `max_pain` below is a lookup into it. Two
    implementations of "the strike at which sellers pay least" is exactly the
    kind of pair that agrees on the day it is written and disagrees a year
    later, and the historical rollup has to be the same number the tab shows or
    it is not preserving anything.

    THE ARITHMETIC IS A MATRIX PRODUCT rather than a loop over settlement
    prices, and that is what makes the historical pass affordable. Payout to
    call holders if the underlying settles at strike Kⱼ is Σᵢ max(Kⱼ − Kᵢ, 0)·cᵢ
    — a matrix M[j,i] = max(Kⱼ − Kᵢ, 0) times the call open-interest vector.
    Puts are the same matrix transposed. Since the strike grid is shared by
    every moment of one expiry, M is built once and multiplied by an entire
    (strikes × moments) table of open interest in one operation. The loop
    version was O(strikes²) in Python per moment; over ~100k moment-expiry
    pairs it does not finish in a useful time.

    ZERO OPEN INTEREST YIELDS NaN, NOT A STRIKE, and this is a deliberate
    change from the previous behaviour rather than an accident of the rewrite.
    With no open interest anywhere, every settlement price has a payout of
    exactly zero, and picking the minimum of all-equal values returned the
    LOWEST STRIKE IN THE CHAIN — a specific, confident, meaningless number.
    Newly listed expiries are in that state routinely. On the tab it showed
    under a "too little open interest" warning; in a stored series it would
    become a permanent artefact nobody could tell from a real reading.
    """
    columns = ["collected_at", "expiry", "max_pain"]
    if df is None or df.empty:
        return pd.DataFrame(columns=columns)

    frame = df[["collected_at", "expiry", "strike", "option_type", "open_interest"]].copy()
    frame["_oi"] = pd.to_numeric(frame["open_interest"], errors="coerce").fillna(0.0)
    rows = []
    for expiry, group in frame.groupby("expiry", sort=True):
        # Both sides on one strike grid and one moment grid, so the matrix
        # below is built once per expiry and reused across every moment of it.
        def side(kind: str) -> pd.DataFrame:
            part = group[group["option_type"] == kind]
            if part.empty:
                return pd.DataFrame()
            return part.pivot_table(
                index="strike", columns="collected_at", values="_oi",
                aggfunc="sum", fill_value=0.0,
            )

        calls, puts = side("call"), side("put")
        strikes = sorted(set(calls.index) | set(puts.index))
        moments = sorted(set(calls.columns) | set(puts.columns))
        if not strikes or not moments:
            continue
        call_oi = calls.reindex(index=strikes, columns=moments, fill_value=0.0).to_numpy(dtype=float)
        put_oi = puts.reindex(index=strikes, columns=moments, fill_value=0.0).to_numpy(dtype=float)
        call_oi = np.nan_to_num(call_oi)
        put_oi = np.nan_to_num(put_oi)

        grid = np.asarray(strikes, dtype=float)
        payout_matrix = np.maximum(grid[:, None] - grid[None, :], 0.0)
        payouts = payout_matrix @ call_oi + payout_matrix.T @ put_oi
        chosen = grid[payouts.argmin(axis=0)]
        # See the docstring: no open interest, no answer.
        empty = (call_oi.sum(axis=0) + put_oi.sum(axis=0)) <= 0
        chosen = np.where(empty, np.nan, chosen)
        rows.append(pd.DataFrame(
            {"collected_at": moments, "expiry": expiry, "max_pain": chosen}
        ))
    if not rows:
        return pd.DataFrame(columns=columns)
    return pd.concat(rows, ignore_index=True)[columns]


def max_pain(df: pd.DataFrame, expiry: pd.Timestamp) -> float | None:
    """The strike with the minimal total payout by option sellers for the given
    expiry, based on the latest available snapshot (spec FR5).

    A lookup into `max_pain_series`, which holds the definition — see there for
    why, and for the one behaviour that changed: an expiry carrying no open
    interest at all now answers None instead of naming its lowest strike."""
    if df is None or df.empty:
        return None
    latest_date = df["collected_at"].max()
    snapshot = df[(df["collected_at"] == latest_date) & (df["expiry"] == expiry)]
    if snapshot.empty:
        return None
    answer = max_pain_series(snapshot)
    if answer.empty or pd.isna(answer["max_pain"].iloc[0]):
        return None
    return float(answer["max_pain"].iloc[0])


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


@functools.lru_cache(maxsize=4096)
def _close_of(date: dt.date) -> pd.Timestamp:
    """16:00 New York on `date`, as naive UTC.

    Memoised because the callers map every row of a chain through it, and a
    chain of ten thousand rows carries only a few dozen distinct expiries: the
    timezone arithmetic below was a quarter of a cold GEX heatmap build,
    repeated for answers already known. Keyed by the calendar date, not by the
    caller's value: two tz-aware timestamps of the same instant in different
    zones are equal and hash alike, yet name different calendar dates, so a
    cache keyed by the timestamp answered the second with the first one's
    close — a day off, depending on call order."""
    local = dt.datetime.combine(date, EXPIRY_CLOSE_ET, tzinfo=EXPIRY_TZ)
    return pd.Timestamp(local.astimezone(dt.timezone.utc)).tz_localize(None)


EXPIRY_OPEN_ET = dt.time(9, 30)


# Index options whose STANDARD monthly series settles on the opening print of
# the expiration date (the Special Opening Quotation), not at the close. Their
# OCC root is the index itself; every sibling series — weeklies, dailies and
# PM-settled monthlies (SPXW, NDXP, RUTW, DJXW) and the mini index XSP — has a
# root of its own and settles at the close. Checked against Cboe's product
# specifications: DJX lists third-Friday AM monthlies beside PM weeklies.
AM_SETTLED_ROOTS = frozenset({"SPX", "NDX", "RUT", "DJX"})


# The first five characters of an AM-settled symbol: root plus the first digit
# of the date, with and without the `O:` prefix one provider writes. Matched as
# a fixed-width prefix in C — a regex per row cost a third of an SPX chain's
# fold elsewhere in this codebase.
_AM_PREFIXES = tuple(f"{root}{digit}" for root in sorted(AM_SETTLED_ROOTS) for digit in range(10))
_AM_PREFIXES_TAGGED = tuple(f"O:{prefix}" for prefix in _AM_PREFIXES)


@functools.lru_cache(maxsize=4096)
def _open_of(date: dt.date) -> pd.Timestamp:
    """09:30 New York on `date`, as naive UTC — memoised and keyed by the
    calendar date for the same reasons as `_close_of`."""
    local = dt.datetime.combine(date, EXPIRY_OPEN_ET, tzinfo=EXPIRY_TZ)
    return pd.Timestamp(local.astimezone(dt.timezone.utc)).tz_localize(None)


def _good_friday(year: int) -> dt.date:
    """Easter Sunday by the anonymous Gregorian algorithm, minus two days."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month, day = divmod(h + m - 7 * n + 114, 31)
    return dt.date(year, month, day + 1) - dt.timedelta(days=2)


def _exchange_holiday_on_third_friday(friday: dt.date) -> bool:
    """Whether the exchange is closed on this third Friday. Only two holidays
    can land there (the 15th to the 21st): Good Friday, and Juneteenth — on
    the 19th itself, or observed on Friday the 18th when the 19th is a
    Saturday. A closure nobody can schedule (a day of mourning) is beyond any
    calendar; the contract symbol, where it is present, still decides."""
    if friday == _good_friday(friday.year):
        return True
    if friday.month == 6 and friday.year >= 2022:
        juneteenth = dt.date(friday.year, 6, 19)
        observed = juneteenth - dt.timedelta(days=1) if juneteenth.weekday() == 5 else juneteenth
        return friday == observed
    return False


@functools.lru_cache(maxsize=4096)
def _is_monthly_expiry(date: dt.date) -> bool:
    """Whether `date` is the standard monthly expiration: the third Friday of
    its month, or the Thursday before it when the exchange is closed that
    Friday. An ordinary Thursday before a third Friday is a daily PM series
    and is NOT monthly."""
    first = date.replace(day=1)
    friday = first + dt.timedelta(days=(4 - first.weekday()) % 7 + 14)
    if _exchange_holiday_on_third_friday(friday):
        return date == friday - dt.timedelta(days=1)
    return date == friday


def _index_root(ticker) -> str | None:
    """The ticker as an OCC root: `^SPX`, `$SPX` and `spx` all read `SPX`."""
    if not isinstance(ticker, str) or not ticker.strip():
        return None
    return ticker.strip().upper().lstrip("^$")


def _ticker_of(frame: pd.DataFrame, ticker=None) -> str | None:
    """The ticker a chain belongs to: the caller's, else the frame's own
    `ticker` column (a chain is one ticker's)."""
    if ticker is not None:
        return ticker
    if "ticker" in frame.columns and len(frame):
        named = frame["ticker"].dropna()
        return str(named.iloc[0]) if len(named) else None
    return None


# The shortest OCC symbol: a one-letter root, six digits of date, the type and
# eight digits of strike. Anything shorter is a label rather than a contract's
# symbol and is read as no symbol at all.
_OCC_MIN_LENGTH = 16


def _symbol_code(symbol) -> int:
    """1 — the symbol names an AM-settled root, 2 — it names any other root,
    0 — there is no symbol, and the ticker-and-date rule decides."""
    if not isinstance(symbol, str) or len(symbol) < _OCC_MIN_LENGTH:
        return 0
    return 1 if symbol.startswith(_AM_PREFIXES) or symbol.startswith(_AM_PREFIXES_TAGGED) else 2


def _settlement_codes(symbols, index: pd.Index) -> np.ndarray:
    """`_symbol_code` for every row, matched as fixed-width prefixes in C."""
    if symbols is None:
        return np.zeros(len(index), dtype=np.int8)
    # OCC symbols are upper-case as every provider writes them; a missing
    # one becomes the empty string, which no prefix matches.
    full = pd.Series(symbols, index=index).fillna("").to_numpy(dtype="<U24")
    tagged = full.astype("<U6")
    am = np.isin(tagged.astype("<U4"), _AM_PREFIXES) | np.isin(tagged, _AM_PREFIXES_TAGGED)
    known = np.char.str_len(full) >= _OCC_MIN_LENGTH
    return np.where(am, 1, np.where(known, 2, 0)).astype(np.int8)


def _settles_at_open(date: dt.date, code: int, index_root: str | None) -> bool:
    # A known ticker outside the list settles at the close whatever its rows
    # say: its chain cannot hold an index's AM series, and not reading the
    # symbols of every other chain is what keeps the common path free.
    if index_root is not None and index_root not in AM_SETTLED_ROOTS:
        return False
    if code:
        return code == 1
    return index_root in AM_SETTLED_ROOTS and _is_monthly_expiry(date)


def _expiry_moment(expiry, contract_symbol=None, ticker=None, as_of=None) -> pd.Timestamp:
    """The instant a contract's life ends, as naive UTC: its settlement.

    16:00 New York on the expiration date for nearly everything. The standard
    monthly series of the indices in AM_SETTLED_ROOTS settle on the OPENING
    print of that date instead, 09:30 — measuring them to 16:00 adds six and
    a half hours to the last day, a quarter to a third of all the time left
    on the Thursday before, and keeps a series that has already settled alive
    through Friday morning.

    Which series a row is: its contract symbol, when the caller has one — the
    root decides, whatever the date (unless the ticker is known and is not one
    of the listed indices: then the close). Without a symbol, the ticker and the date
    decide: an index from the list on its monthly expiration date is taken as
    the AM series — but only while it is still alive: observed (`as_of`) at or
    after the open of that date, the row is read as the PM series of the same
    strike, which trades until the close. A stored chain keeps one row per
    strike, and where both series list it that row is the AM one; once the AM
    series has settled, it is the best stand-in the chain has for the PM
    contract beside it, and pricing it as settled would zero the whole date
    on the day it matters most. With neither symbol nor ticker, the close, as
    before — which is also the right answer to "is this DATE still trading".

    The date is read in the expiry's own zone. A missing expiry (None, NaN,
    NaT) has no moment and gives NaT, so the time left is NaN like any other
    missing input — `NaT.date()` does not raise, and combining it with New
    York used to place the close in year 1 and price the contract as long
    expired."""
    stamp = pd.Timestamp(expiry)
    if pd.isna(stamp):
        return pd.NaT
    date = stamp.date()
    code = _symbol_code(contract_symbol)
    if _settles_at_open(date, code, _index_root(ticker)):
        if code == 0 and as_of is not None and _as_naive_utc(as_of) >= _open_of(date):
            return _close_of(date)
        return _open_of(date)
    return _close_of(date)


def _as_naive_utc(moment) -> pd.Timestamp:
    """Everything stored is naive UTC, but callers reach for whatever is handy
    — `pd.Timestamp.utcnow()` is tz-aware and subtracting it raises. Normalize
    here rather than at each call site, where the mistake is invisible until
    it throws."""
    stamp = pd.Timestamp(moment)
    return stamp.tz_convert("UTC").tz_localize(None) if stamp.tzinfo is not None else stamp


def years_to_expiry(expiry, as_of, contract_symbol=None, ticker=None) -> float:
    """Time to expiry in years, measured to the contract's settlement — the
    16:00 ET close, or the 09:30 open for an AM-settled index series
    (`_expiry_moment`). Without a symbol or a ticker: the close, which is
    when the expiration DATE stops trading.

    Replaces `(expiry - as_of).days / 365`, which was wrong twice over
    (reported live): it compared against MIDNIGHT of the expiration date, so a
    contract still trading through the session already counted as expired and
    every greek collapsed to zero for the whole day; and truncating to whole
    days understated the remaining life by up to 24 hours. Both errors are
    negligible on LEAPS and dominant on the near-dated contracts where gamma
    is largest — which is exactly what the GEX tab is for.

    Negative once trading has stopped, which callers rely on to tell a
    finished contract from a live one."""
    return (_expiry_moment(expiry, contract_symbol, ticker, as_of) - _as_naive_utc(as_of)).total_seconds() / _YEAR_SECONDS


def _expiry_moments(expiry, symbols=None, ticker=None, as_of=None) -> pd.Series:
    """`_expiry_moment` for every row, computed once per distinct expiry:
    the same values, index and dtype as a row-by-row map. `symbols` is the
    rows' contract symbols (or None), `ticker` the chain's, `as_of` the
    instant each row was observed — one for all, or one per row (or None)."""
    series = pd.Series(expiry)
    if series.empty:
        return pd.to_datetime(series.map(_expiry_moment))
    codes, distinct = pd.factorize(series, use_na_sentinel=False)
    closes = pd.to_datetime(pd.Series([_expiry_moment(value) for value in distinct])).to_numpy()
    index_root = _index_root(ticker)
    if (symbols is None or index_root is not None) and index_root not in AM_SETTLED_ROOTS:
        # Every chain that is not one of the listed indices: nothing settles
        # at the open, and nothing below is worth its cost.
        return pd.Series(closes[codes], index=series.index)
    rows = _settlement_codes(symbols, series.index)
    dates = [None if pd.isna(stamp := pd.Timestamp(value)) else stamp.date() for value in distinct]
    opens = pd.to_datetime(pd.Series(
        [pd.NaT if date is None else _open_of(date) for date in dates]
    )).to_numpy().astype(closes.dtype)
    by_rule = np.array([date is not None and _settles_at_open(date, 0, index_root) for date in dates])[codes]
    if as_of is not None and by_rule.any():
        if not np.ndim(as_of):
            when = np.datetime64(_as_naive_utc(as_of))
        else:
            # Positional: a per-row `as_of` may carry an index of its own. A
            # naive datetime column is taken as it is, without a copy.
            when = np.asarray(as_of)
            if when.dtype.kind != "M":
                stamps = pd.to_datetime(pd.Series(when))
                if stamps.dt.tz is not None:
                    stamps = stamps.dt.tz_convert("UTC").dt.tz_localize(None)
                when = stamps.to_numpy()
        by_rule &= ~(when >= opens[codes])
    moments = np.where(
        rows == 1, opens[codes],
        np.where(rows == 2, closes[codes], np.where(by_rule, opens[codes], closes[codes])),
    )
    return pd.Series(moments, index=series.index)


def _frame_expiry_moments(frame: pd.DataFrame, ticker=None) -> pd.Series:
    """`_expiry_moments` for a chain: its own contract symbols where it
    carries them, its ticker from the caller or from its `ticker` column."""
    return _expiry_moments(frame["expiry"], frame.get("contract_symbol"), _ticker_of(frame, ticker),
                           frame.get("collected_at"))


def years_to_expiry_series(expiry: pd.Series, as_of, symbols=None, ticker=None) -> pd.Series:
    """Vectorized counterpart — the screener and GEX price whole chains."""
    moments = _expiry_moments(expiry, symbols, ticker, as_of)
    return (moments - _as_naive_utc(as_of)).dt.total_seconds() / _YEAR_SECONDS


def years_to_expiry_by_moment(expiry, moments: pd.Series, contract_symbol=None, ticker=None) -> pd.Series:
    """The other vectorized shape: ONE contract observed at many moments —
    a contract's history — where `years_to_expiry_series` is many contracts
    at one moment. Row for row what `years_to_expiry(expiry, moment)` gives,
    by the same definition (the contract's settlement, naive UTC)."""
    when = pd.to_datetime(pd.Series(moments))
    if when.dt.tz is not None:
        when = when.dt.tz_convert("UTC").dt.tz_localize(None)
    moments = _expiry_moments(pd.Series(expiry, index=when.index), None if contract_symbol is None else
                              pd.Series(contract_symbol, index=when.index), ticker, when)
    return (moments - when).dt.total_seconds() / _YEAR_SECONDS


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
    """The (r, q) pair every greek in this app is computed with.

    Passed around as one object rather than two floats because they must move
    together: mixing a curve rate with a zero dividend yield is precisely the
    inconsistency this exists to remove. The rate depends on time to
    expiry, so it is resolved per contract, not once per call.

    The default instance keeps the old behaviour — flat config rate, no
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
    """Provider greeks win where present, ours fill the gaps.

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



_SQRT_2PI = np.sqrt(2 * np.pi)


def _norm_cdf(x):
    """Standard normal CDF — scipy's own kernel, without the distribution
    object's argument checks around it, which cost more than the kernel."""
    return ndtr(np.asarray(x, dtype=float))


def _norm_pdf(x):
    """Standard normal density, the expression scipy evaluates."""
    return np.exp(-np.asarray(x, dtype=float) ** 2 / 2.0) / _SQRT_2PI


def _black_scholes_greeks(
    spot: float, strike: float, years_to_expiry: float, iv: float, risk_free_rate: float,
    option_type: str, dividend_yield: float = 0.0,
) -> dict[str, float]:
    """All greeks via generalized Black-Scholes with a continuous dividend
    yield. Theta and charm are per calendar day and per year respectively;
    vega/rho are per 1 pp change in IV/rate (the units traders actually use,
    not raw per-year partial derivatives).

    `dividend_yield` was added later. It defaults to 0 only so that
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
    # `_is_priceable` performs exactly this rejection and has done for a while —
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
    pdf_d1 = _norm_pdf(d1)
    discount = np.exp(-risk_free_rate * years_to_expiry)
    carry_discount = np.exp(-q * years_to_expiry)

    gamma = carry_discount * pdf_d1 / (spot * iv * sqrt_t)
    vega = spot * carry_discount * pdf_d1 * sqrt_t / 100
    vanna = -carry_discount * pdf_d1 * d2 / iv
    shared_charm = carry_discount * pdf_d1 * (
        2 * (risk_free_rate - q) * years_to_expiry - d2 * iv * sqrt_t
    ) / (2 * years_to_expiry * iv * sqrt_t)

    if option_type == "call":
        delta = carry_discount * _norm_cdf(d1)
        theta = (
            -(spot * carry_discount * pdf_d1 * iv) / (2 * sqrt_t)
            - risk_free_rate * strike * discount * _norm_cdf(d2)
            + q * spot * carry_discount * _norm_cdf(d1)
        ) / 365
        rho = strike * years_to_expiry * discount * _norm_cdf(d2) / 100
        charm = q * carry_discount * _norm_cdf(d1) - shared_charm
    else:
        delta = carry_discount * (_norm_cdf(d1) - 1)
        theta = (
            -(spot * carry_discount * pdf_d1 * iv) / (2 * sqrt_t)
            + risk_free_rate * strike * discount * _norm_cdf(-d2)
            - q * spot * carry_discount * _norm_cdf(-d1)
        ) / 365
        rho = -strike * years_to_expiry * discount * _norm_cdf(-d2) / 100
        charm = -q * carry_discount * _norm_cdf(-d1) - shared_charm

    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega, "rho": rho, "vanna": vanna, "charm": charm}


def _mark_unreliable_iv(contract: pd.DataFrame) -> pd.Series:
    """For one contract's full snapshot history (implied_volatility,
    last_price columns, any order), returns implied_volatility with
    unreliable points replaced by NaN.

    First cut of this guard compared each row's IV to an absolute
    Black-Scholes price and flagged it against the reported last_price —
    reverted (found live: real MO LEAPS data) because at the time the app
    tracked no dividend yield and priced everything at q=0, which badly
    overprices long-dated calls on high-yield names in absolute terms — it
    flagged perfectly good IV as "inconsistent" purely because the model
    itself was wrong, not the data. (The greeks have since gained a
    real dividend yield; this guard was not moved back onto pricing, because
    of the second problem below, which no yield fixes.) The same real
    contract also exposed that second issue: for a thin,
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
    a chain priced in one call spans everything from days to years.
    `dividend_yield` must match the one used everywhere else for this ticker —
    the point is that all greeks in the app share one model."""
    # EVERY NUMERIC INPUT IS COERCED FIRST, and this is not belt-and-braces: a
    # column that is entirely NULL in the database comes back as dtype `object`,
    # and object arithmetic here produces object arrays that fail further down
    # with an unreadable message about `isnan` and casting rules. The
    # caller cannot always know — the value may have travelled through a
    # `where` that quietly changed its dtype — so the function that needs floats
    # is the one that insists on them.
    spot = pd.to_numeric(spot, errors="coerce")
    strike = pd.to_numeric(strike, errors="coerce")
    years_to_expiry = pd.to_numeric(years_to_expiry, errors="coerce")
    iv = pd.to_numeric(iv, errors="coerce")

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
    pdf_d1 = _norm_pdf(d1)
    discount = np.exp(-rate * safe_t)
    carry_discount = np.exp(-q * safe_t)

    gamma = carry_discount * pdf_d1 / (safe_spot * safe_iv * sqrt_t)
    vega = safe_spot * carry_discount * pdf_d1 * sqrt_t / 100
    vanna = -carry_discount * pdf_d1 * d2 / safe_iv
    shared_charm = carry_discount * pdf_d1 * (
        2 * (rate - q) * safe_t - d2 * safe_iv * sqrt_t
    ) / (2 * safe_t * safe_iv * sqrt_t)

    is_call = (option_type == "call").to_numpy()
    delta = np.where(is_call, carry_discount * _norm_cdf(d1), carry_discount * (_norm_cdf(d1) - 1))
    theta = np.where(
        is_call,
        (-(safe_spot * carry_discount * pdf_d1 * safe_iv) / (2 * sqrt_t)
         - rate * strike * discount * _norm_cdf(d2)
         + q * safe_spot * carry_discount * _norm_cdf(d1)) / 365,
        (-(safe_spot * carry_discount * pdf_d1 * safe_iv) / (2 * sqrt_t)
         + rate * strike * discount * _norm_cdf(-d2)
         - q * safe_spot * carry_discount * _norm_cdf(-d1)) / 365,
    )
    rho = np.where(
        is_call,
        strike * safe_t * discount * _norm_cdf(d2) / 100,
        -strike * safe_t * discount * _norm_cdf(-d2) / 100,
    )
    # With q != 0 charm no longer coincides for calls and puts (see the scalar
    # version) — it must be split here too, or the screener and the Contract
    # tab would disagree on the same contract.
    charm = np.where(
        is_call,
        q * carry_discount * _norm_cdf(d1) - shared_charm,
        -q * carry_discount * _norm_cdf(-d1) - shared_charm,
    )

    result = pd.DataFrame(
        {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega, "rho": rho, "vanna": vanna, "charm": charm},
        index=spot.index,
    )
    return result.where(valid, 0.0)


# Tickers whose options this solver must NOT touch, and the reason is not
# fussiness.
#
# VIX options are written on the VIX FUTURE of the matching series, not on the
# index. Inverting Black-Scholes against the index spot substitutes a different
# underlying: the answer comes out plausible, is wrong, and is wrong by more
# the further out the expiry — precisely where a wrong number looks most like
# an insight. The futures curve is not something this product collects, so the
# honest position is to have no volatility for VIX rather than an invented one.
#
# SPX, XSP and NDX are the opposite case and are deliberately absent from this
# list: European exercise, cash settled on the index itself, which is exactly
# what generalised Black-Scholes prices. They are the one place in this product
# where the model is exact rather than an approximation.
IV_SOLVER_EXCLUDED_TICKERS = frozenset({"VIX", "VIXW"})

# Bounds for the search. 0.01% and 500% are not opinions about markets, they
# are the range outside which a solved number says more about the price feed
# than about volatility.
_IV_FLOOR = 1e-4
_IV_CEILING = 5.0
_IV_NEWTON_STEPS = 12
_IV_BISECTION_STEPS = 60
# Below this vega a Newton step divides by roughly nothing and throws the guess
# somewhere useless. Measured on 56,484 contracts: 0.73% land here, all of them
# deep out of the money, and the bisection below settles every one.
_IV_MIN_VEGA = 1e-8
_IV_TOLERANCE = 1e-6
# How finely a solved volatility has to be pinned down before we are willing to
# call it a number: one thousandth. Combined with the price tolerance above,
# this is what rejects contracts whose price simply does not depend on
# volatility enough to determine it.
_IV_RESOLUTION = 1e-3


def _bs_price(spot, strike, years, sigma, rate, is_call, dividend_yield):
    """Generalised Black-Scholes price — the function the solver inverts.

    Deliberately a separate small function rather than a branch inside
    `_black_scholes_greeks_batch`: the solver calls it a dozen times per
    contract, and it must compute the price and nothing else.
    """
    sqrt_t = np.sqrt(years)
    d1 = (np.log(spot / strike) + (rate - dividend_yield + 0.5 * sigma**2) * years) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    carry = np.exp(-dividend_yield * years)
    discount = np.exp(-rate * years)
    return np.where(
        is_call,
        spot * carry * _norm_cdf(d1) - strike * discount * _norm_cdf(d2),
        strike * discount * _norm_cdf(-d2) - spot * carry * _norm_cdf(-d1),
    )


def solve_implied_volatility(
    spot, strike, years, price, is_call, rate, dividend_yield=0.0
) -> np.ndarray:
    """The volatility that reproduces this price under our own model.

    WHY THIS EXISTS AT ALL. Two problems, one answer.

    The first is visible today: the paid source serves chains for SPX, XSP, NDX
    and VIX and serves NO implied volatility for them — 0% coverage on 56,484
    rows, 28% of everything collected. Dealer gamma is computed FROM implied
    volatility, so those four tickers have no GEX, no heatmap and no screener
    at all. Eight people watch them and see a blank page; they came for the
    feature and did not "fail to engage with it".

    The second arrives with the source change. The rule that forbids mixing
    providers is right for a number the PROVIDER computes — two of them derive
    volatility with different models, so splicing draws a move that never
    happened. It stops applying the moment we compute it ourselves: one model
    over two price tapes is a single method on different inputs, not two
    opinions.

    WHAT IT IS SOLVED FROM. The contract's own price. That price is a market
    observation; the volatility is not, and never was — every provider's
    "implied volatility" is their inversion of their model against the same
    kind of price. Doing it ourselves does not add an assumption, it replaces
    somebody else's undisclosed one with ours, which is written down.

    NEWTON, THEN BISECTION, and both halves earn their place. Newton converges
    in a handful of steps almost everywhere; where vega is nearly zero it
    divides by nothing and wanders off. Measured on 56,484 contracts: Newton
    alone leaves 0.73% unsolved (median vega 2e-4 — deep out-of-the-money
    strikes), 51 ms; bisection over the bounded interval settles every one of
    them in a further 6 ms. Dropping them instead would be cheaper and wrong:
    a gap downstream is indistinguishable from an absence of data.

    NaN where there is nothing to solve — no price, no time left, or a price at
    or below intrinsic value, where no positive volatility reproduces it. That
    is a value, not a failure: it means this contract carries no information
    about volatility, and the callers already treat NaN as "no number here".
    """
    spot = np.asarray(spot, dtype=float)
    strike = np.asarray(strike, dtype=float)
    years = np.asarray(years, dtype=float)
    price = np.asarray(price, dtype=float)
    is_call = np.asarray(is_call, dtype=bool)
    rate = np.broadcast_to(np.asarray(rate, dtype=float), spot.shape)
    dividend_yield = np.broadcast_to(np.asarray(dividend_yield, dtype=float), spot.shape)

    carry = np.exp(-dividend_yield * np.maximum(years, 0.0))
    discount = np.exp(-rate * np.maximum(years, 0.0))
    # The floor no positive volatility can go below: a European option is worth
    # at least the discounted forward's intrinsic value. A price at or under it
    # carries no volatility information — usually a stale print on a strike
    # nobody has traded today.
    intrinsic = np.where(
        is_call,
        np.maximum(spot * carry - strike * discount, 0.0),
        np.maximum(strike * discount - spot * carry, 0.0),
    )
    solvable = (
        np.isfinite(spot) & np.isfinite(strike) & np.isfinite(price)
        & (spot > 0) & (strike > 0) & (years > 0)
        & (price > intrinsic + 1e-9)
    )
    result = np.full(spot.shape, np.nan)
    if not solvable.any():
        return result

    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        sigma = np.full(spot.shape, 0.5)
        for _ in range(_IV_NEWTON_STEPS):
            modelled = _bs_price(spot, strike, years, sigma, rate, is_call, dividend_yield)
            sqrt_t = np.sqrt(years)
            d1 = (np.log(spot / strike) + (rate - dividend_yield + 0.5 * sigma**2) * years) / (sigma * sqrt_t)
            vega = spot * carry * _norm_pdf(d1) * sqrt_t
            step = np.where(vega > _IV_MIN_VEGA, (modelled - price) / vega, 0.0)
            # The step is clipped as well as the result: an unclipped Newton
            # step on a near-flat vega jumps far outside the bracket and the
            # next iteration starts from nonsense.
            sigma = np.clip(sigma - np.clip(step, -1.0, 1.0), _IV_FLOOR, _IV_CEILING)

        residual = np.abs(_bs_price(spot, strike, years, sigma, rate, is_call, dividend_yield) - price)
        tolerance = np.maximum(_IV_TOLERANCE, np.abs(price) * 1e-6)
        needs_bisection = solvable & ~(residual <= tolerance)

        # Bisection runs on the rows that need it and nowhere else. Bisecting
        # the whole array and selecting afterwards costs sixty extra price
        # evaluations for every contract Newton already solved — measured at
        # five times the total runtime for no different answer.
        if needs_bisection.any():
            idx = np.flatnonzero(needs_bisection)
            low = np.full(idx.shape, _IV_FLOOR)
            high = np.full(idx.shape, _IV_CEILING)
            args = (spot[idx], strike[idx], years[idx], rate[idx], is_call[idx], dividend_yield[idx])
            for _ in range(_IV_BISECTION_STEPS):
                mid = 0.5 * (low + high)
                below = _bs_price(args[0], args[1], args[2], mid, args[3], args[4], args[5]) < price[idx]
                low = np.where(below, mid, low)
                high = np.where(below, high, mid)
            sigma[idx] = 0.5 * (low + high)

        final = np.abs(_bs_price(spot, strike, years, sigma, rate, is_call, dividend_yield) - price)

        # A PRICE THAT MATCHES IS NOT ENOUGH — the number also has to be
        # DETERMINED by that price, and deep in or out of the money it is not.
        # Where vega is tiny, a whole range of volatilities reproduces the same
        # price to within any tolerance we can measure, so the solver converges
        # happily on an arbitrary member of that range.
        #
        # Measured before this gate existed, on 56,484 synthetic contracts whose
        # true volatility was known: the price matched everywhere, and 55 of the
        # answers were wrong by as much as 0.34 in volatility. A confident wrong
        # number is the one failure this product cannot afford — it feeds gamma,
        # max pain and the whole GEX profile, and nothing downstream can tell.
        #
        # So: accept only where the tolerance we solved to pins volatility to
        # about a thousandth. Everything else is NaN, which the callers already
        # read as "no number here".
        sqrt_t = np.sqrt(years)
        d1_final = (
            np.log(spot / strike) + (rate - dividend_yield + 0.5 * sigma**2) * years
        ) / (sigma * sqrt_t)
        vega_final = spot * carry * _norm_pdf(d1_final) * sqrt_t
        determined = vega_final >= tolerance / _IV_RESOLUTION
        solved = solvable & (final <= tolerance) & determined
    result[solved] = sigma[solved]
    return result


def contract_price_for_iv(frame: pd.DataFrame) -> pd.Series:
    """The price the solver inverts: the middle of the quote, else the last print.

    THE MIDDLE IS THE BETTER NUMBER and the reason is timing. `last_price` is
    whatever traded most recently — on an illiquid strike that can be hours or
    days old, and a stale print produces a stale volatility that then looks
    like today's. Bid and ask are live quotes, and their midpoint is the price
    at which the contract is currently valued.

    The fallback is not cosmetic: a contract quoted one-sided, or not quoted at
    all outside the session, has no middle. There the last print is the only
    thing available, and a slightly stale number beats no number — the callers
    already show it as the contract's price.

    HOW OFTEN THE MIDDLE ACTUALLY EXISTS, measured 28.08.2026 on two hours of
    live collection:

        massive   0 of 79,224 rows carry a bid at all
        yahoo   2,521 of 30,115 rows have both sides quoted (8.4%)

    So today this reads as "the last print, almost always". That is not a
    reason to drop the branch — it is the reason to keep it and to say the
    number: the paid source sells chains without quotes, and the volatility we
    solve from it is therefore an inversion of ITS closing price, with all that
    implies about staleness on a strike nobody traded. When the source changes,
    the same code starts using real middles wherever Yahoo supplies them.
    """
    bid = pd.to_numeric(frame.get("bid"), errors="coerce")
    ask = pd.to_numeric(frame.get("ask"), errors="coerce")
    last = pd.to_numeric(frame.get("last_price"), errors="coerce")
    if bid is None or ask is None:
        return last
    quoted = (bid > 0) & (ask > 0) & (ask >= bid)
    return ((bid + ask) / 2.0).where(quoted, last)


def _unsolved(snapshot: pd.DataFrame) -> pd.DataFrame:
    """The frame with the marker columns added and nothing else touched.

    For the paths where the solver does not run at all: VIX, whose options are
    written on the futures rather than the index, and a frame that carries no
    spot to invert against. Their volatility stays the provider's — which is
    the honest answer — and the columns downstream reads for still exist.
    """
    frame = snapshot.copy()
    frame["provider_implied_volatility"] = frame.get("implied_volatility")
    frame["iv_is_ours"] = False
    frame["iv_is_filled"] = False
    return frame


def with_solved_iv(
    snapshot: pd.DataFrame, pricing: PricingInputs = DEFAULT_PRICING, ticker: str | None = None
) -> pd.DataFrame:
    """A chain whose implied volatility is OURS rather than the provider's.

    Applied at the read boundary rather than at write: the stored column keeps
    the provider's own number, because throwing it away would make "how far
    apart are the two models" unanswerable forever, and that question is the
    entire basis on which the source change is being taken.

    The provider's value is kept in `provider_implied_volatility` for anything
    that wants to compare, and `implied_volatility` — the column every
    downstream function reads — becomes ours. Nothing below this line has to
    know the difference, which is the point: gamma, max pain, GEX, the
    screener and the contract history all keep reading one column.

    THE TWO MARKER COLUMNS ARE ALWAYS PRESENT, including on the paths where
    nothing is solved. `implied_volatility` is still left exactly as it was for
    those — that is the substantive promise and it is kept — but a caller that
    has to write `if "iv_is_ours" in frame` before every use is a caller that
    will one day forget, and one did: the rebuild died on VIX, the single
    ticker the solver refuses, after twenty minutes of work.

    So the shape of what comes back does not depend on which branch ran. Whether
    the numbers are ours does, and that is what the flag is for.
    """
    if snapshot is None or snapshot.empty:
        return snapshot
    if ticker and str(ticker).upper() in IV_SOLVER_EXCLUDED_TICKERS:
        return _unsolved(snapshot)
    if "collected_at" not in snapshot or "underlying_price" not in snapshot:
        return _unsolved(snapshot)

    frame = snapshot.copy()
    # PER ROW, not per frame. `years_to_expiry_series` takes ONE instant and is
    # right for a single snapshot; a history frame carries a different
    # `collected_at` on every row, and passing the column to it raises. Built
    # the same way it builds — expiry mapped to the contract's settlement — so
    # the two cannot drift apart on the definition of "time to expiry".
    expiry_moments = _frame_expiry_moments(frame, ticker)
    collected = pd.to_datetime(frame["collected_at"])
    years = (expiry_moments - collected).dt.total_seconds() / _YEAR_SECONDS
    price = contract_price_for_iv(frame)
    solved = solve_implied_volatility(
        spot=pd.to_numeric(frame["underlying_price"], errors="coerce"),
        strike=pd.to_numeric(frame["strike"], errors="coerce"),
        years=years,
        price=price,
        is_call=frame["option_type"].astype(str).str.lower().eq("call"),
        rate=pricing.rate_series(years),
        dividend_yield=pricing.dividend_yield,
    )
    frame["provider_implied_volatility"] = frame.get("implied_volatility")
    ours = pd.Series(solved, index=frame.index)
    # WHERE WE COULD NOT SOLVE, THE PROVIDER'S NUMBER STANDS. Falling back is
    # not a compromise on the "one model" rule — it is what keeps a contract
    # from vanishing off a chart because its quote was one-sided for an hour.
    # Which rows are ours is recorded, so nothing has to guess later.
    # COERCED, AND THE COERCION IS THE FIX FOR A REAL CRASH. A provider column
    # that is entirely NULL — which is exactly what the paid source returns for
    # a cash-settled index, 0% IV coverage on SPX, XSP and NDX — arrives from
    # the database as dtype `object`, not float. `Series.where` takes its dtype
    # from `other`, so falling back to it turned the WHOLE column to object even
    # when a single row fell back, and the greeks batch downstream then built an
    # object array that the normal density could not take:
    #
    #     TypeError: ufunc 'isnan' not supported for the input types
    #
    # Seen on the Screener for SPX. Not reproducible on a ticker whose provider
    # does supply some implied volatility, which is every ticker except the
    # indices — that is, the exact case our own IV solver was written for.
    frame["implied_volatility"] = pd.to_numeric(
        ours.where(ours.notna(), frame.get("implied_volatility")), errors="coerce"
    )
    frame["iv_is_ours"] = ours.notna()
    filled = placeholder_iv_fill(frame, years)
    frame["iv_is_filled"] = filled.notna()
    frame["implied_volatility"] = frame["implied_volatility"].where(filled.isna(), filled)
    return frame


# Strike keys are thousandths; one (moment, expiry) group spans this many.
_FILL_KEY_SPAN = 10**9


def _mean_by_key(keys: np.ndarray, values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Sorted unique keys and the mean value at each — duplicates of a contract
    within one moment are averaged rather than one of them winning by order."""
    unique, inverse = np.unique(keys, return_inverse=True)
    return unique, np.bincount(inverse, weights=values) / np.bincount(inverse)


def placeholder_iv_fill(frame: pd.DataFrame, years: pd.Series | None = None) -> pd.Series:
    """A volatility for every row whose implied volatility is a placeholder,
    NaN for every other row.

    THE PLACEHOLDER. A source files in-the-money contracts with an implied
    volatility of 1e-5 — not a volatility, a "we have no number" written as
    one. The solver cannot replace it: the price it would invert is the last
    print, and on an in-the-money strike nobody traded today that print is
    stale and usually sits at or below intrinsic value, where no positive
    volatility reproduces it. Left in place, the placeholder gives the contract
    a gamma of practically zero while its open interest is real, so net GEX,
    the flip and the largest-GEX strike all lose the in-the-money side of the
    chain. Measured on a live chain of 24 tickers over six moments: 3.5% of the
    rows with open interest, all of them in the money, all at 1e-5, up to 15%
    of a ticker's open interest and more around the open; net GEX moved by up
    to 9% of gross GEX once they were priced.

    THE REPLACEMENT is the expiry's out-of-the-money smile at the contract's
    strike: the usable volatilities of the same moment and expiry, calls above
    the price and puts below it, interpolated linearly in strike and flat
    beyond the outermost one. For an in-the-money contract the other side of
    its own strike is always out of the money, so where that side carries a
    volatility the smile returns exactly it — put-call parity, which makes the
    two equal up to the dividend and early exercise. That is the convention
    every volatility surface is built on: the out-of-the-money side is the one
    the market actually quotes. Interpolation only covers the strikes that
    have no other side.
    An expiry with no usable out-of-the-money volatility keeps its
    placeholder, and with it a gamma of practically zero: there is nothing in
    it to borrow from. In-the-money volatilities are not borrowed — they are
    the ones least determined by their price — and neither is another expiry's,
    which would invent a term structure.

    The filled rows are flagged `iv_is_filled` and stay `iv_is_ours` False:
    nothing was solved for them, and a comparison of our model with the
    provider's must not count them as either.

    Measured on the same chain, holding out the in-the-money contracts that do
    carry a usable volatility and predicting them: open-interest-weighted mean
    absolute error 0.080 for this rule, 0.099 for the expiry's at-the-money
    volatility, 0.135 for a smile of the same option type. Solving against
    intrinsic value as a price floor was also tried and rejected: it returns
    a volatility near zero, which is the placeholder's gamma again.

    `years` is the time to expiry per row, if the caller already has it; rows
    that have expired are never filled."""
    result = np.full(len(frame), np.nan)
    if frame.empty or "implied_volatility" not in frame:
        return pd.Series(result, index=frame.index)
    iv = pd.to_numeric(frame["implied_volatility"], errors="coerce").to_numpy(dtype=float)
    placeholder = np.isfinite(iv) & (iv <= _IV_FLOOR)
    if years is not None:
        placeholder &= pd.to_numeric(pd.Series(years), errors="coerce").to_numpy(dtype=float) > 0
    if not placeholder.any():
        return pd.Series(result, index=frame.index)

    strike = pd.to_numeric(frame["strike"], errors="coerce").to_numpy(dtype=float)
    spot = pd.to_numeric(frame["underlying_price"], errors="coerce").to_numpy(dtype=float)
    is_call = (frame["option_type"] == "call").to_numpy()
    usable = (iv > _IV_FLOOR) & np.isfinite(strike)
    # ONE INTEGER KEY PER (moment, expiry, strike), so that the lookup below
    # is a sort and a binary search rather than a Python loop over expiries —
    # the loop cost more than the solver itself on a full chain. Strikes are
    # keyed to a thousandth, which no listed strike is finer than.
    # Factorised as stored, not parsed: parsing the moment column again costs
    # as much as the rest of this function.
    moment_code, _ = pd.factorize(frame["collected_at"])
    expiry_code, expiries = pd.factorize(frame["expiry"])
    group = moment_code.astype(np.int64) * max(len(expiries), 1) + expiry_code
    key = group * _FILL_KEY_SPAN + np.round(np.nan_to_num(strike) * 1000).astype(np.int64)
    wanted = np.flatnonzero(placeholder & np.isfinite(strike))
    out_of_money = usable & ((is_call & (strike >= spot)) | (~is_call & (strike <= spot)))
    if len(wanted) and out_of_money.any():
        smile_keys, smile_iv = _mean_by_key(key[out_of_money], iv[out_of_money])
        smile_strike = (smile_keys % _FILL_KEY_SPAN) / 1000.0
        smile_group = smile_keys // _FILL_KEY_SPAN
        right = np.searchsorted(smile_keys, key[wanted])
        left = right - 1
        last = len(smile_keys) - 1
        has_right = (right <= last) & (smile_group[np.minimum(right, last)] == group[wanted])
        has_left = (left >= 0) & (smile_group[np.maximum(left, 0)] == group[wanted])
        r, lft = np.minimum(right, last), np.maximum(left, 0)
        span = smile_strike[r] - smile_strike[lft]
        with np.errstate(divide="ignore", invalid="ignore"):
            weight = np.where(span > 0, (strike[wanted] - smile_strike[lft]) / span, 1.0)
        between = smile_iv[lft] + np.clip(weight, 0.0, 1.0) * (smile_iv[r] - smile_iv[lft])
        result[wanted] = np.select(
            [has_left & has_right, has_right, has_left], [between, smile_iv[r], smile_iv[lft]], np.nan
        )
    return pd.Series(np.where(placeholder, result, np.nan), index=frame.index)


def screener_table(
    df: pd.DataFrame, pricing: PricingInputs = DEFAULT_PRICING, ticker: str | None = None
) -> pd.DataFrame:
    """Flat table of every contract in the ticker's latest snapshot with DTE
    and the full set of greeks (spec FR25) — the basis for the range-filter
    screener. Latest snapshot only, not the full history: the screener is a
    "what's out there right now" cross-section, not a time series (per-contract
    history lives in the Contract tab)."""
    snapshot_date = df["collected_at"].max()
    snapshot = df[df["collected_at"] == snapshot_date].copy()
    if snapshot.empty:
        return pd.DataFrame()

    # COERCED, like the line below it already was. `years_to_expiry_series` maps
    # every expiry through `_expiry_moment` and so has always tolerated a string;
    # this subtraction did not, and a string is exactly what `expiry` is before a
    # chain has been through the database. The two lines sat next to each other
    # disagreeing about the shape of the same column.
    snapshot["dte"] = (
        pd.to_datetime(snapshot["expiry"], errors="coerce") - snapshot_date
    ).dt.days
    years = (_frame_expiry_moments(snapshot, ticker) - _as_naive_utc(snapshot_date)).dt.total_seconds() / _YEAR_SECONDS

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


def _contract_gex(frame: pd.DataFrame, pricing: PricingInputs, ticker: str | None = None) -> pd.Series:
    """Dealer GEX for every row of `frame`, obtained in ONE batched greeks call.

    THE QUANTITY IS THE ONE `gamma_exposure_profile` DEFINES, and this is the
    vectorised way of getting it: gamma from the batch kernel instead of a
    row-by-row `DataFrame.apply`. The two are already held to agree to 1e-9 by
    tests/smoke_test.py, and tests/unit_tests.py asserts the matrix built from
    this against a profile built from the scalar path, so "same number" is
    checked rather than assumed.

    WHY IT EXISTS AT ALL — measured. The heatmap built its matrix by
    calling `gamma_exposure_profile` once per expiry, and each of those calls
    priced its expiry through `apply`: 374 ms for ten expiries of a local SPY
    chain (13,160 rows), and 1271 ms when the user dragged the slider to all
    thirty-one. Through this it is 15 ms and 40 ms — the same numbers to the
    last bit, max |Δ| = 0 over the whole matrix. Nothing about the model
    changed; only how many Python-level calls it takes to evaluate it.

    Time to expiry is per row, taken from `collected_at`, because a frame can
    span many moments (`net_gex_series` passes exactly such a frame). For a
    single-moment frame that reduces to the scalar the profile uses.

    Sign convention, unchanged: calls contribute positively, puts negatively.
    That encodes the usual assumption that dealers are long the calls investors
    sell against stock they hold and short the puts investors buy for
    protection — a heuristic about who holds which side, which does NOT reflect
    actual market-maker positioning.
    """
    spot = pd.to_numeric(frame["underlying_price"], errors="coerce")
    collected = pd.to_datetime(frame["collected_at"])
    # Per row, the same way `years_to_expiry` builds it — expiry mapped to the
    # contract's settlement.
    expiry_moments = _frame_expiry_moments(frame, ticker)
    years = (expiry_moments - collected).dt.total_seconds() / _YEAR_SECONDS
    greeks = _black_scholes_greeks_batch(
        spot, pd.to_numeric(frame["strike"], errors="coerce"), years,
        pd.to_numeric(frame["implied_volatility"], errors="coerce"),
        pricing.rate_series(years), frame["option_type"],
        dividend_yield=pricing.dividend_yield,
    )
    contract_gex = greeks["gamma"] * pd.to_numeric(
        frame["open_interest"], errors="coerce"
    ).fillna(0) * 100 * spot
    return contract_gex.where(frame["option_type"] != "put", -contract_gex)


def gamma_exposure_profile(
    df: pd.DataFrame,
    expiry: pd.Timestamp,
    pricing: PricingInputs = DEFAULT_PRICING,
    as_of: pd.Timestamp | None = None,
    ticker: str | None = None,
) -> pd.DataFrame:
    """Approximate dealer GEX profile by strike for the given expiry (spec FR6).
    `as_of=None` — the latest snapshot (current state); otherwise a specific
    collection date from history (Replay, spec section 12, GEX Heatmap). Sign
    convention: calls contribute positively, puts negatively — the assumption
    that dealers are long the calls investors sell against stock and short the
    puts investors buy for protection. A heuristic, which does NOT reflect
    actual market-maker positioning (see the disclaimer in the UI)."""
    snapshot_date = as_of if as_of is not None else df["collected_at"].max()
    snapshot = df[(df["collected_at"] == snapshot_date) & (df["expiry"] == expiry)].copy()
    if snapshot.empty:
        return pd.DataFrame(columns=["strike", "gex"])

    spot = snapshot["underlying_price"].iloc[0]
    # Per row: one date can hold an AM- and a PM-settled series.
    snapshot["__years__"] = (
        _frame_expiry_moments(snapshot, ticker) - _as_naive_utc(snapshot_date)
    ).dt.total_seconds() / _YEAR_SECONDS

    snapshot["gamma"] = snapshot.apply(
        lambda row: _black_scholes_greeks(
            spot, row["strike"], row["__years__"], row["implied_volatility"],
            pricing.rate_for(row["__years__"]), row["option_type"],
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
    ticker: str | None = None,
) -> pd.DataFrame:
    """GEX matrix strike × expiry for a single snapshot (spec section 12, GEX
    Heatmap). Needs no new data — one snapshot already contains the full chain.
    `expiries=None` — every expiry in the snapshot; the UI usually passes only
    the visible subset (a ticker can have 30+ expiries including far-dated
    LEAPS). Index — strike descending (top to bottom, as in a conventional
    heatmap), columns — expiry.

    Priced through `_contract_gex`, one batched call for the whole shown subset.
    It used to loop over `gamma_exposure_profile`, one `apply()` per expiry, and
    that loop was the single most expensive thing in the most-visited view —
    374 ms of a 855 ms render on a local SPY chain, against 32 ms of database
    time. The numbers it returns did not change: max |Δ| = 0 across
    the whole matrix, and a test asserts the two paths against each other."""
    snapshot_date = as_of if as_of is not None else df["collected_at"].max()
    snapshot = df[df["collected_at"] == snapshot_date]
    if expiries is None:
        expiries = sorted(snapshot["expiry"].unique())
    # Narrowed BEFORE the greeks are priced, not after: the UI usually shows ten
    # of thirty-plus expiries, and pricing the rest is work whose result is
    # thrown away. This is also why the loop over `gamma_exposure_profile` is
    # gone — one batched call for the shown subset instead of one apply() per
    # expiry.
    snapshot = snapshot[snapshot["expiry"].isin(expiries)]
    if snapshot.empty:
        return pd.DataFrame()

    combined = pd.DataFrame({
        "strike": snapshot["strike"],
        "expiry": snapshot["expiry"],
        "gex": _contract_gex(snapshot, pricing, ticker),
    })
    # fill_value=0, not NaN: if an expiry has no listing at a given strike,
    # dealer exposure there is genuinely zero (not "unknown") — this removes
    # visual "holes" in the matrix rather than merely masking them.
    matrix = combined.pivot_table(index="strike", columns="expiry", values="gex", aggfunc="sum", fill_value=0)
    return matrix.sort_index(ascending=False)


def net_gex_series(
    df: pd.DataFrame, pricing: PricingInputs = DEFAULT_PRICING, ticker: str | None = None
) -> pd.DataFrame:
    """Net GEX for EVERY (moment, expiry) in the frame (tier 2 of R-01.1).

    Same quantity `net_gamma_exposure(gamma_exposure_profile(…))` produces for
    one snapshot, computed for a frame that spans many. It has to be the same
    number: the stored history and the live tab draw one line between them, and
    a series that changes definition halfway is worse than one that stops.

    THE DIFFERENCE IS ONLY IN HOW GAMMA IS OBTAINED. `gamma_exposure_profile`
    calls the scalar greeks row by row through `DataFrame.apply`, which is fine
    for one expiry of one snapshot and hopeless over 14.5M rows. This calls
    `_black_scholes_greeks_batch` once for the whole frame — and those two are
    already held to agree to 1e-9 by tests/smoke_test.py, so "same number" is
    asserted rather than assumed.

    Sign convention, unchanged: calls contribute positively, puts negatively.
    That encodes the usual assumption that dealers are long the calls investors
    sell against stock they hold and short the puts investors buy for
    protection — a heuristic about who holds which side, which does NOT reflect
    actual market-maker positioning.
    """
    columns = ["collected_at", "expiry", "net_gex"]
    if df is None or df.empty:
        return pd.DataFrame(columns=columns)

    frame = df.copy()
    collected = pd.to_datetime(frame["collected_at"])
    contract_gex = _contract_gex(frame, pricing, ticker)
    grouped = contract_gex.groupby([collected, frame["expiry"]]).sum()
    result = grouped.reset_index()
    result.columns = columns
    return result


def expiry_rollup(
    df: pd.DataFrame, pricing: PricingInputs = DEFAULT_PRICING, ticker: str | None = None
) -> pd.DataFrame:
    """Tier 2 in one call: max pain and net GEX per (moment, expiry).

    The two numbers are stored on one row, so they are computed together — and
    joined with an OUTER join on purpose. An expiry can have max pain and no
    GEX (no time left: gamma is zero by definition, not missing) and the
    reverse is possible too (open interest all on one side). Dropping either
    case would lose a real reading; a row with one column NULL says exactly
    what happened.
    """
    pain = max_pain_series(df)
    gex = net_gex_series(df, pricing, ticker)
    if pain.empty and gex.empty:
        return pd.DataFrame(columns=["collected_at", "expiry", "max_pain", "net_gex"])
    merged = pain.merge(gex, on=["collected_at", "expiry"], how="outer")
    return merged.sort_values(["collected_at", "expiry"], ignore_index=True)


def net_gex_from_matrix(matrix: pd.DataFrame, expiries: list | None = None) -> pd.DataFrame:
    """The heatmap's per-expiry net GEX, read off a matrix that already exists.

    The matrix holds GEX per (strike, expiry) with zeros where an expiry has no
    listing at a strike, so its column sums ARE the per-expiry net figures —
    the same quantity `net_gex_by_expiry` computes, without pricing the chain a
    second time. The view draws both, and a later measurement pass found what that
    repetition cost: three passes over one chain (matrix, flip, sidebar) where one does.

    Summation order differs from `net_gex_series` — down the strikes rather than
    over the raw rows — so the two agree to floating-point tolerance rather than
    bit-for-bit; a test pins that at 1e-6 relative.

    An expiry that was asked for and is not in the matrix comes back carrying
    0.0, which is what the sidebar has always shown for it.
    """
    if expiries is None:
        expiries = list(matrix.columns)
    if matrix.empty:
        return pd.DataFrame({"expiry": expiries, "net_gex": [0.0] * len(expiries)})
    sums = matrix.sum(axis=0)
    return pd.DataFrame({
        "expiry": expiries,
        "net_gex": [float(sums.get(expiry, 0.0)) for expiry in expiries],
    })


def net_gex_by_expiry(
    df: pd.DataFrame,
    as_of: pd.Timestamp | None = None,
    pricing: PricingInputs = DEFAULT_PRICING,
    expiries: list | None = None,
) -> pd.DataFrame:
    """Net GEX per expiry of the snapshot (GEX Heatmap sidebar) — same as
    `net_gamma_exposure`, but for all expiries at once (or for the given
    subset — see `gex_matrix`).

    A projection of `net_gex_series` onto one moment. An expiry that was asked
    for and has no rows in the snapshot still comes back, carrying 0.0, which
    is what the heatmap's sidebar has always shown for it."""
    snapshot_date = as_of if as_of is not None else df["collected_at"].max()
    snapshot = df[df["collected_at"] == snapshot_date]
    if expiries is None:
        expiries = sorted(snapshot["expiry"].unique())
    else:
        # Same reason as in `gex_matrix`: the answer for one expiry does not
        # depend on any other, so pricing the whole chain to report ten of its
        # expiries is work nobody asked for.
        snapshot = snapshot[snapshot["expiry"].isin(expiries)]
    series = net_gex_series(snapshot, pricing).set_index("expiry")["net_gex"]
    return pd.DataFrame({
        "expiry": expiries,
        "net_gex": [float(series.get(expiry, 0.0)) for expiry in expiries],
    })


# How far from the underlying price a wall may stand: twice the expected
# move of the expiries it is taken over, but never closer than five percent.
# A strike outside that window holds open interest nobody expects the price
# to reach before those contracts expire — a level, not a wall.
WALL_WINDOW_MOVES = 2.0
WALL_WINDOW_FLOOR = 0.05


def wall_reach(df: pd.DataFrame, as_of: pd.Timestamp | None = None) -> pd.Series:
    """One expected move per expiry, as a share of the underlying price:
    `σ_ATM × √T`, indexed by expiry. Only expiries that still trade and have
    an at-the-money volatility are listed; the others widen no window.

    σ_ATM is the mean implied volatility at the strike nearest the price,
    calls and puts alike, counting only values above the solver floor: a
    provider's placeholder (1e-5 on in-the-money contracts) is not a
    volatility, and one of the two contracts at the money is always slightly
    in the money."""
    snapshot_date = as_of if as_of is not None else df["collected_at"].max()
    snapshot = df[df["collected_at"] == snapshot_date]
    if snapshot.empty:
        return pd.Series(dtype=float)
    spot = pd.to_numeric(snapshot["underlying_price"], errors="coerce").iloc[0]
    if not np.isfinite(spot) or spot <= 0:
        return pd.Series(dtype=float)
    iv = pd.to_numeric(snapshot["implied_volatility"], errors="coerce")
    strike = pd.to_numeric(snapshot["strike"], errors="coerce")
    usable = snapshot.assign(_iv=iv, _distance=(strike - spot).abs())[(iv > _IV_FLOOR) & strike.notna()]
    if usable.empty:
        return pd.Series(dtype=float)
    nearest = usable.groupby("expiry")["_distance"].transform("min")
    sigma = usable[usable["_distance"] == nearest].groupby("expiry")["_iv"].mean()
    years = pd.Series([years_to_expiry(expiry, snapshot_date) for expiry in sigma.index], index=sigma.index)
    live = years > 0
    return (sigma[live] * np.sqrt(years[live])).astype(float)


def wall_window(reach: pd.Series, expiries=None) -> float:
    """The half-width of the strike window, as a share of the price, for a
    set of expiries (all of `reach` by default): the farthest expected move
    among them, doubled, floored at `WALL_WINDOW_FLOOR`."""
    if expiries is not None:
        reach = reach[reach.index.isin(expiries)]
    widest = float(reach.max()) if not reach.empty else 0.0
    return max(WALL_WINDOW_MOVES * widest, WALL_WINDOW_FLOOR)


def pick_wall(ladder: pd.Series | None, side: str, spot: float, window: float) -> float | None:
    """One side's wall from its open interest by strike (index ascending):
    the strike with the most open interest on that side's half of the chain —
    a call at or above the price, a put at or below it — and inside the
    window. A tie goes to the strike nearer the price. None when that side
    holds no open interest there at all: a put wall above the price would be
    a level dealers sit on the wrong side of, not support."""
    if ladder is None or ladder.empty:
        return None
    return pick_wall_arrays(ladder.index.to_numpy(dtype=float), ladder.to_numpy(dtype=float), side, spot, window)


def pick_wall_arrays(strikes: np.ndarray, values: np.ndarray, side: str, spot: float, window: float) -> float | None:
    """`pick_wall` on bare arrays (strikes ascending), for callers that hold
    many ladders as one grid and would otherwise box each into a Series."""
    if len(strikes) == 0 or spot is None or not np.isfinite(spot) or spot <= 0:
        return None
    # A relative tolerance of 1e-12 keeps a strike that sits exactly on the
    # edge inside: 0.05 has no exact binary form, and without it about four
    # in ten exact edges on a cent grid fall out, while no strike a cent past
    # the edge falls in.
    reach = np.abs(strikes - spot) <= window * spot * (1 + 1e-12)
    half = strikes >= spot if side == "call" else strikes <= spot
    allowed = reach & half & (values > 0)
    if not allowed.any():
        return None
    candidates = np.where(allowed, values, -np.inf)
    if side == "call":
        return float(strikes[int(np.argmax(candidates))])
    return float(strikes[len(strikes) - 1 - int(np.argmax(candidates[::-1]))])


def dealer_walls(
    df: pd.DataFrame, as_of: pd.Timestamp | None = None, expiries: list | None = None
) -> dict[str, float | None]:
    """Call Wall / Put Wall — the strike with the most open interest in
    calls (at or above the price) and in puts (at or below it), aggregated
    across the snapshot's expiries — all by default, or the given subset —
    and no further from the price than `wall_window` allows (a common proxy
    for support/resistance levels created by dealer hedging flows, spec
    section 12). See `pick_wall` for ties and an empty side."""
    snapshot_date = as_of if as_of is not None else df["collected_at"].max()
    snapshot = df[df["collected_at"] == snapshot_date]
    if expiries is not None:
        snapshot = snapshot[snapshot["expiry"].isin(expiries)]
    if snapshot.empty:
        return {"call_wall": None, "put_wall": None}
    spot = float(pd.to_numeric(snapshot["underlying_price"], errors="coerce").iloc[0])
    window = wall_window(wall_reach(snapshot, snapshot_date))
    oi_by_strike = snapshot.groupby(["option_type", "strike"])["open_interest"].sum()
    sides = oi_by_strike.index.get_level_values("option_type")

    def wall(option_type: str) -> float | None:
        ladder = oi_by_strike.loc[option_type] if option_type in sides else None
        return pick_wall(ladder, option_type, spot, window)

    return {"call_wall": wall("call"), "put_wall": wall("put")}


# Furthest the flip may lie from the underlying price, as a share of it. It is
# also the half-width of the price grid the flip is searched on, so a sign
# change further away is never looked for — see `gamma_flip_price`.
GAMMA_FLIP_MAX_DISTANCE = 0.25

# Spacing of that grid, as a share of the underlying price: 0.05%, or 1,001
# prices across the search range — see `gamma_flip_price`.
GAMMA_FLIP_GRID_STEP = 0.0005

# Smallest share of the largest absolute value of the re-priced curve that a
# value on each side of a sign change must exceed for the change to count as
# the gamma flip — see `gamma_flip_price`.
GAMMA_FLIP_MIN_SHARE = 1e-4

# Contracts priced on the grid per block. Bounds the temporary arrays at
# block x grid floats (about 16 MB each at the default step) whatever the
# chain's size; the answer does not depend on it.
_FLIP_BLOCK_ROWS = 2048


def gamma_flip_grid(spot: float) -> np.ndarray:
    """The hypothetical underlying prices the flip is searched on: the spot
    itself and `GAMMA_FLIP_GRID_STEP` steps of it either way, out to
    `GAMMA_FLIP_MAX_DISTANCE`. Exactly symmetric and exactly through the
    spot, so the curve's value there is the net GEX the rest of the app
    shows."""
    steps = int(round(GAMMA_FLIP_MAX_DISTANCE / GAMMA_FLIP_GRID_STEP))
    return float(spot) * (1.0 + GAMMA_FLIP_GRID_STEP * np.arange(-steps, steps + 1))


def _repriced_inputs(snapshot: pd.DataFrame, pricing: PricingInputs, ticker: str | None):
    """Per contract, what its dealer GEX at a hypothetical price needs.

    At an underlying price S a contract's GEX is sign * OI * 100 * S * gamma(S)
    (`_contract_gex`), and Black-Scholes gamma is exp(-qT) * pdf(d1) /
    (S * sigma * sqrt(T)) — so the S cancels and the contribution is
    `weight * exp(-d1(S)^2 / 2)` with d1(S) = (ln S - ln K + drift) / vol. The
    four arrays returned are ln K, drift, vol and weight, for the contracts
    that can carry gamma at all: time left, a positive strike, open interest
    other than zero, and a volatility above the solver's floor. Everything
    else contributes exactly zero at every price.

    THE FLOOR, NOT ZERO. A source files deep in-the-money contracts with a
    placeholder volatility of 1e-5 — 9-17% of the rows of a live thirty-day
    chain. At the actual spot such a contract is many standard deviations
    from its strike and contributes nothing, so `_contract_gex` can keep it;
    re-priced, the grid walks straight through its strike, where a width of
    sigma * sqrt(T) ~ 2e-6 makes it a spike some 1e4 times taller than an
    ordinary contract. One put with an open interest of 10 moved a synthetic
    flip from 97.19 to 100.40; on SPX the spikes multiplied the curve's peak
    eighteenfold, and the significance threshold with it. A volatility no
    solver would return is not a sticky-strike volatility to hold fixed."""
    collected = pd.to_datetime(snapshot["collected_at"])
    years = ((_frame_expiry_moments(snapshot, ticker) - collected).dt.total_seconds() / _YEAR_SECONDS).to_numpy(
        dtype=float)
    strike = pd.to_numeric(snapshot["strike"], errors="coerce").to_numpy(dtype=float)
    iv = pd.to_numeric(snapshot["implied_volatility"], errors="coerce").to_numpy(dtype=float)
    oi = pd.to_numeric(snapshot["open_interest"], errors="coerce").fillna(0).to_numpy(dtype=float)
    rate = pricing.rate_series(pd.Series(years))
    keep = (years > 0) & (iv > _IV_FLOOR) & (strike > 0) & np.isfinite(oi) & (oi != 0)
    years, strike, iv, oi, rate = years[keep], strike[keep], iv[keep], oi[keep], rate[keep]
    q = pricing.dividend_yield
    vol = iv * np.sqrt(years)
    drift = (rate - q + iv ** 2 / 2) * years
    sign = np.where((snapshot["option_type"] == "put").to_numpy()[keep], -1.0, 1.0)
    weight = sign * oi * 100 * np.exp(-q * years) / (vol * _SQRT_2PI)
    return keep, np.log(strike), drift, vol, weight


def repriced_gex_by_group(
    snapshot: pd.DataFrame,
    prices: np.ndarray,
    groups: np.ndarray,
    n_groups: int,
    pricing: PricingInputs = DEFAULT_PRICING,
    ticker: str | None = None,
) -> np.ndarray:
    """Dealer GEX of a single-moment chain re-priced at every price in `prices`,
    summed per group of contracts: row g, column j is the total over the
    contracts with `groups == g` had the underlying stood at `prices[j]`.

    Only the underlying moves. Each contract keeps its own implied volatility
    (sticky strike), its time to expiry and its open interest, which is how
    every published "gamma flip" is computed; at the actual spot the result
    is `_contract_gex` summed, to rounding.

    Priced in blocks of contracts so memory stays flat on the largest chains,
    with the contracts ordered by group so each block is summed per group by
    one `np.add.reduceat`. A matrix product with a group-membership matrix
    was the obvious alternative and the wrong one: 584 ms for all 52 SPX
    expiries against 85 ms this way, because a multithreaded BLAS spends that
    time coordinating threads on products this small (100 ms on one thread)."""
    prices = np.asarray(prices, dtype=float)
    totals = np.zeros((n_groups, len(prices)))
    if snapshot.empty or not len(prices):
        return totals
    keep, log_strike, drift, vol, weight = _repriced_inputs(snapshot, pricing, ticker)
    groups = np.asarray(groups)[keep]
    order = np.argsort(groups, kind="stable")
    groups, log_strike, drift, vol, weight = (
        groups[order], log_strike[order], drift[order], vol[order], weight[order])
    log_prices = np.log(prices)
    for start in range(0, len(weight), _FLIP_BLOCK_ROWS):
        block = slice(start, start + _FLIP_BLOCK_ROWS)
        d1 = (log_prices[None, :] - log_strike[block, None] + drift[block, None]) / vol[block, None]
        exposure = np.exp(-0.5 * d1 * d1)
        exposure *= weight[block, None]
        members = groups[block]
        starts = np.flatnonzero(np.r_[True, members[1:] != members[:-1]])
        totals[members[starts]] += np.add.reduceat(exposure, starts, axis=0)
    return totals


def gamma_flip_price(
    df: pd.DataFrame,
    as_of: pd.Timestamp | None = None,
    pricing: PricingInputs = DEFAULT_PRICING,
    expiries: list | None = None,
    ticker: str | None = None,
) -> float | None:
    """The gamma flip: the underlying price nearest the spot at which total
    dealer GEX (over `expiries`, every expiry by default) changes sign once
    every contract is RE-PRICED at that price — or None if there is no such
    price within `GAMMA_FLIP_MAX_DISTANCE` of the spot.

    THE DEFINITION. Total GEX as a function of a hypothetical underlying price
    S, with gammas recomputed at S and everything else held (see
    `repriced_gex_by_group`); the flip is where that function crosses zero.
    This is the accepted meaning of the level — the price at which dealer
    hedging would turn from damping moves to amplifying them — and the one
    other tools publish.

    WHAT IT REPLACED, AND WHY THAT WAS A DEFECT RATHER THAN A SIMPLIFICATION.
    The flip used to be the zero of the CUMULATIVE per-strike profile at the
    current spot: GEX summed strike by strike from the bottom of the chain.
    That is a different quantity, and on an ordinary chain it cannot give the
    accepted answer. With net exposure negative, put exposure below the money
    and call exposure above it, the running sum first falls and then rises
    without ever getting back above max(first value, total) < 0 — so there is
    no crossing at all, by construction. SPY, QQQ, SPX, IWM and NVDA all showed
    "no flip" on the same morning while other tools put theirs 1-3% above the
    price. With net exposure positive the running sum crosses once, above the
    spot, while the re-priced flip of such a chain lies below it: a synthetic
    chain at 750 gave 754.34 against 744.92. The answer changed sides of the
    price, not just its value.

    THE GRID. The function is evaluated on `gamma_flip_grid` — the spot and
    `GAMMA_FLIP_GRID_STEP` steps either side out to `GAMMA_FLIP_MAX_DISTANCE`
    — and the zero is interpolated linearly between the two grid prices
    bracketing it. The step is set by the nearest expiry near its close, the
    narrowest feature the curve has: with fifteen minutes left an at-the-money
    contract's gamma is about 0.08% of the price wide, and with five minutes
    left a 0.1% step put SPX's flip 0.34% off the level a fifty-times finer
    grid finds, against 0.10% at 0.05%. Right at the close the curve changes
    sign at nearly every strike and the level is noisy at any step. Re-pricing
    the whole chain at every grid price is the cost of the definition (about
    25 ms for SPY's thirty-day chain, 85 ms for SPX's), which is why the
    worker computes it and screens read the stored level.

    Three rules decide which sign change is the flip, each carried over from
    the cumulative walk because each answers a question the definition does
    not.

    A CHANGE IS BETWEEN A STRICTLY POSITIVE AND A STRICTLY NEGATIVE VALUE.
    Prices at which nothing carries gamma — far beyond the chain, where every
    density has underflowed — are exactly 0.0 and take no part; a curve that
    passes through 0.0 on its way from one sign to the other still changes,
    because the signed values either side of the flat part bracket it.

    BOTH SIDES MUST BE SIGNIFICANT. A value takes part only when its magnitude
    exceeds `GAMMA_FLIP_MIN_SHARE` of the curve's largest absolute value, and
    a change counts when the nearest such values either side have opposite
    signs. Far from the money the curve is the faint tail of a few deep
    strikes and can change sign at magnitudes of nothing; that is not a
    change of regime. The level of a change that counts is still interpolated
    between the two grid prices that bracket the actual sign change.

    OF THE CHANGES THAT COUNT, THE ONE NEAREST THE SPOT. The level stands next
    to the price, the walls and the regime and is read as "how far is the
    regime change from here"; a chain whose curve changes sign twice in range
    answers with the nearer one, the first on a tie.

    The search range is `GAMMA_FLIP_MAX_DISTANCE` either way: a quarter of the
    price, about one and a half monthly standard deviations for the most
    volatile chain collected (implied volatility near 57%) and seven for an
    index at 13%. A level further away does not answer the question above,
    and a re-priced curve that far out is the tail of the chain rather than
    its body."""
    snapshot_date = as_of if as_of is not None else df["collected_at"].max()
    snapshot = df[df["collected_at"] == snapshot_date]
    chosen = sorted(snapshot["expiry"].unique()) if expiries is None else sorted(expiries)
    flips = gamma_flip_by_depth(snapshot, chosen, pricing, snapshot_date, ticker)
    return flips[-1] if flips else None


def gamma_flip_by_depth(
    df: pd.DataFrame,
    expiries: list,
    pricing: PricingInputs = DEFAULT_PRICING,
    as_of: pd.Timestamp | None = None,
    ticker: str | None = None,
) -> list[float | None]:
    """The flip of every leading block of `expiries`, in one pass: entry d is
    the flip over the first d + 1 of them, in the order given.

    The heatmap's depth slider shows the nearest one, two, ... expiries, and
    each position has its own flip. Re-pricing is linear in the contracts, so
    each expiry's curve is priced once and every depth's curve is a running
    sum of those — the cost of the deepest position, not of all of them.
    `gamma_flip_price` is the last entry of this over its expiries sorted,
    so the card and the heatmap at the same expiries do the same arithmetic
    rather than two that agree to a tolerance."""
    if not expiries:
        return []
    snapshot_date = as_of if as_of is not None else df["collected_at"].max()
    snapshot = df[(df["collected_at"] == snapshot_date) & df["expiry"].isin(expiries)]
    if snapshot.empty:
        return [None] * len(expiries)
    spot = pd.to_numeric(snapshot["underlying_price"], errors="coerce").iloc[0]
    if not np.isfinite(spot) or spot <= 0:
        return [None] * len(expiries)
    position = {expiry: number for number, expiry in enumerate(expiries)}
    groups = snapshot["expiry"].map(position).to_numpy(dtype=int)
    grid = gamma_flip_grid(spot)
    curves = np.cumsum(repriced_gex_by_group(snapshot, grid, groups, len(expiries), pricing, ticker), axis=0)
    return [_nearest_crossing(grid, curve, float(spot)) for curve in curves]


def _nearest_crossing(prices: np.ndarray, curve: np.ndarray, reference: float) -> float | None:
    """Sign changes of `curve` over ascending `prices` between strictly signed
    values, interpolated linearly; of those whose nearest significant values
    on either side differ in sign, the one nearest the reference, the first
    on a tie — or None if even that one is further from it than
    `GAMMA_FLIP_MAX_DISTANCE`."""
    carrying = np.flatnonzero(curve)
    if len(carrying) < 2:
        return None
    before, after = carrying[:-1], carrying[1:]
    y0, y1 = curve[before], curve[after]
    change = np.sign(y0) != np.sign(y1)
    # Indices of the values large enough to take part (`gamma_flip_price`,
    # second rule). At least one exists: the largest value exceeds any share
    # of itself below one, since `carrying` is not empty.
    magnitude = np.abs(curve)
    significant = np.flatnonzero(magnitude > GAMMA_FLIP_MIN_SHARE * magnitude.max())
    left = np.searchsorted(significant, before, side="right") - 1
    right = np.searchsorted(significant, after, side="left")
    bracketed = (left >= 0) & (right < len(significant))
    left_sign = np.sign(curve[significant[np.clip(left, 0, len(significant) - 1)]])
    right_sign = np.sign(curve[significant[np.clip(right, 0, len(significant) - 1)]])
    change &= bracketed & (left_sign != right_sign)
    if not change.any():
        return None
    x0, x1 = prices[before][change], prices[after][change]
    y0, y1 = y0[change], y1[change]
    # y0 and y1 are nonzero with opposite signs, so they cannot be equal and
    # the interpolation cannot divide by zero.
    crossings = x0 + (0 - y0) * (x1 - x0) / (y1 - y0)
    nearest = crossings[np.argmin(np.abs(crossings - reference))]
    if abs(nearest - reference) > GAMMA_FLIP_MAX_DISTANCE * abs(reference):
        return None
    return float(nearest)


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
        usable = group[group["_priced"]][["implied_volatility", "volume"]].dropna(
            subset=["implied_volatility"]
        )
        weights = usable["volume"].fillna(0)
        if usable.empty or weights.sum() == 0:
            return np.nan
        return np.average(usable["implied_volatility"], weights=weights)

    # Contracts whose expiry date has passed carry no volatility, whatever the
    # source reports for them. Massive reports 20.0 — a sentinel, since implied
    # volatility diverges as time to expiry goes to zero — and they arrive with
    # the whole session's accumulated volume, which on SPY's zero-day expiries
    # is the largest in the chain. The two together dragged the ticker average
    # from 0.163 to 13.53 on every collection taken after the close on an
    # expiry day: one spike per day on the chart, with the real number crushed
    # to a flat line beneath it.
    #
    # The comparison is by DATE and inclusive on purpose: a zero-day contract
    # during its own session is real trading and belongs in the average; the
    # same contract the evening after is not.
    #
    # Grouping still happens over every row, so a moment where nothing is
    # priceable keeps its place in the series and carries NaN. Dropping it
    # instead would draw a straight line across the gap.
    # COERCED, BECAUSE THIS FUNCTION IS CALLED FROM BOTH SIDES OF THE PRODUCT.
    # Read back from the database, `expiry` is a date. Straight from a provider
    # — which is where the collector calls this from, before anything is stored
    # — it is a STRING, and in pandas 3 a string column is a real `str` dtype
    # that refuses to be compared with a datetime at all:
    #
    #     TypeError: Invalid comparison between dtype=str and DatetimeArray
    #
    # On stage that failed for every ticker in the pass, and it failed QUIETLY:
    # the collector swallows anything this raises so that a chain in hand is
    # never lost to a rollup, so the only symptom was the stored average
    # staying the provider's. Our own IV was off entirely and the screens looked
    # normal.
    expiry = pd.to_datetime(df["expiry"], errors="coerce")
    collected = pd.to_datetime(df["collected_at"], errors="coerce")
    priced = df.assign(_priced=expiry >= collected.dt.normalize())
    result = priced.groupby("collected_at").apply(weighted_avg, include_groups=False)
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
    ticker: str | None = None,
    contract_symbol: str | None = None,
) -> pd.DataFrame:
    """Price, IV, and full-greeks history of a specific contract across
    collection dates (spec FR14). Replaces the former iv_by_contract — the
    same drill-down, plus the full set of greeks."""
    contract = df[
        (df["strike"] == strike) & (df["expiry"] == expiry) & (df["option_type"] == option_type)
    ].sort_values("collected_at")
    if contract.empty:
        return pd.DataFrame(
            columns=["collected_at", "last_price", "underlying_price",
                     "implied_volatility", *_GREEK_KEYS]
        )

    # Data-quality guard (see _mark_unreliable_iv): an IV that's a strong
    # outlier vs. this contract's own history, uncorroborated by any real
    # move in last_price, is untrustworthy for that one snapshot — every
    # greek below is derived from `iv`, not the raw row value, so the whole
    # row's worth of derived numbers gaps out together rather than one field
    # silently lying.
    reliable_iv = _mark_unreliable_iv(contract)

    # ONE BATCH, NOT A ROW AT A TIME. This used to call the scalar
    # `_black_scholes_greeks` per snapshot — 0.1 ms a row, which is nothing
    # on a chain of a hundred and 400 ms on a contract collected every
    # fifteen minutes for half a year. The batch kernel is the same
    # formulas on arrays (the audit holds the two to 1e-12), so the only
    # work here is reproducing the scalar path's two rulings row by row:
    # a missing volatility or spot is NaN, not a number (the scalar returns
    # NaN for every greek; the batch would say 0.0), and a provider's greek
    # wins where the row carries one, whatever ours came to.
    # One contract, one settlement: its symbol from the caller or its rows.
    if contract_symbol is None and "contract_symbol" in contract.columns:
        named = contract["contract_symbol"].dropna()
        named = named[named.astype(str) != ""]
        contract_symbol = str(named.iloc[0]) if len(named) else None
    years = years_to_expiry_by_moment(
        expiry, contract["collected_at"], contract_symbol, _ticker_of(contract, ticker)
    )
    spot = pd.to_numeric(contract["underlying_price"], errors="coerce")
    iv = pd.to_numeric(reliable_iv, errors="coerce")
    greeks = _black_scholes_greeks_batch(
        spot, pd.Series(float(strike), index=contract.index), years, iv,
        pricing.rate_series(years), pd.Series(option_type, index=contract.index),
        dividend_yield=pricing.dividend_yield,
    )
    greeks = greeks.mask(iv.isna() | spot.isna())
    # Provider greeks win where this snapshot carries them;
    # rho/vanna/charm are always ours, the provider never supplies them.
    greeks = merge_provider_greeks(greeks, contract)
    history = pd.DataFrame({
        "collected_at": contract["collected_at"].to_numpy(),
        "last_price": contract["last_price"].to_numpy(),
        # The spot this row's greeks were computed against. Carried
        # out with them rather than joined back later: the attribution needs
        # ΔS between two rows, and taking it from anywhere else risks pairing
        # a price from one snapshot with a spot from another.
        "underlying_price": contract["underlying_price"].to_numpy(),
        "implied_volatility": iv.to_numpy(),
    })
    for greek in _GREEK_KEYS:
        history[greek] = greeks[greek].to_numpy()
    return history


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
        # Per YEAR, like the formula above — it is not divided by 365 the way theta
        # is. The earlier wording said "over one day" and was wrong by that factor
        # (audit 02.09.2026); the daily figure is quoted beside it so a reader
        # who thinks in days is not left to do the division.
        f"Charm {latest['charm']:.4f}{trend('charm')} — how much delta \"ages\" over one year "
        f"at an unchanged price, i.e. about {latest['charm'] / 365:.5f} per day (time decay "
        f"of delta itself, not of the contract price).",
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


def iv_surface(df: pd.DataFrame, ticker: str | None = None) -> pd.DataFrame:
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
    otm["years_to_expiry"] = (
        _frame_expiry_moments(otm, ticker) - _as_naive_utc(latest_date)
    ).dt.total_seconds() / _YEAR_SECONDS
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


ATTRIBUTION_TERMS = ("delta", "gamma", "vega", "theta")


class GreekAttribution:
    """What each greek did to a contract's price, day by day.

    `by_day` holds one row per interval — the four terms, the price change that
    actually happened, and the residual. `totals` sums them, and start/end are
    the prices the sum reconciles: start + every term + every residual == end,
    exactly, by construction.
    """

    def __init__(self, by_day, start_price=None, end_price=None,
                 dropped_cheap=0, dropped_unreliable=0, missing_trading_days=0):
        self.by_day = by_day
        self.start_price = start_price
        self.end_price = end_price
        self.dropped_cheap = dropped_cheap
        self.dropped_unreliable = dropped_unreliable
        self.missing_trading_days = missing_trading_days

    @property
    def totals(self) -> dict:
        """Each term summed over the window, plus the residual and the net."""
        if self.by_day.empty:
            return {name: 0.0 for name in (*ATTRIBUTION_TERMS, "residual", "actual")}
        return {name: float(self.by_day[name].sum())
                for name in (*ATTRIBUTION_TERMS, "residual", "actual")}


def greek_attribution(history: pd.DataFrame, min_price: float | None = None) -> GreekAttribution:
    """Splits a contract's price change into what each greek did.

        Δprice ≈ delta·ΔS + ½·gamma·ΔS² + vega·ΔIV + theta·Δt + residual

    NO ENTRY POINT IS NEEDED, and that is the misunderstanding this function
    exists to settle. Every input comes from the snapshots themselves — the
    greeks as they stood at the start of an interval, the moves observed by its
    end. This is a property of the CONTRACT. A position adds only a window, a
    multiplier and the gap between the fill and the snapshot price; it does not
    redistribute the greeks' shares.

    DAILY, not per snapshot. Production collects every 15 minutes: over 96
    intervals a day the theta term is 0.0104 days each and the delta term rides
    on quote jitter, so summing them accumulates noise into the residual. One
    row per trading day, via the same `_last_snapshot_per_day` every other daily
    metric uses.

    UNITS, verified against `_black_scholes_greeks` rather than assumed: theta
    is per CALENDAR day (divided by 365) and vega per IV POINT (divided by 100),
    so IV differences are multiplied by 100 and theta by elapsed calendar time —
    which is why a Friday-to-Monday interval charges three days of decay, not
    one.

    THE RESIDUAL IS ALWAYS RETURNED. The decomposition explains a MODEL price:
    IV is inverted from the price, the greeks come from IV, and everything the
    model does not contain — the spread, a stale print, a rate move — lands
    here. Small residual, the model explains this contract; large residual, it
    does not, and that is a finding rather than something to hide.

    Two stretches are refused rather than reported wrong, and both are counted
    so the interface can say so:

    - **Below `min_price`** (see config.ATTRIBUTION_MIN_PRICE): on a one-cent
      option every greek is arithmetic about rounding.
    - **Rows the IV guard rejected**, which arrive as NaN greeks.

    What survives is the LONGEST CONTIGUOUS RUN of usable intervals. Contiguity
    is not tidiness: the whole point of the output is that start + terms == end,
    and a gap in the middle breaks that identity silently.
    """
    min_price = config.ATTRIBUTION_MIN_PRICE if min_price is None else min_price
    columns = ["day", "actual", *ATTRIBUTION_TERMS, "residual", "missing_trading_days"]
    empty = pd.DataFrame(columns=columns)
    needed = ["last_price", "underlying_price", "implied_volatility", *ATTRIBUTION_TERMS]
    if history.empty or not set(needed) <= set(history.columns):
        return GreekAttribution(empty)

    daily = _last_snapshot_per_day(history).sort_values("collected_at")
    usable_rows = daily.dropna(subset=needed)
    dropped_unreliable = len(daily) - len(usable_rows)
    if len(usable_rows) < 2:
        return GreekAttribution(empty, dropped_unreliable=dropped_unreliable)

    candidates = []
    rows = list(usable_rows.itertuples())
    for previous, current in zip(rows, rows[1:]):
        cheap = min(previous.last_price, current.last_price) < min_price
        elapsed_days = (current.collected_at - previous.collected_at).total_seconds() / 86400
        change_in_spot = current.underlying_price - previous.underlying_price
        candidates.append((cheap, {
            "day": current.collected_at,
            "actual": current.last_price - previous.last_price,
            "delta": previous.delta * change_in_spot,
            "gamma": 0.5 * previous.gamma * change_in_spot ** 2,
            "vega": previous.vega * (current.implied_volatility - previous.implied_volatility) * 100,
            "theta": previous.theta * elapsed_days,
            "missing_trading_days": max(
                0,
                int(np.busday_count(previous.collected_at.date(), current.collected_at.date())) - 1,
            ),
            "_start": previous.last_price,
            "_end": current.last_price,
        }))

    best_start = best_length = run_start = run_length = 0
    for index, (cheap, _) in enumerate(candidates):
        if cheap:
            run_length = 0
            continue
        run_start = index if run_length == 0 else run_start
        run_length += 1
        if run_length > best_length:
            best_start, best_length = run_start, run_length
    if best_length == 0:
        return GreekAttribution(empty, dropped_cheap=len(candidates),
                                dropped_unreliable=dropped_unreliable)

    kept = [payload for _, payload in candidates[best_start:best_start + best_length]]
    frame = pd.DataFrame(kept)
    frame["residual"] = frame["actual"] - frame[list(ATTRIBUTION_TERMS)].sum(axis=1)
    return GreekAttribution(
        frame[columns],
        start_price=float(kept[0]["_start"]),
        end_price=float(kept[-1]["_end"]),
        dropped_cheap=len(candidates) - best_length,
        dropped_unreliable=dropped_unreliable,
        missing_trading_days=int(frame["missing_trading_days"].sum()),
    )
