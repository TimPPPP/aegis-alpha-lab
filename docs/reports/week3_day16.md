# Day 16 Implementation Report

**Date:** 2026-05-05
**Commits:** `ec17952` (Day 15a follow-up prelude) + `2eef246` (Day 16) on `main` + Day 16 hardening follow-up in this changeset
**Status:** **Day 16 complete after hardening.** Day 17 (`EarningsYield(Factor)`) should proceed only from this hardened baseline.

## Top-line

| Metric | Before Day 16 | After Day 16 |
|---|---|---|
| pytest -m "not polygon" | 157 passed, 4 xfailed | **175 passed, 4 xfailed** (+18 tests) |
| pytest -m polygon | 1 passed, 3 skipped, 1 xfailed | unchanged (Day 16 added no polygon-marked tests) |
| Source `.py` files in `src/aegis/` | 30 | **31** (`+ src/aegis/data/fundamentals.py`) |
| `content_hash()` | `b8f31b99…e6bc` | `b8f31b99…e6bc` (unchanged — helpers don't enter `_RESEARCH_IDENTITY_FIELDS`) |
| Quality gates | green | green (ruff + format + mypy + pre-commit) |

## Spine claim

*The pipeline can answer two PIT-correct questions cleanly: "what fundamentals were publicly available for ticker X on date t?" and "what is X's TTM <field> as-of t?" — both with σ-algebra-measurability guarantees verified by truncation-stability tests.* **Met.**

## What landed

### 1. `src/aegis/data/fundamentals.py` (8 helpers)

[src/aegis/data/fundamentals.py](../../src/aegis/data/fundamentals.py) — 292 lines, mypy-clean across 31 source files.

| Helper | Signature | Purpose |
|---|---|---|
| `EXPECTED_COLUMNS` | `tuple[str, ...]` | Mirror of the scraper's column shape; sync-tested |
| `load_fundamentals` | `(path) -> pd.DataFrame` | Column-validated parquet read; coerces dates from `datetime64[ns]` to `date` |
| `fundamentals_at` | `(ticker, as_of, df, *, cik=None) -> pd.Series \| None` | Latest PIT-eligible row across every `period_kind`; CIK-safe |
| `ttm_at` | `(ticker, as_of, df, field, *, cik=None) -> float \| None` | Sum of latest 4 consecutive PIT-eligible quarterlies; restatement-de-duped |
| `ttm_with_status` | `(ticker, as_of, df, field, *, cik=None) -> tuple[float \| None, str \| None]` | Same TTM compute with explicit None reasons |
| `coverage_window` | `(start, end, df) -> pd.DataFrame` | Long-format `(date, ticker, has_pit_fundamentals)` for diagnostics |
| `latest_filing_lag_days` | `(ticker, as_of, df, *, cik=None) -> int \| None` | `(as_of - latest_filing).days` |
| `oldest_ttm_component_lag_days` | `(ticker, as_of, df, *, cik=None) -> int \| None` | `(as_of - oldest_ttm_filing).days` — TTM staleness, not just latest-filing |

### 2. PIT discipline encoded in code, not in tests alone

Strict `filing_date < as_of` now lives in a shared PIT entity-slice helper used by both `fundamentals_at` and the quarterly TTM path. A regression to `<=` would break the strict-comparison test (test 3) and the σ-algebra truncation-stability test (test 4) simultaneously.

The de-dupe key remains `(fiscal_year, fiscal_quarter, period_end_date)` after ticker/CIK identity is resolved. Restatements collapse to the latest filing via `sort_values("filing_date").drop_duplicates(keep="last")`. TTM then selects the latest four fiscal-quarter ordinals and requires them to be consecutive.

### 3. `ttm_with_status` returns valid or named failure statuses

```python
(value, None)                       # valid TTM
(None, "missing_fundamentals")      # ticker has zero PIT-eligible quarterly rows
(None, "insufficient_quarters")     # fewer than 4 consecutive PIT-eligible quarterlies
(None, "missing_field_value")       # 4 quarterlies present but >=1 has field=None/NaN
(None, "ambiguous_cik")             # ticker maps to multiple PIT-eligible CIKs
```

The `missing_field_value` status is **new vs the locked plan**. Rationale: a ticker can have 4 quarterlies all PIT-eligible but `revenues=None` for one of them (Polygon backfills sparse data). Calling that "insufficient_quarters" misleads diagnostics. Day 17's `EarningsYield.compute` can map both `missing_fundamentals` and `missing_field_value` to `invalid_reason="missing_fundamentals"` for the factor (the user-facing distinction doesn't matter for the factor; the diagnostic distinction matters for debugging).

### 4. Engineered `fundamentals_fixture` in conftest

[tests/conftest.py](../../tests/conftest.py) — session-scoped, ~45 rows across 7 engineered tickers. All ticker symbols suffixed `_X` to mark them as fictional. All date arithmetic uses absolute dates (no offsets) for grep-friendly debugging.

| Ticker | Cases encoded | Rows |
|---|---|---|
| `AAPL_X` | 8 normal quarterlies (Sep FY-end), revenues 100..800, net_income 10..80; +1 annual; +1 TTM **booby trap** that must be ignored by `ttm_at` (revenues=99999 — if ever summed in, every TTM assertion explodes) | 10 |
| `MSFT_X` | 8 quarterlies (Jun FY-end) + **restatement of FY24-Q3** (revenues `300 → 350`, filed 30 days later). Tests that de-dupe keeps the later filing | 9 |
| `MAR_FY_X` | 8 quarterlies, **March FY-end**. Designed so that at `as_of=2024-12-01` the latest 4 PIT-eligible quarters span FY24 and FY25 (`FY24-Q3 + FY24-Q4 + FY25-Q1 + FY25-Q2`) — proves TTM crosses fiscal years cleanly | 8 |
| `SHORT_X` | Only 2 PIT-eligible quarterlies → `ttm_at` returns None | 2 |
| `SPARSE_X` | 4 PIT-eligible quarterlies, Q2 has `revenues=None` → `ttm_with_status` returns `(None, "missing_field_value")` | 4 |
| `GAP_X` | 4 PIT-eligible quarterlies but FY24-Q3 is missing → non-consecutive TTM is invalid | 4 |
| `REUSE_X` | Same ticker, two CIKs, each with valid-looking quarterlies → ticker-only lookup returns `ambiguous_cik`; CIK-filtered lookup succeeds | 8 |
| `MISSING_X` | absent from fixture entirely → `(None, "missing_fundamentals")` | 0 |

### 5. 18 acceptance tests

[tests/unit/test_fundamentals.py](../../tests/unit/test_fundamentals.py) — every test pins exactly one behavior so a regression points at one defect.

**PIT discipline (5):**
1. `test_fundamentals_at_returns_latest_filed_before_date` — happy path latest-filed lookup.
2. `test_fundamentals_at_ignores_period_end_date_for_visibility` — the "I forgot which date column to filter on" guard. Q3 has past `period_end` but future `filing_date` → must NOT be visible.
3. `test_fundamentals_lookup_uses_strict_filing_date_before_t` — strict `<` regression guard. Filed-on-t-exactly is invisible at t, visible at t+1.
4. `test_fundamentals_at_is_filtration_measurable` — σ-algebra truncation-stability. Slice frame to `filing_date < t`, output unchanged. Mirrors the Day 5 momentum filtration test.
5. `test_fundamentals_at_returns_none_for_unknown_ticker_or_no_pit_rows` — MISSING_X, unknown ticker, and pre-history all return None without raising.

**TTM correctness (5):**
6. `test_ttm_at_sums_exactly_four_quarterly_reports` — hand-verifiable: AAPL_X TTM after Q4 = 1000, after Q5 = 1400.
7. `test_ttm_dedupes_restatement_by_period_then_latest_filing` — MSFT_X with restated Q3 → TTM uses 350 (later filing), result = 1050. Catches the original-vs-restated bug.
8. `test_ttm_at_returns_none_when_fewer_than_four_quarters_pit_available` — SHORT_X with 2 quarterlies → None.
9. `test_ttm_requires_four_consecutive_fiscal_quarters` — GAP_X has 4 rows but a missing middle quarter → invalid TTM.
10. `test_ttm_at_handles_fiscal_year_boundary` — MAR_FY_X latest 4 = 1800 crossing FY24→FY25. Confirms TTM is "latest 4 consecutive quarterlies", not "current FY's quarterlies".

**`ttm_with_status` / CIK disambiguation (3):**
11. `test_ttm_with_status_distinguishes_missing_from_insufficient` — three outcomes in one test (MISSING_X, SHORT_X, AAPL_X).
12. `test_ttm_with_status_distinguishes_missing_field_value` — SPARSE_X for `revenues` returns `missing_field_value`; same SPARSE_X for `net_income` returns valid 100.0.
13. `test_ttm_requires_cik_when_ticker_history_is_ambiguous` — REUSE_X returns `ambiguous_cik` without CIK and valid TTM with the correct CIK.

**Lag diagnostics (2):**
14. `test_latest_filing_lag_days_basic` — exactly 60 days from filing 2024-01-25 to as_of 2024-03-25.
15. `test_oldest_ttm_component_lag_days_returns_age_of_oldest_quarter` — AAPL_X at 2024-12-01: oldest TTM filing is Q1's 2024-01-25 → lag is exactly `(2024-12-01 - 2024-01-25).days`.

**Frame validation + drift guards (3):**
16. `test_load_fundamentals_validates_column_shape` — bad parquet raises `ValueError` matching `"column shape mismatch"`.
17. `test_fundamentals_columns_match_scraper` — imports `scripts.fetch_polygon_fundamentals` and asserts `EXPECTED_COLUMNS` is identical to the module's. Catches future column drift between the scraper and the consumer.
18. `test_coverage_window_returns_long_format_per_date_per_ticker` — shape contract: 3 days × fixture ticker count; columns `(date, ticker, has_pit_fundamentals)`.

### 6. Day 15a follow-up landed as a clean prelude (`ec17952`)

Before Day 16, the Day 15a follow-up shipped:
- `FundamentalsRow` parquet round-trip coercers (`_coerce_optional_int_from_parquet` for `cik`/`fiscal_year`/`fiscal_quarter`; `_coerce_source_endpoints_from_parquet` for `tuple` reconstruction from `ndarray`/`list`).
- `_validate(df, *, enforce_sanity_floors=True)` so `--limit-tickers` smoke runs don't trip the ≥10k-row / ≥500-ticker floors.
- 6 polygon-free scraper-helper tests in `tests/unit/test_polygon_fundamentals_scraper.py` (CIK formatting, primary-ticker binding, 3-endpoint merge, smoke vs full validation, parquet round-trip).
- Day 15 report corrections (test counts, markdown links, real Polygon pricing).

That commit lifted the baseline from 150 → 157 tests, Day 16's commit lifted it to 173, and the hardening follow-up lifts it to 175.

## Quality gates

```
$ .\.venv\Scripts\python.exe -m pytest -m "not polygon" -q
175 passed, 5 deselected, 4 xfailed in 9.95s

$ .\.venv\Scripts\python.exe -m pytest tests/unit/test_fundamentals.py -q
18 passed in 0.16s

$ .\.venv\Scripts\ruff.exe check src tests scripts
All checks passed!

$ .\.venv\Scripts\ruff.exe format --check src tests scripts
62 files already formatted

$ .\.venv\Scripts\mypy.exe src
Success: no issues found in 31 source files

$ .\.venv\Scripts\pre-commit.exe run --all-files
[all hooks Passed]

$ .\.venv\Scripts\python.exe -c "from aegis.config import load_all; print(load_all().content_hash())"
b8f31b996bcb4e655f4195590be006607884b89106cc73542de0f255e408e6bc
```

`content_hash()` is unchanged — Day 16 ships source code, not config. Hash moves on Day 17 when `factors.yaml` gains entries for `mom_12_1` and `earnings_yield`.

## Decisions made during implementation

1. **Scalar helpers, not vectorized.** Each helper takes `(ticker, as_of)` scalars. Day 17's `EarningsYield.compute` will call them per (date, ticker) — that's potentially 229k call-sites on the 500-ticker × 458-day full-slice. Ruth profiling on Day 17 will tell us if a vectorized `ttm_panel(panel, fundamentals, field)` is needed; Day 16 stays clean and testable. Comment in the module docstring: *"vectorization belongs to factor-compute (Day 17 / Day 19)"*.

2. **Strict `<` enforced via shared PIT entity slicing.** `fundamentals_at` and the quarterly TTM helpers both start from the same strict `filing_date < as_of` entity slice. Future maintainers who try to "fix" the strict comparison hit two failing tests at once (test 3 + test 4).

3. **CIK-safe entity resolution.** Public helpers remain ticker-first but accept `cik=` as a keyword-only filter. If omitted and the PIT ticker slice has multiple non-null CIKs, ticker-only lookups refuse to blend histories: scalar helpers return `None`, and `ttm_with_status` returns `ambiguous_cik`.

4. **`fundamentals_at` spans every period_kind, not just quarterly.** Answers "what's the most recent fundamentals report this ticker filed before t?" — annual / TTM rows count too. Distinct from `ttm_at` which is quarterly-only. The booby-trap TTM row in AAPL_X verifies this distinction works correctly: `fundamentals_at` could legitimately return that TTM row depending on `as_of`, but `ttm_at` must never include it in the sum.

5. **`missing_field_value` and `ambiguous_cik` added as helper statuses.** `missing_field_value` preserves diagnostics when Polygon returns sparse quarterly rows. `ambiguous_cik` prevents ticker-reuse contamination unless Day 17 passes CIK explicitly.

6. **`coverage_window` is calendar-day granular.** No knowledge of trading calendar. Day 17/19 will join to the panel's `date` column to filter to trading days. This keeps `fundamentals.py` independent of the panel module.

## Risks resolved (from the Day 16 plan)

| Risk | Resolution |
|---|---|
| `EXPECTED_COLUMNS` drift between scraper and module | `test_fundamentals_columns_match_scraper` (test 15) imports the scraper and asserts equality. Drift fails CI. |
| Performance on production scale | Deferred to Day 17 profiling. Day 16 ships scalar; if too slow, Day 17 adds a vectorized layer. |
| De-dupe key when CIK is None | Ticker-only helpers now refuse ambiguous multi-CIK PIT slices. Passing `cik=` resolves the entity before de-dupe. |
| Strict `<` vs `<=` regression | Test 3 + test 4 both fail simultaneously if `<` is changed. Test 3 has an explicit comment about why strict matters. |
| TTM-vs-quarterly mix-up | AAPL_X fixture includes a TTM booby trap with `revenues=99999`. Any failure to filter to `period_kind=='quarterly'` would inflate test 6's expected sums (1000, 1400) by ~99000 each. |
| Missing middle quarter accepted as valid TTM | GAP_X has four PIT-eligible rows but skips FY24-Q3. `ttm_at` returns None and `ttm_with_status` returns `insufficient_quarters`. |
| Field NaN vs Python None | `pd.isna` checks throughout. SPARSE_X covers both `revenues` (None) and `net_income` (no None) to assert per-field independence. |

## Files changed

```
docs/reports/week3_day16.md     | 103 ++++++++++++----------
src/aegis/data/fundamentals.py  | 191 ++++++++++++++++++++++++++++++++--------
tests/conftest.py               |  70 ++++++++++++++-
tests/unit/test_fundamentals.py |  43 ++++++++-
4 files changed, 318 insertions(+), 89 deletions(-)
```

Commit `2eef246` on `main` was the original Day 16 baseline. The hardening follow-up above is in this working-tree changeset and should be committed after review.

## What's deferred

| Item | Reason | Unblocked by |
|---|---|---|
| Vectorized `ttm_panel(panel, fundamentals, field) -> pd.Series` | Premature without profile data; scalars are clearer | Day 17 profile shows >10s slowdown |
| Time-window joins onto the panel | Cross-module concern | Day 17 |
| `coverage_window` integration with the trading calendar | Cross-module concern | Day 17/19 |
| Per-row Pydantic validation in `load_fundamentals` | Too slow at 12,800 rows | Tests use the fixture; production trusts the scraper's `_validate` |
| Reading the live `data/reference/fundamentals.parquet` | Doesn't exist (entitlement gap) | Polygon plan upgrade |

## Readiness gate for Day 17 — all three conditions met

1. ✅ `ttm_with_status("AAPL_X", t, fixture, "net_income")` returns `(value, None)` for the happy path and named failure statuses for missing, insufficient/non-consecutive, sparse-field, and ambiguous-CIK cases.
2. ✅ `latest_filing_lag_days` and `oldest_ttm_component_lag_days` produce sensible per-fixture values that Day 17's `EarningsYield.diagnostics` can aggregate (median/p90/max).
3. ✅ `test_fundamentals_at_is_filtration_measurable` passes — proves the helpers are F_t-measurable and Day 17's factor outputs will inherit that property automatically.

Day 17 can proceed from the hardened helper baseline: panel iteration, `ttm_with_status` per row, `mcap` divide, winsorize, z-score, parquet metadata embedding for diagnostics. If the panel carries CIK after Day 18/19, pass it into the helpers; otherwise map `ambiguous_cik` explicitly in the factor's `invalid_reason` policy.

## Next deliverable

**Day 17 — `EarningsYield(Factor)` end-to-end.** Per the locked plan:
- `src/aegis/features/value.py::EarningsYield(Factor)` with `name`, `formula`, `lookback_days`, `compute(panel, *, context=...)`, `diagnostics`.
- `FactorContext(fundamentals=...)` in `src/aegis/features/base.py`.
- `FactorObservation` adds 10th column `invalid_reason: str | None`. Updates Day 13 acceptance test threshold.
- `configs/factors.yaml` gains `mom_12_1` and `earnings_yield` entries — **`content_hash` moves on Day 17**.
- `Momentum12m1m.compute` updated to populate `invalid_reason` (`"history_ineligible"` / None).
- Per-factor diagnostics embedded in parquet metadata via `pyarrow.Table.replace_schema_metadata`.
- ~10 new tests in `tests/unit/test_earnings_yield.py` (formula correctness, dual-formula equivalence, σ-algebra, `invalid_reason` branches including ambiguous-CIK policy, diagnostics round-trip, factor catalog).

Estimated lift: similar to Day 16 (~700 lines of code + tests). Live full-slice run waits for Day 20 + entitlement.
