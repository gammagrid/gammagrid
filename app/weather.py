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

from app import metrics

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
