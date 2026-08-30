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

from app.providers.base import (
    CHAIN_COLUMNS,
    CHAIN_OPTIONAL_COLUMNS,
    ProviderStatus,
    with_retry,
)

CHAIN_RENAMES = {
    "lastPrice": "last_price",
    "openInterest": "open_interest",
    "impliedVolatility": "implied_volatility",
    "inTheMoney": "in_the_money",
    # Yahoo serves the contract's own OCC symbol, and it is the only thing that
    # distinguishes an adjusted series from the standard one at the same strike
    # and expiry. It used to be dropped on the way out of this function, which
    # is what made a split-adjusted ticker fail to collect at all.
    "contractSymbol": "contract_symbol",
}


# What these instruments are called at Yahoo, where they are index QUOTES
# rather than option chains. Yahoo has no chain for any of them, so nothing in
# this file collects them — but the price history behind realized volatility is
# a different endpoint, and it answers for the caret form.
#
# ONE TRANSLATION, IN ONE PLACE. The sibling product learned this the expensive
# way: the map was wired into the spot lookup alone, and the price-history
# lookup then failed with "possibly delisted; no price data found" for every
# index ticker. Nothing crashed, because that caller swallows failures and
# returns nothing — so realized volatility was simply absent, with no symptom.
# One translation used in one of two places is the same bug twice.
INDEX_SYMBOLS = {
    "SPX": "^GSPC",
    "XSP": "^XSP",
    "NDX": "^NDX",
    "RUT": "^RUT",
    "VIX": "^VIX",
    "DJX": "^DJI",
}


def _yahoo_symbol(ticker: str) -> str:
    """What Yahoo calls this instrument. The single place the translation
    happens, so that adding a lookup elsewhere cannot forget it."""
    return INDEX_SYMBOLS.get(ticker.upper(), ticker)


class YahooProvider:
    name = "yahoo"
    price_history_source = "yahoo"
    requires_token = False

    # WHAT YAHOO WILL NOT SERVE, AND WHAT TO OFFER INSTEAD.
    #
    # These are not broken symbols and not typos. SPX, XSP, NDX, RUT and DJX
    # are listed, real and heavily traded; Yahoo has no chain endpoint for a
    # cash-settled index, which is a limitation of this source and not of the
    # options market. Telling somebody who trades SPX that it "has no options"
    # is plainly false, they know it is false, and it costs every other message
    # this product prints its credibility.
    #
    # The substitutes track the same underlying, so the analytics carry over:
    # SPY holds the S&P 500 that SPX and the mini XSP are written on, QQQ holds
    # the Nasdaq 100 behind NDX.
    #
    # VIX MAPS TO NOTHING ON PURPOSE. An ETF on VIX futures is a different
    # instrument with its own term structure, not a proxy for the index, and
    # offering one would be worse than offering nothing.
    unsupported_symbols = {
        "SPX": "SPY", "SPXW": "SPY", "XSP": "SPY",
        "NDX": "QQQ", "NDXP": "QQQ",
        "RUT": "IWM", "DJX": "DIA",
        "VIX": None, "VIXW": None,
    }
    token: str | None = None  # nothing to authenticate with, nothing to scrub

    def _fetch_underlying_price(self, ticker_obj: yf.Ticker) -> float:
        """The spot price, or a refusal a person can act on.

        `float(fast_info["lastPrice"])` used to be the whole body, and what a
        user saw when it went wrong was one of these, verbatim, in the
        collection log:

            float() argument must be a string or a real number, not 'NoneType'
            'currentTradingPeriod'

        Neither says what happened or what to do, and neither is stable: the
        same missing symbol raised a KeyError one week and a TypeError the
        next, because yfinance's shape for "nothing here" changes with its
        version. So the check is on the VALUE — there is no price — rather
        than on the type of the explosion, and the message names both causes
        the user can actually be in.
        """
        try:
            price = ticker_obj.fast_info["lastPrice"]
        except Exception:  # noqa: BLE001 — every shape of "not there" means the same thing
            price = None
        if price is None:
            raise ValueError(
                f"no price for {ticker_obj.ticker} — either the symbol does not exist, "
                "or Yahoo Finance is rate-limiting this installation. Check the spelling "
                "first; if it is right, wait a few minutes and collect again."
            )
        return float(price)

    def fetch_underlying_price(self, ticker: str) -> float:
        """Spot price on its own, without pulling a whole option chain.

        Public rather than private because an options-only provider needs it:
        spot is not optional — it is the input every greek, max pain and GEX
        figure is built on — and a vendor that sells only option chains has no
        way to supply it.
        """
        return with_retry(self._fetch_underlying_price, yf.Ticker(_yahoo_symbol(ticker)))

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

        extras = [name for name in CHAIN_OPTIONAL_COLUMNS if name in combined.columns]
        return combined[CHAIN_COLUMNS + extras]

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
        history = with_retry(lambda: yf.Ticker(_yahoo_symbol(ticker)).history(period=period))
        if history.empty:
            return pd.DataFrame(columns=["close"])
        return history.rename(columns={"Close": "close"})[["close"]]

    def underlying_has_options(self, ticker: str) -> bool | None:
        """Does this symbol have options here at all — True, False, or "no idea".

        Yahoo's answer is its expiry list: no expiries, no chain to collect.

        THREE-VALUED ON PURPOSE. None means the question could not be answered
        — Yahoo was unreachable, rate-limiting, or slow — and the caller must
        let the ticker through on None. Refusing a valid symbol because a free
        data source had a bad minute is a worse failure than the typo this
        catches: the typo is visible in the collection log within the hour, and
        a wrongly rejected ticker looks like the product being broken.

        Not part of the DataProvider Protocol, and called through getattr, so
        that a provider written before this existed is not suddenly an invalid
        provider. A source that cannot answer simply does not offer the method.
        """
        try:
            return bool(yf.Ticker(ticker).options)
        except Exception:  # noqa: BLE001 — every failure means "could not find out"
            return None

    def check_access(self) -> ProviderStatus:
        """yfinance needs no credentials, so there is nothing to verify beyond
        "is Yahoo answering at all". Kept for interface symmetry — a caller
        checks access without caring which provider is active."""
        try:
            with_retry(lambda: yf.Ticker("SPY").fast_info["lastPrice"])
        except Exception as exc:
            return ProviderStatus(False, f"Yahoo Finance is not responding: {exc}")
        return ProviderStatus(True, "Yahoo Finance is reachable. No token required.")
