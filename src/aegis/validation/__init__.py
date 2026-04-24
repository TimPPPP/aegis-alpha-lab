"""Module E — Validation & promotion gate (spec §4.5–§4.11, §5.1).

Purged + embargoed walk-forward CV, Newey-West HAC IC t-stats,
Benjamini-Hochberg FDR at q=0.10, Bailey-López de Prado Deflated Sharpe,
FF3/FF5/FF6 risk-adjusted α, and the longer-horizon decay gate. Emits
Promote / Hold / Retire.
"""
