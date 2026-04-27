"""Module A — Data & PIT panel (spec §6, §7).

Assembles the point-in-time U.S. daily equity panel on CRSP + Compustat PIT
via WRDS. Output: stock-date Parquet panel (~15M rows, ~60 cols). Module
acceptance: reconstructed S&P 500 membership on any past date matches the
published index within 1 name.
"""
