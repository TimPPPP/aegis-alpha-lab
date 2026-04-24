"""Aegis Alpha Lab — long-short U.S. equity research platform.

V1 modules (spec §6):
    data        Module A — Data & PIT panel
    ledger      Module B — Research ledger
    features    Module C — Feature library
    risk        Module D — Barra-lite risk engine
    validation  Module E — Validation & gate
    portfolio   Module F — Portfolio & cost model
    backtest    Ties A-F end-to-end
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("aegis-alpha-lab")
except PackageNotFoundError:  # editable install before build
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
