# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- **The hosted version is open**, and the README says so where it can be seen
  rather than at the bottom: [app.gammagrid.io](https://app.gammagrid.io/), free
  while it is in beta. Nothing about self-hosting changes — this repository is
  still the whole product, and the file that computes every number is
  byte-identical in both, with a CI job that fails if it ever stops being.

## [0.5.1] - 2026-08-18

### Fixed
- **"Where the price went" now says which reason refused the split.** The panel
  has two: a contract whose implied volatility did not pass the data-quality
  guard, and a contract too cheap for the arithmetic to mean anything — under
  five cents, every greek is a statement about rounding. Only the first was ever
  reported, so a cheap contract was told it had a data problem it did not have.
  Measured on the sixty most-collected SPY contracts in a real database: 47
  decompose, 13 are refused, and all 13 are refused for being cheap. The message
  now names the price, says how many days it applies to, and points at the
  contracts that will show a decomposition.

### Changed
- **The README says what the history is for.** The first screen promised dealer
  GEX, max pain, open interest and an IV surface — which is what every option
  chain viewer promises. What none of them do is keep the chain: the price
  decomposition, the overnight open-interest move and the gamma profile as it
  stood last Tuesday all exist because every snapshot is stored. That is now the
  second paragraph rather than something you find by reading to the end.
- **Asking for a star, where a star might be earned**: a button at the bottom of
  the sidebar, and a line at the end of the quick start. Deliberately not a
  dialog that waits for a successful collection and then interrupts it.

## [0.5.0] - 2026-08-18

### Added
- **Expired contracts are reachable again.** Their history was always stored, but
  the Contract tab builds its lists from the latest chain — which has no row for
  something that no longer trades — so a contract vanished from the dropdowns on
  expiry day. A "Show expired contracts" toggle brings them back, marked `⏳`,
  with the real range of collected history spelled out. On the database this was
  built against that is 7,098 contracts that could not be opened before.
- **Where the price went**: a per-contract waterfall splitting the price change
  into delta, gamma, vega, theta and the residual, plus a running total by day.
  No entry price is needed and none is asked for — this is a property of the
  contract. The residual is always shown: a decomposition that hides its own
  error cannot be checked.
- **Times are shown in your own timezone**, labelled with it, everywhere an
  instant appears. Aggregation is unaffected — daily metrics stay anchored to the
  New York trading day, so the same data gives every reader the same numbers.

### Changed
- **The collector sleeps through a closed market**, taking one snapshot per
  closed day instead of one per interval. Measured on a year of data: between two
  adjacent weekend snapshots not one contract of 14,230 changed its price, volume
  or open interest, while implied volatility moved on 13,727 — the provider
  recomputing from a frozen price as the clock ticks. Those snapshots also broke
  three views, so this is a correctness fix as much as a saving.
- **Daily metrics count trading days**, in New York rather than UTC. A Saturday
  is a copy of Friday, and counted as a day it made OI Delta compare Friday with
  Friday, and filled two sevenths of the Unusual Activity baseline with
  repetitions.

### Database
- New table `contract_registry`, one row per contract, backfilled from history on
  first start. It replaces a `SELECT DISTINCT` whose cost grows with the number
  of SNAPSHOTS rather than contracts — measured at 152,409 index entries read to
  return 13,449 contracts.

## [0.4.2] - 2026-08-15

### Fixed

- **Two data sources are never shown together.** Every row has recorded which
  provider produced it since the provider interface landed, but nothing on the
  read side looked at it. With the one provider that ships, that was invisible;
  with a second one it was wrong. Implied volatility is *calculated* by a
  provider rather than observed, so two of them legitimately differ on the same
  contract on the same day, and a chart drawn from both shows a move nobody
  traded.

  The sharper failure was in the views that show the latest collection: they
  took the newest timestamp regardless of source and then every row at it, so
  two providers collecting the same minute fed dealer GEX and Max Pain each
  contract twice, and Unusual Activity listed every contract as two rows.

  Every screen now works from one source — the one that collected most
  recently — and says so when a ticker has history from more than one. Nothing
  is deleted: the other provider's rows stay where they are and reappear when
  it is the freshest source again. Choosing between sources by hand is a
  separate feature and waits until somebody actually runs two.

### Added

- `collection_runs.source`, so the list of collection moments every history
  screen stands on can be scoped to one provider. Existing rows read as
  `yahoo`.

## [0.4.1] - 2026-08-15

### Fixed

- **The Volatility chart stops changing under you.** The volume-weighted
  average IV was computed on read, so a point from three months ago was
  re-averaged over a shrinking set of contracts as they expired and moved to
  the archive — the same July point showed one value in August and another in
  October. It is now computed when the chain is collected and stored with it,
  which is what it was.

### Changed

- **No view loads a ticker's whole history any more.** Every screen used to
  work from one frame of every row collected for the selected ticker, loaded on
  each interaction and filtered in pandas by whichever view was showing. That
  was fine while collection was a button — history only grew when you pressed
  it — and stopped being fine the moment v0.4.0 let it run on a schedule.
  Measured in a running container on 102,124 rows: **301 ms** for the old
  whole-history read against **36 ms** for the latest snapshot, 26 ms for a
  chosen moment in Replay, 59 ms for the two days OI Delta compares, and 1 ms
  for the Replay list. The gap widens with every day you collect.

  Each view now asks for what it shows: one snapshot for the screener, max
  pain, the GEX profile and the selectors; the chosen moment for the heatmap;
  two calendar days for OI Delta; one contract for the Contract view.

- **The Replay list comes from the collection log** rather than `SELECT
  DISTINCT` over the snapshots. The collector writes one timestamp per pass
  into both, and the log holds one row per pass against one row per contract
  per pass — Postgres has no loose index scan, so the old query's cost grew
  with how long you had been collecting rather than with the size of the answer.

- **The Unusual Activity baseline is rebuilt once a day by the worker** instead
  of aggregated on every view: it is a mean and deviation over one snapshot per
  day per contract, and the answer does not grow with time even though the
  input does. What is **not** stored is the verdict — the rules deciding what
  counts as unusual stay in the shared calculation core, and the checks assert
  the stored numbers equal what those functions compute over the same rows.

  Nothing here changes what gets flagged.

## [0.4.0] - 2026-08-15

### Added

- **Scheduled collection.** Pick an interval in the sidebar — off, every 15
  minutes, hourly, every 4 hours, once a day — and a small worker container
  keeps collecting while nobody is looking. Off by default, because a tool that
  starts hitting a free API the moment it is installed has made a decision that
  was not its to make. Fifteen minutes is the floor, enforced in code rather
  than only in the list, since the limit belongs to the data source. The
  interval is a setting the worker re-reads every cycle, so changing it takes
  effect without restarting anything.
- **The interval selector states what it costs.** GB per month for *your*
  watchlist, computed from what you have already collected, shown at the moment
  you choose. A background process filling a stranger's disk without ever
  naming a number is how a tool gets uninstalled angrily; the README carries
  the same arithmetic for both a small watchlist and one with an index ETF.
- **Archiving.** Contracts expired more than 30 days ago move to a separate
  table, so the one every chart reads stops carrying contracts that can never
  trade again. Moved, never deleted — collected data is time, and time cannot
  be re-fetched — and every historical view reads both tables.
- `scripts/import_sqlite.py`, to bring a pre-v0.4.0 database across. Databases
  from before v0.3.0 work too: the greeks and `source` columns they lack are
  filled the way that upgrade filled them. The old file is opened read-only.

### Changed

- **Storage is Postgres, not SQLite.** This is what made scheduled collection
  possible at all: a background writer working every few minutes while the
  dashboard reads is the workload a single-writer lock is worst at. `docker
  compose up` is still one command and now starts a database beside the app.
  The costs, stated plainly: a backup is `pg_dump` rather than copying one file,
  running from source means having a Postgres to point `DATABASE_URL` at, and
  existing data needs the import above. See UPGRADING.md.
- **Schema changes go through a migration runner** rather than `CREATE TABLE IF
  NOT EXISTS` on every connection. Two processes now start together, and
  "create everything if missing" run twice at once is a race; an `ALTER` guarded
  by catching the error re-runs forever and reports nothing. The runner keeps a
  ledger, takes an advisory lock so concurrent starts are safe, and refuses to
  start if a migration that was already applied has since been edited.
- **Both snapshot indexes now lead with `(ticker, source)`.** The column has
  existed since v0.3.0 and the indexes had not caught up, so every query
  filtering by source — which is all of them — left that filter to be applied
  after the scan.
- **Expired contracts no longer count toward the ticker's average IV.** A
  contract past its expiry date has no implied volatility — it diverges as time
  to expiry goes to zero — and a source that reports one anyway reports a
  sentinel. Those contracts still carry the whole session's volume, which on an
  index ETF's zero-day expiries is the largest in the chain, so the weighted
  average followed the sentinel rather than the market: measured on the hosted
  sibling at 13.53 against 0.163 once they are excluded, one such spike per day
  with the real series flattened underneath it. The comparison is by date and
  inclusive — a contract expiring today is real trading during today's session.
  `iv_weighted_average` therefore needs `expiry` and `collected_at` columns now;
  every snapshot frame has both.
- **Unusual Activity compares today against completed days only.** The baseline
  used to include an earlier collection of the current day, and volume
  accumulates through a session — so a contract was partly being compared
  against a fraction of itself, which is the same mixing of moments within a
  trading day that the daily collapse already existed to prevent. Fewer
  contracts will be flagged on a day you have collected several times.
- `volume_stats` is now a function of its own, and `unusual_activity` accepts
  its result as an argument. Nothing changes if you do not pass one. It exists
  so that a deployment large enough to care can compute that baseline outside
  pandas, while the rules deciding what counts as unusual stay here, in one
  place, for every deployment.

### Fixed

- **A contract whose data source quotes no implied volatility no longer breaks
  the greeks.** `_black_scholes_greeks` compared `iv <= 0` without first asking
  whether there was an `iv` at all, and a missing one arrives as `None` — which
  raises rather than comparing false. It now returns NaN for every greek when
  an input is missing, which is a different statement from the zeros an expired
  contract returns: expired means no optionality is left, missing means there
  is nothing to compute from, and drawing the second as a flat zero would make
  an absence of data look like a measurement. Yahoo quotes an IV on nearly
  everything, so this was invisible here — but the provider interface added in
  v0.3.0 exists so that other sources can be plugged in, and a source that
  declines to price a far out-of-the-money strike is ordinary. Found in the
  hosted sibling that shares this file, on a strike 36% out of the money whose
  every snapshot carried a price and no volatility.

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

[Unreleased]: https://github.com/gammagrid/gammagrid/compare/v0.5.1...main
[0.5.1]: https://github.com/gammagrid/gammagrid/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/gammagrid/gammagrid/compare/v0.4.2...v0.5.0
[0.4.2]: https://github.com/gammagrid/gammagrid/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/gammagrid/gammagrid/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/gammagrid/gammagrid/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/gammagrid/gammagrid/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/gammagrid/gammagrid/compare/v0.1.3...v0.2.0
[0.1.3]: https://github.com/gammagrid/gammagrid/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/gammagrid/gammagrid/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/gammagrid/gammagrid/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/gammagrid/gammagrid/releases/tag/v0.1.0
