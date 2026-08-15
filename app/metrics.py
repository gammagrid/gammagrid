"""Metric calculations.

Every function here lives in app/metrics_core.py, which is byte-identical to
the copy in the hosted product's repository — see the rules at the top of that
file before editing it. The two products drifted apart once (the free one
computed greeks with no dividend yield for weeks while the paid one did not),
and a shared file with a checked hash is the fix for the cause rather than for
the symptom.

This module exists so that callers keep importing `app.metrics` and never have
to know where a function lives. The hosted product's copy of this file adds
calculations that need reference data only it fetches; here there are none, so
this is a re-export and nothing else.
"""

from __future__ import annotations

from app.metrics_core import (
    _GREEK_KEYS,
    _YEAR_SECONDS,
    DEFAULT_PRICING,
    EXPIRY_CLOSE_ET,
    EXPIRY_TZ,
    EXTREME_IV_THRESHOLD,
    PROVIDER_GREEKS,
    PricingInputs,
    _as_naive_utc,
    _black_scholes_greeks,
    _black_scholes_greeks_batch,
    _expiry_moment,
    _is_priceable,
    _last_snapshot_per_day,
    _mark_unreliable_iv,
    contract_greeks_history,
    contracts_backing_expiry,
    dealer_walls,
    gamma_exposure_profile,
    gamma_flip_price,
    gex_matrix,
    interpret_greeks,
    iv_surface,
    iv_surface_grid,
    iv_weighted_average,
    max_pain,
    merge_provider_greeks,
    net_gamma_exposure,
    net_gex_by_expiry,
    oi_delta,
    put_call_ratio,
    realized_volatility,
    risk_free_rate,
    screener_table,
    unusual_activity,
    volume_stats,
    years_to_expiry,
    years_to_expiry_series,
)

# Re-exports, including the underscore-prefixed helpers: `app.metrics` stays the
# single entry point for callers and tests, so moving a function into the core
# must not force every call site to learn where it went. Listing them here is
# also what tells the linter these imports are the point of the module.
__all__ = [
    "DEFAULT_PRICING",
    "EXPIRY_CLOSE_ET",
    "EXPIRY_TZ",
    "EXTREME_IV_THRESHOLD",
    "PROVIDER_GREEKS",
    "PricingInputs",
    "_GREEK_KEYS",
    "_YEAR_SECONDS",
    "_as_naive_utc",
    "_black_scholes_greeks",
    "_black_scholes_greeks_batch",
    "_expiry_moment",
    "_is_priceable",
    "_last_snapshot_per_day",
    "_mark_unreliable_iv",
    "contract_greeks_history",
    "contracts_backing_expiry",
    "dealer_walls",
    "gamma_exposure_profile",
    "gamma_flip_price",
    "gex_matrix",
    "interpret_greeks",
    "iv_surface",
    "iv_surface_grid",
    "iv_weighted_average",
    "max_pain",
    "merge_provider_greeks",
    "net_gamma_exposure",
    "net_gex_by_expiry",
    "oi_delta",
    "put_call_ratio",
    "realized_volatility",
    "risk_free_rate",
    "screener_table",
    "unusual_activity",
    "volume_stats",
    "years_to_expiry",
    "years_to_expiry_series",
]
