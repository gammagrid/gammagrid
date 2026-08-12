"""Collection orchestration. Everything here is provider-agnostic.

This module used to be "the only module that talks to the external data
source". That is now app/providers/ — but the rule it protected still holds:
nothing outside app/providers/ makes network requests, and nothing inside
app/providers/ knows about the database.

What stays here is the part that is the same whichever source is active: the
quality gate, run logging, and the guarantee that one failing ticker cannot
take down the rest of the batch.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from app import config, db, providers


def fetch_ticker_snapshot(
    ticker: str, provider: providers.DataProvider | None = None
) -> tuple[float, pd.DataFrame]:
    """Kept as a module-level function for call sites that predate providers."""
    return (provider or providers.get_provider()).fetch_ticker_snapshot(ticker)


def fetch_price_history(
    ticker: str, period: str = "6mo", provider: providers.DataProvider | None = None
) -> pd.DataFrame:
    return (provider or providers.get_provider()).fetch_price_history(ticker, period)


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


def collect_watchlist(
    conn, tickers: list[str], provider: providers.DataProvider | None = None
) -> dict[str, str]:
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
    history once.

    A warning if you add a provider: this threshold was calibrated against
    Yahoo. The hosted version had to scope it per source after a wider chain
    tripped it for five days on a ticker whose data was perfectly good — a
    provider that serves the full chain, including everything untraded, has a
    much higher baseline of zero-OI contracts and is not corrupt for it. The
    fraction is computed and logged either way.

    Every row and every log line is stamped with the provider's name: charts
    must never mix sources, and that is only enforceable if the source is
    recorded at write time.
    """
    active = provider or providers.get_provider()
    results: dict[str, str] = {}
    for ticker in tickers:
        started_at = _now_utc()
        rows_fetched: int | None = None
        oi_zero_fraction: float | None = None
        try:
            underlying_price, chain_df = active.fetch_ticker_snapshot(ticker)
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
            db.insert_snapshot(
                conn, ticker, started_at, underlying_price, chain_df, source=active.name
            )
            db.log_run(
                conn, started_at, _now_utc(), ticker, "success",
                rows_fetched=rows_fetched, oi_zero_fraction=oi_zero_fraction,
            )
            results[ticker] = "success"
        except Exception as exc:
            message = _scrub(str(exc), active)
            db.log_run(
                conn, started_at, _now_utc(), ticker, "failed", message,
                rows_fetched=rows_fetched, oi_zero_fraction=oi_zero_fraction,
            )
            results[ticker] = f"failed: {message}"
    return results


def _scrub(message: str, provider: providers.DataProvider) -> str:
    """Strips the provider's credential out of an error message before it is
    stored.

    Yahoo has no token, so today this does nothing — it is here because the
    moment someone adds an authenticated provider it stops doing nothing.
    Collection failures are written to collection_runs and rendered verbatim in
    the interface, and HTTP client errors routinely quote the request URL. A
    key that leaks into that log is readable by anyone who can open the page.
    """
    secret = getattr(provider, "token", None)
    if not secret:
        return message
    return message.replace(secret, "***")
