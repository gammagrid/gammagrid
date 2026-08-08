# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

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

[Unreleased]: https://github.com/gammagrid/gammagrid/compare/v0.1.3...main
[0.1.3]: https://github.com/gammagrid/gammagrid/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/gammagrid/gammagrid/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/gammagrid/gammagrid/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/gammagrid/gammagrid/releases/tag/v0.1.0
