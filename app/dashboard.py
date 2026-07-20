"""Streamlit UI. Только отображение и пользовательский ввод — вся бизнес-логика
в metrics.py (расчёты) и collector.py (сбор данных)."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components
from matplotlib.colors import LinearSegmentedColormap

from app import collector, config, db, metrics

st.set_page_config(page_title="Options Flow Tracker", layout="wide")


def format_date(value: pd.Timestamp) -> str:
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def format_datetime(value: pd.Timestamp) -> str:
    return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M")


@st.cache_data(ttl=1800)
def _cached_realized_volatility(ticker: str) -> dict[int, float]:
    """Кэш на 30 минут: RV считается на дневной истории цен, которая физически
    не может измениться за это время, а без кэша каждый rerun вкладки «Опцион»
    (выбор другого страйка/экспирации и т.п.) заново дёргал бы yfinance."""
    try:
        price_history = collector.fetch_price_history(ticker)
    except Exception:
        return {}
    if price_history.empty:
        return {}
    return metrics.realized_volatility(price_history)


def render_option_detail(
    conn, df: pd.DataFrame, selected_ticker: str, tracked: pd.DataFrame,
    opt_expiry: pd.Timestamp, opt_strike: float, opt_type: str, key_prefix: str,
) -> None:
    """История цены/IV/греков конкретного контракта — общий рендер для вкладки
    «Опцион» и для карточки контракта на «Скринере» (клик по строке), чтобы не
    дублировать логику в двух местах. `key_prefix` — вкладки рендерятся в одном
    прогоне скрипта одновременно (Streamlit не лениво отрисовывает табы), поэтому
    виджетам с одинаковым label в обоих местах нужны разные key."""
    already_tracked = not tracked.empty and (
        (tracked["expiry"] == opt_expiry)
        & (tracked["strike"] == opt_strike)
        & (tracked["option_type"] == opt_type)
    ).any()
    if already_tracked:
        st.caption("📌 закреплён")
    elif st.button("📌 Закрепить", key=f"{key_prefix}_pin_contract"):
        db.add_tracked_contract(conn, selected_ticker, opt_expiry, opt_strike, opt_type)
        st.rerun()

    greeks_history = metrics.contract_greeks_history(df, opt_strike, opt_expiry, opt_type)

    if greeks_history.empty:
        st.info("Нет истории по выбранному контракту.")
        return

    st.subheader("Цена опциона")
    st.line_chart(greeks_history.set_index("collected_at")["last_price"])
    with st.expander("ℹ️ Как читать"):
        st.write(
            "История цены конкретного контракта (last price на момент каждого сбора). "
            "Сравнивайте движение цены опциона с движением базового актива и с динамикой "
            "IV ниже — если опцион дорожает быстрее, чем объясняется движением цены акции, "
            "вероятно, растёт именно волатильность (см. Vega в блоке греков)."
        )

    st.subheader("Implied Volatility контракта")
    st.line_chart(greeks_history.set_index("collected_at")["implied_volatility"])

    latest_iv = greeks_history.iloc[-1]["implied_volatility"]
    rv = _cached_realized_volatility(selected_ticker)
    if rv:
        st.caption(
            f"Текущий IV контракта: **{latest_iv:.1%}**. Реализованная волатильность БА "
            "(историческая, по дневным закрытиям yfinance — глубина не зависит от того, "
            "сколько дней мы уже собираем опционные цепочки, поэтому не строим как линию "
            "на том же графике: там слишком разные масштабы времени): "
            + " · ".join(f"RV({window}d) **{value:.1%}**" for window, value in sorted(rv.items()))
        )
        st.caption(
            "IV заметно выше RV — рынок закладывает премию за будущую неопределённость "
            "(нормально перед отчётностью/событиями). IV ниже RV — редкая ситуация, "
            "опцион может быть недооценён относительно фактической недавней волатильности БА."
        )
    else:
        st.caption("Реализованная волатильность БА: недостаточно дневной истории котировок.")

    st.subheader("Греки во времени")
    greek_cols = ["delta", "gamma", "theta", "vega", "rho", "vanna", "charm"]
    left, right = st.columns(2)
    for i, greek in enumerate(greek_cols):
        target = left if i % 2 == 0 else right
        target.caption(greek.capitalize())
        target.line_chart(greeks_history.set_index("collected_at")[greek])

    with st.expander("📊 Интерпретация текущих значений и динамики"):
        for note in metrics.interpret_greeks(greeks_history):
            st.write(f"- {note}")


def format_compact(value: float) -> str:
    """1234 -> "1.2к", 1_500_000 -> "1.5м" — короче, чем "1,234,567", чтобы больше
    колонок-экспираций помещалось на экране без горизонтального скролла."""
    if pd.isna(value) or value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    if magnitude >= 1_000_000:
        return f"{sign}{magnitude / 1_000_000:.1f}м"
    if magnitude >= 1_000:
        return f"{sign}{magnitude / 1_000:.1f}к"
    return f"{sign}{magnitude:.0f}"


GEX_CMAP = LinearSegmentedColormap.from_list("gex_dark", ["#b833e0", "#0d0d0d", "#22c55e"])


def style_gex_matrix(matrix: pd.DataFrame, atm_strike: float | None = None):
    """Цветовая шкала с центром в нуле на ЧЁРНОМ фоне (кастомная диверг. палитра
    `GEX_CMAP`, а не стандартный PRGn со светлым центром) — совпадает с тёмной
    темой приложения и с референсным инструментом. Текст (сокращённый,
    `format_compact`) и цвет считаются раздельно (`gmap`) — так текст можно
    сократить (1.2к вместо 1,234) независимо от точных чисел, которые остаются
    в исходной матрице только для расчёта цвета.

    `vmax` берётся не как истинный максимум, а как 90-й перцентиль ненулевых
    |значений| — иначе один сильный выброс (частая ситуация в GEX: одна ATM-
    ячейка на порядок больше соседних) растягивает шкалу так, что почти все
    остальные ячейки становятся неотличимо блёклыми. Ячейки за пределами vmax
    просто закрашиваются предельным цветом — это ожидаемо для heatmap, точное
    число всё равно видно в тексте ячейки.

    Если передан `atm_strike` — ближайшая к цене БА строка помечается маркером
    "➤" в индексе и подсвечивается целиком. Streamlit не маршалит стили индекса
    из pandas Styler (`cellstyle_index` из `Styler._translate()` просто
    отбрасывается — проверено на исходниках `pandas_styler_utils.py`), поэтому
    `Styler.map_index()` для этого не подходит: это должен быть текст индекса
    (гарантированно отображается) плюс стиль обычных ячеек данных (`cellstyle`
    маршалится, это работает)."""
    display = matrix.rename(columns=format_date)
    numeric = display.to_numpy(dtype=float)
    nonzero_abs = np.abs(numeric[numeric != 0])
    vmax = np.percentile(nonzero_abs, 90) if nonzero_abs.size else 0
    vmax = vmax if vmax > 0 else 1

    text = display.map(format_compact)

    # Индекс приводим к строкам ЦЕЛИКОМ (не только ATM-строку) — смешение float
    # и одной str-метки в одном Index даёт object-dtype колонку, которую pyarrow
    # не может сериализовать напрямую (ArrowInvalid при рендере в Streamlit,
    # виден только в логах контейнера — сам виджет подавляет ошибку автофиксом,
    # но это лишняя хрупкость и лишний traceback в логах на каждый рендер).
    atm_label = None
    if atm_strike is not None and atm_strike in text.index:
        atm_label = f"➤ {atm_strike:g}"
    text.index = [atm_label if v == atm_strike else f"{v:g}" for v in text.index]

    styler = text.style.background_gradient(
        cmap=GEX_CMAP, vmin=-vmax, vmax=vmax, gmap=numeric, axis=None, text_color_threshold=0.6
    )
    if atm_label is not None:
        def highlight_atm_row(row: pd.Series) -> list[str]:
            style = "border-top: 2px solid #f59e0b; border-bottom: 2px solid #f59e0b; font-weight: 700;"
            return [style if row.name == atm_label else "" for _ in row]

        styler = styler.apply(highlight_atm_row, axis=1)
    return styler


def render_tradingview_widget(ticker: str, height: int = 300) -> str:
    """Вспомогательная визуализация (не источник данных приложения — фокус
    остаётся на опционной аналитике). Тикер передаётся как есть, без маппинга
    на биржевой префикс (NASDAQ:/AMEX:/...) — TradingView сам резолвит символ;
    если для какого-то тикера резолвится не то, решаем по факту."""
    config_json = json.dumps({
        "width": "100%",
        "height": height,
        "symbol": ticker,
        "interval": "D",
        "timezone": "Etc/UTC",
        "theme": "dark",
        "style": "1",
        "locale": "en",
        "hide_legend": True,
        "hide_top_toolbar": False,
        "hide_side_toolbar": True,
        "allow_symbol_change": False,
        "save_image": False,
        "support_host": "https://www.tradingview.com",
    })
    # autosize:true не растягивал холст на всю высоту контейнера в этом
    # (вложенный iframe) контексте — оставлял пустой хвост внизу. Явные
    # width/height в конфиге виджета вместо autosize решают именно это.
    return f"""
    <div class="tradingview-widget-container" style="height:{height}px;width:100%;margin:0;padding:0">
      <div class="tradingview-widget-container__widget" style="height:100%;width:100%"></div>
      <script type="text/javascript" src="https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js" async>
      {config_json}
      </script>
    </div>
    """


conn = db.get_connection()

st.title("Options Flow Tracker")

with st.sidebar:
    st.header("Watchlist")
    new_ticker = st.text_input("Добавить тикер", placeholder="AAPL").strip().upper()
    if st.button("Добавить") and new_ticker:
        db.add_ticker(conn, new_ticker)
        st.rerun()

    watchlist = db.get_watchlist(conn)
    current_ticker = st.session_state.get("selected_ticker")
    for ticker in watchlist:
        col1, col2 = st.columns([3, 1])
        if col1.button(
            ticker,
            key=f"select_ticker_{ticker}",
            type="primary" if ticker == current_ticker else "secondary",
            use_container_width=True,
        ):
            st.session_state["selected_ticker"] = ticker
            st.rerun()
        if col2.button("✕", key=f"remove_{ticker}"):
            db.remove_ticker(conn, ticker)
            if st.session_state.get("selected_ticker") == ticker:
                # иначе на следующем прогоне selectbox получит значение,
                # которого больше нет в options, и упадёт
                st.session_state.pop("selected_ticker", None)
            st.rerun()

    st.divider()
    if st.button("Собрать данные", type="primary", disabled=not watchlist):
        with st.spinner("Сбор данных..."):
            results = collector.collect_watchlist(conn, watchlist)
        for ticker, status in results.items():
            if status == "success":
                st.success(f"{ticker}: OK")
            else:
                st.error(f"{ticker}: {status}")

    with st.expander("📋 Лог сборов"):
        runs = db.get_recent_runs(conn, limit=30)
        if runs.empty:
            st.caption("Пока нет ни одного сбора.")
        else:
            runs_display = runs.copy()
            runs_display["started_at"] = pd.to_datetime(runs_display["started_at"]).dt.strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            runs_display["oi_zero_fraction"] = runs_display["oi_zero_fraction"].map(
                lambda v: f"{v:.0%}" if pd.notna(v) else "—"
            )
            st.dataframe(
                runs_display[
                    ["started_at", "ticker", "status", "rows_fetched", "oi_zero_fraction", "error_message"]
                ],
                use_container_width=True,
                hide_index=True,
            )
        st.caption(
            "`oi_zero_fraction` — доля контрактов с open_interest=0 в собранной цепочке. "
            f"Выше {config.MAX_ZERO_OI_FRACTION:.0%} — снэпшот считается подозрительным и не "
            "сохраняется (status=failed, причина в `error_message`), даже если сам запрос к "
            "источнику данных прошёл без сетевой ошибки."
        )

if not watchlist:
    st.info("Добавьте тикер в watchlist слева, чтобы начать.")
    st.stop()

selected_ticker = st.selectbox("Тикер для анализа", watchlist, key="selected_ticker")
df = db.get_snapshots(conn, selected_ticker)

if df.empty:
    st.info(f"Нет данных по {selected_ticker}. Нажмите «Собрать данные» слева.")
    st.stop()

latest_date = df["collected_at"].max()
expiries = sorted(df["expiry"].unique())

components.html(render_tradingview_widget(selected_ticker, height=450), height=450)

tab_overview, tab_pain_gex, tab_heatmap, tab_iv, tab_option, tab_screener, tab_unusual, tab_oi = st.tabs(
    ["Обзор", "Max Pain / GEX", "GEX Heatmap", "Волатильность (IV)", "Опцион", "Скринер", "Unusual Activity", "OI Delta"]
)

with tab_overview:
    st.caption(f"Последний сбор: {latest_date}")
    st.subheader("Put/Call Ratio")
    pcr = metrics.put_call_ratio(df)
    st.line_chart(pcr.set_index("collected_at")[["pcr_volume", "pcr_oi"]])
    with st.expander("ℹ️ Как читать"):
        st.write(
            "Соотношение объёма/открытого интереса путов к коллам. Значение заметно выше "
            "0.7–1.0 обычно говорит о защитном/медвежьем настрое, ниже — о бычьем. "
            "Абсолютный уровень значит меньше, чем резкое отклонение от собственной истории "
            "тикера на этом графике — сравнивайте текущее значение с прошлыми, а не с общими "
            "правилами."
        )

    st.subheader("Поверхность волатильности (IV surface)")
    iv_surface_points = metrics.iv_surface(df)
    iv_grid = metrics.iv_surface_grid(iv_surface_points)
    if iv_grid is None:
        st.info(
            "Недостаточно данных для поверхности волатильности — нужно минимум 2 разных "
            "strike и 2 разных expiry с известным IV на последнем снэпшоте."
        )
    else:
        grid_strikes, grid_years, grid_iv = iv_grid

        # Сетка строится линейной интерполяцией — сами узлы x/y (strike, годы)
        # почти никогда не совпадают с реальными торгуемыми контрактами, и в
        # тултипе по умолчанию показывали бы бессмысленные дробные значения.
        # customdata подменяет их на практически полезные: ближайший реальный
        # strike из исходных точек и DTE в днях (годы — только для интерполяции,
        # смотреть на них неудобно).
        real_strikes = np.sort(iv_surface_points["strike"].unique())
        nearest_strike_per_col = np.array(
            [f"{real_strikes[np.abs(real_strikes - gx).argmin()]:g}" for gx in grid_strikes]
        )
        dte_per_row = np.round(grid_years * 365).astype(int).astype(str)
        customdata = np.dstack([
            np.tile(nearest_strike_per_col, (len(grid_years), 1)),
            np.tile(dte_per_row.reshape(-1, 1), (1, len(grid_strikes))),
        ])

        fig = go.Figure(
            data=[
                go.Surface(
                    x=grid_strikes,
                    y=grid_years,
                    z=grid_iv,
                    colorscale="Viridis",
                    customdata=customdata,
                    hovertemplate=(
                        "Strike ≈ %{customdata[0]}<br>"
                        "DTE: %{customdata[1]} дн.<br>"
                        "IV: %{z:.2f}<extra></extra>"
                    ),
                )
            ]
        )
        # Ось Y остаётся непрерывной в годах до экспирации — иначе интерполяция
        # между экспирациями (разный шаг, разное число strike) не имеет смысла.
        # Но подписи на ней — реальные даты экспираций, а не абстрактные доли
        # года: годы нужны только для математики, а не для того, чтобы на них
        # смотреть.
        expiry_ticks = (
            iv_surface_points[["years_to_expiry", "expiry"]]
            .drop_duplicates()
            .sort_values("years_to_expiry")
        )
        fig.update_layout(
            scene={
                "xaxis_title": "Strike",
                "yaxis": {
                    "title": "Экспирация",
                    "tickmode": "array",
                    "tickvals": expiry_ticks["years_to_expiry"].tolist(),
                    "ticktext": [format_date(d) for d in expiry_ticks["expiry"]],
                },
                "zaxis_title": "IV",
            },
            margin={"l": 0, "r": 0, "t": 10, "b": 0},
            height=500,
        )
        st.plotly_chart(fig, use_container_width=True)
        with st.expander("ℹ️ Как читать"):
            st.write(
                "Поверхность подразумеваемой волатильности по всей цепочке на последнем "
                "снэпшоте: strike — по одной оси, дата экспирации — по другой, высота/цвет — "
                "сам IV. Для каждого strike берётся IV OTM-контракта (put ниже текущей цены БА, "
                "call — выше): у ITM-контрактов IV обычно менее надёжен из-за низкой ликвидности "
                "и широких спредов.\n\n"
                "Реальные страйки и экспирации редко ложатся на ровную сетку (у разных "
                "экспираций разный шаг и диапазон strike, у дальних дат — грубее), поэтому "
                "поверхность достраивается линейной интерполяцией между фактическими точками "
                "(а по краям, где интерполяции не хватает данных, — ближайшим соседом), чтобы "
                "не было провалов/дыр. Это только сглаживание отображения, а не новые данные — "
                "исходные точки остаются собственными снэпшотами приложения, без обращения к "
                "внешним источникам. Из-за интерполяции при наведении курсора узел сетки почти "
                "никогда не совпадает с реально торгуемым контрактом — во всплывающей подсказке "
                "показан ближайший реальный strike и DTE в днях, а не интерполированные значения.\n\n"
                "Типичная форма — \"улыбка\" или перекос (skew) по strike на каждой "
                "экспирации (см. вкладку «Волатильность (IV)» для среза по одной экспирации) "
                "и рост/падение общего уровня IV по мере удаления экспирации (term structure). "
                "Резкие локальные пики на поверхности обычно говорят не о реальном рыночном "
                "эффекте, а о недостатке данных в этой области — доверяйте общей форме больше, "
                "чем отдельным всплескам."
            )

with tab_pain_gex:
    selected_expiry = st.selectbox(
        "Экспирация", expiries, format_func=format_date, key="expiry_pain_gex"
    )

    st.subheader("Max Pain")
    mp = metrics.max_pain(df, selected_expiry)
    st.metric("Max Pain страйк", mp if mp is not None else "н/д")
    with st.expander("ℹ️ Как читать"):
        st.write(
            "Страйк, при котором суммарные выплаты продавцам опционов минимальны. Цена на "
            "экспирацию статистически тяготеет к этому уровню, так как маркет-мейкерам "
            "выгоднее закрыть максимум контрактов без выплат — эффект усиливается ближе к "
            "дате экспирации. Это не гарантированный прогноз, а склонность, которую могут "
            "перебить фундаментальные новости."
        )

    st.subheader("Приближённая Gamma Exposure (GEX)")
    gex = metrics.gamma_exposure_profile(df, selected_expiry)
    if not gex.empty:
        net_gex = metrics.net_gamma_exposure(gex)
        if net_gex >= 0:
            st.success(f"Net GEX: {net_gex:,.0f} — положительная гамма: дилеры сглаживают движение цены")
        else:
            st.warning(f"Net GEX: {net_gex:,.0f} — отрицательная гамма: дилеры усиливают движение цены")
        st.bar_chart(gex.set_index("strike")["gex"])
    else:
        st.info("Нет данных для расчёта GEX по выбранной экспирации.")
    st.caption(
        "Оценка через формулу Блэка-Шоулза и эвристику знака (путы — отрицательный "
        "вклад). Не отражает реальные позиции маркет-мейкеров, только приближение."
    )
    with st.expander("ℹ️ Как читать"):
        st.write(
            "Положительная суммарная гамма — рынок в целом более «вязкий», диапазонный: "
            "хеджирование дилеров гасит резкие движения. Отрицательная — движения могут "
            "ускоряться, так как хеджирование работает в ту же сторону, что и тренд. На "
            "графике по strike — самые большие по модулю бары часто становятся уровнями "
            "поддержки/сопротивления от хедж-потоков, особенно ближе к дате экспирации."
        )

with tab_heatmap:
    st.subheader("GEX Heatmap: strike × expiry")

    snapshot_dates = sorted(df["collected_at"].unique(), reverse=True)
    as_of = st.selectbox(
        "Снэпшот (Replay — история сборов, без авто-обновления)",
        snapshot_dates,
        format_func=format_datetime,
        key="heatmap_as_of",
    )
    is_latest = as_of == snapshot_dates[0]
    st.caption("Последний снэпшот (текущее состояние)" if is_latest else "Исторический снэпшот — режим Replay")

    all_expiries_at_snapshot = sorted(df[df["collected_at"] == as_of]["expiry"].unique())
    spot_at_snapshot = df.loc[df["collected_at"] == as_of, "underlying_price"].iloc[0]

    col_n, col_band = st.columns(2)
    n_expiries = col_n.slider(
        "Ближайших экспираций",
        1,
        len(all_expiries_at_snapshot),
        min(10, len(all_expiries_at_snapshot)),
        key="heatmap_n_expiries",
    )
    strike_band_pct = col_band.slider(
        "Диапазон strike вокруг цены БА, %", 5, 50, 15, key="heatmap_strike_band"
    )
    shown_expiries = all_expiries_at_snapshot[:n_expiries]

    matrix_full = metrics.gex_matrix(df, as_of=as_of, expiries=shown_expiries)
    if matrix_full.empty:
        st.info("Нет данных для построения хитмапа на выбранный снэпшот.")
    else:
        band = strike_band_pct / 100
        lower, upper = spot_at_snapshot * (1 - band), spot_at_snapshot * (1 + band)
        matrix_band = matrix_full[(matrix_full.index >= lower) & (matrix_full.index <= upper)]

        walls = metrics.dealer_walls(df, as_of=as_of, expiries=shown_expiries)
        flip = metrics.gamma_flip_price(df, as_of=as_of, expiries=shown_expiries)
        net_by_expiry = metrics.net_gex_by_expiry(df, as_of=as_of, expiries=shown_expiries)
        total_net_gex = net_by_expiry["net_gex"].sum()

        col_price, col_call, col_put, col_flip, col_zone = st.columns(5)
        col_price.metric("Цена БА", f"{spot_at_snapshot:,.2f}")
        col_call.metric("Call Wall", f"{walls['call_wall']:g}" if walls["call_wall"] is not None else "н/д")
        col_put.metric("Put Wall", f"{walls['put_wall']:g}" if walls["put_wall"] is not None else "н/д")
        col_flip.metric("Gamma Flip", f"{flip:,.2f}" if flip is not None else "н/д")
        col_zone.metric("Режим", "Neg" if total_net_gex < 0 else "Pos")

        if matrix_band.empty:
            st.info("Нет страйков в выбранном диапазоне — расширьте диапазон % выше.")
        else:
            # Максимум строк на экране без скролла — центрируем окно вокруг
            # ATM-страйка (ближайшего к цене БА), а не берём первые N сверху,
            # иначе при узком диапазоне выше/ниже цены БА центр всё равно
            # уезжал бы за пределы видимой области.
            max_rows = 45
            strikes = matrix_band.index.to_numpy(dtype=float)
            atm_strike = matrix_band.index[np.abs(strikes - spot_at_snapshot).argmin()]
            if len(matrix_band) > max_rows:
                atm_pos = matrix_band.index.get_loc(atm_strike)
                half = max_rows // 2
                start = max(0, atm_pos - half)
                end = min(len(matrix_band), start + max_rows)
                start = max(0, end - max_rows)
                matrix = matrix_band.iloc[start:end]
            else:
                matrix = matrix_band

            # Экспирации, у которых в показанном диапазоне strike одни нули —
            # убираем: колонка без единого ненулевого значения не несёт
            # информации и просто расширяет таблицу вправо.
            nonzero_cols = matrix.columns[(matrix != 0).any(axis=0)]
            hidden_count = len(matrix.columns) - len(nonzero_cols)
            matrix = matrix[nonzero_cols]
            net_by_expiry_shown = net_by_expiry[net_by_expiry["expiry"].isin(nonzero_cols)]

            st.caption(
                f"Net GEX по {len(nonzero_cols)} экспирациям из {len(shown_expiries)} показанных "
                f"({len(all_expiries_at_snapshot)} всего)"
                + (f" — {hidden_count} скрыто (нет активности в этом диапазоне strike)" if hidden_count else "")
                + ":"
            )
            net_display = net_by_expiry_shown.assign(
                expiry=net_by_expiry_shown["expiry"].map(format_date),
                net_gex=net_by_expiry_shown["net_gex"].map(format_compact),
            )
            st.dataframe(net_display.set_index("expiry").T, use_container_width=True)

            st.caption(
                f"Показано strike: {len(matrix)} из {len(matrix_band)} в пределах ±{strike_band_pct}% "
                f"от цены БА — отцентровано на ближайший к цене strike ({atm_strike:g}, отмечен «➤»)"
            )
            # Компактная высота строки — без неё на экране умещалось ~10 строк,
            # для сравнения всех strike нужно было постоянно скроллить.
            row_height = 24
            table_height = (len(matrix) + 1) * row_height + 3
            st.dataframe(
                style_gex_matrix(matrix, atm_strike=atm_strike),
                use_container_width=True,
                height=table_height,
                row_height=row_height,
            )

        with st.expander("ℹ️ Как читать"):
            st.write(
                "Каждая ячейка — приближённая GEX (Black-Scholes, та же эвристика знака, что и на "
                "вкладке Max Pain/GEX) для конкретного strike и expiry, посчитанная на одном "
                "снэпшоте — не нужно выбирать экспирацию по одной, видно всю цепочку сразу. "
                "Числа сокращены (1.2к = 1 200, 1.5м = 1 500 000) — точные значения не нужны для "
                "визуального сравнения, а короче текст даёт больше места под колонки-экспирации. "
                "Зелёный — положительная GEX (дилеры сглаживают движение), фиолетовый — отрицательная "
                "(дилеры усиливают движение), чёрный — около нуля. Шкала цвета откалибрована по "
                "90-му перцентилю значений, а не по истинному максимуму — иначе один сильный выброс "
                "растягивает шкалу так, что все остальные ячейки становятся почти неразличимо блёклыми; "
                "самые крайние значения просто закрашиваются предельным цветом, точное число всё "
                "равно видно в тексте. 0 — на этом strike у данной экспирации просто нет листинга "
                "(а не «нет данных») — это нормально: у ближних экспираций обычно уже диапазон "
                "реальных страйков, чем у дальних; экспирации, у которых в показанном диапазоне "
                "strike вообще нет ненулевых значений, скрываются из таблицы целиком. Strike, "
                "ближайший к текущей цене БА, помечен «➤» и подсвечен рамкой, а таблица показывает "
                "окно strike, отцентрованное вокруг него.\n\n"
                "**Call Wall / Put Wall** — страйк с максимальным open interest по коллам/путам "
                "соответственно, агрегированным по показанным экспирациям — частый прокси "
                "уровней поддержки/сопротивления от хедж-потоков дилеров.\n\n"
                "**Gamma Flip** — приближённая цена БА, при которой суммарная GEX по показанным "
                "экспирациям меняет знак (линейная интерполяция между ближайшими strike). Это "
                "не переоценка греков на гипотетических ценах БА (было бы точнее, но заметно "
                "дороже по вычислениям), а прокси на основе уже посчитанного профиля на текущей "
                "цене — тот же класс допущения, что и у net GEX. **Режим** Neg/Pos — знак "
                "суммарного net GEX по показанным экспирациям.\n\n"
                "У тикера может быть 20-30+ экспираций (включая дальние LEAPS на 1-2 года вперёд) "
                "с очень разными диапазонами strike — полная матрица получится в основном пустой "
                "и нечитаемой. Ползунки сверху ограничивают вид ближайшими N экспирациями и "
                "страйками в пределах ±X% от цены БА; это влияет только на отображение и на то, "
                "по какому подмножеству считаются Call/Put Wall, Gamma Flip и Net GEX по "
                "экспирациям — сами приближённые формулы не меняются.\n\n"
                "Через селектор снэпшота можно выбрать более ранний сбор из истории (Replay) и "
                "посмотреть, как выглядела картина на тот момент — новых данных для этого не "
                "нужно, автообновления в реальном времени нет (сбор только по кнопке «Собрать "
                "данные»)."
            )

with tab_iv:
    st.subheader("IV: volume-weighted среднее по тикеру")
    iv_avg = metrics.iv_weighted_average(df)
    st.line_chart(iv_avg.set_index("collected_at")["iv_weighted_avg"])
    with st.expander("ℹ️ Как читать"):
        st.write(
            "Рост среднего IV обычно предшествует ожиданию движения (отчётность, новости) "
            "или уже отражает возросшую неопределённость на рынке. Падение — рынок "
            "успокаивается. Сравнивайте текущий уровень с историческим диапазоном на этом "
            "графике, а не с абсолютными цифрами — «нормальный» IV сильно разный для разных "
            "тикеров."
        )

    st.subheader("IV: срез по цепочке (последний снэпшот)")
    selected_expiry_iv = st.selectbox(
        "Экспирация", expiries, format_func=format_date, key="expiry_iv_skew"
    )
    skew_snapshot = df[(df["collected_at"] == latest_date) & (df["expiry"] == selected_expiry_iv)]
    skew_pivot = skew_snapshot.pivot_table(
        index="strike", columns="option_type", values="implied_volatility"
    )
    st.line_chart(skew_pivot)
    with st.expander("ℹ️ Как читать"):
        st.write(
            "Форма кривой показывает, какие страйки рынок оценивает как более рискованные "
            "(выше подразумеваемая волатильность). Крутой перекос в сторону путов на низких "
            "страйках (put skew) обычно означает повышенный спрос на защиту от падения — "
            "типичная картина для большинства акций."
        )

    st.caption("История IV и остальные греки по конкретному контракту — на вкладке «Опцион».")

with tab_option:
    st.subheader("Опцион: цена и греки во времени")

    tracked = db.get_tracked_contracts(conn, selected_ticker)
    if not tracked.empty:
        st.caption("Закреплённые опционы (клик по названию — показать ниже):")
        for _, trow in tracked.iterrows():
            col_a, col_b = st.columns([5, 1])
            label = f"{format_date(trow['expiry'])}  strike {trow['strike']:g}  {trow['option_type']}"
            if col_a.button(label, key=f"select_tracked_{trow['id']}", use_container_width=True):
                # session_state пишем перед rerun — на следующем прогоне селекторы
                # ниже (те же ключи) инициализируются этими значениями.
                st.session_state["opt_expiry"] = trow["expiry"]
                st.session_state["opt_strike"] = trow["strike"]
                st.session_state["opt_type"] = trow["option_type"]
                st.rerun()
            if col_b.button("✕", key=f"untrack_{trow['id']}"):
                db.remove_tracked_contract(conn, trow["id"])
                st.rerun()
        st.divider()

    col1, col2, col3 = st.columns(3)
    opt_expiry = col1.selectbox("Экспирация", expiries, format_func=format_date, key="opt_expiry")
    opt_strikes = sorted(df[df["expiry"] == opt_expiry]["strike"].unique())
    opt_strike = col2.selectbox("Страйк", opt_strikes, key="opt_strike")
    opt_type = col3.selectbox("Тип", ["call", "put"], key="opt_type")

    render_option_detail(conn, df, selected_ticker, tracked, opt_expiry, opt_strike, opt_type, key_prefix="opt")

with tab_screener:
    st.subheader(f"Скринер опционов: {selected_ticker}")
    st.caption(
        "Срез последнего снэпшота (не история) — все контракты тикера с греками и ДТЕ. "
        "Клик по строке открывает те же графики, что и на вкладке «Опцион»."
    )

    screener = metrics.screener_table(df)
    if screener.empty:
        st.info("Нет данных для скринера.")
    else:
        FILTER_SPECS = [
            ("last_price", "Цена опциона"),
            ("strike", "Страйк"),
            ("dte", "ДТЕ (дней)"),
            ("open_interest", "Открытый интерес"),
            ("implied_volatility", "IV"),
            ("delta", "Delta"),
            ("gamma", "Gamma"),
            ("theta", "Theta"),
            ("vega", "Vega"),
            ("rho", "Rho"),
            ("vanna", "Vanna"),
            ("charm", "Charm"),
        ]
        # Числовые поля вместо слайдеров: у части колонок (gamma, vega, IV на дальних
        # страйках и т.п.) диапазон значений крошечный, и слайдером в него не попасть
        # точно — точный ввод числом сразу снимает эту проблему. Мин/макс подставляются
        # из реальных данных (весь диапазон = ничего не отфильтровано по умолчанию).
        INT_FILTER_COLUMNS = {"dte", "open_interest"}
        with st.expander("🔍 Фильтры (диапазоны)", expanded=True):
            filter_cols = st.columns(3)
            mask = pd.Series(True, index=screener.index)
            for i, (col_name, label) in enumerate(FILTER_SPECS):
                series = screener[col_name].dropna()
                col_min, col_max = float(series.min()), float(series.max())
                target = filter_cols[i % 3]
                if col_min == col_max:
                    target.caption(f"{label}: {col_min:g} (единственное значение)")
                    continue
                target.caption(label)
                sub_left, sub_right = target.columns(2)
                # Ключи виджетов включают тикер: без этого при переключении тикера
                # Streamlit сохраняет прежние значения фильтров под тем же ключом
                # (value= действует только при первом монтировании виджета) — границы
                # молча остаются от предыдущего тикера, что либо скрывает часть
                # контрактов нового тикера, либо (если старый диапазон уже не
                # пересекается с новыми данными) обнуляет таблицу целиком.
                if col_name in INT_FILTER_COLUMNS:
                    col_min, col_max = int(col_min), int(col_max)
                    selected_min = sub_left.number_input(
                        "от", min_value=col_min, max_value=col_max, value=col_min,
                        step=1, key=f"screener_filter_{selected_ticker}_{col_name}_min",
                    )
                    selected_max = sub_right.number_input(
                        "до", min_value=col_min, max_value=col_max, value=col_max,
                        step=1, key=f"screener_filter_{selected_ticker}_{col_name}_max",
                    )
                else:
                    step = max((col_max - col_min) / 100, 1e-6)
                    selected_min = sub_left.number_input(
                        "от", min_value=col_min, max_value=col_max, value=col_min,
                        step=step, format="%.4f", key=f"screener_filter_{selected_ticker}_{col_name}_min",
                    )
                    selected_max = sub_right.number_input(
                        "до", min_value=col_min, max_value=col_max, value=col_max,
                        step=step, format="%.4f", key=f"screener_filter_{selected_ticker}_{col_name}_max",
                    )
                mask &= screener[col_name].between(selected_min, selected_max)

        filtered = screener[mask].reset_index(drop=True)
        st.caption(f"Показано контрактов: {len(filtered)} из {len(screener)}")

        selected_contract_key = f"screener_selected_contract_{selected_ticker}"
        scroll_target_key = f"screener_scroll_target_{selected_ticker}"
        selected_contract = st.session_state.get(selected_contract_key)
        if filtered.empty:
            st.info("Ни один контракт не попадает в выбранные диапазоны фильтров.")
        else:
            # Собственная колонка-чекбокс вместо встроенной чек-колонки st.dataframe:
            # так у неё есть подпись, а остальные колонки можно сделать disabled, и
            # тогда их нельзя случайно "выделить"/открыть оверлей редактирования —
            # интерактивна только эта колонка. dtype=bool указан явно: на пустом
            # списке pandas по умолчанию завёл бы float64, а CheckboxColumn с
            # float-колонкой падает с StreamlitAPIException.
            display = filtered.copy()
            display["expiry"] = display["expiry"].map(format_date)
            display.insert(0, "Показать детали", pd.array([
                (row.expiry, row.strike, row.option_type) == selected_contract
                for row in filtered.itertuples()
            ], dtype=bool))

            edited = st.data_editor(
                display,
                use_container_width=True,
                hide_index=True,
                disabled=[c for c in display.columns if c != "Показать детали"],
                column_config={"Показать детали": st.column_config.CheckboxColumn("Показать детали")},
                key=f"screener_table_{selected_ticker}",
            )

            checked = filtered[edited["Показать детали"]]
            if checked.empty:
                new_contract = None
            else:
                # если каким-то образом отмечено больше одной строки — последняя
                # отмеченная выигрывает (эмуляция single-select поверх чекбоксов)
                picked_row = checked.iloc[-1]
                new_contract = (picked_row["expiry"], picked_row["strike"], picked_row["option_type"])

            if new_contract != selected_contract:
                st.session_state[selected_contract_key] = new_contract
                st.rerun()

        if selected_contract is not None:
            picked = filtered[
                (filtered["expiry"] == selected_contract[0])
                & (filtered["strike"] == selected_contract[1])
                & (filtered["option_type"] == selected_contract[2])
            ]
            if not picked.empty:
                picked = picked.iloc[0]
                st.divider()
                st.subheader(
                    f"📌 Параметры опциона: {format_date(picked['expiry'])} "
                    f"strike {picked['strike']:g} {picked['option_type']}"
                )
                render_option_detail(
                    conn, df, selected_ticker, tracked,
                    picked["expiry"], picked["strike"], picked["option_type"],
                    key_prefix="screener",
                )
                is_new_scroll_target = st.session_state.get(scroll_target_key) != selected_contract
                st.session_state[scroll_target_key] = selected_contract
                if is_new_scroll_target:
                    components.html(
                        """<script>
                        const heading = Array.from(
                            window.parent.document.querySelectorAll('h3')
                        ).find(el => el.textContent.includes('Параметры опциона'));
                        if (heading) { heading.scrollIntoView({behavior: 'smooth', block: 'start'}); }
                        </script>""",
                        height=0,
                    )

with tab_unusual:
    st.subheader("Unusual Activity (последний снэпшот)")
    flagged = metrics.unusual_activity(df)
    st.caption(f"Найдено контрактов: {len(flagged)}")
    st.dataframe(flagged, use_container_width=True)
    with st.expander("ℹ️ Как читать"):
        st.write(
            "Флаг ставится, если объём сегодня статистически значимо (z-score выше "
            f"{config.UNUSUAL_Z_THRESHOLD}) превышает собственную историю именно этого "
            "контракта — не общий множитель, одинаковый для всех страйков. Контракты с "
            f"объёмом ниже {config.UNUSUAL_MIN_VOLUME} не попадают в список независимо от "
            "статистики — это шум по неликвидным дальним страйкам. Для контрактов без "
            "достаточной истории используется грубая проверка volume > 2×open interest. "
            "Строки отсортированы по z-score — самое необычное сверху. Это не признак "
            "инсайда — крупная сделка может быть частью хеджа совершенно другой позиции.\n\n"
            "Volume у источника данных — это накопленный объём с начала торговой сессии, "
            "поэтому история для среднего/стандартного отклонения строится по одному "
            "(последнему) снэпшоту за календарный день — несколько сборов за один день не "
            "смешиваются между собой при расчёте статистики."
        )

with tab_oi:
    st.subheader("Изменение Open Interest (последний день vs предыдущий)")
    delta = metrics.oi_delta(df)
    if delta.empty:
        st.info("Нужно данные минимум за два разных календарных дня, чтобы посчитать изменение OI.")
    else:
        st.dataframe(delta, use_container_width=True)
        with st.expander("ℹ️ Как читать"):
            st.write(
                "`open_interest` — актуальное значение на последний день, для масштаба: "
                "прирост на 1000 контрактов — это много при OI=2000 и почти незаметно при "
                "OI=200000. `oi_delta` — абсолютное изменение со знаком: положительное — "
                "на этом страйке за последний календарный день открылись новые позиции (OI "
                "вырос), отрицательное — часть позиций закрылась. `oi_delta_pct` — то же "
                "изменение в процентах от предыдущего значения (пусто, если предыдущий OI "
                "был 0 — рост «с нуля» в процентах не выражается). Строки отсортированы по "
                "модулю абсолютной дельты, а не по проценту и не по знаку — сверху "
                "крупнейшие движения OI в любую сторону. Именно крупные движения "
                "(независимо от знака) чаще всего формируют новые уровни хедж-потоков "
                "дилеров (см. вкладку Max Pain/GEX).\n\n"
                "Open interest у источника данных почти не обновляется внутри дня, поэтому "
                "сравнение идёт между последними снэпшотами двух разных календарных дней, "
                "а не между двумя последними сборами — если сегодня вы собирали данные "
                "несколько раз, для сравнения берётся только самый свежий сбор дня."
            )
