# Roadmap

This is a public, non-binding sketch of where GammaGrid is headed — not a
commitment or a timeline. Priorities shift based on real usage; see
[Get involved](README.md#get-involved) for how to weigh in.

## Shipped (v0.1.0)

- Multi-ticker watchlist with on-demand snapshot collection (Yahoo Finance)
- Put/Call Ratio, Max Pain, approximate dealer GEX (profile + strike × expiry
  heatmap with Call/Put Walls, Gamma Flip, Replay)
- IV surface, skew slice, volume-weighted average IV
- Full-greeks options screener with range filters
- Unusual activity detection, day-over-day OI delta
- Per-contract history with pinning
- Docker quick start, no coding required

## Considering next

- **Scheduled auto-collection.** Collection is a manual button by design for
  v0.1.0 (keeps the self-hosted footprint simple — no background scheduler,
  no extra process to manage). An optional scheduled mode is the most
  requested type of feature for a tool like this — open to it if there's
  real interest.
- **A short product tour GIF** in the README (screenshots are already there).
- **A published Docker image** (`docker run` without cloning the repo first).

## Bigger picture

- **A hosted version** — the same dashboard, no Docker, no local setup,
  running against your watchlist in a browser. The self-hosted, open-source
  version stays free and fully functional either way; hosting just removes
  the "install Docker" step for people who'd rather not. Sign up for updates
  at [gammagrid.io](https://gammagrid.io).
- **A premium data source adapter.** `yfinance` is unofficial and comes with
  the limitations described in the [README](README.md#data-source-limitations)
  (delayed data, occasional gaps, no SLA). A licensed, higher-reliability data
  adapter is a natural addition for anyone who needs it — the free,
  yfinance-based path isn't going away.

## Explicitly not planned

- **Real-time trade-by-trade flow scanning** (sweep/block alerts, tape
  reading). That's a different category of tool and a different data feed;
  see the [README FAQ](README.md#faq) for how GammaGrid's positioning-focused
  approach differs.
- **Trading signals, "AI" calls, or anything positioned as investment
  advice.** GammaGrid computes and displays metrics; what to do with them is
  entirely up to you. See the [disclaimer](README.md#disclaimer).

## Have an idea?

Open an issue or email [hello@gammagrid.io](mailto:hello@gammagrid.io) —
real usage and real requests are what actually shape this list.
