# GammaGrid — Open-Source Options Gamma Exposure (GEX) & Positioning Dashboard

Track **dealer gamma exposure (GEX)**, **max pain**, **open interest**, and the
**IV surface** for your whole options watchlist — not just SPY. Self-hosted,
open source, built on free market data.

**And it keeps what it collects.** Most free options tools draw the chain as it
is this second and forget it a second later. GammaGrid stores every snapshot it
takes, which is what lets it answer the questions the live chain cannot: how
this contract's price split into delta, gamma, vega and theta, day by day; where
open interest actually moved overnight; what the gamma profile looked like at
10:15 last Tuesday. The live numbers are the part everybody has. The history is
the product.

**US-listed options.** European and Asian listings have no options data through
the free data source — most large non-US companies also trade in the US as ADRs,
which do (`ASML`, `SAP`, `SHEL`, `NVO`, `BUD`). See the [FAQ](#faq).

[![License: AGPL v3](https://img.shields.io/badge/license-AGPL--3.0-B833E0.svg)](LICENSE)
[![Runs on Docker](https://img.shields.io/badge/runs%20on-Docker-22C55E.svg)](#quick-start-no-coding-required)
[![Hosted version: open, free in beta](https://img.shields.io/badge/hosted-open%2C%20free%20in%20beta-22C55E.svg)](https://app.gammagrid.io/)

**Don't want to run anything? It is hosted too, and open now** —
**[app.gammagrid.io](https://app.gammagrid.io/)**. Sign in with Google or GitHub,
add your tickers, and the collecting happens on our side. Free while it is in
beta, no card. Self-hosting stays free and open source either way, and both run
the same code: the file that computes the numbers is byte-identical in the two
products, and CI fails if that ever stops being true.

> **No coding required.** If you can install an app and copy-paste one command
> into a terminal, you can run GammaGrid. No Python, no config files, no
> programming experience needed — see [Quick start](#quick-start-no-coding-required) below.

![GEX Heatmap: strike × expiry gamma exposure matrix, with Call Wall, Put Wall, Gamma Flip, and Replay](docs/img/gex-heatmap.png)

*Dealer gamma by strike and expiry, with the Call Wall, Put Wall and Gamma Flip
marked. Replay steps the same grid back through every moment collected.*

## Quick start (no coding required)

1. **Install Docker Desktop** — [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop/).
   It's free; just click through the installer like any other app. **Then start
   it and wait until it says *Engine running*** (the whale icon turns green).
   The first launch takes a minute or two and may install a Windows component
   (WSL 2) or ask you to reboot — let it finish. Everything below talks to
   Docker, so none of it works until Docker itself is up.
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

If it worked, **[star the repo](https://github.com/gammagrid/gammagrid)** — it
takes a second, and stars are how anyone else finds this project. There is no
marketing budget behind it.

Your data lives in a Postgres database that `docker compose` starts alongside
the app and keeps in a Docker volume, so it survives restarts. Press `Ctrl+C` to
stop; run the same command again to bring everything back with your data intact.
`docker compose down` also keeps it — only `down -v` deletes it, and that
removes every snapshot you have collected.

Back it up with:

```bash
docker compose exec postgres pg_dump -U gammagrid gammagrid | gzip > gammagrid-backup.sql.gz
```

**Coming from a version before v0.4.0?** Your history is in `data/options.db`
and the app no longer reads that file. `scripts/import_sqlite.py` copies it
across in one command — see [UPGRADING.md](UPGRADING.md), which says per release
what happens to the database you have already filled.

## If something goes wrong

**`failed to connect to the docker API at npipe:////./pipe/docker_engine`** —
also seen as *"open //./pipe/docker_engine: The system cannot find the file
specified"*, in your own language. Despite how it reads, this is not about a
missing folder or a bad download: that "file" is the channel the `docker`
command uses to reach the Docker engine, and it exists only while Docker
Desktop is running. Start Docker Desktop, wait for *Engine running*, and run
the command again.

**`Cannot connect to the Docker daemon at unix:///var/run/docker.sock`** — the
same thing on Mac or Linux. Start Docker Desktop (Mac) or `sudo systemctl start
docker` (Linux).

To check whether Docker is ready before trying again:

```bash
docker version
```

Two blocks — *Client* and *Server* — means the engine is running. Only *Client*
followed by an error means it is not.

**Docker Desktop itself won't start.** Usually virtualization is turned off in
the BIOS/UEFI, or WSL 2 is missing; Docker Desktop names which one in its own
error window. Those are Docker installation problems rather than GammaGrid
ones, and Docker's own
[Windows install guide](https://docs.docker.com/desktop/install/windows-install/)
covers both.

**`port is already allocated` on 8501 or 5432.** Something else on your machine
is using that port — often an earlier copy of this app. `docker compose down`
first, then start again.

**The first run seems to hang.** It is downloading a few hundred megabytes of
images. Progress lines that sit still for a while are normal on a slow
connection; it only needs to happen once.

Still stuck? [Open an issue](https://github.com/gammagrid/gammagrid/issues) with
the command you ran and everything it printed — that is enough to work from,
and it is how this section gets longer.

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

And, because every snapshot is kept:

- **Where the price went** — a per-contract waterfall splitting the day's price
  change into delta, gamma, vega, theta and the residual. No entry price is
  needed and none is asked for: this is a property of the contract, not of your
  trade. The residual is always shown, because a decomposition that hides its
  own error cannot be checked
- **Historical Replay** of the gamma profile — step back through collected
  moments and watch the walls move
- **Expired contracts stay readable.** They drop out of the live chain on expiry
  day and their history does not; both remain reachable from the Contract tab
- Works for any ticker with a listed options chain — build your own watchlist,
  not a single fixed symbol

## Usage

1. In the left sidebar, enter a ticker (e.g. `AAPL`) and click **Add** — it
   appears in the watchlist.
2. Click **Collect data** — the app fetches the current option chain for every
   watchlist ticker via Yahoo Finance and saves a snapshot. To keep collecting
   without you, pick an interval under **Collect automatically** (see below).
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
snapshots, so history is worth building up before the more interesting screens
say anything.

## Automatic collection, and what it costs on disk

**Off by default.** A tool that starts hitting a free API the moment it is
installed has made a decision that was not its to make, so you turn it on:
pick **Off / Every 15 minutes / Hourly / Every 4 hours / Once a day** in the
sidebar. It runs in a small worker container that `docker compose up` already
started, which means it keeps collecting while nobody is looking — and stops
when you stop the containers. Fifteen minutes is the floor: Yahoo throttles
under frequent requests, and the floor is enforced in code, not just in the
list.

Collecting continuously fills a disk, so here is the arithmetic rather than a
shrug. One stored row costs about 214 bytes, and one pass stores one row per
contract in every watchlist ticker's chain — a few hundred for a small single
name, ~14,000 for SPY:

| Interval | Small watchlist (~1,000 contracts) | With an index ETF (~15,000) |
|---|---|---|
| Once a day | ~6 MB/month | ~96 MB/month |
| Every 4 hours | ~39 MB/month | ~578 MB/month |
| Hourly | ~154 MB/month | ~2.3 GB/month |
| Every 15 minutes | ~617 MB/month | ~9.2 GB/month |

The sidebar shows this figure for **your** watchlist at the moment you choose an
interval, computed from what you have already collected rather than from the
table above.

**Nothing is ever deleted.** Contracts that expired more than 30 days ago move
to a separate table so the one every chart reads stays small; their history
stays readable and still appears in every historical view.

## Screenshots

**IV surface.** The whole chain at once: implied volatility across strikes and
expiries, so a skew that steepened on one expiry is visible without opening it.

![3D implied volatility surface across the option chain](docs/img/iv-surface.png)

**Options screener.** Every greek, not just delta and IV — including vanna and
charm — with range filters on each, and a click on any row opens that contract's
own history.

![Options screener with the full set of greeks and range filters](docs/img/screener.png)

All screenshots here are real GammaGrid output — SPY/QQQ/MSFT via a live
collection, no mockups.

## FAQ

**What is dealer gamma exposure (GEX)?** It's an estimate of how much options
market makers are net long or short gamma across a ticker's option chain.
Positive GEX suggests dealer hedging tends to dampen price moves; negative GEX
suggests it can amplify them. GammaGrid computes this via Black-Scholes as an
approximation from options open interest — it is **not** a measure of actual
market-maker positions, which aren't public data (see the disclaimer below).

**Can I track European or Asian options — SAP.DE, ASML.AS, 7203.T?** No, and
it is not a setting: the data is not there to fetch. Yahoo Finance returns
**zero option expiries** for `SAP.DE`, `ASML.AS`, `AIR.PA` and `SHEL.L`
(measured 2026-08-17, against 34 for `SPY`), so the chain a provider would have
to return simply does not exist on this source. Eurex and Euronext options are
a separate data product with a separate subscription.

There is usually a way around it that works today: most large non-US companies
also have US-listed ADRs with liquid US options — `ASML` (18 expiries), `NVO`
(17), `SAP` (14), `SHEL` (13), `BUD` (11), all measured the same day. An ADR is
not the same contract as the local one — different hours, different liquidity,
a currency layer — but for positioning it is usually the question you were
asking anyway. Add the plain symbol, without the exchange suffix.

**Is this a real-time options flow scanner?** No — GammaGrid takes periodic
snapshots of the option chain (on demand, or on a schedule you choose), it
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
The app logs collection failures (visible on the dashboard in the collection
log) but makes no attempt to circumvent blocks.

**Coverage is US options only.** A non-US listing is not a failure you can fix
by retrying or by waiting: the source returns no expiries for it at all (see
the [FAQ](#faq) for the measurement and for the ADR route).

## Want it hosted, with zero setup?

It exists and it is open: **[app.gammagrid.io](https://app.gammagrid.io/)**. No
Docker, no local install, no card — free while it is in beta. Sign in with
Google or GitHub, add tickers, and a shared collector keeps them up to date
whether or not you have the tab open.

**What you give up by not self-hosting:** your watchlist and your collected
history live on someone else's machine — ours. **What you get:** nothing to
install or keep running, and a collector that does not stop when your laptop
sleeps.

**What is the same either way:** the calculations. `app/metrics_core.py` is
byte-identical in both products and a CI job fails if that ever stops being
true, so a number here and a number there are the same number, computed by the
same code you can read in this repository.

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
