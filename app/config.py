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
# All three numbers are starting hypotheses, not final.
IV_OUTLIER_MIN_HISTORY_POINTS = 3
IV_OUTLIER_REL_THRESHOLD = 0.5  # 50% deviation from the contract's median IV
IV_OUTLIER_PRICE_COROBORATION_THRESHOLD = 0.15  # 15% deviation from median last_price counts as "moved"

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
