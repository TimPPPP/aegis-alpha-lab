# Day 17 Implementation Report

**Date:** 2026-05-06
**Commit:** `4d376a6` on `main`
**Status:** **Day 17 complete.** Day 18 (sector enrichment) and Day 19 (multi-factor refactor) are unblocked.

## Top-line

| Metric | Before Day 17 | After Day 17 |
|---|---|---|
| pytest -m "not polygon" | 175 passed, 4 xfailed | **190 passed, 4 xfailed** (+15 tests) |
| Source `.py` files in `src/aegis/` | 31 | **32** (`+ src/aegis/features/value.py`) |
| `content_hash()` | `b8f31b99…e6bc` | **`805c6e80…3cb5`** (moved — `factors.yaml` populated) |
| Quality gates | green | green (ruff + format + mypy + pre-commit) |
| Factor parquet schema | 9 columns | **10 columns** (`+ invalid_reason`) |

## Spine claim

*The pipeline produces a fundamentals-based factor PIT-correctly end-to-end: `EarningsYield(Factor)` consumes `(panel, FactorContext(fundamentals))`, emits `FactorObservation` rows with a 10th `invalid_reason` column, and embeds per-factor diagnostics (filing-lag distributions + invalid-reason counts) in parquet metadata. `Momentum12m1m` is updated symmetrically.* **Met.**

## What landed

### 1. `FactorObservation` schema bump (10th column)

[`src/aegis/features/base.py`](../../src/aegis/features/base.py) — `invalid_reason: str | None` added with bidirectional Pydantic invariant: `valid_flag=True ⟺ invalid_reason is None`. A row marked invalid must name a reason; a valid row must not. The closed enum (across both factors) is:

```
"history_ineligible"      — mom_12_1: insufficient lookback for 252-day shift
"missing_fundamentals"    — earnings_yield: ticker has no PIT-eligible quarterly rows
                             (or sparse/ambiguous-CIK collapse, see §6)
"insufficient_quarters"   — earnings_yield: 1-3 PIT-eligible quarterlies (need 4)
"invalid_denominator"     — earnings_yield: mcap is None / NaN / ≤ 0
"raw_factor_nan"          — earnings_yield: cross-sectional zscore failed
                             (only one valid ticker on a given date)
None                      — valid row
```

Universe ineligibility never appears in `invalid_reason`. It lives in `tradable_flag` alone — a row with `valid_flag=True, tradable_flag=False, invalid_reason=None` is the canonical "math is fine, universe says no" shape. Test 11 (`test_valid_flag_does_not_encode_universe_ineligible`) pins this.

### 2. `FactorContext` plumbing

```python
@dataclass(frozen=True)
class FactorContext:
    fundamentals: pd.DataFrame | None = None
```

Sidecar data passed into `Factor.compute`. `Momentum12m1m` ignores it (`del context`); `EarningsYield` raises `ValueError` if `context is None or context.fundamentals is None`. Future weeks can add fields (risk-model exposures, sector_proxy maps) without changing factor signatures.

### 3. Parquet diagnostics round-trip

`write_factor_parquet(factor_out, path, diagnostics)` and `read_factor_diagnostics(path) -> dict` use pyarrow's schema metadata key `b"factor_diagnostics"` (JSON-encoded). One parquet file carries both the factor values and per-factor stats. `_run_factor_slice` switched to `write_factor_parquet`, so Week 1's `mom_12_1` slice now also gets diagnostics in metadata.

### 4. `EarningsYield(Factor)` (new)

[`src/aegis/features/value.py`](../../src/aegis/features/value.py) — 218 lines.

```python
class EarningsYield(Factor):
    name = "earnings_yield"
    formula = "ttm_net_income / mcap (E/P)"
    lookback_days = 365
```

Per-row computation:

1. `funds_sub = funds_by_ticker.get(ticker)` → if absent, `invalid_reason="missing_fundamentals"`.
2. `ttm_ni, status = ttm_with_status(ticker, date, funds_sub, "net_income")` → status maps:
   - `"missing_fundamentals"` / `"missing_field_value"` / `"ambiguous_cik"` → `"missing_fundamentals"` (conservative collapse — Day 17 panel doesn't carry CIK; Day 18 will).
   - `"insufficient_quarters"` → `"insufficient_quarters"`.
3. `mcap is None / NaN / ≤ 0` → `"invalid_denominator"`.
4. `raw = ttm_ni / mcap`; if not finite → `"raw_factor_nan"`.
5. Cross-sectional 1%/99% winsorize → 1-σ z-score per date.
6. Post-zscore re-check: rows with finite raw but NaN winsorized/zscore (single-valid-row dates) get backfilled `"raw_factor_nan"`.

**Performance**: pre-slice fundamentals into `funds_by_ticker: dict[str, DataFrame]` once at the top of compute, then per-row walks operate against the small per-ticker frame. Avoids 229k full-frame filters that would otherwise dominate the live full-slice (Day 20). Expected wall time on the 500-ticker live slice: a few seconds for the factor compute itself.

### 5. `Momentum12m1m` updated

10-column output (`+ invalid_reason`). Populates `"history_ineligible"` for short-lookback rows, `None` otherwise. Overrides `diagnostics()` to return `invalid_reason_counts`. Accepts `*, context=None` (ignored). All 13 existing momentum tests still pass.

### 6. `_run_factor_slice` wired

```python
context = FactorContext()  # mom_12_1 doesn't need it; placeholder
factor_out = factor.compute(panel, context=context)
diagnostics = factor.diagnostics(factor_out, context=context)
write_factor_parquet(factor_out, factor_path, diagnostics)
```

The Day 13 acceptance test bumped from `factor.shape == (rows, 9)` to `(rows, 10)`. Re-running `aegis backtest week1` or `aegis backtest full` produces 10-column parquets with diagnostics in pyarrow metadata.

### 7. `configs/factors.yaml` populated

```yaml
factors:
  - name: mom_12_1                # family: momentum, lookback 252, ic 0.04
  - name: earnings_yield          # family: value, lookback 365, ic 0.03
```

Was `[]`. **`content_hash` moves from `b8f31b99…e6bc` to `805c6e80…3cb5`** — expected and documented in the commit message. Old ledger candidates (`79431a46`, `0bb9e71c`, `93508ea1`) continue to report `config_hash_match=False`. Principle 5 contract intact.

### 8. Engineered `value_panel_fixture`

Pairs with `fundamentals_fixture` from Day 16. 7-row panel with hand-verifiable raw values:

| Ticker | as_of | ttm_ni (computed) | mcap | raw | Status |
|---|---|---|---|---|---|
| `AAPL_X` | 2024-12-01 | 100 (Q1+Q2+Q3+Q4 of FY24) | 1000 | **0.10** | valid |
| `MSFT_X` | 2024-12-01 | 145 (FY24-Q2 + Q3-restated + Q4 + FY25-Q1) | 1500 | **0.0967** | valid (uses restatement, crosses fiscal years) |
| `MAR_FY_X` | 2024-12-01 | 180 (FY24-Q3 + Q4 + FY25-Q1 + Q2) | 2000 | **0.090** | valid (latest 4 cross FY boundary) |
| `MISSING_X` | 2025-01-01 | — | 500 | NaN | `missing_fundamentals` |
| `SHORT_X` | 2025-01-01 | — | 500 | NaN | `insufficient_quarters` |
| `SPARSE_X` | 2025-01-01 | 100 (revenues=None doesn't block net_income) | 500 | NaN | `missing_fundamentals` (single-valid-on-date → zscore NaN) |
| `REUSE_X` | 2025-01-01 | — | 500 | NaN | `missing_fundamentals` (ambiguous_cik collapse) |

Three valid tickers all on 2024-12-01 enables the per-date z-score test (test 3) to assert `mean(zscore) == 0` non-degenerately.

### 9. 13 new acceptance tests + 2 schema invariant tests

[`tests/unit/test_earnings_yield.py`](../../tests/unit/test_earnings_yield.py) — every test pins exactly one behavior:

| # | Test | Asserts |
|---|---|---|
| 1 | `test_earnings_yield_formula_correctness` | AAPL_X raw_value == 0.10 (hand-verifiable) |
| 2 | `test_earnings_yield_two_formulas_agree` | `ttm_ni/mcap == ttm_ni/shares_out/adj_close` within 1e-12 |
| 3 | `test_earnings_yield_per_date_zscore_mean_zero` | mean(zscore) at 2024-12-01 ≈ 0 across 3 valid tickers |
| 4 | `test_earnings_yield_is_filtration_measurable` | Truncate fundamentals to `filing_date < t` → output unchanged |
| 5 | `test_earnings_yield_requires_fundamentals_context` | Both `compute(panel)` and `compute(panel, context=FactorContext())` raise ValueError |
| 6 | `test_earnings_yield_missing_fundamentals_marks_invalid_reason` | MISSING_X → `"missing_fundamentals"`, `valid_flag=False` |
| 7 | `test_earnings_yield_insufficient_quarters_marks_invalid_reason` | SHORT_X → `"insufficient_quarters"` |
| 8 | `test_earnings_yield_zero_mcap_marks_invalid_reason` | mcap=0 → `"invalid_denominator"` |
| 9 | `test_earnings_yield_ambiguous_cik_collapses_to_missing_fundamentals` | REUSE_X → `"missing_fundamentals"` (Day 17 conservative collapse) |
| 10 | `test_factor_observation_invalid_reason_shape` | `factor_out.shape[1] == 10` |
| 11 | `test_valid_flag_does_not_encode_universe_ineligible` | eligible_flag=False math-valid row → valid_flag=True, tradable_flag=False, invalid_reason=None |
| 12 | `test_factor_catalog_contains_momentum_and_earnings_yield` | `load_all().factors.factors` has both names |
| 13 | `test_earnings_yield_diagnostics_in_parquet_metadata` | Round-trip via `read_factor_diagnostics`: dict has invalid_reason_counts + latest/oldest lag stats |

Plus 2 new schema tests in [`tests/unit/test_schema.py`](../../tests/unit/test_schema.py):

- `test_factor_observation_rejects_valid_flag_with_invalid_reason_set` — pinches one direction of the bidirectional invariant.
- `test_factor_observation_rejects_invalid_flag_with_null_invalid_reason` — pinches the other.

## Quality gates

```
$ uv run pytest -m "not polygon" -q
190 passed, 5 deselected, 4 xfailed in 10.93s

$ uv run pytest tests/unit/test_earnings_yield.py -v
13 passed in 0.24s

$ uv run ruff check src tests scripts          # All checks passed!
$ uv run ruff format --check src tests scripts # 64 files already formatted
$ uv run mypy src                              # Success: no issues found in 32 source files
$ uv run pre-commit run --all-files            # all hooks Passed
$ uv run python -c "from aegis.config import load_all; print(load_all().content_hash())"
805c6e8004cdc1298d7b303ade5046698c2c235d883902f7f922377ca9583cb5
```

## Decisions made during implementation

1. **`ambiguous_cik` and `missing_field_value` collapse to `missing_fundamentals`.** Day 16's hardening introduced both as distinct `ttm_with_status` outcomes. EarningsYield maps them to a single factor `invalid_reason` because:
   - Day 17 panel doesn't carry CIK; the factor cannot disambiguate REUSE_X without it.
   - Downstream consumers only need to know "this row is unusable for the factor".
   - Day 16's `_TTM_STATUS_TO_INVALID_REASON` dict makes the collapse one line and revisitable. When Day 18 plumbs CIK onto the panel, Day 19 can refine.

2. **Bidirectional `valid_flag ↔ invalid_reason` invariant in Pydantic.** Stronger than the locked plan suggested (the plan said "valid_flag=True ⟹ invalid_reason is None"). I added the reverse too — `valid_flag=False ⟹ invalid_reason is set` — because a False valid_flag without a reason makes diagnostics useless. The two new schema tests (`test_factor_observation_rejects_valid_flag_with_invalid_reason_set` and `..._null_invalid_reason`) pin both directions independently.

3. **Pre-slice fundamentals by ticker once.** The locked plan deferred vectorization to Day 17 if profiling showed slowness. I went with smart-scalar instead: one `groupby("ticker")` at the top of compute, then per-row walks against the per-ticker frame. Avoids the 229k full-frame scans that would dominate live wall time. Matches the locked plan's "vectorization belongs to factor-compute" note from Day 16.

4. **Post-zscore `raw_factor_nan` backfill.** The cross-sectional ops (winsorize then zscore) can introduce new NaNs even when `raw_value` was finite — specifically when only one ticker is valid on a date (std=0 → zscore = NaN). I added a backfill pass: rows that started valid but lost it during winsorize/zscore get marked `"raw_factor_nan"` so the bidirectional invariant still holds. Tests 3 and 13 stress this path.

5. **`_run_factor_slice` adopts `write_factor_parquet` even though Day 19 will refactor it.** Cleaner than waiting — Week 1's `mom_12_1` slice now writes diagnostics in metadata for free. The byte-shape change (pyarrow vs pandas writer) doesn't break any existing test because Day 13's acceptance test creates a fresh slice end-to-end and reads back its own checksum.

6. **`feature_snapshot_id` includes `invalid_reason`.** The Day 16-era hash was over 7 columns (date/ticker/raw/win/zscore/valid_flag/tradable_flag); Day 17 adds `invalid_reason` so post-Day-17 hashes are stable but distinct from pre-Day-17 hashes. Old candidates' stored snapshot IDs continue to verify against their stored bytes (we don't recompute), but any test that calls `Momentum12m1m().compute(panel)` and asserts a specific snapshot ID literal would break — none do, so no test updates needed.

## Risks resolved (from the Day 17 plan)

| Risk | Resolution |
|---|---|
| `feature_snapshot_id` drift on `mom_12_1` | Verified no test asserts a specific literal — only "exists and is hex64" |
| Day 13 acceptance with hardcoded factor shape | One spot, bumped 9 → 10 |
| `_run_factor_slice` parquet write changes break ledger byte-identity | Old artifacts on disk unchanged; only fresh slices use new writer; verify-mode tests create fresh candidates and pass |
| `pyarrow.parquet.write_table` vs pandas | Used `compression="snappy"` to match pandas defaults; round-trip test (test 13) confirms shape preservation |
| `ambiguous_cik` mapping | Test 9 pins the conservative collapse; revisit when Day 18 lands CIK on panel |
| Performance on live full-slice | Pre-slice by ticker; no profile data yet (Day 20 entitlement-gated) but expected ≪10s |

## Files changed

```
configs/factors.yaml                    |  22 ++++  (+22 from `factors: []`)
src/aegis/backtest/_common.py           |  10 +--   (write_factor_parquet + context)
src/aegis/features/base.py              | 122 +++  (FactorContext + parquet helpers + invariant)
src/aegis/features/momentum.py          |  35 ++   (+context, +invalid_reason, +diagnostics)
src/aegis/features/value.py             | 218 +++ (new — EarningsYield)
tests/conftest.py                       |  60 ++   (value_panel_fixture)
tests/unit/test_earnings_yield.py       | 222 +++ (new — 13 tests)
tests/unit/test_full_pipeline.py        |   6 ±   (factor.shape 9 -> 10)
tests/unit/test_schema.py               |  47 ++   (2 new invariant tests)
9 files changed, 755 insertions(+), 34 deletions(-)
```

Commit `4d376a6` on `main`, pushed to `origin/main`.

## What's deferred

| Item | Reason | Unblocked by |
|---|---|---|
| `_run_factor_slice` multi-factor refactor | Locked plan: Day 19 | Day 19 |
| CLI `--factors` flag + `FACTOR_REGISTRY` | Day 19 | Day 19 |
| `MultiFactorSliceResult` / N-candidate ledger layout | Day 19 | Day 19 |
| Live full-slice with both factors via `aegis backtest full` | Day 20 + entitlement | Polygon plan upgrade |
| `BookYield` / `SalesYield` / `CashFlowYield` / `ValueComposite` | Week 4 | Week 3 plumbing complete |
| Vectorized `ttm_panel(panel, fundamentals, field) -> pd.Series` | Premature; pre-slice-by-ticker is fast enough | Day 20 profiling |
| Real CIK on the panel (refines `ambiguous_cik` from collapse to resolution) | Day 18 lands sector_proxy and could plumb CIK then; defer to Day 19 if not | Day 18/19 |

## Readiness gate for Day 18 — both met

1. ✅ `EarningsYield.compute(panel, context=FactorContext(fundamentals=fixture))` produces a clean factor parquet for the engineered fixture (test 1, 2, 3, 4 all green).
2. ✅ `_run_factor_slice` calls `factor.diagnostics` and embeds them — Week 1's `mom_12_1` slice now has diagnostics in its parquet metadata.

Day 18 (sector enrichment, isolated mechanical commit) is purely a panel column rename + SIC mapping CSV. It doesn't depend on Day 17's factor work — orthogonal. The locked plan ordered Day 18 BEFORE Day 19's multi-factor refactor for sector_proxy reasons; Day 17 doesn't perturb that ordering.

## Next deliverable

**Day 18 — Sector enrichment (isolated mechanical commit).** Per the locked plan:
- `data/reference/sic_to_sector_proxy.csv` (checked in, ~50 SIC prefix rows → 10 broad buckets).
- `src/aegis/data/sector_proxy.py::sector_for_sic(sic_code) -> tuple[sector, industry]`.
- Panel column rename: `gics_sector` → `sector_proxy`, `gics_industry` → `industry_proxy` in `StockDailyRow`, `_PANEL_COLUMNS`, `_finalize_panel`.
- ~5 tests in `tests/unit/test_sector_proxy.py`.
- Mechanical search-replace audit: `grep -rn "gics_" src tests` returns zero hits after the commit.

Estimated lift: ~400 lines including the CSV. Single commit. content_hash unchanged (panel column rename doesn't enter `_RESEARCH_IDENTITY_FIELDS`).
