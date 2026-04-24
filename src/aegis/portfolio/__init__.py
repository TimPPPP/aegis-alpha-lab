"""Module F — Portfolio & cost model (spec §9, §10).

OSQP-solved cost-aware quadratic program: dollar/beta-neutral long-short
weights with style and industry exposure budgets, single-name and ADV
capacity caps. Cost model: Corwin-Schultz half-spread floor + Almgren
η|Δw|^{3/2} impact + tiered borrow.
"""
