"""One row per ticker and trading day, and what changed between two of them.

    docker compose run --rm --no-deps worker \\
        python -m app.day_summary --backfill [--days 45] [--dry-run]

WHY A ROW PER DAY EXISTS AT ALL. What this tool has that a web page of today's
numbers does not is the history it collected on your own machine — and it used
to show that history in four places that never said so: the heatmap's Replay
selector, the history switch, OI Delta and a contract's chart. The figures a
daily summary is made of — the walls, the flip, the regime, net GEX over the
near term — existed only for the newest moment, so "what changed since
yesterday" could not be answered from anything stored. Now it can.

WHAT "A DAY" IS. New York's calendar date of the collection, weekdays only —
exactly the rule the OI Delta and the Unusual Activity baseline already apply.
A collection at 04:04 UTC on a Tuesday is 00:04 in New York and files under
Tuesday; by the close the row has been overwritten by Tuesday's own session,
which is what the UPSERT is for. Two views with two different "yesterday"s
would be one screen contradicting another, so no second rule is invented here.

WHAT IS IN THE ROW AND WHAT IS NOT. The summary's figures, the nearest
meaningful expiry's max pain and expected move, the chain's shape and the GEX
profile by strike. Put/call, weighted IV and open-interest totals are NOT
repeated: snapshot_iv_summary already holds them per moment, and the row names
its moment.

THE ARITHMETIC OF A CHANGE IS HERE, NOT IN THE VIEW. `changes` is a pure
function of two rows and their rollups, so anything that reports a change —
the screen today, a notification later — reports the same one.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import logging
from pathlib import Path

import pandas as pd

from app import config, market_calendar, metrics, metrics_core, weather

log = logging.getLogger("day_summary")

_HERE = Path(__file__).resolve().parent
_FORMULA_FILES = ("metrics_core.py", "metrics.py", "day_summary.py")


def _code_sha() -> str:
    digest = hashlib.sha256()
    for name in _FORMULA_FILES:
        digest.update((_HERE / name).read_bytes())
    return digest.hexdigest()


# The formulas a row was computed with. A row whose sha lags the running code
# is rebuilt rather than compared against numbers arrived at differently: an
# upgrade that changes a formula would otherwise show its own release as a
# market event.
CODE_SHA = _code_sha()

# The kinds a change can be — the closed set the view draws chips from. Colour
# belongs to the REGIME, not to the direction: green and purple already mean
# damping and amplifying on every chart here, so a chip is coloured only when
# the regime moved, and "moved" stays in plain ink.
SAME = "same"              # nothing to report, drawn quietly
MOVED = "moved"            # a number changed; the delta says how much
DAMPING = "damping"        # the regime moved towards positive gamma (green)
AMPLIFYING = "amplifying"  # the regime moved towards negative gamma (purple)
GONE = "gone"              # existed before, absent now (an expiry rolled off)
NEW = "new"                # absent before, exists now
UNKNOWN = "unknown"        # one side has no value: nothing to compare

KINDS = (SAME, MOVED, DAMPING, AMPLIFYING, GONE, NEW, UNKNOWN)

# The order the states go in, worst weather last: a move to a later state is
# towards amplification, to an earlier one towards damping.
_STATE_ORDER = (
    metrics.WEATHER_CLEAR,
    metrics.WEATHER_FAIR,
    metrics.WEATHER_UNSETTLED,
    metrics.WEATHER_RAIN,
    metrics.WEATHER_STORM,
)


def session_day(moment) -> dt.date:
    """The trading day a collection belongs to — New York's date."""
    stamp = pd.Timestamp(moment).to_pydatetime()
    return market_calendar.market_day(stamp)


def is_session_day(day: dt.date) -> bool:
    """Weekdays only, as the per-day moment list already filters. The
    exchange calendar's holidays are deliberately NOT consulted here, so that
    the two rules cannot drift apart; a holiday's single closed-market snapshot
    makes a row that says the same as the day before, which is honest."""
    return day.isoweekday() <= 5


# --- the row --------------------------------------------------------------


def summarize(
    chain: pd.DataFrame, pricing: metrics_core.PricingInputs, reading: dict | None
) -> dict | None:
    """The day row for one solved chain, given the weather figures already
    computed from it (`metrics.gamma_weather`).

    None where the chain cannot support a summary — no rows, no spot, too few
    expiries — because a day without walls and a state is not a day this view
    can compare, and inventing one would be worse than the day being absent.

    THE NEAREST EXPIRY IS THE NEAREST WITH ENOUGH OPEN CONTRACTS. A max pain
    resting on three contracts moves a strike a day on noise, and a view whose
    entire job is to report what changed would be reporting the noise.
    """
    if reading is None or chain.empty:
        return None
    moment = pd.Timestamp(reading["collected_at"])
    snapshot = chain[chain["collected_at"] == moment]
    if snapshot.empty:
        return None
    # Expiries as timestamps whatever the frame holds (dates from a fixture,
    # datetime64 from the database), so the comparisons below hold.
    snapshot = snapshot.assign(expiry=pd.to_datetime(snapshot["expiry"], errors="coerce"))
    expiries = sorted(pd.Timestamp(e) for e in snapshot["expiry"].dropna().unique())
    nearest = None
    for expiry in expiries:
        subset = snapshot[snapshot["expiry"] == expiry]
        if metrics.years_to_expiry(expiry, moment) <= 0:
            continue
        if metrics.contracts_backing_expiry(subset, expiry) < config.MIN_CONTRACTS_FOR_EXPIRY_METRICS:
            continue
        nearest = expiry
        break
    max_pain = None
    move = None
    if nearest is not None:
        pain = metrics_core.max_pain(snapshot[snapshot["expiry"] == nearest], nearest)
        max_pain = None if pain is None or pd.isna(pain) else float(pain)
        move = metrics.expected_move(snapshot, nearest, moment)
    return {
        "day": session_day(moment),
        "collected_at": moment.to_pydatetime(),
        "underlying_price": float(reading["underlying_price"]),
        "net_gex": float(reading["net_gex"]),
        "call_wall": _finite(reading.get("call_wall")),
        "put_wall": _finite(reading.get("put_wall")),
        "gamma_flip": _finite(reading.get("gamma_flip")),
        "state": reading["state"],
        "expiry_count": int(reading["expiry_count"]),
        "horizon_dte": int(reading["horizon_dte"]),
        "nearest_expiry": None if nearest is None else nearest.date(),
        "nearest_max_pain": max_pain,
        "move_lower": None if move is None else float(move["lower"]),
        "move_upper": None if move is None else float(move["upper"]),
        "contracts": int(len(snapshot)),
        **_flow(snapshot),
        "expiries": [e.strftime("%Y-%m-%d") for e in expiries],
        "gex_by_strike": [[float(s), float(g)] for s, g in reading.get("gex_by_strike", [])],
    }


def _flow(snapshot: pd.DataFrame) -> dict:
    """The day's volume and open interest by side.

    Summed here, from the chain already in memory, because nothing else stores
    them per collection: the rollup table holds the volatility average and
    nothing more. Reading them back later would mean re-reading a whole chain
    of a past day to count two columns.
    """
    kinds = snapshot["option_type"].astype(str).str.lower()
    out = {}
    for side, prefix in (("call", "call"), ("put", "put")):
        rows = snapshot[kinds == side]
        for column, name in (("volume", "volume"), ("open_interest", "oi")):
            total = pd.to_numeric(rows[column], errors="coerce").sum()
            out[f"{prefix}_{name}"] = None if pd.isna(total) else int(total)
    return out


def _finite(value) -> float | None:
    if value is None:
        return None
    number = pd.to_numeric(value, errors="coerce")
    return None if pd.isna(number) else float(number)


# --- the comparison -------------------------------------------------------


def strike_step(row: dict) -> float | None:
    """The smallest gap between listed strikes in the row's profile — the unit
    a wall or a max pain moves in.

    Levels are reported in strikes rather than percent for the same reason the
    heatmap's slider counts strikes: "+1 strike · +5.00" is a statement about
    the chain, "+0.66%" is a statement about nothing.
    """
    strikes = sorted({float(s) for s, _g in row.get("gex_by_strike") or []})
    gaps = [b - a for a, b in zip(strikes, strikes[1:]) if b - a > 1e-9]
    return min(gaps) if gaps else None


def _ratio(numerator, denominator) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return float(numerator) / float(denominator)


def _number_change(
    key: str, label: str, before, after, *, unit: str, step: float | None = None
) -> dict:
    """One ladder line for two plain numbers.

    `unit` says how the delta is to be read: "price" (absolute and percent),
    "strikes" (in strike steps), "pts" (percentage points, for a volatility),
    "count", "pct" (percent of the earlier value, for open interest), "gex",
    "ratio".
    """
    line = {
        "key": key, "label": label, "before": before, "after": after, "unit": unit,
        "delta": None, "delta_pct": None, "strikes": None, "kind": UNKNOWN,
    }
    if before is None and after is None:
        return line
    if before is None:
        line["kind"] = NEW
        return line
    if after is None:
        line["kind"] = GONE
        return line
    delta = float(after) - float(before)
    line["delta"] = delta
    if abs(delta) < 1e-9:
        line["kind"] = SAME
        return line
    line["kind"] = MOVED
    if unit in ("price", "pct") and float(before) != 0:
        line["delta_pct"] = delta / abs(float(before)) * 100
    if unit == "strikes" and step:
        line["strikes"] = round(delta / step, 2)
    return line


def changes(
    previous: dict | None,
    current: dict,
    previous_rollup: dict | None,
    current_rollup: dict | None,
    previous_same_expiry_pain: float | None = None,
) -> list[dict]:
    """The ladder: one line per figure, before -> after -> how it changed.

    In reading order, and the order is an argument: the regime comes first
    because the first question on a second visit is whether the weather turned,
    then the levels, then the flow.

    `previous` None means the first day on record: every line is UNKNOWN and
    the view says so, rather than drawing a screenful of "new" that reads as a
    screenful of events.

    `previous_same_expiry_pain` is yesterday's max pain of TODAY's nearest
    expiry, for the day the nearest expiry rolls over: max pain is then still
    compared for one expiry, and the line names which expiry was nearest
    before. Without it the line would claim a new level that is really the same
    chain seen through a different expiry.
    """
    prev = previous or {}
    p_roll = previous_rollup or {}
    c_roll = current_rollup or {}
    step = strike_step(current) or strike_step(prev)
    lines: list[dict] = []

    # Weather: a word, coloured by which way the regime moved.
    before_state, after_state = prev.get("state"), current.get("state")
    state = {
        "key": "state", "label": "Gamma weather", "before": before_state, "after": after_state,
        "unit": "state", "delta": None, "delta_pct": None, "strikes": None, "kind": UNKNOWN,
    }
    if before_state is not None and after_state is not None:
        b, a = _STATE_ORDER.index(before_state), _STATE_ORDER.index(after_state)
        state["kind"] = SAME if a == b else (AMPLIFYING if a > b else DAMPING)
    lines.append(state)

    lines.append(_number_change(
        "price", "Price", prev.get("underlying_price"), current.get("underlying_price"), unit="price",
    ))

    net = _number_change("net_gex", "Net GEX", prev.get("net_gex"), current.get("net_gex"), unit="gex")
    if net["kind"] == MOVED and (net["before"] < 0) != (net["after"] < 0):
        net["kind"] = DAMPING if net["after"] >= 0 else AMPLIFYING
    lines.append(net)

    lines.append(_number_change(
        "call_wall", "Call wall", prev.get("call_wall"), current.get("call_wall"),
        unit="strikes", step=step,
    ))
    lines.append(_number_change(
        "put_wall", "Put wall", prev.get("put_wall"), current.get("put_wall"),
        unit="strikes", step=step,
    ))
    lines.append(_number_change(
        "gamma_flip", "Gamma flip", prev.get("gamma_flip"), current.get("gamma_flip"), unit="price",
    ))

    # Max pain: comparable only for the same expiry, so a nearest expiry that
    # rolled off between the two days is named rather than differenced.
    p_exp, c_exp = prev.get("nearest_expiry"), current.get("nearest_expiry")
    same_expiry = p_exp is not None and c_exp is not None and str(p_exp) == str(c_exp)
    if same_expiry or p_exp is None:
        pain_before = prev.get("nearest_max_pain")
    else:
        pain_before = previous_same_expiry_pain
    pain = _number_change(
        "max_pain", f"Max pain · {c_exp}" if c_exp else "Max pain",
        pain_before, current.get("nearest_max_pain"), unit="strikes", step=step,
    )
    if previous and p_exp is not None and not same_expiry:
        if pain_before is None:
            pain["kind"] = NEW if c_exp is not None else GONE
        pain["note"] = f"nearest expiry was {p_exp}"
    lines.append(pain)

    move = {
        "key": "expected_move", "label": f"Expected move · {c_exp}" if c_exp else "Expected move",
        "before": _band(prev) if same_expiry else None, "after": _band(current), "unit": "band",
        "delta": None, "delta_pct": None, "strikes": None, "kind": UNKNOWN,
    }
    if move["before"] is not None and move["after"] is not None:
        width_before = move["before"][1] - move["before"][0]
        width_after = move["after"][1] - move["after"][0]
        move["delta"] = width_after - width_before
        move["kind"] = SAME if abs(move["delta"]) < 1e-9 else MOVED
    lines.append(move)

    lines.append(_number_change(
        "pcr_volume", "Put/Call · volume",
        _ratio(p_roll.get("put_volume"), p_roll.get("call_volume")),
        _ratio(c_roll.get("put_volume"), c_roll.get("call_volume")), unit="ratio",
    ))
    lines.append(_number_change(
        "pcr_oi", "Put/Call · open interest",
        _ratio(p_roll.get("put_oi"), p_roll.get("call_oi")),
        _ratio(c_roll.get("put_oi"), c_roll.get("call_oi")), unit="ratio",
    ))
    lines.append(_number_change(
        "iv", "IV · volume-weighted",
        p_roll.get("iv_weighted_avg"), c_roll.get("iv_weighted_avg"), unit="pts",
    ))
    lines.append(_number_change(
        "call_oi", "Open interest · calls", p_roll.get("call_oi"), c_roll.get("call_oi"), unit="pct",
    ))
    lines.append(_number_change(
        "put_oi", "Open interest · puts", p_roll.get("put_oi"), c_roll.get("put_oi"), unit="pct",
    ))

    # The chain's shape: expiries that rolled off and ones newly listed, named
    # — "31 -> 30" says less than "11 Sep expired".
    before_list = list(prev.get("expiries") or [])
    after_list = list(current.get("expiries") or [])
    shape = _number_change(
        "expiries", "Expiries listed",
        len(before_list) if previous else None, len(after_list), unit="count",
    )
    shape["rolled_off"] = [e for e in before_list if e not in after_list]
    shape["listed"] = [e for e in after_list if e not in before_list]
    lines.append(shape)
    lines.append(_number_change(
        "contracts", "Contracts in the chain", prev.get("contracts"), current.get("contracts"), unit="count",
    ))

    if previous is None:
        # The first day on record: there is nothing to compare, and "new" would
        # read as an event. Every line unknown, and the view says why.
        for line in lines:
            line["kind"] = UNKNOWN
    return lines


def _band(row: dict) -> list[float] | None:
    lower, upper = row.get("move_lower"), row.get("move_upper")
    if lower is None or upper is None:
        return None
    return [float(lower), float(upper)]


def headline(lines: list[dict]) -> str:
    """One descriptive sentence for the top of the view: the regime if it
    moved, else the levels, else that nothing did. Never advisory — the same
    rule the weather's words follow."""
    by_key = {line["key"]: line for line in lines}
    state = by_key["state"]
    if state["kind"] in (DAMPING, AMPLIFYING):
        return f"Gamma weather turned from {_word(state['before'])} to {_word(state['after'])}."
    net = by_key["net_gex"]
    if net["kind"] in (DAMPING, AMPLIFYING):
        return "Net gamma changed sign: dealer hedging now " + (
            "damps moves." if net["kind"] == DAMPING else "amplifies moves."
        )
    moved = [
        line["label"].split(" · ")[0].lower()
        for line in lines
        if line["key"] in ("call_wall", "put_wall", "max_pain") and line["kind"] == MOVED
    ]
    if moved:
        return "Levels moved: " + ", ".join(moved) + "."
    if state["kind"] == SAME:
        return f"Same weather as the day before ({_word(state['after'])}); the levels held."
    return "First day on record — nothing to compare yet."


def _word(state: str | None) -> str:
    words = weather.WEATHER_WORDS
    return words[state][0].lower() if state in words else str(state)


# --- how a line is read out -----------------------------------------------


def format_value(line: dict, side: str) -> str:
    """One side of a ladder line, formatted by what it is.

    THE UNIT DECIDES THE FORMAT, not the caller. A price wants two decimals, a
    volatility wants a percentage, net GEX wants a scale suffix and a band
    wants both ends — and a view that formats them itself would be a second
    place for "what is this number" to be answered differently.
    """
    value = line.get(side)
    if value is None:
        return "—"
    unit = line["unit"]
    if unit == "state":
        words = weather.WEATHER_WORDS
        return words[value][0] if value in words else str(value)
    if unit == "band":
        return f"{value[0]:,.2f} – {value[1]:,.2f}"
    if unit == "gex":
        return weather.format_gex(value, signed=False)
    if unit == "pts":
        return f"{float(value) * 100:.1f}%"
    if unit in ("count",):
        return f"{int(value):,}"
    if unit == "pct":
        return f"{int(value):,}"
    if unit == "ratio":
        return f"{float(value):.2f}"
    return f"{float(value):,.2f}"


def format_delta(line: dict) -> str:
    """The change itself, in the terms the figure moves in.

    Levels move in strikes, because that is the grid they live on: "+1 strike ·
    +5.00" says something about the chain and "+0.66%" says something about
    nothing. Open interest moves in percent, because its absolute size is
    already on the line. A volatility moves in percentage points, because a
    percentage of a percentage is how people misread it.
    """
    kind, unit = line["kind"], line["unit"]
    if kind == UNKNOWN:
        return "—"
    if kind == GONE:
        return "gone"
    if kind == NEW:
        return "new"
    if kind == SAME:
        return "unchanged"
    if unit == "state":
        return "amplifying" if kind == AMPLIFYING else "damping"
    delta = line.get("delta")
    if delta is None:
        return ""
    printed = _printed_delta(line, delta, unit)
    # A CHANGE TOO SMALL TO PRINT IS NOT A CHANGE. Without this a ratio that
    # moved in the fourth decimal reads "+0.00" next to two identical numbers,
    # which asks the reader to find a difference that is not there.
    if _is_zero(printed):
        return "unchanged"
    return printed


def _is_zero(printed: str) -> bool:
    return all(character in "+-0.,%" for character in printed.split(" ")[0])


def _printed_delta(line: dict, delta: float, unit: str) -> str:
    if unit == "gex":
        return weather.format_gex(delta)
    if unit == "pts":
        return f"{delta * 100:+.1f} pts"
    if unit == "strikes" and line.get("strikes") is not None:
        strikes = line["strikes"]
        whole = int(strikes) if float(strikes).is_integer() else strikes
        plural = "" if abs(float(strikes)) == 1 else "s"
        return f"{whole:+g} strike{plural} · {delta:+,.2f}"
    if unit == "band":
        return f"{delta:+,.2f} wide"
    if unit in ("count",):
        return f"{int(delta):+,}"
    if unit == "pct" and line.get("delta_pct") is not None:
        return f"{line['delta_pct']:+.1f}%"
    if line.get("delta_pct") is not None:
        return f"{delta:+,.2f} ({line['delta_pct']:+.1f}%)"
    return f"{delta:+,.2f}"


# --- building and filling in ----------------------------------------------


def build_row(conn, ticker: str, source: str, moment, pricing) -> dict | None:
    """One day row from the stored snapshot of one moment: read, solve,
    weather, summarize — the collection pass's own path, replayed."""
    from app import db  # local import: db imports config, not this module

    raw = db.get_snapshots_at(conn, ticker, [moment], source=source)
    if raw.empty:
        return None
    chain = metrics.with_solved_iv(raw, pricing, ticker=ticker)
    reading = metrics.gamma_weather(
        chain,
        pricing,
        near_pct=config.GAMMA_WEATHER_NEAR_FLIP_PCT,
        far_pct=config.GAMMA_WEATHER_FAR_FLIP_PCT,
        horizon_days=config.GAMMA_WEATHER_HORIZON_DAYS,
        min_expiries=config.GAMMA_WEATHER_MIN_EXPIRIES,
    )
    return summarize(chain, pricing, reading)


def backfill(conn, source: str, tickers: list[str], days: int, *, dry_run: bool = False) -> int:
    """Write the day rows that are missing, ticker by ticker, from the
    snapshots as stored. Returns how many rows were written — or would be,
    under `dry_run`.

    WHY THIS IS NOT A COMMAND PEOPLE HAVE TO RUN. A machine that was switched
    off for a week, an upgrade that brings these rows in for the first time,
    and a formula that changed all leave the same hole, and a hole in a view
    about what changed is indistinguishable from nothing having changed. The
    worker calls this at startup and once a day, bounded; the command line is
    for filling a long history on purpose.

    ONE DAY MUST NOT STOP THE REST. A snapshot that cannot be solved is logged
    and skipped, because the alternative is one bad day hiding every good one
    behind it.
    """
    from app import db

    written = 0
    for ticker in tickers:
        missing = db.missing_day_summaries(conn, ticker, source, days, CODE_SHA)
        if not missing:
            continue
        log.info(
            "Day rows to build for %s: %d (%s).",
            ticker, len(missing), "dry run" if dry_run else "writing",
        )
        for moment in missing:
            if dry_run:
                written += 1
                continue
            try:
                row = build_row(conn, ticker, source, moment, metrics.DEFAULT_PRICING)
            except Exception:  # noqa: BLE001 — one day must not stop the rest
                log.error("Day row not built for %s at %s", ticker, moment, exc_info=True)
                continue
            if row is None:
                continue
            db.upsert_day_summary(conn, ticker, source, row, CODE_SHA)
            written += 1
    return written


def main(argv: list[str] | None = None) -> int:  # pragma: no cover — a command, exercised by hand
    from app import db

    parser = argparse.ArgumentParser(
        description="Fill in the per-day rows the Changes view compares."
    )
    parser.add_argument("--backfill", action="store_true", help="Build the rows missing from the last --days days.")
    parser.add_argument("--days", type=int, default=config.DAY_SUMMARY_BACKFILL_DAYS)
    parser.add_argument("--ticker", help="Only this ticker (default: every watched ticker).")
    parser.add_argument("--dry-run", action="store_true", help="Count what would be built; write nothing.")
    args = parser.parse_args(argv)
    if not args.backfill:
        parser.print_help()
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    conn = db.get_connection()
    try:
        tickers = [args.ticker.upper()] if args.ticker else db.get_watchlist(conn)
        count = 0
        for ticker in tickers:
            source = db.active_source(conn, ticker)
            if source is None:
                continue
            count += backfill(conn, source, [ticker], args.days, dry_run=args.dry_run)
        log.info(
            "%s %d day row(s) for %d ticker(s).",
            "Would build" if args.dry_run else "Built", count, len(tickers),
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
