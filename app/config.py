import os

# Postgres replaced SQLite in this release. The reason is the scheduled
# collector: a background worker writing every few minutes while the dashboard
# reads is exactly the workload SQLite's single-writer lock is worst at, and the
# archiving and per-contract bookkeeping that continuous collection needs are
# the features that made a real engine pay for itself.
#
# What it costs, stated plainly because the README used to promise otherwise:
# the app no longer runs from a single file, and a backup is `pg_dump` rather
# than copying one. `docker compose up` is still one command.
#
# The default matches docker-compose.yml, so nothing has to be set by hand
# there. Running from source against your own server means setting this.
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://gammagrid:gammagrid@localhost:5432/gammagrid"
)

# Unusual activity (spec FR16): volume must be a z-score outlier relative to
# the contract's own history, not just exceed a flat multiplier — otherwise
# liquid tickers flag thousands of rows with no signal.
UNUSUAL_Z_THRESHOLD = 2.5
# Cuts noise from illiquid far-OTM strikes, where even a large relative
# volume spike means nothing in dollar terms.
UNUSUAL_MIN_VOLUME = 50
# With fewer history snapshots than this per contract, the z-score is
# unreliable and a simplified fallback is used (volume > 2×OI).
UNUSUAL_MIN_HISTORY_POINTS = 5

# Risk-free rate for the Black-Scholes formula in greeks/GEX calculations
# (spec FR6, FR14). A fixed constant rather than a market rate — precision
# is not critical here; greeks are weakly sensitive to small changes in r.
RISK_FREE_RATE = 0.05

# Data-quality guard for the Contract tab (found live: yfinance's reported
# implied_volatility can be stale/wrong for one specific snapshot while
# last_price stays normal — every greek derived from that IV then spikes
# even though nothing about the contract actually changed, producing a
# jagged chart on an otherwise flat price series). A first version priced
# the reported IV via Black-Scholes and compared it to last_price directly
# — reverted (found live, real MO LEAPS data) because that needs a
# dividend yield the app doesn't track: ignoring dividends (q=0) badly
# overprices long-dated calls on high-yield names, flagging perfectly good
# IV purely because the pricing model itself was wrong. It also assumed
# last_price is a live, trustworthy reference — false for thinly-traded
# strikes, where last_price is often just stale (no new trade) while IV
# keeps updating from live quotes.
#
# This version instead compares a snapshot to the CONTRACT'S OWN history:
# an IV is untrusted only if it's a strong outlier vs. the contract's own
# median IV *and* last_price does not corroborate a real move of
# comparable size. Provider- and magnitude-agnostic by construction —
# option price is monotonic in IV for any dividend yield, so a genuine
# large real move always shows a matching price move too; an IV move with
# no price move at all is what the live incident actually looked like.
#
# A median is only trustworthy while outliers are a small minority of the
# sample — found live on THIS repo's own shorter local history (4
# snapshots, an old stuck IV value and the current one split 2-2): the
# median landed almost exactly between the two clusters, so BOTH looked
# like outliers from it, and the whole contract's greeks went blank.
# IV_OUTLIER_MAX_UNRELIABLE_FRACTION caps how much of a contract's history
# this guard is allowed to suppress before it no longer trusts its own
# reference and backs off entirely. All four numbers are starting
# hypotheses, not final.
# Greek attribution: below this contract price the decomposition is not shown
# for that stretch of days. Measured on the real trade the feature was built
# from: at 1c the provider's IV read 141%, and the vega term came out at +20.9c
# — twenty times the option's whole price. IV is inverted FROM the price, so on
# a one-cent quote a one-cent tick is a 100% move and every greek derived from
# it is arithmetic about rounding. 5c is where that stops.
ATTRIBUTION_MIN_PRICE = 0.05

IV_OUTLIER_MIN_HISTORY_POINTS = 3
IV_OUTLIER_REL_THRESHOLD = 0.5  # 50% deviation from the contract's median IV
IV_OUTLIER_PRICE_COROBORATION_THRESHOLD = 0.15  # 15% deviation from median last_price counts as "moved"
IV_OUTLIER_MAX_UNRELIABLE_FRACTION = 0.3  # never suppress more than 30% of a contract's history

MAX_FETCH_RETRIES = 3
BACKOFF_BASE_SECONDS = 2

# Threshold for the fraction of contracts with open_interest=0 in a freshly
# collected chain, above which the snapshot is considered suspect and is not
# saved. Found via a real incident (2026-07-17): the data source twice in a
# row returned a chain with working volume/prices but open_interest=0 almost
# everywhere (94.4% on SPY vs. the usual ~8%) and understated IV — the
# snapshot looked "successful" but broke the GEX Heatmap / OI Delta views.
# 0.5 leaves a wide margin above the normal level.
MAX_ZERO_OI_FRACTION = 0.5

# How much history a normal page load reads. Collection is a manual button
# today, so history grows only when you press it — but scheduled collection is
# on the roadmap, and an unbounded read is what turns that feature into a
# complaint about the app being slow. Measured on the hosted sibling, which
# collects continuously: a year of one liquid ticker is ~1.2M rows and ~390MB
# once in pandas, fetched on every interaction, to draw charts that only ever
# show the last few days.
#
# A DEFAULT for the normal page load, not a cap: collected data is the most
# valuable thing in the app, and a rolling window must never silently hide a
# long-lived contract's early history. The dashboard carries a "Load full
# history" toggle that passes days=None straight through — bounded by default,
# unbounded on request, never unbounded-then-secretly-trimmed.
SNAPSHOT_HISTORY_DAYS = int(os.environ.get("SNAPSHOT_HISTORY_DAYS", "365"))


# --- scheduled collection ---

# What the interval selector offers, in minutes. A short list rather than a
# free-text field, because the floor is set by the data source and not by
# taste: Yahoo throttles under frequent requests, and a box accepting "1"
# invites exactly the setting that gets somebody blocked.
COLLECTOR_INTERVAL_CHOICES = {
    "Off": 0,
    "Every 15 minutes": 15,
    "Hourly": 60,
    "Every 4 hours": 240,
    "Once a day": 1440,
}

# OFF BY DEFAULT, deliberately. A tool that starts hitting a free API the
# moment it is installed is a bad citizen, and this one promises no account and
# no card — the least it can do is wait to be asked. Turning it on is also when
# the interface states what it will cost in disk space, which is the honest
# moment for that number.
COLLECTOR_INTERVAL_DEFAULT_MINUTES = 0

# The floor is enforced in code, not only in the list above: a setting written
# straight into the database, or a list edited later, must not be able to point
# the collector at something the source will refuse to serve.
PROVIDER_MIN_INTERVAL_MINUTES = 15

# Roughly what one stored snapshot row costs on disk, measured on the hosted
# product across millions of rows. Used to tell the user, at the moment they
# choose an interval, how fast their database will grow — see
# db.estimated_growth_mb_per_month.
BYTES_PER_SNAPSHOT_ROW = 214

# How far back the z-score's baseline reaches, in calendar days. A cap rather
# than a preference: the statistics are built from one snapshot per day per
# contract, so the work grows with the window forever while the answer does
# not. Sixty days is also the more defensible baseline — "unusual for this
# contract" measured against volume from ten months ago says more about how the
# underlying traded last autumn than about today.
UNUSUAL_HISTORY_DAYS = 60

# How many stored volatility averages one pass rewrites with our own model, per
# ticker. The stored rollup is the only number in this product that was written
# by the data source's model rather than ours, and the worker walks it in the
# background rather than asking anybody to run anything — see app/iv_backfill.py.
#
# Sized so that a pass is never the reason a cycle is late. One moment costs a
# read of that chain plus the solve — single-digit milliseconds for a chain of a
# few thousand contracts — so fifty is well under a second even on a small
# machine, and a year of collection at fifteen-minute intervals is worked
# through in a few dozen cycles. Nothing waits on it: the screens already show
# our number, and only the historical average is catching up.
IV_BACKFILL_MOMENTS_PER_PASS = int(os.environ.get("IV_BACKFILL_MOMENTS_PER_PASS", "50"))

# How often the symbol directory behind the Add-ticker search box is downloaded
# again. Weekly rather than nightly, and the difference is who is paying for it:
# this product runs on somebody's own machine, the file changes by a handful of
# rows a week, and a daily request to a third party is not something to sign an
# installation up for without a reason. A missing catalogue is fetched
# immediately whatever this says — see app/catalogue.py.
CATALOGUE_REFRESH_DAYS = int(os.environ.get("CATALOGUE_REFRESH_DAYS", "7"))

# Contracts expired longer ago than this move to option_snapshots_archive.
# Nothing is deleted; the point is only to keep the table every live query
# reads from carrying years of contracts that can never trade again.
CONTRACT_ARCHIVE_GRACE_DAYS = int(os.environ.get("CONTRACT_ARCHIVE_GRACE_DAYS", "30"))


# --- which revision this is ---

# Read from CHANGELOG.md rather than written down here, and that is the whole
# point of the choice. A constant in the code is one more place to remember to
# bump at release time, and a version that lies is worse than no version at
# all: it makes a support answer confidently wrong. The changelog's top heading
# is already mandatory to update — the project's own rule — so it cannot be
# forgotten without the omission being obvious in the release itself.
#
# `--build-arg` was the third option and was rejected: almost everyone installs
# with a plain `docker compose up`, and a version that only appears for people
# who passed a flag is a version that is missing exactly when it is needed.
_CHANGELOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "CHANGELOG.md")


def _read_version() -> str:
    """The release this code came from, as a person would name it.

    Returns the tag ("v0.5.1"). A commit hash would be more precise and less
    useful: it means nothing to the person reading a screenshot, and there is
    no git inside the container to produce one anyway.

    A checkout with unreleased work says so — "v0.5.2+unreleased" — because
    reporting the last tag flat would claim this is a released build when it is
    not. Somebody running from main is exactly the person whose bug report
    needs that distinction.

    WHETHER THERE IS UNRELEASED WORK IS DECIDED BY CONTENT, NOT BY THE
    HEADING. Keep a Changelog leaves an empty `[Unreleased]` heading at the top
    of the file permanently, so a build cut at a tag would otherwise call
    itself "+unreleased" forever — which is what the first release after this
    function was written actually did. The alternative was to delete the empty
    heading at release time and put it back with the next change, and that is a
    ritual step somebody eventually forgets. So the section counts only when
    something is written under it.

    Failure is silent and returns "unknown": a missing or unreadable changelog
    must never be the reason the dashboard will not start.
    """
    try:
        with open(_CHANGELOG, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return "unknown"

    headings = [i for i, line in enumerate(lines) if line.startswith("## [")]
    if not headings:
        return "unknown"

    def name(index: int) -> str:
        return lines[index][len("## ["):].split("]")[0]

    unreleased = False
    if name(headings[0]).lower() == "unreleased":
        end = headings[1] if len(headings) > 1 else len(lines)
        unreleased = any(line.strip() for line in lines[headings[0] + 1:end])

    released = next((name(i) for i in headings if name(i).lower() != "unreleased"), None)
    if released is None:
        return "unknown"
    return f"v{released}+unreleased" if unreleased else f"v{released}"


APP_VERSION = _read_version()


# How many failures in a row make a symbol hopeless rather than unlucky. Six is
# an hour and a half at the fastest interval the product allows — long enough
# that a data source having a bad afternoon is not mistaken for a symbol that
# does not exist.
#
# The count only ever applies to a symbol that has NEVER produced a snapshot.
# See db.unresolvable_tickers for why that single condition is what separates
# "this ticker is imaginary" from "the provider was down".
UNRESOLVABLE_AFTER_FAILURES = int(os.environ.get("UNRESOLVABLE_AFTER_FAILURES", "6"))


# How long to leave the data source alone after it has refused us. Half an hour
# is a guess made in the safe direction: too short and the cooldown does not
# work, too long and the only cost is a gap in the history — which the next
# pass fills in, because the chain is a snapshot and not a stream.
PROVIDER_COOLDOWN_MINUTES = int(os.environ.get("PROVIDER_COOLDOWN_MINUTES", "30"))
