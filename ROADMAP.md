# Roadmap

This is a public, non-binding sketch of where GammaGrid is headed — not a
commitment and not a timeline. Priorities shift based on real usage; see
[Get involved](README.md#get-involved) for how to weigh in.

For what actually changed and when, read [CHANGELOG.md](CHANGELOG.md). This
page is about direction.

## Shipped so far

Through v0.5.1. The list below is what the tool does today, not a history —
the changelog has the history.

- Multi-ticker watchlist, collected on demand or on a schedule you choose —
  and not while the market is shut, where the chain does not change
- Put/Call Ratio, Max Pain, approximate dealer GEX (profile + strike × expiry
  heatmap with Call/Put Walls, Gamma Flip, Replay)
- IV surface, skew slice, volume-weighted average IV
- Full-greeks options screener with range filters
- Unusual activity detection, day-over-day OI delta
- Per-contract history with pinning, including contracts that have expired
- Per-contract price attribution: what delta, gamma, vega and theta each did
- A data source is a plug-in: the interface is small and documented, and Yahoo
  is simply the one that ships
- Docker quick start, no coding required

## Being considered

Deliberately vague, because these are directions rather than plans. Anything
here may move, arrive in a different shape, or turn out not to be worth it.

- **More data sources.** The provider interface exists precisely so that
  adding one is a small, self-contained piece of work. Which sources make
  sense depends on what people actually run into with the free one.
- **Continued performance work.** Collecting on a schedule means the database
  grows while you sleep, and the interesting screens are the ones that read
  history. This has had one round of attention already and will get more as
  people's collections get older than the code's assumptions.
- **Fewer steps to get started** — a published image so that trying it does
  not begin with cloning a repository, and a shorter path for people who do
  not want to think about Docker at all.
- **Whatever comes up often enough.** The list above is a guess; the section
  below is how it gets corrected.

## Bigger picture

- **A hosted version** — the same dashboard, no Docker, no local setup,
  running against your watchlist in a browser. The self-hosted, open-source
  version stays free and fully functional either way; hosting just removes
  the "install Docker" step for people who would rather not. Sign up for
  updates at [gammagrid.io](https://gammagrid.io).
- **A premium data source adapter.** `yfinance` is unofficial and comes with
  the limitations described in the [README](README.md#data-source-limitations)
  (delayed data, occasional gaps, no SLA). A licensed, higher-reliability data
  adapter is a natural addition for anyone who needs it — the free,
  yfinance-based path is not going away.

## Explicitly not planned

- **Real-time trade-by-trade flow scanning** (sweep/block alerts, tape
  reading). That is a different category of tool and a different data feed;
  see the [README FAQ](README.md#faq) for how GammaGrid's positioning-focused
  approach differs.
- **Trading signals, "AI" calls, or anything positioned as investment
  advice.** GammaGrid computes and displays metrics; what to do with them is
  entirely up to you. See the [disclaimer](README.md#disclaimer).

## Have an idea?

This is the part that matters most, and it is not a formality: the largest
feature in the last release — collecting on a schedule — was on this page for
months as "open to it if there's real interest", and it moved because people
asked.

Open an issue or email [hello@gammagrid.io](mailto:hello@gammagrid.io). A
description of what you were trying to do when the tool got in your way is
worth more than a feature name.
