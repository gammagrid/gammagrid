# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.3.0] - 2026-08-12

### Added

- **A shared calculation core.** `app/metrics_core.py` now holds every metric
  calculation and is byte-identical to the copy in the hosted version, with the
  rules written at the top of the file. `app/metrics.py` re-exports it, so
  nothing that calls a metric had to change. The reason is a real defect rather
  than tidiness: the two versions had drifted, and this one was computing greeks
  with no dividend yield — measured on the other side at 11% off on a high-yield
  name's forward and 0.05 of delta on long-dated contracts. Carrying fixes
  across by hand is what failed; a shared file with a checked hash is the
  replacement.
- **Greeks now take a dividend yield.** `_black_scholes_greeks` uses the
  generalized Black-Scholes formulas, and charm is computed per option type
  rather than shared between calls and puts — sharing it is only correct when
  the yield is zero. **No number in this release changed**: with no yield
  supplied the formulas reduce exactly to the previous ones, and a check
  asserts that over 432 combinations of spot, strike, maturity, volatility and
  rate, against the previous implementation itself rather than pasted
  constants.
- **A provider interface, so a data source is a plug-in.** `app/providers/`
  defines what a source has to do; `collector.py` no longer imports `yfinance`
  and works over whatever it is handed. Adding your own source is a new file
  there and two lines in the registry — the module docstring walks through it.
  Yahoo remains the default and still needs no account, no token and no card.
- Four provider-greek columns and a `source` column in `option_snapshots`.
  Existing databases upgrade in place on first open, with no manual step;
  existing rows are labelled `yahoo`, which is what they are. The label has to
  exist before a second source does — two providers derive implied volatility
  differently, and a chart mixing them draws a move that never happened, which
  cannot be untangled afterwards if the rows were never labelled.

- `UPGRADING.md`, new: what each release does to a database you have already
  filled, and whether it can be undone. The changelog says what changed; that
  file says what it costs, which is the question that decides whether to
  upgrade today. This release's entry is **one-way** — safe, but keep a copy of
  `data/options.db` until you have seen a chart.

### Fixed

- One contract with no implied volatility no longer empties the whole day's
  volume-weighted average IV. `np.average` computes `sum(a·w)/sum(w)` and
  `NaN·0` is still `NaN`, so a single zero-weighted row with a missing IV
  poisoned the result. Invisible while Yahoo was the only source, because it
  quotes an IV on everything; a provider that legitimately reports none for
  part of a chain empties the chart completely. Fixed now rather than later
  because the provider interface in this release is exactly what makes that
  possible here.

## [0.2.0] - 2026-08-08

### Added

- Two more check scripts, and CI runs both. `tests/unit_tests.py` covers the
  pure functions — is the number right — without needing a database;
  `tests/render_views.py` draws every dashboard view on a throwaway database and
  also reads the source to catch a name defined in one view and used in another,
  which is a failure only that view would show. `tests/coverage_report.py` runs
  the unit and smoke scripts together and fails if any function in `db.py`,
  `metrics.py` or `collector.py` has no check calling it at all — network calls
  are exempt by name, with the reason written next to them. All of it is offline:
  nothing in CI depends on Yahoo being reachable.

### Changed

- The eight sections are now a view selector rather than tabs, and only the one
  you are looking at is computed. Streamlit has no lazy tabs: the body of every
  tab ran on every interaction and the browser simply hid the other seven, so
  each click paid for eight views to show one. Measured on a realistically-shaped
  chain (3,520 contracts per collection, 211,200 stored rows), a page render went
  from 1.68s to 0.63s — and tripling the stored rows had barely moved the old
  number, which is what says the cost was the tabs rather than the data. The
  trade, stated plainly: switching between sections used to be instant because
  everything was already in the browser, and now costs one render of that one
  section.
- A page load now reads the last 365 days of a ticker's history instead of all of
  it, with a "Load full history" toggle that fetches everything on request. The
  bound is a default, never a cap: collected data is the most valuable thing in the
  app and a rolling window must not silently hide a long-lived contract's early
  history. Collection is a manual button today, so history only grows when you press
  it — but an unbounded read is what would turn scheduled collection, when it
  arrives, into a complaint about the app being slow.
- The Put/Call Ratio chart is aggregated in SQL rather than by loading every raw row
  and grouping in pandas — the same numbers (asserted against the previous
  implementation to 1e-9) for a fraction of the work.

### Fixed

- Clicking a pinned contract now actually selects it. The handler wrote to the
  selectors' old, unsuffixed session keys after those selectors were keyed by
  ticker, so the click silently did nothing.
- The Contract tab no longer offers a call/put side that was never collected.
  Far-OTM strikes are routinely quoted on one side only, and the type selector
  offered both regardless — picking the missing one gave "No history for the
  selected contract", which was true and useless. It now lists only the sides
  present for the chosen expiry and strike, and a remembered selection that
  disappears from a narrowed list is forgotten rather than raising.
- The screener hides contracts that have expired since the snapshot was
  collected, with a checkbox to include them and a count of how many there are.
  Their greeks and DTE are correct as of the collection — that is the only
  honest way to price them — but with collection on a manual button the latest
  snapshot can be days old, and listing a dead contract with a positive DTE in a
  "what can I trade" view is misleading. On an 11-day-old snapshot this is 826 of
  5,546 contracts for GLD and 2,205 of 10,526 for SPY.
- Switching tickers no longer leaves charts blank. Expiry, strike and option-type
  selectors kept their value across a ticker change, so a date the previous ticker
  traded carried over to one that doesn't — Max Pain, the GEX profile and the IV
  chain slice then filtered to nothing and looked broken while the data was fine.
  Touching the selector "fixed" it, which is what made this look like a rendering
  glitch rather than a wrong value.
- The Max Pain / GEX tab now defaults to the nearest expiry that still has time
  left. Most tickers list an expiry for the current day and it sorts first, so the
  default landed on the one expiry whose gamma is zero by definition: an empty
  chart under a "Net GEX: 0" banner, with Max Pain right above it showing a normal
  number because it needs only open interest. Expiries with no time left stay
  selectable, labelled "no gamma left", and explain themselves instead of drawing a
  flat zero. The GEX Heatmap drops them from the matrix — a heatmap has nowhere to
  explain a blank column.
- The GEX Heatmap no longer crashes for a ticker with a single tradeable expiry
  (`st.slider` requires min < max). Found by rendering the page for a thin ticker.

- Time to expiry is now measured to the 16:00 ET close instead of to midnight of
  the expiration date, and keeps its fractional part. Two errors compounded in the
  old `(expiry - collected_at).days / 365`: a contract still trading through its
  final session already counted as expired, so every greek collapsed to zero from
  midnight onward; and truncating to whole days understated the remaining life by
  up to 24 hours. Both are negligible on long-dated contracts and dominant on the
  near-dated ones where gamma is largest — measured on a real snapshot two days
  before expiry, 2.00 days by the old formula against 2.93 actual, which overstated
  net GEX by roughly 15% (6% at four days). Affects the screener's greeks, the GEX
  profile and heatmap, the Contract tab's greek history, and the IV surface's
  maturity axis. Daylight saving is handled properly: the same expiry date closes at
  20:00 UTC in summer and 21:00 UTC in winter.

## [0.1.3] - 2026-07-29

### Added

- Data-quality guard on the Contract tab: `implied_volatility` that's a strong outlier
  vs. a contract's own history and isn't corroborated by a matching move in
  `last_price` is treated as unreliable for that one snapshot — the chart shows a gap
  instead of a spike or dip. Deliberately provider- and magnitude-agnostic: a genuine
  large real move always shows a matching price move too, so this never fires on real
  volatility. (An earlier version compared to an absolute Black-Scholes price instead;
  reverted the same day after it broke on real long-dated, high-dividend-yield names —
  see commit history.) Also fails open when suppression would cover more than 30% of a
  contract's history: found live on this repo's own shorter local dataset, where a
  near-even split between an old glitch value and the current one made the median
  itself unreliable and blanked an entire contract's greeks instead of just the glitch.

## [0.1.2] - 2026-07-24

### Fixed

- `StreamlitColorLengthError` on the Volatility (IV) tab's chain-slice chart: it
  crashed whenever the latest snapshot for a given expiry had only calls or only
  puts (a fixed 2-color list no longer matched the actual column count). Colors
  are now built from the chart's real columns, and an empty slice shows an info
  message instead of reaching the chart at all.

## [0.1.1] - 2026-07-23

### Changed

- Dashboard visuals aligned with the GammaGrid brand: grid background, mono display
  font on headings/tabs, purple/green chart palette, and a sign-colored GEX-by-strike
  bar chart (Altair). The IV surface deliberately keeps Viridis — a perceptually-uniform
  scale reads better on a continuous 3-D surface.

## [0.1.0] - 2026-07-20

Initial public release — see the [Roadmap](ROADMAP.md) for what's next.

### Added

- Multi-ticker options watchlist with on-demand snapshot collection via Yahoo Finance (`yfinance`)
- Put/Call Ratio over time
- Max Pain per expiry
- Approximate dealer Gamma Exposure (GEX): per-expiry profile and a strike × expiry
  heatmap with Call Wall / Put Wall, Gamma Flip level, and historical Replay
- Implied volatility surface, per-expiry skew slice, and volume-weighted average IV over time
- Options screener with the full set of greeks (delta, gamma, theta, vega, rho, vanna, charm) and range filters
- Unusual activity detection based on a per-contract volume z-score, with a fallback for thin history
- Day-over-day open interest delta, sorted by move size
- Per-contract price/IV/greeks history with pinning
- Collection log with data-quality diagnostics (`oi_zero_fraction`) surfaced in the UI
- Docker / Docker Compose quick start; GammaGrid brand theme (`.streamlit/config.toml`)
- AGPL-3.0 license

[Unreleased]: https://github.com/gammagrid/gammagrid/compare/v0.2.0...main
[0.2.0]: https://github.com/gammagrid/gammagrid/compare/v0.1.3...v0.2.0
[0.1.3]: https://github.com/gammagrid/gammagrid/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/gammagrid/gammagrid/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/gammagrid/gammagrid/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/gammagrid/gammagrid/releases/tag/v0.1.0
