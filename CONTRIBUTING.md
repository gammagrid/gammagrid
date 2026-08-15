# Contributing to GammaGrid

Thanks for considering a contribution. GammaGrid intentionally stays small and
readable — plain functions over SQLite, no ORM, no dependency injection
framework, no premature abstraction. Please keep that spirit in any change.

## Development setup

```bash
git clone https://github.com/gammagrid/gammagrid.git
cd gammagrid
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
PYTHONPATH=. streamlit run app/dashboard.py
```

See the [README](README.md#for-developers-running-from-source) for why
`PYTHONPATH=.` is required.

## Running the checks

```bash
# Lint
pip install ruff
ruff check .

# Unit checks (is the number right) and smoke checks (does the SQLite
# plumbing work), plus functional coverage — this is what CI runs
python tests/coverage_report.py

# Either script on its own, when you want just that one
python tests/unit_tests.py
python tests/smoke_test.py

# Render every dashboard view on a throwaway database (also offline)
python tests/render_views.py

# Docker build
docker build -t gammagrid .
```

All of them run in CI on every pull request; please run them locally first.

## Code style

- **`app/metrics_core.py` is a shared file — please read its header before
  changing it.** It is byte-identical to a copy in the hosted version of
  GammaGrid, and the checks will fail if you edit it without updating
  `app/metrics_core.sha256`. That is not bureaucracy: the two drifted apart
  once, and the free version spent weeks computing greeks without a dividend
  yield while the paid one did not — a wrong number, in the version more people
  use. A pull request that changes the core is very welcome; it just also has
  to be carried across, and the failing check is what makes sure someone does
  it. Anything that needs data this application cannot fetch does not belong in
  the core at all.
- **A new function in `db.py`, `metrics.py`, `metrics_core.py`,
  `collector.py` or `providers/` arrives with a check.**
  `tests/coverage_report.py` fails if any non-exempt function has none calling
  it — write it in `unit_tests.py` if it needs no database, in
  `smoke_test.py` if it does. Something that genuinely cannot be checked
  offline goes in that file's `EXEMPT` list, by name and with the reason.
- Match the existing style: pure functions in `metrics_core.py` (input a
  DataFrame, output a DataFrame/number, no side effects), all DB access
  funneled through `db.py`, all network calls funneled through
  `app/providers/` — `collector.py` orchestrates and never talks to a network
  itself. `dashboard.py` is display and user input only — no business logic.
- **Adding a data source is a new file in `app/providers/`** and two lines in
  its `__init__.py`; the module docstring there walks through it. Nothing else
  in the app should need to know your source exists. Two of them are never
  shown together: every screen works from whichever source collected most
  recently for that ticker, and a read that does not scope itself to one source
  is a defect — see `db.active_source`.
- **A change to the database schema arrives with an `UPGRADING.md` entry.**
  Not a changelog line — a statement of what happens to a database that is
  already full, and whether going back is possible. People run this on their own
  machine, often with no backup, and they upgrade by `git pull`; the changelog
  tells them what is new, and only that file tells them what it costs. Saying
  "nothing happens to your data" counts and is worth writing, because silence
  reads as the same thing without anyone having checked.
- Comment the *why*, not the *what* — a comment should explain a non-obvious
  constraint or a workaround, not restate what the code already says.
- Keep the "ℹ️ How to read this" explanations under each chart intact and
  accurate if you touch the metric they describe — they're one of the
  product's main value points, not throwaway copy.

## Reporting bugs / requesting features

Use the GitHub issue templates. For security issues, see
[SECURITY.md](SECURITY.md) instead of opening a public issue.

## Pull requests

- Keep PRs focused — one change per PR is easier to review than a bundle.
- Update `CHANGELOG.md` under `[Unreleased]` for any user-facing change.
- Make sure `ruff check .` and all three test scripts pass before opening the PR.
- Touching `dashboard.py`? `tests/render_views.py` is the one that matters: only
  the view you are looking at renders, so a name defined in one view and read in
  another is a `NameError` on exactly the view nobody opened.

## Questions

Email [hello@gammagrid.io](mailto:hello@gammagrid.io) or open an issue if
you're unsure where to start.
