"""The only module that talks to the external data source (yfinance).
Nothing outside this module makes network requests."""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

from app import config, db

CHAIN_COLUMNS = {
    "lastPrice": "last_price",
    "openInterest": "open_interest",
    "impliedVolatility": "implied_volatility",
    "inTheMoney": "in_the_money",
}


def _fetch_underlying_price(ticker_obj: yf.Ticker) -> float:
    return float(ticker_obj.fast_info["lastPrice"])


def _fetch_chain_for_expiry(ticker_obj: yf.Ticker, expiry: str) -> pd.DataFrame:
    chain = ticker_obj.option_chain(expiry)

    calls = chain.calls.copy()
    calls["option_type"] = "call"
    puts = chain.puts.copy()
    puts["option_type"] = "put"

    combined = pd.concat([calls, puts], ignore_index=True)
    combined["expiry"] = expiry
    combined = combined.rename(columns=CHAIN_COLUMNS)

    return combined[[
        "expiry", "strike", "option_type", "last_price", "bid", "ask",
        "volume", "open_interest", "implied_volatility", "in_the_money",
    ]]


def _with_retry(fn, *args, **kwargs):
    last_error: Exception | None = None
    for attempt in range(config.MAX_FETCH_RETRIES):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # yfinance/requests raise assorted error and rate-limit types
            last_error = exc
            if attempt < config.MAX_FETCH_RETRIES - 1:
                time.sleep(config.BACKOFF_BASE_SECONDS * (2 ** attempt))
    raise last_error


def fetch_ticker_snapshot(ticker: str) -> tuple[float, pd.DataFrame]:
    """Returns (underlying price, the full option chain across all expiries)."""
    ticker_obj = yf.Ticker(ticker)
    underlying_price = _with_retry(_fetch_underlying_price, ticker_obj)
    expiries = _with_retry(lambda: ticker_obj.options)

    frames = [_with_retry(_fetch_chain_for_expiry, ticker_obj, expiry) for expiry in expiries]
    chain_df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return underlying_price, chain_df


def fetch_price_history(ticker: str, period: str = "6mo") -> pd.DataFrame:
    """Daily close history of the underlying, straight from yfinance — for
    realized volatility (spec FR24). A separate lightweight read-only request,
    unrelated to collect_watchlist: it doesn't write to the DB, doesn't take
    part in snapshot quality checks, and doesn't depend on how many days of
    option chains we have already collected."""
    history = _with_retry(lambda: yf.Ticker(ticker).history(period=period))
    if history.empty:
        return pd.DataFrame(columns=["close"])
    return history.rename(columns={"Close": "close"})[["close"]]


def _now_utc() -> datetime:
    """Naive datetime, UTC by convention. No tzinfo — avoids tz-aware/tz-naive
    conflicts in date arithmetic with expiry dates in metrics.py, which come
    from the DB as naive (dates without time)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _oi_zero_fraction(chain_df: pd.DataFrame) -> float:
    """Fraction of contracts with open_interest=0 in a freshly collected
    chain. The real-world signal for detecting corrupted data-source
    responses — see `config.MAX_ZERO_OI_FRACTION`."""
    if chain_df.empty:
        return 1.0
    return float((chain_df["open_interest"].fillna(0) == 0).mean())


def collect_watchlist(conn, tickers: list[str]) -> dict[str, str]:
    """Collects a snapshot for every ticker in the watchlist. One ticker
    failing does not interrupt collection for the rest (spec FR12). Returns
    a status per ticker.

    Before saving, the fraction of contracts with open_interest=0 is checked
    (spec FR23): the data source sometimes responds "successfully" (no
    exception, chain structure intact — same strikes/expiries, volume and
    prices look normal) but open_interest and implied_volatility come back
    almost entirely zero/near-zero. Such a snapshot looks ordinary but breaks
    the GEX Heatmap (gamma is computed from OI) and OI Delta (day-over-day
    comparison). The check rejects it before it reaches the DB — better to
    skip one collection cycle for a ticker than to silently corrupt the
    history once."""
    results: dict[str, str] = {}
    for ticker in tickers:
        started_at = _now_utc()
        rows_fetched: int | None = None
        oi_zero_fraction: float | None = None
        try:
            underlying_price, chain_df = fetch_ticker_snapshot(ticker)
            if chain_df.empty:
                raise ValueError("Empty option chain")
            rows_fetched = len(chain_df)
            oi_zero_fraction = _oi_zero_fraction(chain_df)
            if oi_zero_fraction > config.MAX_ZERO_OI_FRACTION:
                raise ValueError(
                    f"{oi_zero_fraction:.0%} of contracts have open_interest=0 "
                    f"(threshold {config.MAX_ZERO_OI_FRACTION:.0%}) — either the data "
                    "source returned a corrupted/incomplete snapshot, or the market is "
                    "closed right now (Yahoo Finance commonly reports open_interest=0 "
                    "across the whole chain outside regular trading hours); not saving. "
                    "Try again while the market is open."
                )
            db.insert_snapshot(conn, ticker, started_at, underlying_price, chain_df)
            db.log_run(
                conn, started_at, _now_utc(), ticker, "success",
                rows_fetched=rows_fetched, oi_zero_fraction=oi_zero_fraction,
            )
            results[ticker] = "success"
        except Exception as exc:
            db.log_run(
                conn, started_at, _now_utc(), ticker, "failed", str(exc),
                rows_fetched=rows_fetched, oi_zero_fraction=oi_zero_fraction,
            )
            results[ticker] = f"failed: {exc}"
    return results
