"""Единственный модуль, обращающийся к внешнему источнику данных (yfinance).
Ничего, кроме этого модуля, не делает сетевых запросов."""

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
        except Exception as exc:  # yfinance/requests кидают разные типы ошибок и рейт-лимитов
            last_error = exc
            if attempt < config.MAX_FETCH_RETRIES - 1:
                time.sleep(config.BACKOFF_BASE_SECONDS * (2 ** attempt))
    raise last_error


def fetch_ticker_snapshot(ticker: str) -> tuple[float, pd.DataFrame]:
    """Возвращает (цена базового актива, вся опционная цепочка по всем экспирациям)."""
    ticker_obj = yf.Ticker(ticker)
    underlying_price = _with_retry(_fetch_underlying_price, ticker_obj)
    expiries = _with_retry(lambda: ticker_obj.options)

    frames = [_with_retry(_fetch_chain_for_expiry, ticker_obj, expiry) for expiry in expiries]
    chain_df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return underlying_price, chain_df


def fetch_price_history(ticker: str, period: str = "6mo") -> pd.DataFrame:
    """Дневная история закрытий БА напрямую от yfinance — для реализованной
    волатильности (ТЗ, FR24). Отдельный лёгкий read-only запрос, не связанный
    с collect_watchlist: не пишет в БД, не участвует в проверке качества
    снэпшота, не завязан на то, сколько дней мы уже собираем опционные цепочки."""
    history = _with_retry(lambda: yf.Ticker(ticker).history(period=period))
    if history.empty:
        return pd.DataFrame(columns=["close"])
    return history.rename(columns={"Close": "close"})[["close"]]


def _now_utc() -> datetime:
    """Naive datetime, UTC по соглашению. Без tzinfo — чтобы не ловить
    tz-aware/tz-naive конфликты при арифметике с датами экспирации в metrics.py,
    которые приходят из БД как naive (даты без времени)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _oi_zero_fraction(chain_df: pd.DataFrame) -> float:
    """Доля контрактов с open_interest=0 в свежесобранной цепочке. Реальный
    источник шума для обнаружения повреждённых ответов источника данных —
    см. `config.MAX_ZERO_OI_FRACTION`."""
    if chain_df.empty:
        return 1.0
    return float((chain_df["open_interest"].fillna(0) == 0).mean())


def collect_watchlist(conn, tickers: list[str]) -> dict[str, str]:
    """Собирает снэпшот по каждому тикеру из watchlist. Сбой одного тикера
    не прерывает сбор по остальным (ТЗ, FR12). Возвращает статус по каждому тикеру.

    Перед сохранением проверяется доля контрактов с open_interest=0 (ТЗ, FR23):
    источник данных иногда отвечает "успешно" (нет исключения, структура
    цепочки на месте — те же strike/expiry, volume и цены выглядят нормально),
    но open_interest и implied_volatility приходят почти сплошь нулевыми/около
    нулевыми. Такой снэпшот выглядит как обычный, но ломает GEX Heatmap
    (гамма считается от OI) и OI Delta (день-в-день сравнение). Проверка
    отсекает это до записи в БД — лучше пропустить сбор тикера в этом цикле,
    чем один раз тихо испортить историю."""
    results: dict[str, str] = {}
    for ticker in tickers:
        started_at = _now_utc()
        rows_fetched: int | None = None
        oi_zero_fraction: float | None = None
        try:
            underlying_price, chain_df = fetch_ticker_snapshot(ticker)
            if chain_df.empty:
                raise ValueError("Пустая опционная цепочка")
            rows_fetched = len(chain_df)
            oi_zero_fraction = _oi_zero_fraction(chain_df)
            if oi_zero_fraction > config.MAX_ZERO_OI_FRACTION:
                raise ValueError(
                    f"{oi_zero_fraction:.0%} контрактов с open_interest=0 (порог "
                    f"{config.MAX_ZERO_OI_FRACTION:.0%}) — источник данных, похоже, "
                    "вернул повреждённый/неполный снэпшот, не сохраняем"
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
