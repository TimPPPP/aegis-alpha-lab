"""End-to-end backtest runner — ties Modules A–F together (spec §12).

Week 1 scope: Module A (data) + Module C (momentum factor) only. Modules
B (ledger writes) are stitched in alongside. Modules D/E/F (risk, validation,
portfolio) land in their respective weeks and will extend this package.
"""

from aegis.backtest.week1 import EXPERIMENT_NAME, Week1SliceResult, run_week1_slice

__all__ = ["EXPERIMENT_NAME", "Week1SliceResult", "run_week1_slice"]
