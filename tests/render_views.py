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
    "Overview", "Max Pain / GEX", "GEX Heatmap", "Volatility (IV)",
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

def main():
    problems = check_no_cross_view_names()
    if problems:
        print("CROSS-VIEW NAME CHECK FAILED:")
        for line in problems:
            print(f"  - {line}")
        raise SystemExit(1)
    print("Cross-view name check passed (no view depends on another's locals)")

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

        from app import collector, db, viewtime
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
            else:
                print(f"  {view:<18} ok ({drawn} elements)")

    if failures:
        print("\nRENDER CHECKS FAILED:")
        for line in failures:
            print(f"  - {line}")
        raise SystemExit(1)
    print("\nALL VIEWS RENDERED")

if __name__ == "__main__":
    main()
