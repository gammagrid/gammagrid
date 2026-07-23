# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/gammagrid/gammagrid/compare/v0.1.1...main
[0.1.1]: https://github.com/gammagrid/gammagrid/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/gammagrid/gammagrid/releases/tag/v0.1.0
