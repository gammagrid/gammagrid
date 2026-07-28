# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
