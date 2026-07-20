import os

DB_PATH = os.environ.get("OPTIONS_TRACKER_DB", "data/options.db")

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
