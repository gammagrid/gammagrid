"""Renders every view of the dashboard, one per script run, on a throwaway DB.

Why this is a third file rather than more of smoke_test.py: it is the only
check that executes `dashboard.py` at all. The other two verify that numbers
are right and that the SQLite plumbing works; neither would notice a page that
raises before drawing anything.

It exists because the dashboard stopped using `st.tabs`. Streamlit has no lazy
tabs — every `with tab_x:` body ran on every rerun, so a name assigned in one
tab and read in another worked by accident. With one view rendering at a time
that becomes a NameError on exactly one view: the one nobody opened before
shipping. Both halves of this file guard that:

  * `check_no_cross_view_names` reads the source, so it also sees branches
    behind a button click that no default render reaches. That matters — the
    first real instance of this bug was on a code path that only runs after
    clicking a table row.
  * the render pass then actually draws each view, which catches everything
    static analysis cannot: a widget given impossible arguments, an empty
    frame reaching a chart, a column that isn't there.

Usage: python tests/render_views.py
"""

import ast
import builtins
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testdb  # noqa: E402

testdb.configure()

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.join(REPO, "app", "dashboard.py")

VIEWS = [
    "Overview", "Changes", "Max Pain / GEX", "GEX Heatmap", "Volatility (IV)",
    "Contract", "Screener", "Unusual Activity", "OI Delta",
]

def check_no_cross_view_names() -> list[str]:
    """No view may read a name that only another view assigns."""
    with open(DASHBOARD) as handle:
        tree = ast.parse(handle.read())

    def view_label(node):
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
            return None
        test = node.test
        if not (isinstance(test.left, ast.Name) and test.left.id == "active_view"):
            return None
        if len(test.comparators) != 1 or not isinstance(test.comparators[0], ast.Constant):
            return None
        return str(test.comparators[0].value)

    views, module_names = {}, set()
    for node in tree.body:
        label = view_label(node)
        if label is not None:
            views[label] = node
            continue
        # Anything bound at module level outside a view is available to all.
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                module_names.add(sub.id)
            elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                module_names.add(sub.name)
            elif isinstance(sub, ast.alias):
                module_names.add((sub.asname or sub.name).split(".")[0])

    def collect(nodes):
        """(assigned, read) for one view body, respecting nested scopes.

        Without the scope handling this cries wolf: a nested helper's parameter
        and a comprehension's target both look like one view reading another's
        local. A check that reports things that are fine is a check people
        learn to skip."""
        stores, loads = set(), set()

        def params_of(spec):
            names = {
                arg.arg
                for group in (spec.posonlyargs, spec.args, spec.kwonlyargs)
                for arg in group
            }
            for extra in (spec.vararg, spec.kwarg):
                if extra is not None:
                    names.add(extra.arg)
            return names

        def walk(node, shadowed):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                stores.add(node.name)
                spec = node.args
                # Defaults and decorators are evaluated where the function is
                # written, not inside it — they see the view's own names.
                for default in [*spec.defaults, *[d for d in spec.kw_defaults if d]]:
                    walk(default, shadowed)
                for decorator in node.decorator_list:
                    walk(decorator, shadowed)
                for statement in node.body:
                    walk(statement, shadowed | params_of(spec))
                return
            if isinstance(node, ast.Lambda):
                walk(node.body, shadowed | params_of(node.args))
                return
            if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                targets = {
                    sub.id
                    for generator in node.generators
                    for sub in ast.walk(generator.target)
                    if isinstance(sub, ast.Name)
                }
                inner = shadowed | targets
                for generator in node.generators:
                    walk(generator.iter, inner)
                    for condition in generator.ifs:
                        walk(condition, inner)
                elements = ([node.key, node.value]
                            if isinstance(node, ast.DictComp) else [node.elt])
                for element in elements:
                    walk(element, inner)
                return
            if isinstance(node, ast.Name):
                if isinstance(node.ctx, ast.Store):
                    stores.add(node.id)
                elif node.id not in shadowed:
                    loads.add(node.id)
                return
            if isinstance(node, ast.ClassDef):
                stores.add(node.name)
            elif isinstance(node, ast.alias):
                stores.add((node.asname or node.name).split(".")[0])
                return
            for child in ast.iter_child_nodes(node):
                walk(child, shadowed)

        for body_node in nodes:
            walk(body_node, frozenset())
        return stores, loads

    assigned, read = {}, {}
    for label, node in views.items():
        assigned[label], read[label] = collect(node.body)

    problems = []
    for label in views:
        elsewhere = {
            name: other
            for other, names in assigned.items() if other != label
            for name in names
        }
        for name in sorted(read[label] - assigned[label] - module_names):
            if name in elsewhere and name not in dir(builtins):
                problems.append(
                    f"view {label!r} reads `{name}`, which only the "
                    f"{elsewhere[name]!r} view assigns"
                )
    return problems

def seed(db) -> None:
    """A small but complete chain: two expiries, three strikes, both sides,
    three collections. A view that renders only because its data was empty
    proves nothing, so every view has something real to draw.

    One expiry is deliberately today's — the case that used to leave the GEX
    chart blank, and the one that makes the heatmap fall back to a single
    tradeable expiry."""
    conn = db.get_connection()
    db.add_ticker(conn, "RENDERTEST")
    today = datetime.utcnow().date()
    base = datetime.utcnow().replace(microsecond=0) - timedelta(hours=3)
    rows = []
    for days_out in (0, 21, 45):
        expiry = (today + timedelta(days=days_out)).isoformat()
        for strike in (95.0, 100.0, 105.0):
            for option_type in ("call", "put"):
                itm = strike < 100 if option_type == "call" else strike > 100
                rows.append({
                    "expiry": expiry, "strike": strike, "option_type": option_type,
                    "last_price": abs(100 - strike) / 10 + 1.0,
                    "bid": 0.9, "ask": 1.1,
                    "volume": 100 + int(strike) % 7,
                    "open_interest": 500 + int(strike),
                    "implied_volatility": 0.25 + days_out / 1000,
                    "in_the_money": itm,
                })
    chain = pd.DataFrame(rows)
    for step in range(3):
        # Volume and OI have to move between collections or OI Delta and
        # Unusual Activity compute on nothing and pass on empty input.
        moving = chain.copy()
        moving["volume"] = moving["volume"] + step * 25
        moving["open_interest"] = moving["open_interest"] + step * 60
        db.insert_snapshot(
            conn, "RENDERTEST", base + timedelta(hours=step), 100.0 + step, moving
        )
    conn.close()

@contextmanager
def empty_database():
    """Truncate every table the renders touch, and hand back a clean database.

    A context manager rather than a plain call so that the block below keeps
    its shape from when this was a temporary SQLite file — and so that the
    emptying is visibly scoped to the renders rather than something that
    happened earlier in the module.

    TRUNCATE, not DROP: the schema is owned by the migrations, and recreating
    it here would mean this file quietly holds a second copy of it.
    """
    from app import db

    conn = db.get_connection()
    try:
        testdb.truncate_all(conn)
        yield
    finally:
        conn.close()

def expanders(block) -> int:
    """How many expanders a part of the page drew, at any depth."""
    children = getattr(block, "children", None) or {}
    return sum((child.type == "expander") + expanders(child) for child in children.values())

def guide_links(block, lesson: str) -> list[bool]:
    """Every element linking to `lesson`, as whether the element right after it
    in the same block is an expander — the explanation it belongs on."""
    found = []
    children = getattr(block, "children", None) or {}
    items = [children[key] for key in sorted(children)]
    for index, child in enumerate(items):
        if child.type == "markdown" and f'href="{lesson}"' in (child.value or ""):
            after = items[index + 1] if index + 1 < len(items) else None
            found.append(after is not None and after.type == "expander")
        found.extend(guide_links(child, lesson))
    return found

def check_every_view_links_its_lesson() -> list[str]:
    """Each view's first explanation is `howto(<that view>)`, which puts the
    view's lesson in the Guide on its label — read from the source, so that a
    view whose explanation only appears with enough data is held to it too.

    A view counts the explanations of the functions it calls (the Contract
    view's lives in the contract card it shares with the Screener)."""
    from app import guide

    with open(DASHBOARD) as handle:
        tree = ast.parse(handle.read())
    functions = {
        node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
    }

    def explanations(node, seen=frozenset()):
        """The expanders a piece of the page opens, in source order: the view
        named by `howto("...")`, or None for a bare `st.expander(...)`."""
        found = []
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            name = sub.func.attr if isinstance(sub.func, ast.Attribute) else getattr(sub.func, "id", None)
            if name == "howto" and sub.args and isinstance(sub.args[0], ast.Constant):
                found.append((sub.lineno, sub.args[0].value))
            elif name == "expander":
                found.append((sub.lineno, None))
            elif name in functions and name not in seen:
                found.extend(
                    (sub.lineno, view) for _, view in explanations(functions[name], seen | {name})
                )
        return sorted(found, key=lambda item: item[0])

    problems = []
    for node in tree.body:
        if not (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                and isinstance(node.test.left, ast.Name) and node.test.left.id == "active_view"):
            continue
        view = node.test.comparators[0].value
        opened = [label for _, label in explanations(node)]
        if view not in guide.VIEW_LESSONS:
            problems.append(f"view {view!r} has no lesson in app/guide.py")
        elif not opened or opened[0] != view:
            problems.append(f"view {view!r}: its first explanation is not howto({view!r}) (found {opened[:1]})")
    return problems

# ---------------------------------------------------------------------------
# DEX, vanna and charm on the Max Pain / GEX view, held to numbers. The pass in
# main() proves each branch draws; this proves what it draws: the figure in
# the sentence is the expiry's total, its unit and words follow the expiry,
# the bars are coloured by their own sign, and GEX is still the default.
#
# THE FIXTURE DOES NOT CARRY THE CALENDAR. Every moment is a literal: one
# collection on Friday 2026-11-13 at 13:00 New York time, and expiries the day
# before (no time left), the same day (three hours to the close) and five weeks
# out. The view measures time from the collection, not from the clock, so the
# expiry-day branch is reached whatever day the check runs on.

QA_TICKER = "RENDERQA"
QA_MOMENT = datetime(2026, 11, 13, 18, 0)           # 13:00 ET, a Friday session
QA_EXPIRIES = ("2026-11-12", "2026-11-13", "2026-12-18")
QA_SPOT = 100.0


def _bs_price(spot, strike, years, iv, kind, rate=0.05):
    """Black-Scholes price at the config's flat rate, no dividend — only so that
    the solved IV the view runs on is a sensible number, not to check anything."""
    from math import erf, exp, log, sqrt

    def cdf(x):
        return 0.5 * (1 + erf(x / sqrt(2)))

    if years <= 0:
        return max(0.0, spot - strike) if kind == "call" else max(0.0, strike - spot)
    d1 = (log(spot / strike) + (rate + iv * iv / 2) * years) / (iv * sqrt(years))
    d2 = d1 - iv * sqrt(years)
    if kind == "call":
        return spot * cdf(d1) - strike * exp(-rate * years) * cdf(d2)
    return strike * exp(-rate * years) * cdf(-d2) - spot * cdf(-d1)


def seed_dealer_greeks(db) -> None:
    """Strikes 90-110; calls heavy above the spot and puts heavy below, except
    at 105 and 107.5, where puts outweigh calls — so both signs of vanna appear
    among the bars, as they do on a real chain."""
    conn = db.get_connection()
    db.add_ticker(conn, QA_TICKER)
    close = {e: datetime.fromisoformat(e).replace(hour=21) for e in QA_EXPIRIES}   # 16:00 ET in November
    rows = []
    for expiry in QA_EXPIRIES:
        years = (close[expiry] - QA_MOMENT).total_seconds() / (365 * 24 * 3600)
        for strike in (90.0, 92.5, 95.0, 97.5, 100.0, 102.5, 105.0, 107.5, 110.0):
            for option_type in ("call", "put"):
                above = strike > QA_SPOT
                oi = 4000 if (option_type == "call") == above else 600
                if strike in (105.0, 107.5):
                    oi = 5000 if option_type == "put" else 300
                iv = 0.18 + abs(strike - QA_SPOT) / 200
                price = max(_bs_price(QA_SPOT, strike, years, iv, option_type), 0.01)
                rows.append({
                    "expiry": expiry, "strike": strike, "option_type": option_type,
                    "last_price": round(price, 2), "bid": round(price * 0.98, 2), "ask": round(price * 1.02, 2),
                    "volume": 100, "open_interest": oi, "implied_volatility": iv,
                    "in_the_money": strike < QA_SPOT if option_type == "call" else strike > QA_SPOT,
                })
    db.insert_snapshot(conn, QA_TICKER, QA_MOMENT, QA_SPOT, pd.DataFrame(rows))
    conn.close()


def _qa_expected(db, metrics, expiry: str) -> dict:
    """The expiry's totals from the core, on the chain the view itself reads
    (the stored snapshot with IV solved by our model, the default pricing)."""
    conn = db.get_connection()
    frame = metrics.with_solved_iv(db.get_latest_snapshot(conn, QA_TICKER), metrics.DEFAULT_PRICING,
                                   ticker=QA_TICKER)
    conn.close()
    rows = frame[pd.to_datetime(frame["expiry"]) == pd.Timestamp(expiry)]
    assert rows["implied_volatility"].notna().mean() > 0.9, "the fixture's prices did not solve to an IV"
    totals = metrics.dealer_exposures(rows, metrics.DEFAULT_PRICING, QA_TICKER)
    gex = metrics.gamma_exposure_profile(frame, rows["expiry"].iloc[0])
    gross = float((pd.to_numeric(rows["open_interest"]) * 100 * QA_SPOT).sum())
    return {"DEX": float(totals["dex"].sum()), "Vanna": float(totals["vanna"].sum()),
            "Charm": float(totals["charm"].sum()), "GEX": float(metrics.net_gamma_exposure(gex)), "gross": gross}


_SCALE = {"": 1.0, "K": 1e3, "M": 1e6, "B": 1e9}


def _printed_money(line: str, metric: str) -> float:
    """The signed dollar figure after 'Net <metric>' in the sentence."""
    import re

    word = "DEX" if metric == "DEX" else metric.lower()
    found = re.search(rf"Net {word} \*\*([+−])\\?\$([0-9.]+)([KMB]?)\*\*", line)
    assert found, f"no 'Net {word} ±$…' figure in {line!r}"
    value = float(found.group(2)) * _SCALE[found.group(3)]
    return -value if found.group(1) == "−" else value


def _same_printed(shown: float, want: float) -> bool:
    """Equal to the two decimals of the unit the figure is printed in."""
    size = abs(want)
    unit = 1e9 if size >= 1e9 else 1e6 if size >= 1e6 else 1e3 if size >= 1e3 else 1.0
    return abs(shown - want) <= 0.005 * unit + 1e-9 * size


def _chart_bars(app) -> list[tuple[float, str]]:
    """(value, sign label) of every bar of the view's one bar chart."""
    import json

    charts = app.get("vega_lite_chart")
    assert len(charts) == 1, f"{len(charts)} charts on the view, want the one by-strike chart"
    spec = json.loads(charts[0].proto.spec)
    datasets = spec.get("datasets") or {}
    rows = [row for data in datasets.values() for row in data]
    if not rows:                                   # Streamlit can pass the frame beside the spec
        import pyarrow as pa
        for dataset in charts[0].proto.datasets:
            table = pa.ipc.open_stream(dataset.data.data).read_all().to_pandas()
            rows.extend(table.to_dict("records"))
    field = spec["encoding"]["y"]["field"]
    return [(float(row[field]), row["sign"]) for row in rows]


def check_dealer_greeks_on_screen(AppTest, db) -> list[str]:
    from app import metrics

    seed_dealer_greeks(db)
    failures = []
    calls = []
    real = metrics.dealer_exposures

    def counted(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    def run(metric=None, expiry_index=None):
        app = AppTest.from_file(DASHBOARD, default_timeout=300)
        app.session_state["selected_ticker"] = QA_TICKER
        app.session_state["active_view"] = "Max Pain / GEX"
        if metric is not None:
            app.session_state["exposure_metric"] = metric
        app.run()
        if expiry_index is not None:
            calls.clear()                          # count the run on the chosen expiry only
            app.selectbox(key=f"expiry_pain_gex_{QA_TICKER}").select_index(expiry_index).run()
        assert not app.exception, app.exception[0].value if app.exception else ""
        return app

    metrics.dealer_exposures = counted
    try:
        five_weeks, expiry_day, expired = 2, 1, 0
        want = _qa_expected(db, metrics, QA_EXPIRIES[five_weeks])
        signs = {m: v > 0 for m, v in want.items()}
        assert signs["Vanna"] != signs["Charm"], f"fixture must give vanna and charm opposite signs: {want}"

        # GEX IS THE DEFAULT, AND IT IS THE BLOCK IT WAS: its heading, its net
        # figure in its old colour by sign, its caption — and nothing of the
        # other three, and no call into the dealer exposures.
        app = run(expiry_index=five_weeks)
        headings = [h.value for h in app.subheader]
        box = app.success if want["GEX"] >= 0 else app.warning
        if "Approximate Gamma Exposure (GEX)" not in headings:
            failures.append(f"default: not the GEX heading ({headings})")
        if not any(f"Net GEX: {want['GEX']:,.0f}" in b.value for b in box):
            failures.append(f"default: no 'Net GEX: {want['GEX']:,.0f}' in the box of its sign")
        if any("Net " in i.value for i in app.info):
            failures.append("default: a dealer greek sentence on the GEX block")
        if calls:
            failures.append(f"default GEX called dealer_exposures {len(calls)} time(s); it reads gamma_exposure_profile")
        print(f"  {'GEX default':<26} {'ok' if not failures else 'WRONG'}")

        for metric in ("DEX", "Vanna", "Charm"):
            before = len(failures)
            app = run(metric, five_weeks)
            label = f"{metric} · five weeks"
            if len(calls) != 1:
                failures.append(f"{label}: dealer_exposures called {len(calls)} times per render, want 1")
            lines = [i.value for i in app.info if "Net " in i.value]
            if len(lines) != 1:
                failures.append(f"{label}: {len(lines)} net sentences")
                continue
            shown = _printed_money(lines[0], metric)
            if not _same_printed(shown, want[metric]):
                failures.append(f"{label}: screen says {shown:,.0f}, the expiry's total is {want[metric]:,.2f}")
            words = {"DEX": "open interest leans to calls" if want["DEX"] >= 0 else "open interest leans to puts",
                     "Vanna": "if IV rises, dealer delta grows" if want["Vanna"] >= 0 else "if IV rises, dealer delta shrinks",
                     "Charm": ("dealer delta grows by tomorrow" if want["Charm"] >= 0
                               else "dealer delta shrinks by tomorrow")}[metric]
            if words not in lines[0]:
                failures.append(f"{label}: sign words {words!r} missing from {lines[0]!r}")
            unit_words = {"DEX": "shares", "Vanna": "per 1 IV pt", "Charm": "per day"}[metric]
            if unit_words not in lines[0]:
                failures.append(f"{label}: unit {unit_words!r} missing from {lines[0]!r}")
            if metric == "DEX":
                import re
                held = re.search(r"≈ ([0-9.]+)([KMB]?) shares", lines[0])
                if not held or abs(float(held.group(1)) * _SCALE[held.group(2)] - abs(want["DEX"]) / QA_SPOT) > \
                        0.005 * _SCALE[held.group(2)]:
                    failures.append(f"{label}: shares are not |DEX| / spot in {lines[0]!r}")
            if any("counted to the close" in c.value for c in app.caption):
                failures.append(f"{label}: 'counted to the close' on a five-week expiry")
            bars = _chart_bars(app)
            wrong = [(v, s) for v, s in bars if (s == "Positive") != (v >= 0)]
            if wrong:
                failures.append(f"{label}: bars coloured against their sign: {wrong[:3]}")
            if metric == "Vanna" and {s for _, s in bars} != {"Positive", "Negative"}:
                failures.append(f"{label}: the fixture's bars should carry both signs, got {sorted({s for _, s in bars})}")
            print(f"  {label:<26} {'ok' if len(failures) == before else 'WRONG'}")

        # EXPIRY DAY: charm is counted to the close and says so; the other two
        # keep their units and get no such note.
        day = _qa_expected(db, metrics, QA_EXPIRIES[expiry_day])
        for metric in ("DEX", "Vanna", "Charm"):
            before = len(failures)
            app = run(metric, expiry_day)
            label = f"{metric} · expiry day"
            line = next((i.value for i in app.info if "Net " in i.value), "")
            noted = any("counted to the close" in c.value for c in app.caption)
            if metric == "Charm":
                if "to the close" not in line or "per day" in line or "by the close" not in line:
                    failures.append(f"{label}: sentence not counted to the close: {line!r}")
                if not noted:
                    failures.append(f"{label}: no 'counted to the close' note")
                if abs(day["Charm"]) > day["gross"]:
                    failures.append(f"{label}: |charm| above the chain's whole notional")
            elif noted:
                failures.append(f"{label}: 'counted to the close' note on {metric}")
            if line and not _same_printed(_printed_money(line, metric), day[metric]):
                failures.append(f"{label}: screen {line!r}, total {day[metric]:,.2f}")
            print(f"  {label:<26} {'ok' if len(failures) == before else 'WRONG'}")

        # NO TIME LEFT: said in words, no chart of zeros, no figure.
        for metric in ("DEX", "Vanna", "Charm"):
            before = len(failures)
            app = run(metric, expired)
            label = f"{metric} · expired"
            if not any("no time left" in i.value and "not computed" in i.value for i in app.info):
                failures.append(f"{label}: no 'no time left … not computed' line")
            if any("Net " in i.value for i in app.info) or app.get("vega_lite_chart"):
                failures.append(f"{label}: a figure or a chart for an expiry with no time left")
            print(f"  {label:<26} {'ok' if len(failures) == before else 'WRONG'}")
    finally:
        metrics.dealer_exposures = real
    return failures


def check_am_monthly_charm_on_screen(AppTest, db) -> list[str]:
    """An SPX monthly settles at the 09:30 open, and the view's words follow
    the core's horizon rather than the 16:00 close. Three collections, all
    literals, the latest one read each time: Thursday 09:00 ET (25.5 hours to
    the open — a full day, "per day"), Thursday 13:00 ET (20.5 hours to the
    open, 27 to the close — counted to settlement), Friday 08:00 ET (before the
    open — counted to settlement), Friday 11:00 ET (the monthly has settled;
    what is still listed for the date is the PM weekly, five hours to the
    close — still the default expiry, not "no gamma left"). The ticker has to
    be SPX itself: the settlement rule is keyed by the index, and this
    database is disposable."""
    failures = []
    conn = db.get_connection()
    db.add_ticker(conn, "SPX")
    expiry = "2026-11-20"                                   # third Friday of November
    rows = [{"expiry": listed, "strike": strike, "option_type": option_type,
             "last_price": 20.0, "bid": 19.5, "ask": 20.5, "volume": 100, "open_interest": 1000,
             "implied_volatility": 0.15,
             "in_the_money": strike < 6000 if option_type == "call" else strike > 6000}
            for listed in (expiry, "2026-11-27")
            for strike in (5950.0, 6000.0, 6050.0) for option_type in ("call", "put")]
    cases = (
        (datetime(2026, 11, 19, 14, 0), "per day", False),       # Thursday 09:00 ET
        (datetime(2026, 11, 19, 18, 0), "to the close", True),   # Thursday 13:00 ET
        (datetime(2026, 11, 20, 13, 0), "to the close", True),   # Friday 08:00 ET, before the open
        (datetime(2026, 11, 20, 16, 0), "to the close", True),   # Friday 11:00 ET, after the open
    )
    for moment, words, noted in cases:
        db.insert_snapshot(conn, "SPX", moment, 6000.0, pd.DataFrame(rows))
        conn.commit()
        app = AppTest.from_file(DASHBOARD, default_timeout=300)
        app.session_state["selected_ticker"] = "SPX"
        app.session_state["active_view"] = "Max Pain / GEX"
        app.session_state["exposure_metric"] = "Charm"
        app.run()
        label = f"SPX monthly · {moment:%a %H:%M} UTC"
        if app.exception:
            failures.append(f"{label}: {app.exception[0].value}")
            continue
        box = app.selectbox(key="expiry_pain_gex_SPX")
        chosen = str(box.value)[:10]
        shown = box.format_func(box.value) if box.format_func else str(box.value)
        line = next((i.value for i in app.info if "Net charm" in i.value), "")
        has_note = any("counted to the close" in c.value for c in app.caption)
        before = len(failures)
        if chosen != expiry or "no gamma left" in shown:
            failures.append(f"{label}: default expiry {chosen} shown as {shown!r}, want {expiry} with time left")
        if words not in line:
            failures.append(f"{label}: charm should read {words!r} (settles 09:30 ET), got {line!r}")
        if has_note != noted:
            failures.append(f"{label}: the 'counted to the close' note is {'missing' if noted else 'shown'}")
        print(f"  {label:<26} {'ok' if len(failures) == before else 'WRONG'}")
    conn.close()
    return failures


def main():
    problems = check_no_cross_view_names()
    if problems:
        print("CROSS-VIEW NAME CHECK FAILED:")
        for line in problems:
            print(f"  - {line}")
        raise SystemExit(1)
    print("Cross-view name check passed (no view depends on another's locals)")

    problems = check_every_view_links_its_lesson()
    if problems:
        print("GUIDE LINK CHECK FAILED:")
        for line in problems:
            print(f"  - {line}")
        raise SystemExit(1)
    print("Guide link check passed (every view's first explanation carries its lesson)")

    from streamlit.testing.v1 import AppTest

    # The same throwaway database the other suites use, emptied first: the
    # views are rendered against fixtures written below, and a leftover row
    # from another run would change what is on screen.
    with empty_database():
        # The Contract view fetches daily prices to show realized volatility.
        # AppTest runs the script in this very process, so replacing the
        # function here is enough — and it has to be replaced: the checks in
        # this project are offline by design, and a render that depends on
        # Yahoo being up would fail in CI for reasons that have nothing to do
        # with the change under review.
        # Same shape the real one returns: a single lowercase "close" column
        # indexed by date. Prices wobble rather than rise in a straight line —
        # realized volatility over a perfectly linear series is zero, and a
        # zero would exercise none of the formatting below it.
        # THE READER IS IN PRAGUE, not in UTC. In bare mode `st.context.timezone`
        # is None, the app falls back to UTC, and every conversion becomes the
        # identity — which leaves this harness structurally blind to timezone
        # bugs. It cost the sibling product a day of a missing chart: a lookup
        # key was converted for display, every row lookup missed, and the page
        # rendered its legitimate "no data yet" branch while the gate stayed
        # green, because with UTC the wrong key happened to equal the right one.
        from zoneinfo import ZoneInfo

        from app import collector, db, guide, viewtime
        viewtime.viewer_timezone = lambda: ZoneInfo("Europe/Prague")

        collector.fetch_price_history = lambda ticker, period="6mo": pd.DataFrame(
            {"close": [100.0 + (i % 7) - 3 for i in range(60)]},
            index=pd.date_range("2026-05-01", periods=60, freq="D"),
        )

        seed(db)
        failures = []
        for view in VIEWS:
            app = AppTest.from_file(DASHBOARD, default_timeout=300)
            app.session_state["selected_ticker"] = "RENDERTEST"
            app.session_state["active_view"] = view
            app.run()

            if app.exception:
                detail = (app.exception[0].value or "").splitlines()
                failures.append(f"{view}: {detail[0] if detail else app.exception[0]}")
                print(f"  {view:<18} FAILED")
                continue

            # "Did not raise" is not "rendered": a view whose body was skipped
            # by a mislabelled condition raises nothing at all.
            drawn = sum(
                len(getattr(app, kind))
                for kind in ("markdown", "dataframe", "metric", "caption",
                             "subheader", "info", "warning")
            )
            if drawn < 3:
                failures.append(f"{view}: rendered only {drawn} element(s)")
                print(f"  {view:<18} EMPTY ({drawn})")
                continue

            # Where the view drew its first explanation, the lesson link is the
            # element right before it — that is what places it on the block's
            # label (app/guide.py says why not inside it). The check above
            # already holds every view to having one, including the two whose
            # explanation waits for data this fixture does not have.
            lesson = guide.url(guide.VIEW_LESSONS[view][0])
            links = guide_links(app.main, lesson)
            explained = expanders(app.main) > 0
            if links and not all(links):
                failures.append(f"{view}: the link to {lesson} is not on an explanation block")
                print(f"  {view:<18} GUIDE LINK ADRIFT")
            elif explained and not links:
                failures.append(f"{view}: explained, but no link to {lesson}")
                print(f"  {view:<18} NO GUIDE LINK")
            else:
                print(f"  {view:<18} ok ({drawn} elements{', guide link' if links else ''})")

        # THE METRIC SWITCH ON MAX PAIN / GEX. The pass above draws the view at
        # its default, GEX; each of the other three is a different branch with
        # its own sentence, chart and caption, so each is drawn on its own.
        for metric, title, words in (
            ("DEX", "Net delta of open interest (DEX)", "Net DEX"),
            ("Vanna", "Vanna exposure", "Net vanna"),
            ("Charm", "Charm exposure", "Net charm"),
        ):
            app = AppTest.from_file(DASHBOARD, default_timeout=300)
            app.session_state["selected_ticker"] = "RENDERTEST"
            app.session_state["active_view"] = "Max Pain / GEX"
            app.session_state["exposure_metric"] = metric
            app.run()
            label = f"Max Pain / GEX · {metric}"
            if app.exception:
                detail = (app.exception[0].value or "").splitlines()
                failures.append(f"{label}: {detail[0] if detail else app.exception[0]}")
                print(f"  {label:<24} FAILED")
                continue
            headings = [h.value for h in app.subheader]
            sentences = [i.value for i in app.info]
            charts = len(app.get("vega_lite_chart"))
            if title not in headings:
                failures.append(f"{label}: no {title!r} heading (found {headings})")
            elif not any(words in line for line in sentences):
                failures.append(f"{label}: no {words!r} line (found {sentences})")
            elif charts < 1:
                failures.append(f"{label}: no by-strike chart")
            else:
                print(f"  {label:<24} ok")
                continue
            print(f"  {label:<24} WRONG")

        # The same view, held to its numbers on a fixture of its own.
        failures.extend(check_dealer_greeks_on_screen(AppTest, db))
        failures.extend(check_am_monthly_charm_on_screen(AppTest, db))

    if failures:
        print("\nRENDER CHECKS FAILED:")
        for line in failures:
            print(f"  - {line}")
        raise SystemExit(1)
    print("\nALL VIEWS RENDERED")

if __name__ == "__main__":
    main()
