"""Yahoo Finance via yfinance — the default provider.

Moved here from collector.py and reshaped into a class; no behaviour changed in
the move. Same per-expiry retry granularity, same column renaming, same column
order.

This is the source that requires nothing from you — no account, no token, no
card — and it stays the default for exactly that reason. Its limitations are
real and documented in the README: the API is unofficial, the data is delayed,
and there is no SLA. That is the trade a free default makes.
"""

from __future__ import annotations

import pandas as pd
import yfinance as yf

from app.providers.base import CHAIN_COLUMNS, ProviderStatus, with_retry

CHAIN_RENAMES = {
    "lastPrice": "last_price",
    "openInterest": "open_interest",
    "impliedVolatility": "implied_volatility",
    "inTheMoney": "in_the_money",
}


class YahooProvider:
    name = "yahoo"
    price_history_source = "yahoo"
    requires_token = False
    token: str | None = None  # nothing to authenticate with, nothing to scrub

    def _fetch_underlying_price(self, ticker_obj: yf.Ticker) -> float:
        return float(ticker_obj.fast_info["lastPrice"])

    def fetch_underlying_price(self, ticker: str) -> float:
        """Spot price on its own, without pulling a whole option chain.

        Public rather than private because an options-only provider needs it:
        spot is not optional — it is the input every greek, max pain and GEX
        figure is built on — and a vendor that sells only option chains has no
        way to supply it.
        """
        return with_retry(self._fetch_underlying_price, yf.Ticker(ticker))

    def _fetch_chain_for_expiry(self, ticker_obj: yf.Ticker, expiry: str) -> pd.DataFrame:
        chain = ticker_obj.option_chain(expiry)

        calls = chain.calls.copy()
        calls["option_type"] = "call"
        puts = chain.puts.copy()
        puts["option_type"] = "put"

        combined = pd.concat([calls, puts], ignore_index=True)
        combined["expiry"] = expiry
        combined = combined.rename(columns=CHAIN_RENAMES)

        # Yahoo serves no greeks. The columns exist so that every provider hands
        # back the same shape; they stay empty here and the reader computes
        # greeks from implied volatility instead. Explicitly None rather than 0
        # — a zero delta is a real value a deep-OTM contract can have, and
        # storing 0 would be indistinguishable from "the provider said so".
        for greek in ("delta", "gamma", "theta", "vega"):
            combined[greek] = None

        return combined[CHAIN_COLUMNS]

    def fetch_ticker_snapshot(self, ticker: str) -> tuple[float, pd.DataFrame]:
        ticker_obj = yf.Ticker(ticker)
        underlying_price = with_retry(self._fetch_underlying_price, ticker_obj)
        expiries = with_retry(lambda: ticker_obj.options)

        frames = [
            with_retry(self._fetch_chain_for_expiry, ticker_obj, expiry) for expiry in expiries
        ]
        chain_df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        return underlying_price, chain_df

    def fetch_price_history(self, ticker: str, period: str = "6mo") -> pd.DataFrame:
        """Daily close history of the underlying, for realized volatility. A
        separate lightweight read-only request, unrelated to collect_watchlist:
        it doesn't write to the database, doesn't take part in snapshot quality
        checks, and doesn't depend on how many days of option chains have
        already been collected."""
        history = with_retry(lambda: yf.Ticker(ticker).history(period=period))
        if history.empty:
            return pd.DataFrame(columns=["close"])
        return history.rename(columns={"Close": "close"})[["close"]]

    def check_access(self) -> ProviderStatus:
        """yfinance needs no credentials, so there is nothing to verify beyond
        "is Yahoo answering at all". Kept for interface symmetry — a caller
        checks access without caring which provider is active."""
        try:
            with_retry(lambda: yf.Ticker("SPY").fast_info["lastPrice"])
        except Exception as exc:
            return ProviderStatus(False, f"Yahoo Finance is not responding: {exc}")
        return ProviderStatus(True, "Yahoo Finance is reachable. No token required.")
