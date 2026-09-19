"""The words that go next to the gamma numbers.

WHY THE WORDS LIVE APART FROM THE ARITHMETIC. `metrics.gamma_weather` answers
what the regime is; this answers how to say it. Two screens ask — the summary
line above the view switcher and the day-to-day comparison — and a product that
phrases the same state differently in two places is a product that reads as two
products.

WHY THERE ARE WORDS AT ALL. Net GEX is a number with eight digits and a sign,
and the sign is the part that matters: positive means dealer hedging leans
against the move and damps it, negative means it leans with the move and
amplifies it. Somebody who has just installed this has no reason to know that,
and a dashboard that only prints the number teaches it to nobody.
"""

from __future__ import annotations

import pandas as pd

from app import metrics, theme

# Label and sentence per state. The label is what the eye lands on; the
# sentence is what makes it mean something the first time it is read.
WEATHER_WORDS = {
    metrics.WEATHER_CLEAR: (
        "Clear",
        "Positive gamma with the flip far off — dealer hedging is damping moves.",
    ),
    metrics.WEATHER_FAIR: (
        "Fair",
        "Positive gamma, but the flip is within reach — the damping may not hold.",
    ),
    metrics.WEATHER_UNSETTLED: (
        "Unsettled",
        "The price is sitting on the gamma flip — the regime can turn either way.",
    ),
    metrics.WEATHER_RAIN: (
        "Showers",
        "Negative gamma, though the flip is close — moves are amplified for now.",
    ),
    metrics.WEATHER_STORM: (
        "Storm",
        "Negative gamma and no boundary nearby — dealer hedging amplifies moves.",
    ),
}


def format_gex(value: float | None, signed: bool = True) -> str:
    """Net GEX as a person reads it, scaled to its own size.

    A FIXED UNIT IS THE WRONG UNIT FOR SOMEBODY'S WATCHLIST. An index carries
    billions of gamma and a mid-cap carries tens of millions; printed in
    billions the second one reads "+0.00B" on every day of its life, which is
    a line saying nothing where a line was supposed to explain something. The
    unit follows the number instead.
    """
    if value is None:
        return "—"
    magnitude = abs(value)
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if magnitude >= limit:
            return f"{value / limit:{'+' if signed else ''}.2f}{suffix}"
    return f"{value:{'+' if signed else ''}.0f}"


def describe(weather: dict, latest_moment=None) -> dict:
    """Label, sentence and scope for one weather reading.

    THE SCOPE IS PART OF THE ANSWER, not decoration. The summary reads the near
    term rather than the whole chain, and a regime stated without the range it
    was measured over invites the reader to compare it with a heatmap covering
    something else entirely.

    A CHAIN WITH NO FLIP GETS ITS OWN SENTENCE. Four of the five states are
    phrased in terms of distance to the flip, and there is no honest way to say
    "far from" a boundary that does not exist — an index whose cumulative
    profile never changes sign below its lowest strike is the ordinary case,
    not a corner one. The sign then speaks alone and says so.

    `from_earlier_moment` is for screens that draw this beside fresher figures:
    the weather is built from one snapshot, and when that snapshot is not the
    one the rest of the page is showing, the page has to be able to say so
    instead of quietly mixing two moments.
    """
    label, sentence = WEATHER_WORDS[weather["state"]]
    scope = ""
    if weather.get("expiry_count") and weather.get("horizon_dte") is not None:
        n = int(weather["expiry_count"])
        scope = f"next {int(weather['horizon_dte'])} days · {n} {'expiry' if n == 1 else 'expiries'}"
    if weather["gamma_flip"] is None:
        sentence = (
            "This chain has no gamma flip inside its strike range, so only the sign of "
            "gamma is known — "
            + (
                "positive, hedging damps moves."
                if weather["net_gex"] >= 0
                else "negative, hedging amplifies moves."
            )
        )
    earlier = (
        latest_moment is not None
        and pd.Timestamp(weather["collected_at"]) != pd.Timestamp(latest_moment)
    )
    return {
        "label": label,
        "sentence": sentence,
        "scope": scope,
        "from_earlier_moment": bool(earlier),
    }


# The five states as a glyph. Line art rather than an emoji: an emoji renders
# in whatever the operating system decided it looks like, at a size it also
# decided, and the one thing this mark has to do is read the same on every
# machine. Green is damping and purple is amplification, as everywhere else.
_ART = {
    metrics.WEATHER_CLEAR: (
        f'<circle cx="22" cy="22" r="9" fill="{theme.ACCENT}"/>'
        f'<g stroke="{theme.ACCENT}" stroke-width="2.6" stroke-linecap="round">'
        '<line x1="22" y1="3" x2="22" y2="8"/><line x1="22" y1="36" x2="22" y2="41"/>'
        '<line x1="3" y1="22" x2="8" y2="22"/><line x1="36" y1="22" x2="41" y2="22"/>'
        '<line x1="8.5" y1="8.5" x2="12" y2="12"/><line x1="32" y1="32" x2="35.5" y2="35.5"/>'
        '<line x1="8.5" y1="35.5" x2="12" y2="32"/><line x1="32" y1="12" x2="35.5" y2="8.5"/>'
        "</g>"
    ),
    metrics.WEATHER_FAIR: (
        f'<circle cx="15" cy="13" r="7" fill="{theme.ACCENT}"/>'
        f'<g stroke="{theme.ACCENT}" stroke-width="2.2" stroke-linecap="round">'
        '<line x1="15" y1="1" x2="15" y2="4"/><line x1="3" y1="13" x2="6" y2="13"/>'
        '<line x1="6" y1="4" x2="8" y2="6"/></g>'
        f'<rect x="12" y="25" width="28" height="12" rx="6" fill="{theme.FAINT}"/>'
        f'<rect x="17" y="18" width="15" height="15" rx="7.5" fill="{theme.FAINT}"/>'
        f'<rect x="28" y="21" width="12" height="12" rx="6" fill="{theme.FAINT}"/>'
    ),
    metrics.WEATHER_UNSETTLED: (
        f'<circle cx="11" cy="10" r="6" fill="{theme.ACCENT}"/>'
        f'<rect x="7" y="17" width="30" height="12" rx="6" fill="{theme.FAINT}"/>'
        f'<rect x="13" y="10" width="16" height="16" rx="8" fill="{theme.FAINT}"/>'
        f'<rect x="25" y="13" width="12" height="12" rx="6" fill="{theme.FAINT}"/>'
        f'<polygon points="28,30 21,30 25,36 21,36 31,44 27,37 32,37" fill="{theme.PRIMARY}"/>'
    ),
    metrics.WEATHER_RAIN: (
        f'<rect x="7" y="15" width="30" height="12" rx="6" fill="{theme.FAINT}"/>'
        f'<rect x="13" y="8" width="16" height="16" rx="8" fill="{theme.FAINT}"/>'
        f'<rect x="25" y="11" width="12" height="12" rx="6" fill="{theme.FAINT}"/>'
        f'<g stroke="{theme.PRIMARY}" stroke-width="3.4" stroke-linecap="round">'
        '<line x1="16" y1="31" x2="13" y2="40"/><line x1="24" y1="31" x2="21" y2="40"/>'
        '<line x1="32" y1="31" x2="29" y2="40"/></g>'
    ),
    metrics.WEATHER_STORM: (
        f'<rect x="7" y="13" width="30" height="12" rx="6" fill="{theme.FAINT}"/>'
        f'<rect x="13" y="6" width="16" height="16" rx="8" fill="{theme.FAINT}"/>'
        f'<rect x="25" y="9" width="12" height="12" rx="6" fill="{theme.FAINT}"/>'
        f'<polygon points="25,27 14,27 21,35 15,35 29,44 24,36 30,36" fill="{theme.PRIMARY}"/>'
    ),
}


def _level(label: str, value: str, tone: str = "") -> str:
    classes = f' class="{tone}"' if tone else ""
    return f"<div><dt>{label}</dt><dd{classes}>{value}</dd></div>"


def panel(reading: dict, latest_moment=None) -> str:
    """The summary panel that sits above the view switcher.

    WHY IT IS A PANEL AND NOT A LINE OF TEXT. It answers the first question
    somebody has on opening a ticker — is hedging damping this or amplifying
    it — and it answers it on every view, so it has to be readable in the half
    second before the eye moves on. That means a shape: the state as a glyph
    and a word, the reasoning as one sentence, and the levels as tiles where
    the number is the content and its label is an eyebrow above it.

    The same shape the hosted product draws, deliberately. Two products with
    one name should not read as two tools.
    """
    words = describe(reading, latest_moment=latest_moment)
    flip = reading.get("gamma_flip")
    levels = [
        _level("Price", f"{reading['underlying_price']:,.2f}"),
        _level("Net GEX", format_gex(reading["net_gex"]),
               "gg-green" if reading["net_gex"] >= 0 else "gg-purple"),
    ]
    if reading.get("call_wall") is not None:
        levels.append(_level("Call wall", f"{reading['call_wall']:,.2f}", "gg-green"))
    if reading.get("put_wall") is not None:
        levels.append(_level("Put wall", f"{reading['put_wall']:,.2f}", "gg-purple"))
    levels.append(_level(
        "Gamma flip",
        "—" if flip is None else f"{flip:,.2f}",
        "",
    ))
    if flip is not None:
        levels.append(_level("Distance", f"{reading['flip_distance_pct']:.1f}%"))

    art = _ART.get(reading["state"], "")
    scope = f" · {words['scope']}" if words["scope"] else ""
    stale = ""
    if words["from_earlier_moment"]:
        stale = " · from an earlier collection than the rest of this screen"
    return (
        '<div class="gg-weather">'
        f'<svg width="44" height="44" viewBox="0 0 44 44" aria-hidden="true">{art}</svg>'
        '<div class="gg-weather-words">'
        f'<div class="gg-weather-label">{words["label"]}'
        f'<span class="gg-weather-scope"> · gamma weather{scope}{stale}</span></div>'
        f'<div class="gg-weather-sentence">{words["sentence"]}</div>'
        "</div>"
        f'<dl class="gg-levels">{"".join(levels)}</dl>'
        "</div>"
    )
