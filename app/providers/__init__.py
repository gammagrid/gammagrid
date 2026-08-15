"""Provider registry.

get_provider() is the only place that turns a name into a working object.
Everything else — collector, dashboard, tests — takes a provider it was handed
and never asks which one it got.

## Adding your own data source

The whole point of this package is that a second source is a new file here and
nothing else. Write a class with the attributes and methods in
`base.DataProvider` (it is a runtime-checkable Protocol, so
`isinstance(mine, DataProvider)` tells you whether you have them all), return
exactly `base.CHAIN_COLUMNS` from `fetch_ticker_snapshot`, and register it in
`known_providers()` and `get_provider()` below. Import it lazily, the way this
file already does for anything optional, so that people who do not use your
provider do not have to install its dependencies.

One thing to get right, because it is invisible until it is not: every row is
stamped with `name` in `option_snapshots.source`, and the reads act on that
stamp. Two providers derive implied volatility with different models, so the
same contract on the same day legitimately differs between them; the app
therefore shows one source at a time — the one that collected most recently
for that ticker — rather than blending them (see `db.active_source`). Pick a
name and never change it. Changing it does not merely rename rows: the old
name's history stops being the freshest under any name, so it disappears from
every screen while sitting in the database in full.

The hosted version of GammaGrid ships an adapter for a paid, licensed feed. It
is not in this repository; the interface it implements is exactly the one
above, so nothing stops you writing your own against whatever you subscribe to.
"""

from __future__ import annotations

from app.providers.base import CHAIN_COLUMNS, DataProvider, ProviderStatus, with_retry
from app.providers.yahoo import YahooProvider

DEFAULT_PROVIDER = "yahoo"

__all__ = [
    "CHAIN_COLUMNS",
    "DEFAULT_PROVIDER",
    "DataProvider",
    "ProviderStatus",
    "YahooProvider",
    "get_provider",
    "known_providers",
    "with_retry",
]


def known_providers() -> tuple[str, ...]:
    return ("yahoo",)


def get_provider(name: str | None = None, token: str | None = None) -> DataProvider:
    """Builds a provider by name.

    An unknown name raises rather than quietly falling back to the default:
    silently substituting a different data source than the one asked for is the
    kind of surprise that gets diagnosed as "the numbers look wrong" weeks
    later.
    """
    resolved = (name or DEFAULT_PROVIDER).strip().lower()

    if resolved == "yahoo":
        return YahooProvider()

    raise ValueError(
        f"Unknown data provider {resolved!r}. Known providers: {', '.join(known_providers())}. "
        "Adding one is a new file in app/providers/ — see the module docstring."
    )
