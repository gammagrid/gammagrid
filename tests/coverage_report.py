"""Runs the test scripts and reports which application functions they exercised.

Deliberately not coverage.py: this measures FUNCTIONAL coverage — "does any
check call this behaviour at all" — rather than lines or branches. That is the
question worth asking here, it adds no dependency, and the answer is
actionable ("these three functions are untested") instead of a percentage of
lines nobody can act on.

Exits non-zero if a script failed or coverage is below COVERAGE_FLOOR.

Usage: python tests/coverage_report.py
"""

import ast
import importlib
import io
import os
import pathlib
import sys
import tempfile
import time

# Set before the suites are imported, because config.DB_PATH is read at import
# time: whichever module imports app.config first fixes the path for the whole
# process. One throwaway database, chosen here, for every suite in this run.
os.environ.setdefault(
    "OPTIONS_TRACKER_DB",
    os.path.join(tempfile.mkdtemp(prefix="gammagrid-coverage-"), "checks.db"),
)

REPO = pathlib.Path(__file__).resolve().parent.parent

# The modules whose behaviour the checks are expected to reach. dashboard.py is
# excluded on purpose: it is a Streamlit script, not a library — importing it
# runs the page — and it is verified by rendering instead (render_views.py).
MEASURED_MODULES = ("app/db.py", "app/metrics.py", "app/collector.py")

SUITES = ("tests.unit_tests", "tests.smoke_test")

# Functions that cannot run in an offline check, each with its reason. Listed
# by name rather than tolerated silently: an exemption should be an argument
# you can read, and this list is the argument.
EXEMPT = {
    "collector.fetch_ticker_snapshot": "network: yfinance",
    "collector._fetch_chain_for_expiry": "network: yfinance",
    "collector._fetch_underlying_price": "network: yfinance",
    "collector.fetch_price_history": "network: yfinance",
    "collector._with_retry": "retries a network call; only reachable through the ones above",
    "db.get_connection": "used by every check; not a behaviour to assert",
}

COVERAGE_FLOOR = 100.0  # percent, excluding EXEMPT


def declared_functions() -> dict[str, str]:
    """{qualified name: file:line} for every def in the measured modules."""
    found = {}
    for relative in MEASURED_MODULES:
        path = REPO / relative
        module = path.stem
        tree = ast.parse(path.read_text())

        def walk(node, prefix=""):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.ClassDef):
                    walk(child, f"{prefix}{child.name}.")
                elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # Nested helpers are part of their parent's behaviour, not
                    # separately callable units — don't inflate the denominator.
                    found[f"{module}.{prefix}{child.name}"] = f"{relative}:{child.lineno}"

        walk(tree)
    return found


def run_suites_with_tracing():
    """Executes every suite in-process under a profiler, so we see what ran.
    A subprocess would isolate better but would show us nothing."""
    called = set()
    measured_files = {str((REPO / relative).resolve()) for relative in MEASURED_MODULES}
    # co_filename is whatever path the import used, and the suites put
    # `tests/..` on sys.path — so modules arrive as ".../tests/../app/db.py"
    # and never match a normalized path. Resolving per call would be far too
    # expensive, hence the cache.
    resolved = {}

    def module_of(filename):
        if filename not in resolved:
            try:
                full = str(pathlib.Path(filename).resolve())
            except OSError:
                full = filename
            resolved[filename] = pathlib.Path(full).stem if full in measured_files else None
        return resolved[filename]

    def profile(frame, event, _arg):
        if event != "call":
            return
        code = frame.f_code
        module = module_of(code.co_filename)
        if module is None:
            return
        name = code.co_qualname if hasattr(code, "co_qualname") else code.co_name
        called.add(f"{module}.{name}")

    sys.path.insert(0, str(REPO))
    captured = io.StringIO()
    stdout, stderr = sys.stdout, sys.stderr
    started = time.perf_counter()
    results = {}
    try:
        sys.stdout = sys.stderr = captured
        sys.setprofile(profile)
        try:
            for suite in SUITES:
                # The import is inside the try: a suite that fails to even
                # import must be reported as that suite failing, not as the
                # harness dying, or the output names no culprit.
                try:
                    module = importlib.import_module(suite)
                    module.main()
                    results[suite] = True
                except BaseException as exc:  # noqa: BLE001 — report every suite
                    results[suite] = False
                    captured.write(f"\n[{suite}] {type(exc).__name__}: {exc}\n")
        finally:
            sys.setprofile(None)
    finally:
        sys.stdout, sys.stderr = stdout, stderr
    return results, captured.getvalue(), called, time.perf_counter() - started


def matches(declared, called):
    """A declared `module.Class.method` counts as covered under any shape the
    tracer can report it (qualname, or bare name on older Pythons)."""
    if declared in called:
        return True
    module, _, rest = declared.partition(".")
    return f"{module}.{rest.rsplit('.', 1)[-1]}" in called


def main() -> int:
    results, output, called, seconds = run_suites_with_tracing()
    declared = declared_functions()
    exempt = {name: why for name, why in EXEMPT.items() if name in declared}
    measurable = {name: where for name, where in declared.items() if name not in exempt}
    covered = {name for name in measurable if matches(name, called)}
    missing = sorted(set(measurable) - covered)
    percent = 100.0 * len(covered) / len(measurable) if measurable else 100.0

    summary = " ".join(
        f"{suite.split('.')[-1]}={'ok' if ok else 'FAIL'}" for suite, ok in results.items()
    )
    print(f"{summary}  {seconds:.1f}s  coverage: {percent:.1f}% "
          f"({len(covered)}/{len(measurable)} functions, {len(exempt)} exempt)")

    if not all(results.values()) or not results:
        print(output[-3000:], file=sys.stderr)
        return 1
    if missing:
        print("\nNo check exercises:", file=sys.stderr)
        for name in missing:
            print(f"  {name}  ({measurable[name]})", file=sys.stderr)
    if percent < COVERAGE_FLOOR:
        print(f"\nFunctional coverage {percent:.1f}% is below the required "
              f"{COVERAGE_FLOOR:.0f}%.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
