"""Чистые функции расчёта метрик. Вход — DataFrame из db.get_snapshots(),
выход — DataFrame/число. Без побочных эффектов, без обращения к БД или сети —
поэтому тестируются без поднятия базы и без сети."""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.interpolate import griddata
from scipy.stats import norm

from app import config


def _last_snapshot_per_day(df: pd.DataFrame) -> pd.DataFrame:
    """Схлопывает несколько внутридневных сборов до одного (последнего) на
    календарный день. Нужно там, где сравниваются значения, несопоставимые
    между собой в течение дня: volume у Yahoo — это накопленный объём с начала
    сессии (растёт до закрытия), а open_interest практически не обновляется
    внутри дня — сравнение "последних двух снэпшотов" без этого схлопывания
    может сравнивать два снэпшота одного и того же дня вместо дня с днём."""
    daily_latest = df.groupby(df["collected_at"].dt.date)["collected_at"].transform("max")
    return df[df["collected_at"] == daily_latest]


def put_call_ratio(df: pd.DataFrame) -> pd.DataFrame:
    """По каждой дате сбора — соотношение путов к коллам по объёму и по OI (ТЗ, FR4)."""
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
    """Страйк с минимальной суммарной выплатой продавцам опционов на указанную
    экспирацию, по последнему доступному снэпшоту (ТЗ, FR5)."""
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
    """Все греки по формулам Блэка-Шоулза без учёта дивидендной доходности (q=0) —
    та же упрощающая предпосылка, что уже использовалась для гаммы в GEX (ТЗ, FR6, FR14).
    При q=0 charm совпадает для call и put (delta_put = delta_call - const, поэтому их
    производные по времени равны) — сознательно не считаем его дважды.
    Theta — за календарный день, vega/rho — на 1 п.п. изменения IV/ставки (принятые
    у трейдеров единицы, не «сырые» частные производные за год)."""
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
    """Векторизованный аналог `_black_scholes_greeks` — считает греки для всех
    строк сразу (скринер, ТЗ FR25), а не в python-цикле по контрактам. Формулы
    идентичны, только на numpy-массивах вместо скаляров."""
    valid = (years_to_expiry > 0) & (iv > 0) & (spot > 0)
    # под невалидными строками считаем на заглушках (1.0), чтобы не ловить
    # log(0)/деление на ноль — сами значения ниже обнуляются через .where(valid)
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
    """Плоская таблица всех контрактов последнего снэпшота тикера с ДТЕ и полным
    набором греков (ТЗ, FR25) — основа для скринера с фильтрами по диапазонам.
    Только последний снэпшот, не вся история: скринер — срез "что есть сейчас",
    а не time series (для истории по конкретному контракту есть вкладка «Опцион»)."""
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
    """Приближённый профиль дилерской GEX по strike для указанной экспирации (ТЗ, FR6).
    `as_of=None` — последний снэпшот (текущее состояние); иначе — конкретная дата сбора
    из истории (Replay, ТЗ раздел 12, GEX Heatmap). Знак: путы учитываются с отрицательным
    вкладом — эвристика "дилеры net long путы / net short коллы от розничного потока", НЕ
    отражает реальные позиции маркет-мейкеров (см. дисклеймер в UI)."""
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
    """Суммарный net GEX по экспирации (ТЗ, FR15) — знак определяет режим:
    положительный — дилеры сглаживают движение цены, отрицательный — усиливают."""
    if gex_profile.empty:
        return 0.0
    return float(gex_profile["gex"].sum())


def gex_matrix(
    df: pd.DataFrame,
    as_of: pd.Timestamp | None = None,
    risk_free_rate: float = config.RISK_FREE_RATE,
    expiries: list | None = None,
) -> pd.DataFrame:
    """Матрица GEX strike × expiry на один снэпшот сразу (ТЗ, раздел 12, GEX Heatmap).
    Строится переиспользованием `gamma_exposure_profile` в цикле по экспирациям; новых
    данных не требует — один снэпшот уже содержит полную цепочку. `expiries=None` —
    все экспирации снэпшота; UI обычно передаёт только видимое подмножество (у
    тикера может быть 30+ экспираций, включая дальние LEAPS — считать профиль по
    всем сразу расточительно, если показывается только часть). Индекс — strike по
    убыванию (сверху вниз, как в привычном heatmap), колонки — expiry."""
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
    # fill_value=0, не NaN: если у конкретной экспирации нет листинга на этом
    # strike, дилерская экспозиция там действительно нулевая (не "неизвестна") —
    # так пропадают визуальные "дыры" в матрице, а не просто маскируются.
    matrix = combined.pivot_table(index="strike", columns="expiry", values="gex", aggfunc="sum", fill_value=0)
    return matrix.sort_index(ascending=False)


def net_gex_by_expiry(
    df: pd.DataFrame,
    as_of: pd.Timestamp | None = None,
    risk_free_rate: float = config.RISK_FREE_RATE,
    expiries: list | None = None,
) -> pd.DataFrame:
    """Net GEX отдельно по каждой экспирации снэпшота (сайдбар GEX Heatmap) —
    то же самое, что `net_gamma_exposure`, но по всем экспирациям сразу (или по
    переданному подмножеству — см. `gex_matrix`)."""
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
    """Call Wall / Put Wall — страйк с максимальным open interest по коллам/путам,
    агрегированным по экспирациям снэпшота — всем по умолчанию, либо по переданному
    подмножеству (частый прокси для уровней поддержки/сопротивления от хедж-потоков
    дилеров, ТЗ раздел 12)."""
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
    """Приближённая цена БА, при которой суммарная (по всем экспирациям) дилерская
    GEX меняет знак — прокси для "gamma flip" (ТЗ раздел 12). Упрощение: берём уже
    посчитанный per-strike GEX-профиль (на фактической текущей цене БА, как и везде
    в приложении), суммируем по экспирациям, идём по strike по возрастанию и ищем,
    где кумулятивная сумма меняет знак — с линейной интерполяцией между двумя
    ближайшими страйками, чтобы получить не только страйк, а ценовой уровень.
    Не переоценивает греки на сетке гипотетических цен БА (это было бы точнее, но
    существенно дороже по вычислениям) — тот же класс допущения, что уже
    используется для net GEX (ТЗ, FR6/FR15)."""
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
    """Контракты последнего снэпшота с аномальным объёмом (ТЗ, FR16). Флаг —
    z-score объёма относительно собственной истории контракта выше `z_threshold`
    (а не плоский множитель — иначе на ликвидных тикерах флагуются тысячи строк).
    Контрактам с историей короче `min_history_points` z-score не доверяем —
    для них используется грубый fallback (volume > 2×OI). `min_volume` отсекает
    шум по неликвидным дальним страйкам независимо от статистики.

    История для среднего/std схлопывается до одного снэпшота на календарный
    день (`_last_snapshot_per_day`) — volume у Yahoo это накопленный объём с
    начала сессии, и без схлопывания несколько сборов за один день исказили бы
    среднее/дисперсию смешением разных моментов внутри торгового дня."""
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
    """Уровень 1 (ТЗ, FR8a): volume-weighted среднее IV по всей цепочке, по датам сбора."""
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
    """Реализованная (историческая) волатильность БА close-to-close, annualized
    (ТЗ, FR24). `price_history` — дневные закрытия с колонкой "close", НЕ наша
    собственная история снэпшотов: та слишком короткая и разреженная (несколько
    дней, редкие внутридневные точки) для честного расчёта — окно в 20-30 торговых
    дней растянулось бы на месяцы реального сбора. Вместо этого используется
    отдельная глубокая дневная история цен (yfinance), не зависящая от того,
    сколько мы уже собираем опционные цепочки.

    Возвращает {окно_в_днях: годовая волатильность} только для окон, для которых
    хватило истории; отсутствующие окна просто не попадают в результат."""
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
    """История цены, IV и всех греков конкретного контракта по датам сбора (ТЗ, FR14).
    Заменяет прежний iv_by_contract — тот же drill-down, плюс полный набор греков."""
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
    """Текстовая интерпретация последних значений греков и их динамики относительно
    предыдущего снэпшота (ТЗ, FR14) — генерация по шаблону на числах, не LLM-вызов."""
    if history.empty:
        return []

    latest = history.iloc[-1]
    prior = history.iloc[-2] if len(history) >= 2 else None

    def trend(col: str) -> str:
        if prior is None:
            return ""
        diff = latest[col] - prior[col]
        if abs(diff) < 1e-6:
            return " (без изменений с прошлого снэпшота)"
        return f" ({'рост' if diff > 0 else 'снижение'} с прошлого снэпшота)"

    return [
        f"Delta {latest['delta']:.2f}{trend('delta')} — при движении базового актива на $1 "
        f"цена контракта меняется примерно на ${abs(latest['delta']):.2f}.",
        f"Gamma {latest['gamma']:.4f}{trend('gamma')} — насколько быстро сама delta меняется "
        f"при движении актива на $1; чем выше, тем резче меняется хедж-потребность у дилеров.",
        f"Theta {latest['theta']:.2f}{trend('theta')} — контракт теряет примерно "
        f"${abs(latest['theta']):.2f} в день только от течения времени, при прочих равных.",
        f"Vega {latest['vega']:.2f}{trend('vega')} — при росте подразумеваемой волатильности "
        f"на 1 п.п. цена контракта меняется примерно на ${latest['vega']:.2f}.",
        f"Rho {latest['rho']:.2f}{trend('rho')} — чувствительность к процентной ставке, обычно "
        f"второстепенный фактор для опционов с горизонтом до года.",
        f"Vanna {latest['vanna']:.4f}{trend('vanna')} — как delta реагирует на изменение "
        f"волатильности (симметрично: как vega реагирует на движение цены).",
        f"Charm {latest['charm']:.4f}{trend('charm')} — насколько delta «состарится» за один "
        f"день при неизменной цене (time decay самой дельты, а не цены контракта).",
    ]


def oi_delta(df: pd.DataFrame) -> pd.DataFrame:
    """Разница open interest между последним и предыдущим календарным днём (не
    снэпшотом — если за день было несколько сборов, они схлопываются до
    последнего через `_last_snapshot_per_day`). Open interest у Yahoo почти не
    обновляется внутри дня, поэтому сравнение двух снэпшотов одного и того же
    дня почти всегда даёт дельту 0 и не отражает реальное день-в-день изменение
    (ТЗ, FR9)."""
    daily = _last_snapshot_per_day(df)
    dates = sorted(daily["collected_at"].unique())
    if len(dates) < 2:
        return pd.DataFrame(columns=["expiry", "strike", "option_type", "open_interest", "oi_delta", "oi_delta_pct"])

    latest_date, previous_date = dates[-1], dates[-2]
    contract_keys = ["expiry", "strike", "option_type"]

    latest = daily[daily["collected_at"] == latest_date].set_index(contract_keys)["open_interest"]
    previous = daily[daily["collected_at"] == previous_date].set_index(contract_keys)["open_interest"]

    result = pd.DataFrame({"open_interest": latest, "oi_delta": latest - previous}).dropna()
    # % от предыдущего значения — сама по себе абсолютная дельта не показывает
    # масштаб (прирост на 1000 контрактов — это много при OI=2000 и почти ничего
    # при OI=200000). previous=0 даёт NaN (открытие "с нуля" не выражается в %).
    result["oi_delta_pct"] = (result["oi_delta"] / previous.replace(0, np.nan)) * 100
    result = result.reset_index()

    # Сортировка по модулю абсолютной дельты — иначе крупные ОТРИЦАТЕЛЬНЫЕ
    # изменения (закрытие позиций) проваливаются в конец таблицы, хотя по
    # значимости они не менее важны, чем рост. Знак сохраняется в значении.
    return result.sort_values("oi_delta", key=abs, ascending=False)


def iv_surface(df: pd.DataFrame) -> pd.DataFrame:
    """Точки поверхности волатильности на последнем снэпшоте: для каждого strike
    берётся IV OTM-контракта (put ниже цены БА, call выше — стандартная практика
    построения vol surface, ITM-котировки обычно менее ликвидны и зашумлены).
    Возвращает длинный формат (strike, expiry, years_to_expiry, implied_volatility),
    готовый для интерполяции/визуализации. Строки с нулевым/отсутствующим IV
    отбрасываются — их обычно не строят, дают достраивать (`iv_surface_grid`)."""
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
    """Достраивает регулярную сетку strike × years_to_expiry поверх (обычно
    неровных — не у всех экспираций одинаковые страйки, дальние даты грубее)
    точек `iv_surface` линейной интерполяцией (`griddata`). Линейная интерполяция
    не определена за пределами выпуклой оболочки точек (края сетки) — там
    достраивается ближайшим соседом, чтобы на поверхности не было дыр/NaN.
    None, если точек недостаточно для интерполяции (нужно ≥2 разных strike и
    ≥2 разных expiry — иначе точки лежат на одной линии, а не на поверхности)."""
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
