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

# Smoke test (offline, synthetic data — no network calls)
python tests/smoke_test.py

# Docker build
docker build -t gammagrid .
```

All three run in CI on every pull request; please run them locally first.

## Code style

- Match the existing style: pure functions in `metrics.py` (input a DataFrame,
  output a DataFrame/number, no side effects), all DB access funneled through
  `db.py`, all network calls funneled through `collector.py`. `dashboard.py`
  is display and user input only — no business logic.
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
- Make sure `ruff check .` and `python tests/smoke_test.py` pass before opening the PR.

## Questions

Email [hello@gammagrid.io](mailto:hello@gammagrid.io) or open an issue if
you're unsure where to start.
