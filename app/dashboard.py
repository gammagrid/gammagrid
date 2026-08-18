"""Streamlit UI. Display and user input only — all business logic lives in
metrics.py (calculations) and collector.py (data collection)."""

from __future__ import annotations

import datetime as dt
import json

import altair as alt
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components
from matplotlib.colors import LinearSegmentedColormap

from app import collector, config, db, market_calendar, metrics
from app.viewtime import (
    format_date,
    format_datetime,
    with_viewer_index,
)

# Same 2x2 diagonal "chip" mark used as the favicon on gammagrid.io — keep
# this data URI in sync with the teaser site's <link rel="icon"> if the mark
# ever changes.
FAVICON = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
    "%3Crect width='32' height='32' fill='%230A0C0B'/%3E"
    "%3Crect x='4' y='4' width='10' height='10' fill='%23B833E0'/%3E"
    "%3Crect x='18' y='4' width='10' height='10' fill='%23222'/%3E"
    "%3Crect x='4' y='18' width='10' height='10' fill='%23222'/%3E"
    "%3Crect x='18' y='18' width='10' height='10' fill='%2322C55E'/%3E%3C/svg%3E"
)

st.set_page_config(page_title="GammaGrid", page_icon=FAVICON, layout="wide")


def _drop_stale_choice(key: str, options) -> None:
    """Forgets a stored selection that is no longer offered.

    Streamlit raises when a widget's remembered value is absent from its
    options, and these lists narrow as the user drills down — pick an expiry
    that lists strike 275, then one that does not, and the Contract tab would
    die on a perfectly ordinary sequence of clicks."""
    if key in st.session_state and st.session_state[key] not in list(options):
        del st.session_state[key]


@st.cache_data(ttl=1800)
def _cached_realized_volatility(ticker: str) -> dict[int, float]:
    """30-minute cache: RV is computed on daily price history, which physically
    cannot change within that window, and without the cache every rerun of the
    Contract tab (picking another strike/expiry etc.) would hit yfinance again."""
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
    """Price/IV/greeks history for a specific contract — shared renderer for the
    Contract tab and for the contract card on the Screener (row click), so the
    logic isn't duplicated in two places. `key_prefix` — tabs render in the same
    script run simultaneously (Streamlit doesn't lazy-render tabs), so widgets
    with identical labels in both places need distinct keys."""
    already_tracked = not tracked.empty and (
        (tracked["expiry"] == opt_expiry)
        & (tracked["strike"] == opt_strike)
        & (tracked["option_type"] == opt_type)
    ).any()
    if already_tracked:
        st.caption("📌 pinned")
    elif st.button("📌 Pin", key=f"{key_prefix}_pin_contract"):
        db.add_tracked_contract(conn, selected_ticker, opt_expiry, opt_strike, opt_type)
        st.rerun()

    greeks_history = metrics.contract_greeks_history(df, opt_strike, opt_expiry, opt_type)

    if greeks_history.empty:
        st.info("No history for the selected contract.")
        return

    price_column, attribution_column = st.columns(2)
    with attribution_column:
        render_greek_attribution(greeks_history)

    with price_column:
        st.subheader("Option price")
        st.line_chart(with_viewer_index(greeks_history)["last_price"], color=BRAND_PURPLE)
    with st.expander("ℹ️ How to read this"):
        st.write(
            "Price history of this specific contract (last price at each collection). "
            "Compare the option's price action with the underlying's move and with the IV "
            "dynamics below — if the option is gaining faster than the stock move explains, "
            "it's likely volatility itself that's rising (see Vega in the greeks block)."
        )

    st.subheader("Contract implied volatility")
    st.line_chart(with_viewer_index(greeks_history)["implied_volatility"], color=BRAND_GREEN)

    latest_iv = greeks_history.iloc[-1]["implied_volatility"]
    rv = _cached_realized_volatility(selected_ticker)
    if rv:
        st.caption(
            f"Current contract IV: **{latest_iv:.1%}**. Realized volatility of the underlying "
            "(historical, from yfinance daily closes — its depth doesn't depend on how many "
            "days we've been collecting option chains, which is why it isn't drawn as a line "
            "on the same chart: the time scales are too different): "
            + " · ".join(f"RV({window}d) **{value:.1%}**" for window, value in sorted(rv.items()))
        )
        st.caption(
            "IV well above RV — the market is pricing a premium for future uncertainty "
            "(normal ahead of earnings/events). IV below RV is a rare situation — the option "
            "may be underpriced relative to the underlying's actual recent volatility."
        )
    else:
        st.caption("Realized volatility of the underlying: not enough daily price history.")

    st.subheader("Greeks over time")
    greek_cols = ["delta", "gamma", "theta", "vega", "rho", "vanna", "charm"]
    left, right = st.columns(2)
    for i, greek in enumerate(greek_cols):
        target = left if i % 2 == 0 else right
        target.caption(greek.capitalize())
        target.line_chart(
            with_viewer_index(greeks_history)[greek],
            color=BRAND_PURPLE if i % 2 == 0 else BRAND_GREEN,
        )

    with st.expander("📊 Interpreting current values and their trend"):
        for note in metrics.interpret_greeks(greeks_history):
            st.write(f"- {note}")


def render_greek_attribution(greeks_history: pd.DataFrame) -> None:
    """Where the contract's money went, split by greek.

    A WATERFALL, because it is the only shape that shows the arithmetic: the
    bars start at the price the contract had, each greek moves it, and the last
    bar lands on the price it has now — so the residual is not a number beside
    the chart, it is the bar that closes the gap.

    In cents. The terms on a 19c option are hundredths of a dollar, and
    "-0.476" invites a misread that "-47.6c" does not.
    """
    attribution = metrics.greek_attribution(greeks_history)
    st.subheader("Where the price went")
    if attribution.by_day.empty:
        st.info(
            "Not enough usable days yet to split the price change by greek. This needs "
            "at least two days whose implied volatility passed the data-quality guard — "
            "and with collection on a schedule, days that are actually a day apart."
        )
        return

    totals = attribution.totals
    labels = ["Start", "Delta", "Gamma", "Vega", "Theta", "Residual", "End"]
    steps = [totals[name] * 100 for name in (*metrics.ATTRIBUTION_TERMS, "residual")]
    figure = go.Figure(go.Waterfall(
        orientation="v",
        measure=["absolute", *["relative"] * 5, "total"],
        x=labels,
        y=[attribution.start_price * 100, *steps, 0],
        text=waterfall_labels(attribution.start_price * 100, steps),
        textposition="outside",
        decreasing={"marker": {"color": BRAND_PURPLE}},
        increasing={"marker": {"color": BRAND_GREEN}},
        totals={"marker": {"color": "#8B8194"}},
        connector={"line": {"color": "#3A3340"}},
    ))
    figure.update_layout(
        height=300, margin={"l": 8, "r": 8, "t": 8, "b": 8},
        showlegend=False, yaxis={"title": "¢ per share", "zeroline": True},
    )
    st.plotly_chart(figure, use_container_width=True)

    cumulative = attribution.by_day[["day", *metrics.ATTRIBUTION_TERMS]].copy()
    for name in metrics.ATTRIBUTION_TERMS:
        cumulative[name] = cumulative[name].cumsum() * 100
    st.caption("Running total by day, in cents — when each greek did its work")
    st.line_chart(
        with_viewer_index(cumulative, "day"), height=200,
        color=[BRAND_GREEN, "#F5B342", BRAND_PURPLE, "#5AA9E6"],
    )

    # WHAT A DAY MEANS HERE, said in the interface and not only in the code.
    # This product can be collected by hand, and then "a day" is whatever the
    # person happened to press: today at 17:40, tomorrow at 11:05. Theta is
    # charged per calendar day and stays honest; the delta term does not, and
    # the difference lands in the residual looking like noise in the data rather
    # than a consequence of the schedule.
    spans = attribution.by_day["missing_trading_days"]
    st.caption(
        f"A day is the LAST collection of each trading day, New York time — "
        f"{len(attribution.by_day)} interval(s) in this window. Collect on a schedule for "
        "these numbers to mean what they say."
    )
    notes = []
    if attribution.dropped_cheap:
        notes.append(
            f"{attribution.dropped_cheap} day(s) left out: the contract traded below "
            f"{config.ATTRIBUTION_MIN_PRICE * 100:.0f}c there, where implied volatility "
            "inverted from a one-cent quote makes every greek meaningless."
        )
    if attribution.dropped_unreliable:
        notes.append(
            f"{attribution.dropped_unreliable} day(s) left out: their implied volatility "
            "failed the outlier guard, so the greeks are missing rather than wrong."
        )
    if int(spans.sum()):
        notes.append(
            f"{int(spans.sum())} trading day(s) inside the window were never collected — "
            "their moves are folded into the neighbouring interval, not counted as zero. "
            "The gamma term approximates a move locally; over a stretched interval it "
            "approximates less well, and the difference lands in the residual."
        )
    residual_share = abs(totals["residual"]) / max(abs(totals["actual"]), 1e-9)
    if residual_share > 0.5:
        notes.append(
            f"The residual is {residual_share:.0%} of the whole move: the model does not "
            "explain this contract — the spread, a stale print or a rate move did more "
            "than the greeks."
        )
    for note in notes:
        st.caption(f"⚠️ {note}")


def waterfall_labels(start: float, steps: list[float]) -> list[str]:
    """Bar labels for the waterfall, including the total.

    Computed here rather than from the `y` array, because Plotly IGNORES the y
    value of a `measure="total"` bar and derives its height itself — so the
    array carries a zero there, and a label built from it announces a contract
    worth nothing. The total is the sum, and this is the one place that says so.
    """
    return [f"{start:.1f}¢", *(f"{value:+.1f}¢" for value in steps),
            f"{start + sum(steps):.1f}¢"]


def format_compact(value: float) -> str:
    """1234 -> "1.2k", 1_500_000 -> "1.5m" — shorter than "1,234,567", so more
    expiry columns fit on screen without horizontal scrolling."""
    if pd.isna(value) or value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    if magnitude >= 1_000_000:
        return f"{sign}{magnitude / 1_000_000:.1f}m"
    if magnitude >= 1_000:
        return f"{sign}{magnitude / 1_000:.1f}k"
    return f"{sign}{magnitude:.0f}"


GEX_CMAP = LinearSegmentedColormap.from_list("gex_dark", ["#b833e0", "#0d0d0d", "#22c55e"])

# Brand accent pair, reused across every chart below instead of Streamlit's
# default blue palette — purple as the primary series, green as the
# secondary/comparison one, so the color language stays consistent with the
# heatmap and the gammagrid.io teaser. The IV surface deliberately stays on
# Viridis rather than a brand gradient: a perceptually-uniform scale reads
# far better on a continuous 3-D surface.
BRAND_PURPLE = "#B833E0"
BRAND_GREEN = "#22C55E"


def style_gex_matrix(matrix: pd.DataFrame, atm_strike: float | None = None):
    """Zero-centered color scale on a BLACK background (custom diverging palette
    `GEX_CMAP`, not the stock PRGn with its light center) — matches the app's
    dark theme and the reference tool. Text (abbreviated, `format_compact`) and
    color are computed separately (`gmap`) — so the text can be shortened (1.2k
    instead of 1,234) independently of the exact numbers, which stay in the
    source matrix solely for color computation.

    `vmax` is taken not as the true maximum but as the 90th percentile of
    nonzero |values| — otherwise a single strong outlier (common in GEX: one
    ATM cell an order of magnitude above its neighbors) stretches the scale so
    far that nearly all other cells become indistinguishably washed out. Cells
    beyond vmax are simply painted the limit color — expected behavior for a
    heatmap; the exact number is still visible in the cell text.

    If `atm_strike` is given, the row closest to the underlying price is marked
    with a "➤" marker in the index and highlighted as a whole. Streamlit does
    not marshal index styles from a pandas Styler (`cellstyle_index` from
    `Styler._translate()` is simply dropped — verified against the
    `pandas_styler_utils.py` sources), so `Styler.map_index()` won't work for
    this: it has to be index text (guaranteed to display) plus styling of
    regular data cells (`cellstyle` is marshaled, that works)."""
    display = matrix.rename(columns=format_date)
    numeric = display.to_numpy(dtype=float)
    nonzero_abs = np.abs(numeric[numeric != 0])
    vmax = np.percentile(nonzero_abs, 90) if nonzero_abs.size else 0
    vmax = vmax if vmax > 0 else 1

    text = display.map(format_compact)

    # Convert the ENTIRE index to strings (not just the ATM row) — mixing floats
    # and a single str label in one Index yields an object-dtype column that
    # pyarrow cannot serialize directly (ArrowInvalid when rendering in
    # Streamlit, visible only in container logs — the widget itself suppresses
    # the error with an auto-fix, but that's extra fragility and an extra
    # traceback in the logs on every render).
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
    """Auxiliary visualization (not a data source for the app — the focus stays
    on options analytics). The ticker is passed as-is, with no exchange-prefix
    mapping (NASDAQ:/AMEX:/...) — TradingView resolves the symbol itself; if
    some ticker resolves to the wrong thing, we'll deal with it then."""
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
    # autosize:true did not stretch the canvas to the container's full height in
    # this (nested iframe) context — it left an empty tail at the bottom.
    # Explicit width/height in the widget config instead of autosize fixes
    # exactly that.
    return f"""
    <div class="tradingview-widget-container" style="height:{height}px;width:100%;margin:0;padding:0">
      <div class="tradingview-widget-container__widget" style="height:100%;width:100%"></div>
      <script type="text/javascript" src="https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js" async>
      {config_json}
      </script>
    </div>
    """


conn = db.get_connection()

# The teaser's grid background and mono display type, applied globally so
# the app shares the gammagrid.io visual language instead of reading as bare
# default Streamlit. Deliberately NOT touching st.dataframe/st.data_editor
# styling: dense tables need to stay plain and readable, not branded —
# over-styling them would visually overload the page.
st.markdown(
    """
    <style>
      .stApp {
        background-image:
          linear-gradient(#1C2320 1px, transparent 1px),
          linear-gradient(90deg, #1C2320 1px, transparent 1px);
        background-size: 64px 64px;
        background-position: center -1px;
      }
      [data-testid="stHeading"] h1,
      [data-testid="stHeading"] h2,
      [data-testid="stHeading"] h3 {
        font-family: "SF Mono","JetBrains Mono","IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace;
      }
      [data-testid="stTab"] p {
        font-family: "SF Mono","JetBrains Mono","IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace;
        letter-spacing: .02em;
      }
    </style>
    """,
    unsafe_allow_html=True,
)

# Header wordmark: the same 2x2 diagonal chip mark as the favicon, at the
# "large instance" size documented for the brand (34px cells, 4px gap) next
# to the title text, matching the teaser site's wordmark treatment.
st.markdown(
    """
    <div style="display:flex;align-items:center;gap:14px;margin-bottom:0.25rem;">
      <div style="display:grid;grid-template-columns:34px 34px;grid-template-rows:34px 34px;gap:4px;flex-shrink:0;">
        <div style="background:#B833E0;"></div>
        <div style="background:#2A332E;"></div>
        <div style="background:#2A332E;"></div>
        <div style="background:#22C55E;"></div>
      </div>
      <h1 style="margin:0;font-family:inherit;font-weight:700;">GammaGrid</h1>
    </div>
    """,
    unsafe_allow_html=True,
)
st.caption(
    "Open-source options positioning dashboard — dealer gamma exposure, max pain, "
    "open interest, and IV surface for your whole watchlist."
)

with st.sidebar:
    st.header("Watchlist")
    new_ticker = st.text_input(
        "Add ticker",
        placeholder="AAPL",
        help="Data comes from Yahoo Finance (yfinance) — not every ticker or every "
        "options chain is available there.",
    ).strip().upper()
    if st.button("Add") and new_ticker:
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
                # otherwise on the next run the selectbox would receive a value
                # that is no longer among its options, and crash
                st.session_state.pop("selected_ticker", None)
            st.rerun()

    st.divider()
    if st.button(
        "Collect data",
        type="primary",
        disabled=not watchlist,
        help="Best run while the US options market is open. Outside market "
        "hours, Yahoo Finance often reports open_interest=0 across the whole "
        "chain, which fails the collection for that ticker.",
    ):
        with st.spinner("Collecting data..."):
            results = collector.collect_watchlist(conn, watchlist)
        for ticker, status in results.items():
            if status == "success":
                st.success(f"{ticker}: OK")
            else:
                st.error(f"{ticker}: {status}")

    # Scheduled collection. The list is short on purpose: the floor is set by
    # the data source rather than by preference, and a free-text box inviting
    # "every minute" invites getting blocked. Off is the default — a tool that
    # starts hitting an API the moment it is installed made a decision that was
    # not its to make.
    current_interval = db.get_collector_interval(conn)
    labels = list(config.COLLECTOR_INTERVAL_CHOICES)
    current_label = next(
        (name for name, minutes in config.COLLECTOR_INTERVAL_CHOICES.items()
         if minutes == current_interval),
        labels[0],
    )
    chosen = st.selectbox(
        "Collect automatically",
        labels,
        index=labels.index(current_label),
        help="Runs in the background worker container, not in this page — it keeps "
        "collecting while nobody is looking, and stops when you stop the containers.",
    )
    chosen_minutes = config.COLLECTOR_INTERVAL_CHOICES[chosen]
    if chosen_minutes != current_interval:
        db.set_collector_interval(conn, chosen_minutes)
        st.rerun()

    # The cost, stated at the moment of the decision and computed from THIS
    # watchlist rather than from a generic figure — chain sizes differ by an
    # order of magnitude between one small ticker and a few index ETFs.
    if chosen_minutes > 0:
        growth = db.estimated_growth_mb_per_month(conn, chosen_minutes)
        if growth > 0:
            st.caption(
                f"About {growth / 1000:.1f} GB per month at this interval, for your current "
                f"watchlist. Nothing is ever deleted; expired contracts move to a separate "
                f"table after {config.CONTRACT_ARCHIVE_GRACE_DAYS} days."
            )
        else:
            st.caption(
                "Collect once first and this will say how much disk a month of collection "
                "costs for your watchlist."
            )
    else:
        st.caption("Automatic collection is off. Use the button above to collect on demand.")

    with st.expander("📋 Collection log"):
        runs = db.get_recent_runs(conn, limit=30)
        if runs.empty:
            st.caption("No collections yet.")
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
            "`oi_zero_fraction` — the fraction of contracts with open_interest=0 in the "
            f"collected chain. Above {config.MAX_ZERO_OI_FRACTION:.0%} the snapshot is "
            "considered suspect and is not saved (status=failed, reason in `error_message`), "
            "even if the request to the data source itself completed without a network error. "
            "The most common cause is collecting outside regular market hours — Yahoo Finance "
            "often reports open_interest=0 across the whole chain while the market is closed. "
            "If a ticker keeps failing, try again during market hours."
        )

    st.divider()
    st.caption("Questions or feedback: [hello@gammagrid.io](mailto:hello@gammagrid.io)")

if not watchlist:
    st.info("Add a ticker to the watchlist on the left to get started.")
    st.stop()

selected_ticker = st.selectbox("Ticker to analyze", watchlist, key="selected_ticker")

# Bounded by default, never a permanent cap on what you can see: collected data
# is the most valuable thing in the app, and a rolling window must not silently
# hide a long-lived contract's early history — an 18-month-old two-year LEAP
# would lose its first year. days=None below fetches everything.
load_full_history = st.checkbox(
    f"Load full history (default: last {config.SNAPSHOT_HISTORY_DAYS} days)",
    key="load_full_history",
    help="Slower on tickers with a long collection history.",
)
history_days = None if load_full_history else config.SNAPSHOT_HISTORY_DAYS

# NO VIEW READS THE WHOLE TICKER. What used to be here was one frame of every
# row this ticker has, loaded on every interaction and filtered in pandas by
# whichever view was showing — which is fine while collection is a button and
# stops being fine the moment it runs on a schedule. Measured on the hosted
# product, which reached that point first: 3.1M rows, 22 seconds and 0.8 GB of
# memory for one liquid ticker, to draw charts a few hundred points wide.
#
# Each view now asks for what it shows: one snapshot, two calendar days, one
# contract, or a stored rollup. `db.get_snapshots` remains for tooling; if it
# reappears in a view, this is what regressed.
#
# ONE SOURCE PER PAGE, resolved once here and passed to every read below rather
# than looked up inside each of them: a render then pays for one small query
# instead of a dozen identical ones. GammaGrid never blends two providers on
# one screen — see db.active_source for why implied volatility makes that a
# correctness problem and not a tidiness one.
page_source = db.active_source(conn, selected_ticker)
latest_df = db.get_latest_snapshot(conn, selected_ticker, source=page_source)

# What the collector last believed about the market, phrased by the calendar.
# The app never asks the provider itself: a page render has no business making a
# network call, and two processes asking separately would eventually disagree in
# front of the reader.
_market_note = market_calendar.status_note(
    db.get_setting(conn, "market_state"),
    db.get_setting(conn, "market_state_at"),
    max(db.get_collector_interval(conn), 15),
)

if _market_note:
    st.caption(_market_note)

if latest_df.empty:
    st.info(f"No data for {selected_ticker}. Click “Collect data” on the left.")
    st.stop()

# Said out loud only when there is something to say. A person who switched
# providers sees their older history disappear from the charts, and hidden
# history with no explanation is indistinguishable from data loss.
_ticker_sources = db.sources_for(conn, selected_ticker) if page_source else []
if len(_ticker_sources) > 1:
    others = ", ".join(s for s in _ticker_sources if s != page_source)
    st.caption(
        f"Showing data from **{page_source}**. This ticker also has history from {others}, "
        "which is kept but not shown: two providers calculate implied volatility "
        "differently, so mixing them on one chart would draw moves that never happened."
    )

latest_date = latest_df["collected_at"].max()
expiries = sorted(latest_df["expiry"].unique())

components.html(render_tradingview_widget(selected_ticker, height=450), height=450)

# Deliberately not st.tabs. Streamlit has no lazy tabs: the body of every
# `with tab_x:` runs on every rerun and the browser merely hides the seven you
# are not looking at, so each interaction pays for eight views to show one.
# Measured on a realistically-shaped chain (3,520 contracts per snapshot), one
# render is 1.3-1.7s — and tripling the stored rows barely moved that, which
# says the cost is the tabs, not the data, and will not improve on its own.
#
# The trade this makes, stated plainly: switching views used to be instant
# (everything was already in the browser) and now costs one script run. That
# run renders a single view, so it is a fraction of what every interaction used
# to cost.
# Pinned contracts are needed by two views — the Contract tab and the contract
# card the Screener opens on a row click. This used to be fetched inside the
# Contract tab's body and read from the Screener's, which worked only because
# st.tabs executed every tab on every run. With one view rendering at a time
# that is a NameError on the Screener, reachable only after clicking a row —
# the sibling project shipped exactly that and a user found it in the browser.
_tracked_cache: dict[str, pd.DataFrame] = {}


def get_tracked() -> pd.DataFrame:
    if "df" not in _tracked_cache:
        _tracked_cache["df"] = db.get_tracked_contracts(conn, selected_ticker)
    return _tracked_cache["df"]


VIEWS = [
    "Overview", "Max Pain / GEX", "GEX Heatmap", "Volatility (IV)",
    "Contract", "Screener", "Unusual Activity", "OI Delta",
]
active_view = st.segmented_control(
    "View", VIEWS, default=VIEWS[0], key="active_view", label_visibility="collapsed"
)
# A segmented control returns None when the current pill is deselected; an
# empty page is never what that gesture means here.
active_view = active_view or VIEWS[0]

if active_view == "Overview":
    st.caption(f"Latest collection: {latest_date}")
    st.subheader("Put/Call Ratio")
    # Aggregated in SQL rather than by grouping every raw row here: same
    # numbers, a fraction of the work. metrics.put_call_ratio remains the
    # definition of the ratio and the oracle the tests check this against.
    pcr = db.get_put_call_ratio(conn, selected_ticker, days=history_days, source=page_source)
    st.line_chart(with_viewer_index(pcr)[["pcr_volume", "pcr_oi"]], color=[BRAND_PURPLE, BRAND_GREEN])
    with st.expander("ℹ️ How to read this"):
        st.write(
            "The ratio of put to call volume/open interest. A value noticeably above "
            "0.7–1.0 usually signals a defensive/bearish stance, below — a bullish one. "
            "The absolute level matters less than a sharp deviation from the ticker's own "
            "history on this chart — compare the current value to past ones, not to "
            "general rules of thumb."
        )

    st.subheader("Volatility surface (IV surface)")
    iv_surface_points = metrics.iv_surface(latest_df)
    iv_grid = metrics.iv_surface_grid(iv_surface_points)
    if iv_grid is None:
        st.info(
            "Not enough data for a volatility surface — need at least 2 distinct strikes "
            "and 2 distinct expiries with known IV in the latest snapshot."
        )
    else:
        grid_strikes, grid_years, grid_iv = iv_grid

        # The grid is built by linear interpolation — its x/y nodes (strike,
        # years) almost never coincide with actually traded contracts, and the
        # default tooltip would show meaningless fractional values. customdata
        # swaps them for practically useful ones: the nearest real strike from
        # the source points and DTE in days (years exist only for the
        # interpolation math — they're awkward to read).
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
                        "DTE: %{customdata[1]} d<br>"
                        "IV: %{z:.2f}<extra></extra>"
                    ),
                )
            ]
        )
        # The Y axis stays continuous in years-to-expiry — otherwise the
        # interpolation between expiries (uneven spacing, differing strike
        # counts) would make no sense. But its tick labels are the real expiry
        # dates rather than abstract fractions of a year: years exist for the
        # math, not for reading.
        expiry_ticks = (
            iv_surface_points[["years_to_expiry", "expiry"]]
            .drop_duplicates()
            .sort_values("years_to_expiry")
        )
        fig.update_layout(
            scene={
                "xaxis_title": "Strike",
                "yaxis": {
                    "title": "Expiry",
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
        with st.expander("ℹ️ How to read this"):
            st.write(
                "The implied volatility surface across the whole chain at the latest "
                "snapshot: strike on one axis, expiry date on the other, height/color — the "
                "IV itself. For each strike the OTM contract's IV is used (put below the "
                "current underlying price, call above): ITM contracts' IV is usually less "
                "reliable due to low liquidity and wide spreads.\n\n"
                "Real strikes and expiries rarely fall on a regular grid (different expiries "
                "have different strike spacing and ranges, far dates are coarser), so the "
                "surface is filled in by linear interpolation between the actual points (and "
                "at the edges, where interpolation runs out of data, by nearest neighbor) to "
                "avoid gaps/holes. This is display smoothing only, not new data — the source "
                "points remain the app's own snapshots, with no calls to external sources. "
                "Because of the interpolation, a grid node under the cursor almost never "
                "matches an actually traded contract — the tooltip shows the nearest real "
                "strike and DTE in days rather than the interpolated values.\n\n"
                "The typical shape is a \"smile\" or a skew across strikes at each expiry "
                "(see the Volatility (IV) tab for a single-expiry slice) and a rising/falling "
                "overall IV level as expiry moves further out (term structure). Sharp local "
                "spikes on the surface usually indicate sparse data in that region rather "
                "than a real market effect — trust the overall shape more than individual "
                "bumps."
            )

if active_view == "Max Pain / GEX":
    # Most tickers list an expiry for today, and it sorts first — so the
    # default landed on the one expiry whose gamma is zero by definition, and
    # the GEX chart came up empty under a "Net GEX: 0" banner while Max Pain
    # right above it showed a number (it needs only open interest). Default to
    # the first expiry that still has time left instead; the others stay in the
    # list, labelled, because looking at them is legitimate.
    has_time_left = [e for e in expiries if metrics.years_to_expiry(e, latest_date) > 0]
    default_expiry_index = expiries.index(has_time_left[0]) if has_time_left else 0

    def format_gex_expiry(expiry) -> str:
        if metrics.years_to_expiry(expiry, latest_date) > 0:
            return format_date(expiry)
        return f"{format_date(expiry)}  ⚠️ no gamma left"

    selected_expiry = st.selectbox(
        "Expiry", expiries, index=default_expiry_index, format_func=format_gex_expiry,
        # Keyed by ticker: a widget value outlives a rerun, so without this the
        # expiry picked for the previous ticker carries over, and if the new
        # ticker doesn't trade that date every chart below filters to nothing.
        key=f"expiry_pain_gex_{selected_ticker}",
    )
    years_left = metrics.years_to_expiry(selected_expiry, latest_date)

    st.subheader("Max Pain")
    mp = metrics.max_pain(latest_df, selected_expiry)
    st.metric("Max Pain strike", mp if mp is not None else "n/a")
    with st.expander("ℹ️ How to read this"):
        st.write(
            "The strike at which total payouts by option sellers are minimal. The price at "
            "expiration statistically gravitates toward this level, since market makers "
            "prefer to close the maximum number of contracts without payouts — the effect "
            "strengthens closer to the expiry date. It is not a guaranteed forecast but a "
            "tendency that fundamental news can override."
        )

    st.subheader("Approximate Gamma Exposure (GEX)")
    gex = metrics.gamma_exposure_profile(latest_df, selected_expiry)
    if years_left <= 0:
        # Say why, instead of drawing a flat line at zero. Gamma is undefined
        # once there is no time left, so this is a property of the contract,
        # not a gap in the data — which is also why Max Pain above still shows
        # a number: it is computed from open interest alone.
        st.info(
            f"⚠️ This expiry ({format_date(selected_expiry)}) has no time left as of the "
            f"latest collection ({format_datetime(latest_date)}), so gamma — and therefore "
            "GEX — is zero by definition, not missing. Pick a later expiry to see a profile."
        )
    elif not gex.empty:
        net_gex = metrics.net_gamma_exposure(gex)
        if net_gex >= 0:
            st.success(f"Net GEX: {net_gex:,.0f} — positive gamma: dealers dampen price moves")
        else:
            st.warning(f"Net GEX: {net_gex:,.0f} — negative gamma: dealers amplify price moves")
        # Colored by sign (green/purple), matching the same diverging language
        # as the GEX Heatmap — st.bar_chart can't condition color on a row's
        # own value, so this one chart uses Altair directly instead of the
        # thin st.bar_chart wrapper.
        gex_chart_data = gex.assign(sign=np.where(gex["gex"] >= 0, "Positive", "Negative"))
        st.altair_chart(
            alt.Chart(gex_chart_data)
            .mark_bar()
            .encode(
                x=alt.X("strike:O", title="Strike"),
                y=alt.Y("gex:Q", title="GEX"),
                color=alt.Color(
                    "sign:N",
                    scale=alt.Scale(domain=["Positive", "Negative"], range=[BRAND_GREEN, BRAND_PURPLE]),
                    legend=None,
                ),
            ),
            use_container_width=True,
        )
    else:
        st.info("No data to compute GEX for the selected expiry.")
    st.caption(
        "Estimated via the Black-Scholes formula and a sign heuristic (puts contribute "
        "negatively). Does not reflect actual market-maker positioning — an approximation only."
    )
    with st.expander("ℹ️ How to read this"):
        st.write(
            "Positive total gamma — the market as a whole is more \"sticky\", range-bound: "
            "dealer hedging dampens sharp moves. Negative — moves can accelerate, since "
            "hedging works in the same direction as the trend. On the by-strike chart, the "
            "largest bars by magnitude often become support/resistance levels created by "
            "hedging flows, especially closer to the expiry date."
        )

if active_view == "GEX Heatmap":
    st.subheader("GEX Heatmap: strike × expiry")

    # From the run log rather than DISTINCT over the snapshots: the collector
    # writes one timestamp per pass into both, and the log has one row per pass
    # against one row per contract per pass.
    snapshot_dates = [
        pd.Timestamp(moment)
        for moment in db.get_collection_moments(conn, selected_ticker, days=history_days, source=page_source)
    ] or [pd.Timestamp(latest_date)]
    as_of = st.selectbox(
        "Snapshot (Replay — collection history, no auto-refresh)",
        snapshot_dates,
        format_func=format_datetime,
        key="heatmap_as_of",
    )
    is_latest = as_of == snapshot_dates[0]
    st.caption("Latest snapshot (current state)" if is_latest else "Historical snapshot — Replay mode")

    # The chosen moment's chain, fetched by equality on collected_at. Everything
    # this view draws — the matrix, the walls, the flip, per-expiry net GEX — is
    # a function of one moment, and every metric below already takes `as_of`.
    snapshot_df = db.get_snapshots_at(conn, selected_ticker, [as_of], source=page_source)
    if snapshot_df.empty:
        # The run log is a superset of the data: a pass can be recorded as a
        # success and still have left nothing for this ticker. Before the list
        # came from the log, `as_of` came from the rows themselves and could
        # not miss.
        st.info("That collection is in the log but stored no rows — pick another moment.")
        st.stop()

    # Expiries with no time left are dropped rather than shown as a column of
    # exact zeros: gamma is undefined there, and unlike the GEX tab a heatmap
    # has nowhere to explain why one column is blank. Deliberately NOT st.stop()
    # when none are left — this runs inside `with tab_heatmap:`, and st.stop()
    # ends the entire script run, taking every tab below it with it.
    all_expiries_at_snapshot = [
        e for e in sorted(snapshot_df["expiry"].unique())
        if metrics.years_to_expiry(e, as_of) > 0
    ]
    spot_at_snapshot = snapshot_df["underlying_price"].iloc[0]

    if not all_expiries_at_snapshot:
        st.info(
            "Every expiry in this snapshot has already stopped trading, so the whole "
            "matrix would be zero by definition. Pick a more recent snapshot above."
        )
        matrix_full = pd.DataFrame()
        shown_expiries = []
        strike_band_pct = 15
    else:
        col_n, col_band = st.columns(2)
        # A slider needs min < max: with exactly one expiry to show, st.slider
        # raises instead of rendering a fixed handle. Found by rendering the
        # page for a ticker that trades a single expiry — thin tickers really
        # do look like this, and it took out the whole tab.
        if len(all_expiries_at_snapshot) > 1:
            n_expiries = col_n.slider(
                "Nearest expiries",
                1,
                len(all_expiries_at_snapshot),
                min(10, len(all_expiries_at_snapshot)),
                # Keyed by ticker: the upper bound is the number of expiries,
                # which differs per ticker, so a carried-over value can fall
                # outside the range and raise rather than merely render oddly.
                key=f"heatmap_n_expiries_{selected_ticker}",
            )
        else:
            n_expiries = 1
            col_n.caption("Nearest expiries: 1 (the only one still trading)")
        strike_band_pct = col_band.slider(
            "Strike range around underlying price, %", 5, 50, 15, key="heatmap_strike_band"
        )
        shown_expiries = all_expiries_at_snapshot[:n_expiries]
        matrix_full = metrics.gex_matrix(snapshot_df, as_of=as_of, expiries=shown_expiries)

    if matrix_full.empty:
        st.info("No data to build the heatmap for the selected snapshot.")
    else:
        band = strike_band_pct / 100
        lower, upper = spot_at_snapshot * (1 - band), spot_at_snapshot * (1 + band)
        matrix_band = matrix_full[(matrix_full.index >= lower) & (matrix_full.index <= upper)]

        walls = metrics.dealer_walls(snapshot_df, as_of=as_of, expiries=shown_expiries)
        flip = metrics.gamma_flip_price(snapshot_df, as_of=as_of, expiries=shown_expiries)
        net_by_expiry = metrics.net_gex_by_expiry(snapshot_df, as_of=as_of, expiries=shown_expiries)
        total_net_gex = net_by_expiry["net_gex"].sum()

        col_price, col_call, col_put, col_flip, col_zone = st.columns(5)
        col_price.metric("Underlying price", f"{spot_at_snapshot:,.2f}")
        col_call.metric("Call Wall", f"{walls['call_wall']:g}" if walls["call_wall"] is not None else "n/a")
        col_put.metric("Put Wall", f"{walls['put_wall']:g}" if walls["put_wall"] is not None else "n/a")
        col_flip.metric("Gamma Flip", f"{flip:,.2f}" if flip is not None else "n/a")
        col_zone.metric("Regime", "Neg" if total_net_gex < 0 else "Pos")

        if matrix_band.empty:
            st.info("No strikes in the selected range — widen the % range above.")
        else:
            # Maximize on-screen rows without scrolling — center the window
            # around the ATM strike (closest to the underlying price) instead
            # of taking the first N from the top, otherwise with a narrow range
            # above/below the underlying price the center would still drift out
            # of the visible area.
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

            # Expiries that are all zeros within the shown strike range get
            # dropped: a column without a single nonzero value carries no
            # information and just widens the table to the right.
            nonzero_cols = matrix.columns[(matrix != 0).any(axis=0)]
            hidden_count = len(matrix.columns) - len(nonzero_cols)
            matrix = matrix[nonzero_cols]
            net_by_expiry_shown = net_by_expiry[net_by_expiry["expiry"].isin(nonzero_cols)]

            st.caption(
                f"Net GEX across {len(nonzero_cols)} of {len(shown_expiries)} shown expiries "
                f"({len(all_expiries_at_snapshot)} total)"
                + (f" — {hidden_count} hidden (no activity in this strike range)" if hidden_count else "")
                + ":"
            )
            net_display = net_by_expiry_shown.assign(
                expiry=net_by_expiry_shown["expiry"].map(format_date),
                net_gex=net_by_expiry_shown["net_gex"].map(format_compact),
            )
            st.dataframe(net_display.set_index("expiry").T, use_container_width=True)

            st.caption(
                f"Strikes shown: {len(matrix)} of {len(matrix_band)} within ±{strike_band_pct}% "
                f"of the underlying price — centered on the strike closest to it ({atm_strike:g}, marked “➤”)"
            )
            # Compact row height — without it only ~10 rows fit on screen, and
            # comparing all strikes required constant scrolling.
            row_height = 24
            table_height = (len(matrix) + 1) * row_height + 3
            st.dataframe(
                style_gex_matrix(matrix, atm_strike=atm_strike),
                use_container_width=True,
                height=table_height,
                row_height=row_height,
            )

        with st.expander("ℹ️ How to read this"):
            st.write(
                "Each cell is the approximate GEX (Black-Scholes, same sign heuristic as on the "
                "Max Pain/GEX tab) for a specific strike and expiry, computed on a single "
                "snapshot — no need to pick expiries one at a time, the whole chain is visible at "
                "once. Numbers are abbreviated (1.2k = 1,200, 1.5m = 1,500,000) — exact values "
                "aren't needed for visual comparison, and shorter text leaves more room for "
                "expiry columns. Green — positive GEX (dealers dampen the move), purple — "
                "negative (dealers amplify the move), black — near zero. The color scale is "
                "calibrated to the 90th percentile of values rather than the true maximum — "
                "otherwise a single strong outlier stretches the scale until every other cell "
                "becomes nearly indistinguishably washed out; the most extreme values are simply "
                "painted the limit color, and the exact number is still visible as text. 0 means "
                "this strike simply has no listing for that expiry (not \"no data\") — that's "
                "normal: near expiries usually have a tighter range of actual strikes than far "
                "ones; expiries with no nonzero values at all within the shown strike range are "
                "hidden from the table entirely. The strike closest to the current underlying "
                "price is marked “➤” and outlined, and the table shows a strike window "
                "centered on it.\n\n"
                "**Call Wall / Put Wall** — the strike with maximum open interest in calls/puts "
                "respectively, aggregated over the shown expiries — a common proxy for "
                "support/resistance levels created by dealer hedging flows.\n\n"
                "**Gamma Flip** — the approximate underlying price at which total GEX across the "
                "shown expiries flips sign (linear interpolation between the nearest strikes). "
                "This is not a re-pricing of greeks at hypothetical underlying prices (more "
                "accurate but noticeably more expensive to compute), but a proxy based on the "
                "already-computed profile at the current price — the same class of assumption as "
                "net GEX. **Regime** Neg/Pos — the sign of total net GEX across the shown "
                "expiries.\n\n"
                "A ticker can have 20-30+ expiries (including far-dated LEAPS 1-2 years out) "
                "with very different strike ranges — the full matrix would be mostly empty and "
                "unreadable. The sliders above limit the view to the nearest N expiries and "
                "strikes within ±X% of the underlying price; this affects only the display and "
                "which subset Call/Put Wall, Gamma Flip, and per-expiry Net GEX are computed "
                "over — the approximation formulas themselves don't change.\n\n"
                "The snapshot selector lets you pick an earlier collection from history (Replay) "
                "and see how the picture looked at that moment — no new data is needed, and "
                "there is no real-time auto-refresh (collection happens only via the “Collect "
                "data” button)."
            )

if active_view == "Volatility (IV)":
    st.subheader("IV: volume-weighted average for the ticker")
    # From the rollup written at collection time, not aggregated on read: the
    # answer is one row per collection and the input was one row per contract
    # per collection. It also stops the chart changing under you as contracts
    # expire and move to the archive.
    iv_avg = db.get_iv_weighted_average(conn, selected_ticker, days=history_days, source=page_source)
    st.line_chart(with_viewer_index(iv_avg)["iv_weighted_avg"], color=BRAND_PURPLE)
    with st.expander("ℹ️ How to read this"):
        st.write(
            "Rising average IV usually precedes an anticipated move (earnings, news) or "
            "already reflects increased market uncertainty. Falling — the market is calming "
            "down. Compare the current level against the historical range on this chart, "
            "not against absolute numbers — \"normal\" IV differs a lot between tickers."
        )

    st.subheader("IV: chain slice (latest snapshot)")
    selected_expiry_iv = st.selectbox(
        "Expiry", expiries, format_func=format_date, key=f"expiry_iv_skew_{selected_ticker}"
    )
    skew_snapshot = latest_df[latest_df["expiry"] == selected_expiry_iv]
    skew_pivot = skew_snapshot.pivot_table(
        index="strike", columns="option_type", values="implied_volatility"
    )
    if skew_pivot.empty:
        st.info("No data for this expiry in the latest snapshot.")
    else:
        # A fixed 2-color list crashes (StreamlitColorLengthError, found live
        # on real illiquid tickers) whenever the latest snapshot for this
        # expiry only has calls or only puts — pivot_table then produces a
        # single column, not two. Built to match skew_pivot's actual columns
        # instead of assuming both are always present.
        option_type_colors = {"call": BRAND_PURPLE, "put": BRAND_GREEN}
        st.line_chart(skew_pivot, color=[option_type_colors[c] for c in skew_pivot.columns])
    with st.expander("ℹ️ How to read this"):
        st.write(
            "The curve's shape shows which strikes the market prices as riskier (higher "
            "implied volatility). A steep skew toward puts at low strikes (put skew) "
            "usually means elevated demand for downside protection — the typical picture "
            "for most stocks."
        )

    st.caption("IV history and the rest of the greeks for a specific contract — on the Contract tab.")

if active_view == "Contract":
    st.subheader("Contract: price and greeks over time")

    if not get_tracked().empty:
        st.caption("Pinned contracts (click a name to show it below):")
        for _, trow in get_tracked().iterrows():
            col_a, col_b = st.columns([5, 1])
            label = f"{format_date(trow['expiry'])}  strike {trow['strike']:g}  {trow['option_type']}"
            if col_a.button(label, key=f"select_tracked_{trow['id']}", use_container_width=True):
                # Written before the rerun so the selectors below initialize
                # with these values. The keys MUST match the ones the selectors
                # use, ticker suffix included — they were keyed by ticker to stop
                # one ticker's expiry leaking into another, and this handler kept
                # writing the old unsuffixed names, so clicking a pinned contract
                # silently did nothing at all.
                st.session_state[f"opt_expiry_{selected_ticker}"] = trow["expiry"]
                st.session_state[f"opt_strike_{selected_ticker}"] = trow["strike"]
                st.session_state[f"opt_type_{selected_ticker}"] = trow["option_type"]
                st.rerun()
            if col_b.button("✕", key=f"untrack_{trow['id']}"):
                db.remove_tracked_contract(conn, trow["id"])
                st.rerun()
        st.divider()

    # EXPIRED CONTRACTS. Their history is stored and perfectly readable; what
    # they cannot do is appear in these selectors, because the lists are built
    # from the LATEST snapshot and it has no row for something that no longer
    # trades. On expiry day a contract simply vanishes from the dropdown — and
    # if a stale expiry survives in the widget's state, the strike list under it
    # comes back empty and the page shows a validly selected contract with no
    # data at all.
    #
    # Off by default: on this database 7,098 of 26,894 contracts have already
    # expired, a ratio that only grows, and always-on would bury the live chain
    # under the archive.
    expired = db.get_expired_contracts(conn, selected_ticker, source=page_source)
    expired_expiries = []
    if not expired.empty:
        if st.checkbox(
            f"Show expired contracts ({len(expired)} with collected history)",
            key=f"show_expired_{selected_ticker}",
            help="Contracts that have already expired. Nothing was deleted — they simply "
                 "cannot appear in these lists on their own, because the lists come from "
                 "the current chain.",
        ):
            expired_expiries = sorted(pd.Timestamp(e) for e in expired["expiry"].unique())

    live_expiries = [pd.Timestamp(e) for e in expiries]
    all_expiries = sorted(set(live_expiries) | set(expired_expiries))

    def _format_expiry_option(expiry) -> str:
        label = format_date(expiry)
        return label if expiry in live_expiries else f"{label}  ⏳ expired"

    col1, col2, col3 = st.columns(3)
    # All three keyed by ticker for the same reason as the GEX expiry above —
    # and the strike list especially: strikes differ between tickers, so a
    # carried-over value can be one this ticker does not trade at all.
    _drop_stale_choice(f"opt_expiry_{selected_ticker}", all_expiries)
    opt_expiry = col1.selectbox(
        "Expiry", all_expiries, format_func=_format_expiry_option,
        key=f"opt_expiry_{selected_ticker}",
    )
    live_strikes = set(latest_df[latest_df["expiry"] == opt_expiry]["strike"].unique())
    expired_strikes = set()
    if expired_expiries:
        expired_strikes = set(expired[expired["expiry"] == opt_expiry]["strike"].unique())
    opt_strikes = sorted(live_strikes | expired_strikes)
    # A stored widget value that is no longer among the options makes Streamlit
    # raise, and the option lists here narrow as you go: strikes depend on the
    # expiry, and the sides quoted depend on the strike. Drop the stale value
    # rather than letting the tab die on a legitimate sequence of clicks.
    _drop_stale_choice(f"opt_strike_{selected_ticker}", opt_strikes)
    opt_strike = col2.selectbox("Strike", opt_strikes, key=f"opt_strike_{selected_ticker}")
    # Only the sides actually quoted for this expiry and strike. Offering both
    # unconditionally let you select a contract that was never collected — far
    # OTM strikes are routinely listed on one side only — and the app then said
    # "No history for the selected contract", which is true and useless.
    available_types = sorted(
        latest_df[(latest_df["expiry"] == opt_expiry) & (latest_df["strike"] == opt_strike)]["option_type"].unique()
    )
    if not available_types and expired_expiries:
        available_types = sorted(
            expired[(expired["expiry"] == opt_expiry) & (expired["strike"] == opt_strike)]["option_type"].unique()
        )
    available_types = available_types or ["call", "put"]
    _drop_stale_choice(f"opt_type_{selected_ticker}", available_types)
    opt_type = col3.selectbox("Type", available_types, key=f"opt_type_{selected_ticker}")

    # THE WINDOW HAS TO COME OFF FOR AN EXPIRED CONTRACT. The day limit is
    # measured back from now(), and an expired contract's history is entirely in
    # the past — so the limit trims it silently, and once the contract is older
    # than the limit it trims it to nothing. The page would then say "no history"
    # about a contract whose history is sitting in the table.
    is_expired = pd.Timestamp(opt_expiry) < pd.Timestamp(dt.date.today())
    contract_df = db.get_contract_history(
        conn, selected_ticker, opt_expiry, opt_strike, opt_type,
        days=None if is_expired else history_days, source=page_source,
    )
    if is_expired and not contract_df.empty:
        first, last = contract_df["collected_at"].min(), contract_df["collected_at"].max()
        st.caption(
            f"⏳ **Expired contract** — expired {format_date(opt_expiry)}. Stored history "
            f"covers {format_date(first)} to {format_date(last)}, which starts when this "
            "ticker was first collected rather than when the contract was listed."
        )
    render_option_detail(
        conn, contract_df, selected_ticker, get_tracked(), opt_expiry, opt_strike, opt_type,
        key_prefix="opt",
    )

if active_view == "Screener":
    st.subheader(f"Options screener: {selected_ticker}")
    st.caption(
        "A slice of the latest snapshot (not history) — every contract of the ticker with "
        "greeks and DTE. Clicking a row opens the same charts as the Contract tab."
    )

    screener = metrics.screener_table(latest_df)
    # The greeks and DTE are correct AS OF THE SNAPSHOT — that is the only
    # honest way to price them. But collection here is a manual button, so the
    # latest snapshot can be days or weeks old, and contracts that were live
    # when it was taken may have expired since. Showing them in a "what can I
    # trade" screener with a cheerful positive DTE is misleading, so they are
    # hidden by default rather than silently mixed in — and counted out loud,
    # with a toggle, because hiding collected data without saying so is worse
    # than showing it.
    # years_to_expiry normalizes a tz-aware "now" itself — that is what it is
    # for, and doing the conversion at the call site is how the tz-aware/naive
    # mismatch keeps coming back.
    now = pd.Timestamp.utcnow()
    expired_since = pd.Series(dtype=bool)
    if not screener.empty:
        expired_since = screener["expiry"].map(lambda e: metrics.years_to_expiry(e, now) <= 0)
    show_expired = False
    if expired_since.any():
        stale_days = int(metrics.years_to_expiry(latest_date, now) * -365)
        show_expired = st.checkbox(
            f"Include {int(expired_since.sum())} contract(s) that have expired since this "
            f"snapshot was collected ({stale_days} day(s) ago)",
            key=f"screener_show_expired_{selected_ticker}",
            help="Their greeks and DTE are as of the collection, which is why they can "
                 "still show time left. Collect again to refresh the chain.",
        )
    if not show_expired and not screener.empty:
        screener = screener[~expired_since].reset_index(drop=True)

    if screener.empty:
        st.info("No data for the screener.")
    else:
        FILTER_SPECS = [
            ("last_price", "Option price"),
            ("strike", "Strike"),
            ("dte", "DTE (days)"),
            ("open_interest", "Open interest"),
            ("implied_volatility", "IV"),
            ("delta", "Delta"),
            ("gamma", "Gamma"),
            ("theta", "Theta"),
            ("vega", "Vega"),
            ("rho", "Rho"),
            ("vanna", "Vanna"),
            ("charm", "Charm"),
        ]
        # Number inputs instead of sliders: some columns (gamma, vega, IV at far
        # strikes, etc.) have a tiny value range, and a slider can't hit it
        # precisely — exact numeric input removes that problem outright. Min/max
        # come from the actual data (full range = nothing filtered by default).
        INT_FILTER_COLUMNS = {"dte", "open_interest"}
        with st.expander("🔍 Filters (ranges)", expanded=True):
            filter_cols = st.columns(3)
            mask = pd.Series(True, index=screener.index)
            for i, (col_name, label) in enumerate(FILTER_SPECS):
                series = screener[col_name].dropna()
                col_min, col_max = float(series.min()), float(series.max())
                target = filter_cols[i % 3]
                if col_min == col_max:
                    target.caption(f"{label}: {col_min:g} (single value)")
                    continue
                target.caption(label)
                sub_left, sub_right = target.columns(2)
                # Widget keys include the ticker: without this, when switching
                # tickers Streamlit keeps the previous filter values under the
                # same key (value= only applies at first widget mount) — the
                # bounds silently stay from the previous ticker, which either
                # hides part of the new ticker's contracts or (if the old range
                # no longer intersects the new data) empties the table entirely.
                if col_name in INT_FILTER_COLUMNS:
                    col_min, col_max = int(col_min), int(col_max)
                    selected_min = sub_left.number_input(
                        "from", min_value=col_min, max_value=col_max, value=col_min,
                        step=1, key=f"screener_filter_{selected_ticker}_{col_name}_min",
                    )
                    selected_max = sub_right.number_input(
                        "to", min_value=col_min, max_value=col_max, value=col_max,
                        step=1, key=f"screener_filter_{selected_ticker}_{col_name}_max",
                    )
                else:
                    step = max((col_max - col_min) / 100, 1e-6)
                    selected_min = sub_left.number_input(
                        "from", min_value=col_min, max_value=col_max, value=col_min,
                        step=step, format="%.4f", key=f"screener_filter_{selected_ticker}_{col_name}_min",
                    )
                    selected_max = sub_right.number_input(
                        "to", min_value=col_min, max_value=col_max, value=col_max,
                        step=step, format="%.4f", key=f"screener_filter_{selected_ticker}_{col_name}_max",
                    )
                mask &= screener[col_name].between(selected_min, selected_max)

        filtered = screener[mask].reset_index(drop=True)
        st.caption(f"Contracts shown: {len(filtered)} of {len(screener)}")

        selected_contract_key = f"screener_selected_contract_{selected_ticker}"
        scroll_target_key = f"screener_scroll_target_{selected_ticker}"
        selected_contract = st.session_state.get(selected_contract_key)
        if filtered.empty:
            st.info("No contract falls within the selected filter ranges.")
        else:
            # A custom checkbox column instead of st.dataframe's built-in check
            # column: this way it gets a label, and the remaining columns can be
            # made disabled so they can't be accidentally "selected"/opened in
            # the edit overlay — only this column is interactive. dtype=bool is
            # explicit: on an empty list pandas would default to float64, and a
            # CheckboxColumn over a float column crashes with
            # StreamlitAPIException.
            display = filtered.copy()
            display["expiry"] = display["expiry"].map(format_date)
            display.insert(0, "Show details", pd.array([
                (row.expiry, row.strike, row.option_type) == selected_contract
                for row in filtered.itertuples()
            ], dtype=bool))

            edited = st.data_editor(
                display,
                use_container_width=True,
                hide_index=True,
                disabled=[c for c in display.columns if c != "Show details"],
                column_config={"Show details": st.column_config.CheckboxColumn("Show details")},
                key=f"screener_table_{selected_ticker}",
            )

            checked = filtered[edited["Show details"]]
            if checked.empty:
                new_contract = None
            else:
                # if more than one row somehow ends up checked — the last
                # checked one wins (single-select emulation over checkboxes)
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
                    f"📌 Contract details: {format_date(picked['expiry'])} "
                    f"strike {picked['strike']:g} {picked['option_type']}"
                )
                render_option_detail(
                    conn,
                    db.get_contract_history(
                        conn, selected_ticker, picked["expiry"], picked["strike"],
                        picked["option_type"], days=history_days, source=page_source,
                    ),
                    selected_ticker, get_tracked(),
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
                        ).find(el => el.textContent.includes('Contract details'));
                        if (heading) { heading.scrollIntoView({behavior: 'smooth', block: 'start'}); }
                        </script>""",
                        height=0,
                    )

if active_view == "Unusual Activity":
    st.subheader("Unusual Activity (latest snapshot)")
    # Only the latest snapshot is loaded; the baseline comes from the table the
    # worker rebuilds once a day. The rules that decide what is unusual stay in
    # metrics — only the aggregation moved.
    flagged = metrics.unusual_activity(
        latest_df, stats=db.get_volume_stats(conn, selected_ticker, source=page_source)
    )
    st.caption(f"Contracts found: {len(flagged)}")
    st.dataframe(flagged, use_container_width=True)
    with st.expander("ℹ️ How to read this"):
        st.write(
            "A contract is flagged when today's volume statistically significantly exceeds "
            f"(z-score above {config.UNUSUAL_Z_THRESHOLD}) this specific contract's own "
            "history — not a blanket multiplier applied to every strike. Contracts with "
            f"volume below {config.UNUSUAL_MIN_VOLUME} never make the list regardless of the "
            "statistics — that's noise from illiquid far strikes. Contracts without enough "
            "history use the crude check volume > 2×open interest instead. Rows are sorted "
            "by z-score — the most unusual on top. This is not a sign of insider activity — "
            "a large trade can be part of a hedge for an entirely different position.\n\n"
            "The data source's volume is cumulative since the start of the trading session, "
            "so the history for the mean/standard deviation is built from one (the latest) "
            "snapshot per calendar day — multiple same-day collections are never mixed "
            "together when computing the statistics."
        )

if active_view == "OI Delta":
    st.subheader("Open Interest change (latest day vs. previous)")
    # Two calendar days is the entire input to a day-over-day difference, and
    # the days are named by the run log before any snapshot row is read.
    delta = metrics.oi_delta(
        db.get_snapshots_at(
            conn, selected_ticker,
            db.get_collection_moments(
                conn, selected_ticker, days=history_days, per_day=True, source=page_source
            )[:2],
            source=page_source,
        )
    )
    if delta.empty:
        st.info("Data from at least two distinct calendar days is needed to compute the OI change.")
    else:
        st.dataframe(delta, use_container_width=True)
        with st.expander("ℹ️ How to read this"):
            st.write(
                "`open_interest` — the current value as of the latest day, for scale: a "
                "1000-contract increase is a lot at OI=2000 and barely noticeable at "
                "OI=200000. `oi_delta` — the absolute signed change: positive — new "
                "positions opened at this strike over the last calendar day (OI grew), "
                "negative — some positions closed. `oi_delta_pct` — the same change as a "
                "percentage of the previous value (empty if the previous OI was 0 — growth "
                "\"from zero\" has no percentage form). Rows are sorted by the magnitude of "
                "the absolute delta, not by percentage or sign — the largest OI moves in "
                "either direction are on top. It is precisely the large moves (regardless "
                "of sign) that most often form new dealer hedging-flow levels (see the Max "
                "Pain/GEX tab).\n\n"
                "The data source's open interest barely updates intraday, so the comparison "
                "is between the latest snapshots of two distinct calendar days rather than "
                "the two most recent collections — if you collected data several times "
                "today, only the freshest collection of the day is used for the comparison."
            )
