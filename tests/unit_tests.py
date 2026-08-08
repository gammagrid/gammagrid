"""Unit checks for the pure functions in metrics.py — no database, no network.

Separate from smoke_test.py on purpose. That one exercises the db.py +
metrics.py plumbing on synthetic data: it answers "does this work against a
real SQLite file". These answer "is the number right", which needs no database
at all and so should not pay for one.

Not a pytest suite — plain asserts and a main(), matching smoke_test.py and the
project's preference for small and readable over frameworks.

Usage: python tests/unit_tests.py
"""

import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import metrics  # noqa: E402


def check_years_to_expiry():
    """Time to expiry is measured to the 16:00 ET close, not to midnight.

    This is the arithmetic behind every greek on the screener, the GEX tab and
    the Contract tab, and it was wrong in two ways at once for a long time:
    `(expiry - as_of).days / 365` measured to midnight of the expiration date,
    and truncated the fraction. Both are invisible on LEAPS and dominant on the
    near-dated contracts gamma cares about most."""
    expiry = "2026-08-21"

    # A contract expiring TODAY still trades until the close. The old formula
    # gave exactly zero from midnight onward, which zeroed every greek for the
    # whole session — the single most visible symptom of the bug.
    morning = datetime(2026, 8, 21, 13, 0)  # 09:00 ET, market open
    remaining = metrics.years_to_expiry(expiry, morning)
    assert remaining > 0, f"a contract trading until 16:00 ET is not expired at 09:00 ET ({remaining})"
    hours_left = remaining * 365 * 24
    assert abs(hours_left - 7) < 0.01, f"expected ~7 hours to the close, got {hours_left:.2f}"

    # Past the close it must go negative, not clamp: callers tell a finished
    # contract from a live one by the sign.
    after = datetime(2026, 8, 21, 21, 0)  # 17:00 ET
    assert metrics.years_to_expiry(expiry, after) < 0

    # The fraction is kept. The exact pair from the live measurement: a
    # snapshot at 21:44 UTC against an expiry two calendar days later. The old
    # `(expiry - as_of).days / 365` scored this as exactly 2.00 days — the
    # midnight target and the truncation cancelling into a round number that
    # looked entirely plausible.
    as_of = datetime(2026, 8, 19, 21, 44)
    later = "2026-08-22"
    old_formula_days = (pd.Timestamp(later) - pd.Timestamp(as_of)).days
    assert old_formula_days == 2, "the pair being reproduced is the one that scored 2.00"
    days = metrics.years_to_expiry(later, as_of) * 365
    assert 2.9 < days < 2.95, f"expected ~2.93 days, got {days:.3f}"
    assert days > old_formula_days, "truncating to whole days understates the remaining life"
    two_days = metrics.years_to_expiry(expiry, as_of)

    # Daylight saving: the same clock time on the expiry date is a different
    # UTC instant in August (EDT, UTC-4) and December (EST, UTC-5). A fixed
    # offset would pass in summer and be an hour wrong all winter.
    summer = metrics._expiry_moment("2026-08-21")
    winter = metrics._expiry_moment("2026-12-18")
    assert summer.hour == 20, f"16:00 EDT is 20:00 UTC, got {summer}"
    assert winter.hour == 21, f"16:00 EST is 21:00 UTC, got {winter}"

    # tz-aware input must not raise: stored timestamps are naive UTC, but
    # callers reach for pd.Timestamp.utcnow(), which is aware.
    aware = pd.Timestamp("2026-08-19 21:44", tz="UTC")
    assert abs(metrics.years_to_expiry(expiry, aware) - two_days) < 1e-12

    # The vectorized form must agree with the scalar one, element for element —
    # the screener uses one and the GEX tab the other, and a divergence between
    # them would show up as two tabs disagreeing about the same contract.
    expiries = pd.Series(["2026-08-21", "2026-09-18", "2027-01-15"])
    as_of = datetime(2026, 8, 19, 21, 44)
    vector = metrics.years_to_expiry_series(expiries, as_of).to_numpy()
    scalar = np.array([metrics.years_to_expiry(e, as_of) for e in expiries])
    assert np.allclose(vector, scalar, rtol=1e-12), (vector, scalar)
    print("years_to_expiry checks passed")


def check_greeks_respond_to_time():
    """Gamma scales with 1/sqrt(T), which is why the time-to-expiry error
    mattered: understating T overstates gamma, and GEX is gamma × open
    interest. Asserted as a property rather than a fixed number so the check
    survives a change of units."""
    at_the_money = dict(spot=100.0, strike=100.0, iv=0.30, risk_free_rate=0.05, option_type="call")
    near = metrics._black_scholes_greeks(years_to_expiry=2 / 365, **at_the_money)
    far = metrics._black_scholes_greeks(years_to_expiry=2.92 / 365, **at_the_money)
    assert near["gamma"] > far["gamma"], "less time left must mean more gamma at the money"
    overstatement = near["gamma"] / far["gamma"] - 1
    assert 0.1 < overstatement < 0.3, (
        f"a 2.00 vs 2.92 day error should overstate gamma by roughly 15%, got {overstatement:.1%}"
    )

    # A contract past its close has no greeks rather than nonsense ones.
    expired = metrics._black_scholes_greeks(years_to_expiry=-0.5 / 365, **at_the_money)
    assert all(value == 0.0 for value in expired.values()), expired
    print("greeks/time checks passed")


def main():
    check_years_to_expiry()
    check_greeks_respond_to_time()
    print("\nALL UNIT CHECKS PASSED")


if __name__ == "__main__":
    main()
