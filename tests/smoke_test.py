"""Ручная проверка плюмбинга db.py + metrics.py на синтетических данных,
без сети и без реального yfinance. Не часть pytest-сюиты — просто быстрый
прогон при разработке. Запуск: python tests/smoke_test.py"""

import os
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["OPTIONS_TRACKER_DB"] = "/tmp/options_tracker_smoke_test.db"

from app import collector, db, metrics  # noqa: E402

EXPIRY = "2026-09-18"
STRIKES = [90, 95, 100, 105, 110]


def make_chain(iv_shift: float, strike_100_volume: int, strike_100_oi: int) -> pd.DataFrame:
    """strike_100_volume/oi varies per snapshot to exercise дневную нормализацию;
    остальные страйки держим стабильными, чтобы не путать с общим ростом активности."""
    rows = []
    for strike in STRIKES:
        for option_type, base_iv in [("call", 0.30), ("put", 0.35)]:
            volume = strike_100_volume if strike == 100 else (20 + strike % 3)
            oi = strike_100_oi if strike == 100 else 50
            rows.append({
                "expiry": EXPIRY,
                "strike": float(strike),
                "option_type": option_type,
                "last_price": 2.5,
                "bid": 2.4,
                "ask": 2.6,
                "volume": volume,
                "open_interest": oi,
                "implied_volatility": base_iv + iv_shift,
                "in_the_money": strike < 100 if option_type == "call" else strike > 100,
            })
    return pd.DataFrame(rows)


def main():
    if os.path.exists(os.environ["OPTIONS_TRACKER_DB"]):
        os.remove(os.environ["OPTIONS_TRACKER_DB"])

    conn = db.get_connection()
    db.add_ticker(conn, "TEST")
    assert db.get_watchlist(conn) == ["TEST"]

    base_day = datetime(2026, 7, 1, 21, 0)
    # Дни 1-5: один сбор в день, OI растёт понемногу день ото дня (95..100, OI 50..61).
    # День 6: ДВА сбора (утро и вечер) — тот самый сценарий из бага: OI внутри дня
    # не меняется (65->65), а volume растёт как накопленный дневной объём (200->500).
    daily_snapshots = [
        # (offset_hours_from_base, volume, open_interest)
        (0, 95, 50),
        (24, 105, 52),
        (48, 90, 55),
        (72, 110, 58),
        (96, 100, 61),
        (120 - 12, 200, 65),  # день 6, утро
        (120, 500, 65),       # день 6, вечер — та же OI, что и утром
    ]

    for i, (offset_hours, volume, oi) in enumerate(daily_snapshots):
        moment = base_day + timedelta(hours=offset_hours)
        db.insert_snapshot(
            conn, "TEST", moment, underlying_price=100.0 + i * 0.1,
            chain_df=make_chain(iv_shift=i * 0.01, strike_100_volume=volume, strike_100_oi=oi),
        )
    latest_moment = base_day + timedelta(hours=daily_snapshots[-1][0])
    db.log_run(conn, latest_moment, latest_moment + timedelta(seconds=1), "TEST", "success")

    df = db.get_snapshots(conn, "TEST")
    assert len(df) == 10 * len(daily_snapshots), f"unexpected row count: {len(df)}"

    pcr = metrics.put_call_ratio(df)
    print("Put/Call Ratio:\n", pcr, "\n")
    assert len(pcr) == len(daily_snapshots)

    expiry = pd.Timestamp(EXPIRY)

    mp = metrics.max_pain(df, expiry)
    print("Max Pain:", mp, "\n")
    assert mp is not None

    gex = metrics.gamma_exposure_profile(df, expiry)
    print("GEX profile:\n", gex, "\n")
    assert not gex.empty

    net_gex = metrics.net_gamma_exposure(gex)
    print("Net GEX:", net_gex, "\n")
    assert isinstance(net_gex, float)

    unusual = metrics.unusual_activity(df)
    print("Unusual activity (z-score based, daily-collapsed history):\n", unusual, "\n")
    assert (unusual["strike"] == 100.0).any(), "strike 100 had a clear volume spike on day 6 evening, should be flagged"
    assert len(unusual) <= 4, f"expected only strike-100 call/put flagged (z-score), got {len(unusual)} rows"
    flagged_call = unusual[(unusual["strike"] == 100.0) & (unusual["option_type"] == "call")].iloc[0]
    # день 6 утро (volume=200) не должен попасть в историю дважды — история
    # должна схлопнуться до 6 календарных дней (1-5 + утро дня 6), не 6 сырых строк с дублем
    assert flagged_call["avg_volume"] < 200, (
        f"история должна включать день 6 (утро, volume=200) только один раз, "
        f"avg_volume={flagged_call['avg_volume']} выглядит нетронутым схлопыванием"
    )

    iv_avg = metrics.iv_weighted_average(df)
    print("IV weighted average:\n", iv_avg, "\n")
    assert len(iv_avg) == len(daily_snapshots)

    greeks_history = metrics.contract_greeks_history(df, 100.0, expiry, "call")
    print("Greeks history:\n", greeks_history, "\n")
    assert len(greeks_history) == len(daily_snapshots)
    for col in ("delta", "gamma", "theta", "vega", "rho", "vanna", "charm"):
        assert col in greeks_history.columns

    notes = metrics.interpret_greeks(greeks_history)
    print("Greeks interpretation:")
    for note in notes:
        print(" -", note)
    print()
    assert len(notes) == 7

    delta = metrics.oi_delta(df)
    print("OI delta (day-over-day, collapsed):\n", delta, "\n")
    assert not delta.empty
    strike_100_row = delta[(delta["strike"] == 100.0) & (delta["option_type"] == "call")].iloc[0]
    # Без схлопывания старый код сравнил бы два сегодняшних сбора (OI 65 vs 65) => 0.
    # С фиксом сравниваются день 6 (65) и день 5 (61) => +4.
    assert strike_100_row["oi_delta"] == 4, f"expected day-over-day OI delta of +4 (65-61), got {strike_100_row['oi_delta']}"
    assert strike_100_row["open_interest"] == 65, f"expected latest OI of 65, got {strike_100_row['open_interest']}"
    expected_pct = 4 / 61 * 100
    assert abs(strike_100_row["oi_delta_pct"] - expected_pct) < 1e-9, (
        f"expected oi_delta_pct ~{expected_pct:.3f}, got {strike_100_row['oi_delta_pct']}"
    )

    matrix = metrics.gex_matrix(df)
    print("GEX matrix (strike x expiry):\n", matrix, "\n")
    assert not matrix.empty
    assert expiry in matrix.columns
    assert (matrix.index == sorted(matrix.index, reverse=True)).all(), "strikes should be sorted descending"
    assert not matrix.isna().any().any(), "missing strike/expiry combos should fill with 0, not NaN"

    net_by_expiry = metrics.net_gex_by_expiry(df)
    print("Net GEX by expiry:\n", net_by_expiry, "\n")
    assert len(net_by_expiry) == 1  # синтетика содержит только одну expiry
    assert abs(net_by_expiry["net_gex"].iloc[0] - net_gex) < 1e-6, "should match net_gamma_exposure for the same expiry"

    walls = metrics.dealer_walls(df)
    print("Dealer walls:", walls, "\n")
    # strike 100 держит самый высокий OI (65) из всех страйков синтетики (у остальных - 50)
    assert walls["call_wall"] == 100.0, f"expected call wall at strike 100, got {walls['call_wall']}"
    assert walls["put_wall"] == 100.0, f"expected put wall at strike 100, got {walls['put_wall']}"

    flip = metrics.gamma_flip_price(df)
    print("Gamma flip price:", flip, "\n")
    # синтетика не гарантирует смену знака (весь профиль может остаться одного
    # знака) — важно, что функция не падает и возвращает либо None, либо float
    assert flip is None or isinstance(flip, float)

    # Replay: as_of на более раннюю дату должен воспроизводить исторический снэпшот,
    # а не тихо падать обратно на последний
    earlier_date = pd.Timestamp(base_day)
    matrix_earlier = metrics.gex_matrix(df, as_of=earlier_date)
    assert not matrix_earlier.empty
    assert not matrix_earlier.equals(matrix), "historical snapshot should differ from the latest one"

    # iv_surface/iv_surface_grid — отдельный небольшой синтетический снэпшот с
    # двумя экспирациями (основная синтетика выше использует только одну, а для
    # поверхности нужно минимум 2x2 разных strike/expiry)
    iv_snapshot = pd.DataFrame([
        {"collected_at": latest_moment, "underlying_price": 100.0, "expiry": pd.Timestamp("2026-08-01"),
         "strike": 90.0, "option_type": "put", "implied_volatility": 0.35},
        {"collected_at": latest_moment, "underlying_price": 100.0, "expiry": pd.Timestamp("2026-08-01"),
         "strike": 110.0, "option_type": "call", "implied_volatility": 0.30},
        {"collected_at": latest_moment, "underlying_price": 100.0, "expiry": pd.Timestamp("2026-09-01"),
         "strike": 90.0, "option_type": "put", "implied_volatility": 0.38},
        {"collected_at": latest_moment, "underlying_price": 100.0, "expiry": pd.Timestamp("2026-09-01"),
         "strike": 110.0, "option_type": "call", "implied_volatility": 0.33},
    ])
    iv_surface_points = metrics.iv_surface(iv_snapshot)
    print("IV surface points:\n", iv_surface_points, "\n")
    assert len(iv_surface_points) == 4
    assert set(iv_surface_points["strike"]) == {90.0, 110.0}

    iv_grid = metrics.iv_surface_grid(iv_surface_points, strike_points=5, expiry_points=5)
    assert iv_grid is not None
    grid_strikes, grid_years, grid_iv = iv_grid
    assert grid_iv.shape == (5, 5)
    assert not np.isnan(grid_iv).any(), "gaps at the grid edges should be filled by nearest-neighbor fallback"

    # меньше 2 разных strike или expiry — не поверхность, а линия; должно вернуть None, не упасть
    assert metrics.iv_surface_grid(iv_surface_points[iv_surface_points["strike"] == 90.0]) is None

    # screener_table (ТЗ, FR25) — плоская таблица последнего снэпшота с греками
    screener = metrics.screener_table(df)
    assert len(screener) == 10, f"latest snapshot has 5 strikes x 2 types = 10 rows, got {len(screener)}"
    assert set(screener.columns) == {
        "expiry", "strike", "option_type", "dte", "last_price", "open_interest",
        "implied_volatility", "delta", "gamma", "theta", "vega", "rho", "vanna", "charm",
    }
    screener_call_100 = screener[(screener["strike"] == 100.0) & (screener["option_type"] == "call")].iloc[0]
    # тот же контракт на ту же дату должен давать те же греки, что и по вкладке «Опцион»
    matching_history_row = greeks_history[greeks_history["collected_at"] == latest_moment].iloc[0]
    assert abs(screener_call_100["delta"] - matching_history_row["delta"]) < 1e-9
    assert abs(screener_call_100["gamma"] - matching_history_row["gamma"]) < 1e-9
    assert screener_call_100["dte"] == (expiry - latest_moment).days

    # realized_volatility (ТЗ, FR24) — синтетическая дневная история с
    # постоянным лог-доходом на шаг, чтобы годовая vol считалась предсказуемо
    rng = np.random.default_rng(42)
    daily_log_returns = rng.normal(loc=0.0, scale=0.02, size=40)
    closes = 100.0 * np.exp(np.cumsum(daily_log_returns))
    price_history = pd.DataFrame({"close": closes})
    rv = metrics.realized_volatility(price_history, windows=(10, 20, 30))
    assert set(rv.keys()) == {10, 20, 30}, f"all three windows should fit in 40 days, got {rv.keys()}"
    for value in rv.values():
        assert 0 < value < 2, f"annualized RV out of sane range: {value}"
    # окно больше доступной истории просто не попадает в результат, не падает
    rv_short = metrics.realized_volatility(price_history.head(15), windows=(10, 20, 30))
    assert set(rv_short.keys()) == {10}, f"only the 10d window fits in 15 days, got {rv_short.keys()}"

    # collector._oi_zero_fraction + лог сборов (ТЗ, FR23) — реальный инцидент
    # 2026-07-17: источник данных вернул "успешный" ответ с рабочими volume/
    # ценами, но open_interest почти сплошь нулевым. Доля нулей должна
    # считаться верно и попадать в лог независимо от исхода.
    healthy_chain = pd.DataFrame({"open_interest": [10, 0, 50, 200, 0]})
    corrupted_chain = pd.DataFrame({"open_interest": [0, 0, 0, 0, 12]})
    assert collector._oi_zero_fraction(healthy_chain) == 0.4
    assert collector._oi_zero_fraction(corrupted_chain) == 0.8
    assert collector._oi_zero_fraction(pd.DataFrame({"open_interest": []})) == 1.0

    # заведомо позже всех остальных log_run в этом прогоне — иначе при равном
    # started_at порядок среди них для ORDER BY ... DESC не гарантирован
    log_moment = latest_moment + timedelta(hours=1)
    db.log_run(
        conn, log_moment, log_moment + timedelta(seconds=1), "TEST", "failed",
        error_message="80% контрактов с open_interest=0 (порог 50%) — не сохраняем",
        rows_fetched=5, oi_zero_fraction=0.8,
    )
    recent_runs = db.get_recent_runs(conn, limit=1)
    print("Recent collection run log:\n", recent_runs, "\n")
    assert recent_runs.iloc[0]["rows_fetched"] == 5
    assert abs(recent_runs.iloc[0]["oi_zero_fraction"] - 0.8) < 1e-9
    assert recent_runs.iloc[0]["status"] == "failed"

    db.add_tracked_contract(conn, "TEST", expiry, 100.0, "call")
    tracked = db.get_tracked_contracts(conn, "TEST")
    print("Tracked contracts:\n", tracked, "\n")
    assert len(tracked) == 1
    db.remove_tracked_contract(conn, int(tracked.iloc[0]["id"]))
    assert db.get_tracked_contracts(conn, "TEST").empty

    print("ALL SMOKE CHECKS PASSED")


if __name__ == "__main__":
    main()
