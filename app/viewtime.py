"""Turning stored instants into the reader's local time — display only.

THE RULE THIS MODULE EXISTS TO KEEP. Everything is stored as naive UTC
(`collector.py` writes `datetime.now(timezone.utc).replace(tzinfo=None)`), and
that is what every aggregate is anchored to. Converting for display also moves
rows across calendar-day boundaries — a snapshot at 20:00 in New York is 03:00
the next day in Moscow — so if grouping followed the reader's zone, OI Delta
would compare a different pair of days for every reader and the same data would
produce different numbers per person. Daily aggregates stay on the New York
trading day (эпик С-21.1); only the labels move.

Kept out of dashboard.py so it can be checked: that file is a Streamlit script
which executes on import and expects a logged-in session, so nothing in it can
be asserted directly.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st


def viewer_timezone():
    """The reader's own timezone, as the browser reports it (эпик пункт 12).

    Read fresh on every call rather than memoised: it is an attribute lookup,
    not I/O, and ANY caching of it is a hazard. `@st.cache_data` is shared
    across sessions inside one process, so a cached timezone — or a frame
    already converted with one — would show a reader in Prague the timestamps
    of whoever rendered the page before them.

    Returns UTC when there is no browser to ask: that is the render gate
    (`tests/render_views.py`), which runs the whole app in bare mode. Measured
    17.08 — `st.context.timezone` is None there rather than raising, so the
    fallback is a value, not an exception handler.
    """
    try:
        name = st.context.timezone
    except Exception:  # noqa: BLE001 — no browser, no context, no timezone
        name = None
    if not name:
        return dt.timezone.utc
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 — a browser is free to send nonsense
        return dt.timezone.utc


def _as_aware(value) -> pd.Timestamp:
    """A single instant as an aware UTC Timestamp, whatever it arrived as."""
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tz is None else stamp


def to_viewer(value):
    """Naive-UTC instants (a Timestamp, a Series, an index) as the reader's
    local wall time.

    PRESENTATION ONLY, and that is the whole rule of this change. Converting
    also moves rows across calendar-day boundaries — a snapshot at 20:00 in
    New York is 03:00 the next day in Moscow — so if grouping followed the
    reader's zone, OI Delta would compare a different pair of days for every
    reader and the same data would produce different numbers per person. Every
    daily aggregate stays anchored to the New York trading day (эпик С-21.1);
    only the labels move.

    Returned tz-naive: it has already been shifted, and a visible offset on
    every axis tick buys nothing.
    """
    zone = viewer_timezone()
    if isinstance(value, (pd.Series, pd.DatetimeIndex)):
        values = pd.to_datetime(value)
        localized = values.dt if isinstance(values, pd.Series) else values
        if localized.tz is None:
            values = values.dt.tz_localize("UTC") if isinstance(values, pd.Series) else values.tz_localize("UTC")
        converted = (
            values.dt.tz_convert(zone).dt.tz_localize(None)
            if isinstance(values, pd.Series)
            else values.tz_convert(zone).tz_localize(None)
        )
        return converted
    return _as_aware(value).tz_convert(zone).tz_localize(None)


def timezone_label(at=None) -> str:
    """What to write next to a time so the reader knows whose clock it is.

    Takes the instant being labelled, because the abbreviation is a property of
    that instant and not of today: asked for "now" while formatting a January
    timestamp, a European reader gets CEST against a time that is CET — the
    hour right and the label an hour's worth of wrong. Caught by the checks,
    17.08.

    Never omitted. A reader in Moscow seeing 03:15 against a US session has to
    be able to tell at a glance whose morning that is — otherwise the number
    silently disagrees with their broker's terminal and nothing on screen
    explains it.
    """
    zone = viewer_timezone()
    moment = dt.datetime.now(zone) if at is None else _as_aware(at).tz_convert(zone)
    return moment.strftime("%Z") or str(zone)


def with_viewer_index(df: pd.DataFrame, column: str = "collected_at") -> pd.DataFrame:
    """A frame indexed by the reader's local time, for charting.

    Applied HERE, at the point of drawing, and never inside a `_cached_*`
    function: the cache is process-wide, and a converted frame in it is one
    reader's clock shown to the next.
    """
    return df.assign(**{column: to_viewer(df[column])}).set_index(column)


def format_date(value: pd.Timestamp) -> str:
    """A calendar date — an expiry, a settlement day — and NOT converted.

    An expiry is a date, not an instant: 2026-09-18 is the same day in Prague
    and in Chicago, and shifting it by a timezone would be inventing a
    difference the contract does not have.
    """
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def format_datetime(value: pd.Timestamp) -> str:
    return f"{to_viewer(value):%Y-%m-%d %H:%M} {timezone_label(value)}"

def waterfall_labels(start: float, steps: list[float], end: float) -> list[str]:
    """Bar labels for the attribution waterfall, in cents.

    Written as its own function because the label for the LAST bar cannot be
    taken from the value passed to Plotly. For `measure="total"` Plotly ignores
    the y it is given and draws the running total itself, so the array carries a
    zero there — and a text array built from that array printed "0.0¢" over a
    bar sitting correctly at 57¢. Reported from the running product, 17.08.

    Steps keep their sign (+0.5, −88.0); the two totals do not, because "start"
    and "end" are levels rather than movements.
    """
    return [
        f"{start:.1f}¢",
        *(f"{value:+.1f}¢" for value in steps),
        f"{end:.1f}¢",
    ]
