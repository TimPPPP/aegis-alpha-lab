"""Module D — Barra-lite risk engine (spec §4.2–§4.4).

Cross-sectional √mcap-weighted WLS on 9 style factors + 24 GICS industries
+ 1 country intercept. EWMA factor covariance Ω (halflife=90d) and specific
D (halflife=60d). Residualization of candidate signals before IC scoring.
"""
