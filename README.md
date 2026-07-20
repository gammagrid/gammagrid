# GammaGrid — Open-Source Options Gamma Exposure (GEX) & Positioning Dashboard

Track **dealer gamma exposure (GEX)**, **max pain**, **open interest**, and the
**IV surface** for your whole options watchlist — not just SPY. Self-hosted,
open source, built on free market data.

[![License: AGPL v3](https://img.shields.io/badge/license-AGPL--3.0-B833E0.svg)](LICENSE)
[![Runs on Docker](https://img.shields.io/badge/runs%20on-Docker-22C55E.svg)](#quick-start-no-coding-required)

> **No coding required.** If you can install an app and copy-paste one command
> into a terminal, you can run GammaGrid. No Python, no config files, no
> programming experience needed — see [Quick start](#quick-start-no-coding-required) below.

## Quick start (no coding required)

1. **Install Docker Desktop** — [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop/).
   It's free; just click through the installer like any other app.
2. **Download this project** — click the green **Code** button at the top of
   this page → **Download ZIP**, then unzip it. (Comfortable with git instead?
   `git clone` this repo.)
3. **Open a terminal in the unzipped folder** — on Mac: right-click the folder
   → *New Terminal at Folder*. On Windows: open the folder in File Explorer,
   type `cmd` in the address bar, press Enter.
4. **Run one command:**
   ```bash
   docker compose up
   ```
   (Older Docker installs: use `docker-compose up` instead — same effect.)
5. **Open [http://localhost:8501](http://localhost:8501) in your browser.**
   That's it — GammaGrid is running.

Data is saved to `data/options.db` on your machine and survives restarts.
Press `Ctrl+C` in the terminal to stop the app; run the same command again to
bring it back up with your data intact.

## What you get

- **Dealer gamma exposure (GEX)** — per-expiry profile and a strike × expiry
  heatmap, with Call Wall / Put Wall, Gamma Flip level, and historical Replay
- **Max Pain** for any expiry
- **Open interest**, including day-over-day OI Delta sorted by the size of the move
- **IV surface** (3D volatility surface) plus per-expiry skew and
  volume-weighted average IV over time
- **Options screener** with the full set of greeks (delta, gamma, theta, vega,
  rho, vanna, charm) and range filters — not just delta/IV like most free tools
- **Unusual activity** detection — flags contracts whose volume is a
  statistical outlier against that specific contract's own history, not a
  flat threshold
- **Put/Call Ratio** and per-contract price/IV/greeks history with pinning
- Works for any ticker with a listed options chain — build your own watchlist,
  not a single fixed symbol

## Usage

1. In the left sidebar, enter a ticker (e.g. `AAPL`) and click **Add** — it
   appears in the watchlist.
2. Click **Collect data** — the app fetches the current option chain for every
   watchlist ticker via Yahoo Finance and saves a snapshot. Collection is
   manual; there is no automatic schedule.
3. Pick a ticker in the dropdown above the tabs to open the metrics:
   - **Overview** — Put/Call Ratio over time and the IV surface
   - **Max Pain / GEX** — max pain and the approximate gamma-exposure profile for a selected expiry
   - **GEX Heatmap** — strike × expiry GEX matrix with Call/Put Walls, Gamma Flip, and snapshot Replay
   - **Volatility (IV)** — ticker-average IV over time and a chain skew slice
   - **Contract** — price, IV, and greeks history for a specific contract, with pinning
   - **Screener** — every contract of the latest snapshot with greeks and range filters
   - **Unusual Activity** — contracts with anomalous volume in the latest snapshot
   - **OI Delta** — open interest change between the two latest calendar days

Most history-based metrics (other than Put/Call Ratio, average IV, and OI
Delta) need several days of collection — some charts require at least two
snapshots. Click **Collect data** daily (or a few times a day) to build up history.

> Screenshots are on the way — for now, the fastest way to see it is to run
> the Quick start above; it takes about two minutes.

## FAQ

**What is dealer gamma exposure (GEX)?** It's an estimate of how much options
market makers are net long or short gamma across a ticker's option chain.
Positive GEX suggests dealer hedging tends to dampen price moves; negative GEX
suggests it can amplify them. GammaGrid computes this via Black-Scholes as an
approximation from options open interest — it is **not** a measure of actual
market-maker positions, which aren't public data (see the disclaimer below).

**Is this a real-time options flow scanner?** No — GammaGrid takes periodic
snapshots of the option chain (on-demand, via the **Collect data** button), it
does not stream live trade-by-trade tape. If you need tick-by-tick sweep/block
alerts, that's a different category of tool. GammaGrid is for tracking
positioning and structure (GEX, max pain, OI, IV) across a watchlist over time.

## For developers: running from source

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
PYTHONPATH=. streamlit run app/dashboard.py
```

`PYTHONPATH=.` is required: Streamlit adds the script's own directory (`app/`)
to `sys.path`, not the project root, and without it the app fails with
`ModuleNotFoundError: No module named 'app'` — the imports in `dashboard.py`
(`from app import ...`) expect the project root to be visible in `sys.path`.
In Docker the same thing is handled by `ENV PYTHONPATH=/app` in the
`Dockerfile`.

## Data source limitations

`yfinance` is an unofficial wrapper around Yahoo Finance, with no SLA or
official support. Expect possible data delays (15–20 minutes), irregular
intraday open-interest updates, and temporary blocks under frequent requests.
The app logs collection failures (visible on the dashboard after clicking
**Collect data**) but makes no attempt to circumvent blocks.

## Want it hosted, with zero setup?

A hosted version of GammaGrid (no Docker, no local install) is planned. Join
the list at **[gammagrid.io](https://gammagrid.io)** to hear when it's ready.

## Get involved

Questions, feedback, or found a bug? Email
[hello@gammagrid.io](mailto:hello@gammagrid.io) or open an issue. If
GammaGrid is useful to you, starring the repo genuinely helps — it's the main
signal used to decide what gets built next.

## License

[AGPL-3.0](LICENSE).

## Disclaimer

This software is for informational and educational purposes only and does not
constitute investment advice. All metrics are approximations built on delayed,
unofficial data.
