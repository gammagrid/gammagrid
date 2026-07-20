# GammaGrid

The open-source options positioning dashboard — dealer gamma exposure, max
pain, open interest, and IV surface for your whole watchlist, not just SPY.

A local dashboard for on-demand daily collection of option-chain data for a
watchlist of tickers, computing positioning analytics: Put/Call Ratio, Max
Pain, approximate GEX (per-expiry profile and strike × expiry heatmap with
Call/Put Walls, Gamma Flip, and Replay), unusual activity, IV (current slice,
volume-weighted history, IV surface, per-contract drill-down with a full set
of greeks), an options screener with range filters, and day-over-day OI delta.

> **Note:** this README is a minimal placeholder — a full rewrite with
> screenshots is planned before the public v0.1.0 release.

## Requirements

- Docker and Docker Compose

## Running

```bash
docker-compose up
```

The dashboard will be available at [http://localhost:8501](http://localhost:8501).

Data is stored in the SQLite file `data/options.db` on the host (a volume) —
it survives container restarts and rebuilds.

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
snapshots.

## Running locally without Docker (for development)

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

## License

[AGPL-3.0](LICENSE).

## Disclaimer

This software is for informational and educational purposes only and does not
constitute investment advice. All metrics are approximations built on delayed,
unofficial data.
