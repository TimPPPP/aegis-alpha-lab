"""End-to-end backtest runner — ties Modules A–F together (spec §12).

Week 1 scope: Module A (data) + Module C (momentum factor) only. Modules
B (ledger writes) are stitched in alongside. Modules D/E/F (risk, validation,
portfolio) land in their respective weeks and will extend this package.

Public API:
    SliceResult              — frozen result dataclass returned by every slice
    run_week1_slice          — Week 1 vertical slice (8 hardcoded blue-chips)
    run_full_slice           — Week 2 widened-universe slice (active_on(date))
    EXPERIMENT_NAME          — Week 1's experiment-row label
    EXPERIMENT_NAME_PREFIX   — Week 2's experiment-row prefix (gets ``_<date>`` appended)
    Week1SliceResult         — back-compat alias for SliceResult
"""

from aegis.backtest._common import SliceResult
from aegis.backtest.full import EXPERIMENT_NAME_PREFIX, run_full_slice
from aegis.backtest.week1 import EXPERIMENT_NAME, Week1SliceResult, run_week1_slice

__all__ = [
    "EXPERIMENT_NAME",
    "EXPERIMENT_NAME_PREFIX",
    "SliceResult",
    "Week1SliceResult",
    "run_full_slice",
    "run_week1_slice",
]
