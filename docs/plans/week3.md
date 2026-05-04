# Week 3 — Fundamentals + sector proxy + multi-factor pipeline

> **Dates:** 2026-05-04 → 2026-05-10 (Days 15–21)
> **Spine claim:** *The pipeline integrates Polygon's PIT fundamentals endpoint, computes a fundamentals-based factor (`earnings_yield`) PIT-correctly end-to-end, replaces the `gics_*` placeholder columns with `sector_proxy` / `industry_proxy` derived from SIC, and runs N factors per slice with one ledger candidate per factor.*

## Week 3 priorities

Three workstreams, each must-land:

1. **Polygon fundamentals** — PIT-correct revenue / earnings / book / cash-flow per ticker, walked over the same date window the panel already covers. PIT discipline is *filing-date*-driven (not period_of_report_date).
2. **Sector enrichment** — SIC → coarse-sector mapping (~10 broad buckets). Panel column rename `gics_*` → `sector_proxy` / `industry_proxy` lands in **one isolated mechanical commit** before the multi-factor refactor.
3. **Multi-factor pipeline + first fundamentals factor** — `earnings_yield` (TTM net_income / mcap) end-to-end, plus `_run_factor_slice(factors=[…])` accepting an N-factor list. One experiment, N candidates, 2N artifacts (panel + factor per candidate), per-factor diagnostics in parquet metadata.

The full **value composite** (V_{i,t} = ¼ [z(B/P) + z(E/P) + z(S/P) + z(CF/P)] from §8.1) is **deferred to Week 4** — it's structurally trivial once one yield-ratio factor works end-to-end.

## Locked decisions (from Day 14 plan review)

| Question | Choice | Justification |
|---|---|---|
| Scope | One factor + multi-factor refactor + sector_proxy. **Defer full value composite to Week 4.** | The hard part isn't writing four yield formulas; it's making the fundamentals pipeline PIT-correct, robust to missing reports, and compatible with the ledger and multi-factor runner. Build the plumbing solid; the composite is mechanical follow-up. |
| Factor naming | `earnings_yield` (NOT `e_p`). | Readable in logs and reports. Record `formula_string="ttm_net_income / mcap (E/P)"` in the ledger so the spec notation is preserved. |
| Aggregation window | TTM (trailing twelve months). | TTM updates more frequently than annual and avoids near-stale-for-a-year factor values. The fixture-based fiscal-year-boundary tests are mandatory mitigation. |
| Formula form | `ttm_net_income / mcap` (NOT `ttm_net_income / shares_out / adj_close`). | Cleaner, not split-adjustment-sensitive. An explicit equivalence test asserts the two forms agree within float tolerance on the engineered fixture. |
| `invalid_reason` enum | Add as 9th column on `FactorObservation` (alongside `valid_flag`). Bumps factor parquet shape to (rows, 9). | A boolean `valid_flag=False` without a reason makes diagnostics hard. Distinguish: missing-fundamentals, insufficient-quarters, zero-or-negative-denominator, raw-factor-nan, universe-or-history-ineligible. |
| Per-factor diagnostics | Embedded in parquet's pyarrow key-value metadata (NOT a separate sidecar artifact). | One artifact per factor stays one artifact; sha256_file still verifies the diagnostics; verify-mode replay coverage doesn't change. |
| CLI factor defaults | `aegis backtest week1` defaults to `[mom_12_1]` (back-compat). `aegis backtest full` defaults to `[mom_12_1, earnings_yield]`. | Both accept `--factors mom_12_1,earnings_yield,…` to override. Resolved factor list is recoverable from the ledger (one candidate row per factor) — no need for a slice-level diagnostics artifact. |
| Sector mapping granularity | ~10 broad buckets. NOT 24-bucket pseudo-GICS. | This is explicitly a *proxy*. False precision is worse than honest coarseness. Real GICS lands with Barra-lite (Week 6+). |
| `content_hash` drift | Yes — adding `earnings_yield` to factors.yaml is a research-identity change. | Capture the new hash in the Week 3 report and `project_aegis.md` memory. Run one fresh candidate after the move and verify it cleanly under the new hash (don't rely only on old candidates reporting `config_hash_match=False`). |
| Ledger code changes | None. | Multi-factor uses the existing `register_experiment` / `register_candidate` / `register_artifact` interface. N candidates per slice, 2 artifacts per candidate (panel + factor). No new artifact types, no Experiment-table additions. |

## Spec compliance audit

The proposal's foundational principles + relevant §-sections that touch Week 3:

| Spec / principle | What it requires | How Week 3 honors it |
|---|---|---|
| **§4.1 σ-algebra measurability** | f_{i,t} ∈ F_t — no future information leaks. | `fundamentals_at(ticker, date, df)` returns the latest row whose `filing_date < date` (NOT `period_of_report_date`). σ-algebra regression test: truncating the fundamentals frame to `filing_date <= t` produces identical factor outputs at t. |
| **§8 cross-sectional 1%/99% winsorize + z-score** | Per-date winsorize then z-score. | Existing `winsorize_cross_section` + `zscore_cross_section` operators (Day 5). No new operator code. |
| **§8.1 Value composite (deferred)** | V_{i,t} = ¼ [z(B/P) + z(E/P) + z(S/P) + z(CF/P)] | Week 3 ships first input (`earnings_yield`); Week 4 composes the four. Each input lands as its own factor with its own ledger candidate so composite provenance walks back through individual yield-ratios. |
| **Principle 5 — Auditability** | Every promoted candidate has a full ledger trail. | One experiment per slice, N candidates per slice. Each candidate owns its panel + factor artifact (artifact rows duplicated across candidates pointing at the same panel file with same sha — tiny ledger cost, cleaner verify-mode semantics). |
| **Terminology discipline** | "sector_proxy" / "industry_proxy" — NOT "gics_*". Factor formulas readable in reports. | Panel columns renamed in Day 18's isolated mechanical commit. Factor `formula_string` records the spec notation explicitly. |

## Workstream A — Polygon fundamentals plumbing (Days 15-16)

### Day 15 — scraper + checked-in fundamentals snapshot

**Build**
- `scripts/fetch_polygon_fundamentals.py` — pulls quarterly + annual financial reports for every ticker in `sp500_membership.csv` over the last ~5 years. Tolerant per-ticker error handling (mirrors Day 9): skip with logged warning if Polygon returns NOT_FOUND or empty. Records `coverage_failed` in the meta sidecar.
- Schema (`FundamentalsRow` Pydantic in `src/aegis/data/schema.py`):
  - `ticker: str`, `filing_date: date`, `period_of_report_date: date`, `period_end_date: date`
  - `period_kind: Literal["quarterly", "annual"]`
  - `revenues: float | None`, `net_income: float | None`
  - `eps_basic: float | None`, `eps_diluted: float | None`
  - `weighted_avg_shares_basic: float | None`, `weighted_avg_shares_diluted: float | None`
  - `common_equity: float | None`, `total_assets: float | None`
  - `operating_cash_flow: float | None`
- `data/reference/fundamentals.parquet` (gitignored, regenerable). ~640 tickers × ~20 quarterly reports ≈ ~12,800 rows.
- `data/reference/fundamentals.meta.json` provenance sidecar: source URL, fetched_at_utc, scraper_git_sha, parquet_sha256, row_count, coverage_failed (list of tickers that returned no fundamentals), api_calls_made.

**Acceptance:** parquet exists, ≥10,000 rows, ≥640 unique tickers covered, meta sidecar's `parquet_sha256` matches on-disk file.

### Day 16 — PIT lookup helpers

**Build** — `src/aegis/data/fundamentals.py`:
- `load_fundamentals(path) -> pd.DataFrame` — column-validated read.
- `fundamentals_at(ticker, date, df) -> pd.Series | None` — latest row whose `filing_date < date`. Returns None if no PIT-eligible report exists for that ticker.
- `ttm_at(ticker, date, df, field) -> float | None` — sum of last 4 *quarterly* values of `field` whose `filing_date < date`. Returns None if fewer than 4 quarterly reports are PIT-eligible.
- `coverage_window(start, end, df) -> pd.DataFrame` — long-format `(date, ticker, has_pit_fundamentals)` over a date range; useful for diagnostics.
- `fundamental_lag_days(ticker, date, df) -> int | None` — `(date - filing_date).days` for the row `fundamentals_at` would return.

**Tests** — `tests/unit/test_fundamentals.py` (~6 tests):
- `test_fundamentals_at_returns_latest_filed_before_date`
- `test_fundamentals_at_excludes_period_of_report_date_after_filing_date` (PIT discipline regression — engineered case where filing_date is mid-quarter and period_of_report_date is later)
- `test_fundamentals_at_is_filtration_measurable` (truncation-stability; mirrors the σ-algebra patterns from Day 8/9)
- `test_ttm_at_sums_exactly_four_quarterly_reports`
- `test_ttm_at_returns_none_when_fewer_than_four_quarters_pit_available`
- `test_ttm_at_handles_fiscal_year_boundary` (engineered fixture: ticker with FY ending in March; assert Q1 of new fiscal year + Q4-Q3-Q2 of old fiscal year produce a sensible TTM)

## Workstream B — Sector enrichment (Day 18, **isolated commit**)

This is one mechanical refactor commit, landed **before** the multi-factor refactor on Day 19. Single search-replace audit beforehand.

**Build**
- `data/reference/sic_to_sector_proxy.csv` — hand-curated 10-bucket mapping (checked in). Columns: `(sic_prefix, sector_proxy, industry_proxy)`.
- `src/aegis/data/sector_proxy.py::sector_for_sic(sic_code: str | None) -> tuple[str, str]` — returns `(sector_proxy, industry_proxy)` or `("Unknown", "Unknown")` if SIC is None or unmapped.

**The 10 buckets (proxies, NOT GICS):**
| sector_proxy | SIC prefix range | Examples |
|---|---|---|
| Energy | 13xx | XOM, CVX, COP |
| Materials | 10-14, 24-32 | DOW, FCX, NEM |
| Industrials | 15-17, 33-39, 40-47 | CAT, BA, GE, RTX |
| Consumer Discretionary | 50-59 (excl. food/drug) | AMZN, HD, NKE |
| Consumer Staples | 20-21 (food/bev), 54 (food retail) | PG, KO, WMT |
| Health Care | 28xx (pharma), 80xx (services) | JNJ, PFE, UNH |
| Financials | 60-67 | JPM, BAC, BRK.B |
| Information Technology | 35-36 (computers/electronics), 73 (software/services) | AAPL, MSFT, NVDA |
| Communication Services | 48xx (telecom), 78-79 (entertainment) | VZ, T, NFLX |
| Utilities | 49xx | NEE, SO, DUK |
| (Real Estate folded into Financials for the proxy; honest GICS arrives with Barra-lite) |  |  |

**Modify (mechanical rename in this commit):**
- `src/aegis/data/schema.py::StockDailyRow` — rename `gics_sector` → `sector_proxy`, `gics_industry` → `industry_proxy`.
- `src/aegis/data/panel.py::_PANEL_COLUMNS` — same rename.
- `src/aegis/data/panel.py::_finalize_panel` — populate `sector_proxy` / `industry_proxy` from `ticker_metadata.sic_code` via `sector_for_sic` (joining ticker_metadata into the panel).
- `tests/unit/test_panel.py`, `test_schema.py`, `test_full_pipeline.py`, `test_week1_pipeline.py` — search-replace `gics_sector` → `sector_proxy`, `gics_industry` → `industry_proxy`. Update existing assertions (`gics_columns_are_null_in_week1` becomes `sector_columns_are_proxy_typed_in_week1` or similar).
- `tests/conftest.py::pipeline_fixture` — already emits None-valued sector columns; no behavioral change, just rename.

**Tests** — `tests/unit/test_sector_proxy.py` (~5 tests):
- `test_sector_for_sic_known_codes` — 10 representative codes map correctly.
- `test_sector_for_sic_none_returns_unknown_unknown`.
- `test_sector_for_sic_unmapped_prefix_returns_unknown_unknown`.
- `test_sic_mapping_is_exhaustive_over_ticker_metadata` — every non-null SIC in `ticker_metadata.parquet` maps to a non-Unknown sector_proxy. (Skipif parquet not present.)
- `test_panel_sector_columns_populated_from_metadata` — end-to-end: pipeline_fixture's panel has sector_proxy/industry_proxy populated from the ticker_metadata join.

**Acceptance:** all existing tests pass after the rename. No `gics_*` references remain in `src/aegis/` or `tests/`. `_PANEL_COLUMNS` still has 15 columns, just renamed.

## Workstream C — First fundamentals factor + multi-factor pipeline (Days 17, 19, 20)

### Day 17 — `EarningsYield(Factor)` end-to-end

**Build** — `src/aegis/features/value.py::EarningsYield(Factor)`:
- `name = "earnings_yield"`
- `formula = "ttm_net_income / mcap (E/P)"`
- `lookback_days = 365` (need 4 quarters of fundamentals)
- `compute(panel, fundamentals) -> pd.DataFrame` returning `FactorObservation` rows.
- Per-row computation: for each `(date, ticker)`:
  1. `ttm_ni = ttm_at(ticker, date, fundamentals, "net_income")` → `invalid_reason="missing_fundamentals"` if None or `invalid_reason="insufficient_quarters"` if fewer than 4 PIT quarterlies.
  2. `mcap = panel.mcap` — if None or ≤0, `invalid_reason="invalid_denominator"`.
  3. `raw = ttm_ni / mcap` — if NaN or inf, `invalid_reason="raw_factor_nan"`.
  4. Cross-sectional 1%/99% winsorize per date → `winsorized_value`.
  5. Cross-sectional z-score per date → `zscore_value`.
- New `configs/factors.yaml` entry for `earnings_yield`. **Moves `content_hash`** — capture the new value, log to Week 3 report and memory.

**`FactorObservation` schema change** — add 9th column `invalid_reason: str | None`.
- Possible values: `"missing_fundamentals"`, `"insufficient_quarters"`, `"invalid_denominator"`, `"raw_factor_nan"`, `"universe_ineligible"`, `"history_ineligible"`, `None` (when valid).
- `valid_flag = (invalid_reason is None)` — kept for back-compat.
- The mom_12_1 factor needs a small update to populate `invalid_reason` (`"history_ineligible"` for the first 252 rows per ticker, `None` otherwise).
- Day 13 acceptance test threshold updates: `factor.shape == (rows, 9)`.

**Per-factor diagnostics in parquet metadata** — `Factor.diagnostics(factor_out, fundamentals_or_none) -> dict`:
- For `earnings_yield`: median / p90 / max `fundamental_lag_days` across all PIT-eligible rows; count by `invalid_reason`.
- For `mom_12_1`: just the `invalid_reason` count by category (no fundamentals so no lag stats).
- Written to parquet via `pyarrow.Table.replace_schema_metadata({b"factor_diagnostics": json.dumps(diag).encode()})`.
- A small helper `read_factor_diagnostics(parquet_path) -> dict` extracts them. Useful for Week 3 report's diagnostics section.

**Tests** — `tests/unit/test_earnings_yield.py` (~8 tests):
- `test_earnings_yield_formula_correctness` — engineered fundamentals + panel: 1 ticker, 1 date, hand-verify the math.
- `test_earnings_yield_two_formulas_agree` — assert `ttm_ni / mcap == ttm_ni / shares_out / adj_close` within `1e-9` tolerance on the engineered fixture (split-adjustment equivalence).
- `test_earnings_yield_per_date_zscore_mean_zero` — across all eligible dates with ≥2 valid tickers.
- `test_earnings_yield_is_filtration_measurable` — truncate fundamentals to `filing_date <= t`, output unchanged at t.
- `test_earnings_yield_missing_fundamentals_marks_invalid_reason` — engineered ticker with no fundamentals → `invalid_reason="missing_fundamentals"`, not just `valid_flag=False`.
- `test_earnings_yield_insufficient_quarters_marks_invalid_reason` — engineered ticker with only 2 quarterly reports → `invalid_reason="insufficient_quarters"`.
- `test_earnings_yield_zero_mcap_marks_invalid_reason` — engineered zero-mcap row.
- `test_earnings_yield_diagnostics_in_parquet_metadata` — round-trip via pyarrow read_metadata.

### Day 19 — Multi-factor pipeline refactor

**Build** — `src/aegis/backtest/_common.py`:
- `_run_factor_slice(cfg, ledger_path, *, tickers, experiment_name, factors=None, sleep_between_calls, panel_filename=None, factor_filename_template=None, ...)`. Default `factors=(Momentum12m1m(),)` for back-compat with Week 1.
- New `MultiFactorSliceResult` frozen dataclass with `experiment_id` + `candidates: tuple[CandidateSliceResult, ...]`. Each `CandidateSliceResult` carries the per-factor info that the old `SliceResult` had (candidate_id, factor name, factor_path, factor_checksum, panel_artifact_id, factor_artifact_id, panel_rows, factor_valid_rows).
- `SliceResult` retained as a type alias for the single-factor case (`MultiFactorSliceResult` with `len(candidates) == 1`); back-compat shim returns the first candidate's fields.
- Each candidate gets registered with: panel artifact (same path + same sha across all N candidates) + its own factor artifact. Total: 1 experiment + N candidates + 2N artifacts.
- Factor parquet filename template: `factor_<factor_name>_<slice_tag>.parquet`.

**Modify**
- `src/aegis/backtest/week1.py::run_week1_slice` — passes `factors=(Momentum12m1m(),)`, returns `MultiFactorSliceResult`.
- `src/aegis/backtest/full.py::run_full_slice(cfg, ledger_path, sample_date, *, factors=None, ...)` — default `factors=(Momentum12m1m(), EarningsYield())`. Loads `fundamentals.parquet` from `cfg.data.paths.reference`.
- `src/aegis/cli.py` — both `backtest week1` and `backtest full` accept `--factors mom_12_1,earnings_yield`. Comma-separated string parsed via a small `FACTOR_REGISTRY: dict[str, type[Factor]]`. CLI prints one summary block per candidate after the run.

**Tests** — `tests/unit/test_full_pipeline.py` updates + `tests/unit/test_multi_factor.py` (new, ~4 tests):
- `test_multi_factor_slice_writes_per_factor_parquets`
- `test_multi_factor_slice_ledger_layout_one_experiment_n_candidates_2n_artifacts`
- `test_multi_factor_slice_each_candidate_independently_verifiable_via_replay`
- `test_multi_factor_slice_factor_order_deterministic` — shuffling the input list doesn't change which factor gets which UUID.

Existing 3 Day-13 tests update to assert the new (1, N, 2N) ledger shape and the (rows, 9) factor parquet shape.

### Day 20 — Live multi-factor full-slice run

**Build / verify**
- `aegis backtest full --date 2025-06-15 --factors mom_12_1,earnings_yield` end-to-end against Polygon Starter.
- ~503 S&P members × ~458 trading days × 2 factors. Wall time ~22 min for the panel pull (one-time cost; factor compute is sub-second per factor).
- New ledger layout: 1 experiment + 2 candidates + 4 artifacts (1 panel registered to each of 2 candidates + 2 factor parquets).
- `aegis ledger replay <candidate_id>` for each candidate independently — both should report `all_ok=True` (under the new `content_hash`).

**Acceptance**
- New experiment row stamped under the new `content_hash` (post-`earnings_yield`-addition).
- Both candidates' verify-mode replay returns `all_ok=True` (artifacts intact + config_hash_match=True under the new hash + git_sha_available=True).
- earnings_yield's per-factor diagnostics (median fundamental_lag_days, p90, max, dropped-by-reason) recorded in the parquet metadata.
- Old candidates (`79431a46-…`, `0bb9e71c-…`, `93508ea1-…`) continue to report `config_hash_match=False` against the new hash — that's the principle 5 contract working as designed.

## Day 21 — Week 3 report

**Build** — `docs/reports/week3.md` mirrors `week2.md`:
- Top-line numbers: pytest count (~170 passed, 4 xfailed); new `content_hash`; Week 3's two new ledger candidates.
- Day-by-day summary (Days 15-21).
- Major learnings (PIT filing-date discipline gotchas, TTM aggregation surprises, SIC mapping coverage, multi-factor refactor decisions).
- Terminology restated: "sector_proxy" not "GICS"; `earnings_yield` not "alpha"; "TTM" framing.
- Deferred items for Week 4+: the 3 remaining yield ratios + the value composite.

**Modify**
- `README.md` Module table: C goes from "🟡 1/~40 factors landed" → "🟡 2/~40 factors landed" with a note about the multi-factor pipeline.

## Files to change

### Create (must land)
| Path | Day | Purpose |
|---|---|---|
| `scripts/fetch_polygon_fundamentals.py` (~200 lines) | 15 | Polygon vx fundamentals scraper, ticker-by-ticker, tolerant errors |
| `data/reference/fundamentals.parquet` | 15 | Gitignored cache (`.parquet` rule) |
| `data/reference/fundamentals.meta.json` | 15 | Gitignored provenance sidecar (per `/data/*` ignore rule) |
| `src/aegis/data/fundamentals.py` (~120 lines) | 16 | `load_fundamentals`, `fundamentals_at`, `ttm_at`, `coverage_window`, `fundamental_lag_days` |
| `src/aegis/features/value.py` (~120 lines) | 17 | `EarningsYield(Factor)` (Week 4 will add `BookYield` / `SalesYield` / `CashFlowYield` / `ValueComposite` here) |
| `data/reference/sic_to_sector_proxy.csv` | 18 | Hand-curated ~10-bucket SIC → sector mapping (checked in) |
| `src/aegis/data/sector_proxy.py` (~60 lines) | 18 | `sector_for_sic` |
| `tests/unit/test_fundamentals.py` (~150 lines) | 16 | 6 tests for the PIT helpers |
| `tests/unit/test_earnings_yield.py` (~150 lines) | 17 | 8 tests for the factor + diagnostics + dual-formula equivalence |
| `tests/unit/test_sector_proxy.py` (~80 lines) | 18 | 5 tests for the mapping |
| `tests/unit/test_multi_factor.py` (~100 lines) | 19 | 4 tests for the multi-factor pipeline |
| `tests/integration/test_polygon_fundamentals.py` (polygon-marked) | 15 | 2-3 live tests, auto-skipped without API key |
| `docs/reports/week3.md` (~250 lines) | 21 | Week 3 formal report |

### Modify (must land)
| Path | Day | Change |
|---|---|---|
| `configs/factors.yaml` | 17 | Add `earnings_yield` FactorSpec. **Moves `content_hash`.** |
| `src/aegis/data/schema.py` | 17, 18 | (17) Add `FundamentalsRow` Pydantic; add `FactorObservation.invalid_reason: str \| None`. (18) Rename `StockDailyRow.gics_*` → `sector_proxy`/`industry_proxy`. |
| `src/aegis/data/panel.py` | 18 | `_PANEL_COLUMNS` rename; `_finalize_panel` populates sector_proxy / industry_proxy from ticker_metadata join. |
| `src/aegis/features/momentum.py` | 17 | Update `Momentum12m1m.compute` to populate `invalid_reason` ("history_ineligible" / None). |
| `src/aegis/features/base.py` | 17 | Add optional `Factor.diagnostics(factor_out, *args) -> dict` method (default returns empty dict). |
| `src/aegis/backtest/_common.py` | 19 | `_run_factor_slice(factors=…)`; new `MultiFactorSliceResult`; `SliceResult` becomes a single-factor alias. |
| `src/aegis/backtest/full.py`, `src/aegis/backtest/week1.py` | 19 | Pass factor list explicitly. Default factor sets per locked decisions. |
| `src/aegis/cli.py` | 19 | `--factors` flag on both `backtest week1` and `backtest full`. `FACTOR_REGISTRY` dict. |
| Existing tests touched by sector rename + multi-factor + invalid_reason | 17, 18, 19 | Mechanical updates; details in each day's test list |
| `README.md` | 21 | Module C row: 1/~40 → 2/~40 + multi-factor note |

### Not modified (explicitly)
- `src/aegis/ledger/` — verify-mode replay works as-is for any `(candidate, artifact)` shape. No new artifact_type, no Experiment-table additions.
- `src/aegis/data/index_membership.py`, `src/aegis/data/ticker_reference.py` — Week 2's universe-realism layer is untouched.
- `data/reference/sp500_membership.csv`, `ticker_aliases.csv`, `ticker_metadata.parquet` — unchanged.
- `aegis.config._RESEARCH_IDENTITY_FIELDS` — unchanged. Adding factors is research-identity drift via `factors.yaml` content; no new top-level field.

## Verification gates

End-of-week (must all pass):

1. **Quality gates clean:**
   ```bash
   uv run pytest -m "not polygon"           # ~170 passed, 4 xfailed
   uv run ruff check src tests scripts      # clean
   uv run ruff format --check src tests scripts
   uv run mypy src                          # clean across ~32 source files
   uv run pre-commit run --all-files
   ```

2. **`content_hash` drift captured.** New value (post-`earnings_yield`-addition) recorded in:
   - `docs/reports/week3.md` top-line table
   - `project_aegis.md` memory file
   - The Day 17 commit message that adds `earnings_yield` to factors.yaml
   ```bash
   uv run python -c "from aegis.config import load_all; print(load_all().content_hash())"
   ```

3. **Fresh-candidate verify under the new hash.** The Day 20 multi-factor live run produces 2 new candidates; `aegis ledger replay <candidate_id>` for each returns `all_ok=True` under the new hash. (Per the locked-decision requirement: don't rely only on old candidates reporting `config_hash_match=False`.)

4. **Old candidates flip cleanly.** All 3 prior candidates (`79431a46-…`, `0bb9e71c-…`, `93508ea1-…`) continue to report `config_hash_match=False` against the new hash but `artifacts_failed=[]`. Principle 5 contract intact.

5. **Sector-rename doesn't regress Week 1/2.** Week 1's `aegis backtest week1` and Week 2's `aegis backtest full` still produce healthy panels with the renamed columns.

6. **Module A acceptance still passes.** `test_sp500_reconstruction_within_1_name` continues to pass on the two ground-truth dates after the sector enrichment touches `_finalize_panel`.

7. **Module B verify-mode acceptance still passes.** `test_promoted_factor_replays_bit_identical` continues to pass; the new `MultiFactorSliceResult` shape doesn't break the existing test.

## Risks and fallbacks

1. **Polygon vx fundamentals API quirks.** The `vx` namespace is experimental; response shape may shift. Mitigation: pin `polygon-api-client>=1.16.3` (already done); tolerant per-ticker error handling; integration test for schema round-trip; record `coverage_failed` per ticker in the meta sidecar.

2. **TTM aggregation correctness — biggest bug surface.** Walking 4 quarterlies is fiddly: fiscal-year boundaries, restated quarters, missing reports, leap-year boundaries. Mitigation per locked-plan suggestion: **engineered unit fixtures over live-API tests.** Fixtures cover: FY-end transition (Q1 of new FY + Q4 of old FY summed correctly), missing Q3 (returns None per the "exactly four required" rule), 5-quarter ticker (uses latest 4, ignores oldest), restated quarter (uses the restated value because filing_date is later).

3. **SIC mapping coverage.** Some `ticker_metadata` rows have null `sic_code` (older tickers). Mitigation: `sector_for_sic(None) = ("Unknown", "Unknown")` with a logged warning; the panel column populates with the fallback rather than failing. Test asserts the fallback path.

4. **`gics_*` → `sector_proxy` rename collateral damage.** Many test files reference these column names. Mitigation per locked-plan suggestion: **isolated mechanical commit on Day 18, BEFORE the multi-factor refactor.** Pre-commit search-replace audit: `grep -rn "gics_" src tests` returns zero hits (other than docstrings explaining the proxy/honest-GICS distinction).

5. **`FactorObservation` schema bump (8 → 9 columns) breaks Day 13 acceptance test.** Mitigation: update the test's threshold in the same commit that adds `invalid_reason` (Day 17). Doc-comment on the test pins the column count to `_FACTOR_OBSERVATION_COLUMNS` so future schema changes have one place to update.

6. **Multi-factor refactor creep.** Mitigation per locked-plan suggestion: **keep it minimal.** One experiment, N candidates, 2N artifacts (panel + factor per candidate), deterministic order, replay works for each candidate independently. No new ledger code, no new artifact_types, no Experiment-table metadata column.

7. **Live full-slice wall time on multi-factor.** The factor compute itself is sub-second per factor; the ~22-min cost is the Polygon panel pull, which is shared across factors. So 2-factor wall time ≈ 1-factor wall time + a few seconds. No risk.

## Out of scope (explicit)

- **`BookYield`, `SalesYield`, `CashFlowYield` factors.** Week 4. Each is structurally identical to `EarningsYield` once Week 3's plumbing exists.
- **`ValueComposite(Factor)`** (the §8.1 4-input z-score-and-average). Week 4. Trivial composition.
- **Real GICS codes.** Week 6+ Barra-lite. Paid license.
- **Module C IC + neutralization machinery.** Weeks 9-12 (Spec §12 timeline). The Module C `xfail` (`test_12m1m_momentum_reference_ic_within_0_005`) needs Module E's HAC IC infrastructure (Weeks 13-15).
- **Barra-lite WLS + EWMA covariance.** Weeks 6-8. Module D.
- **HAC IC, BH-FDR, DSR, FF6 α, decay gate.** Weeks 13-15. Module E.
- **Cost-aware QP optimizer.** Weeks 16-18. Module F.
- **Full-listing universe (~3,000 tickers).** Later week. The S&P 500 stepping-stone holds.

## Definition of done

Must-land (Week 3 succeeds with all eleven):

- [ ] `data/reference/fundamentals.parquet` generated, ≥10,000 rows, ≥640 unique tickers (gitignored).
- [ ] `data/reference/fundamentals.meta.json` provenance sidecar (gitignored), `parquet_sha256` matches.
- [ ] `data/reference/sic_to_sector_proxy.csv` checked in, 10-bucket mapping covers every observed SIC code in `ticker_metadata.parquet`.
- [ ] `src/aegis/data/fundamentals.py` exposes `load_fundamentals`, `fundamentals_at`, `ttm_at`, `coverage_window`, `fundamental_lag_days`.
- [ ] `src/aegis/data/sector_proxy.py` exposes `sector_for_sic`.
- [ ] `src/aegis/features/value.py::EarningsYield` exposes `name`, `formula`, `lookback_days`, `compute`, `diagnostics`.
- [ ] Panel columns renamed: `gics_sector` → `sector_proxy`, `gics_industry` → `industry_proxy`. No `gics_*` references remain in `src/aegis/` or `tests/`.
- [ ] `_run_factor_slice` accepts `factors: list[Factor]`. `MultiFactorSliceResult` shape works.
- [ ] CLI: `aegis backtest full --date 2025-06-15` runs end-to-end with default factors `[mom_12_1, earnings_yield]`. Produces 1 experiment + 2 candidates + 4 artifacts.
- [ ] `aegis ledger replay <new_candidate_id>` returns `all_ok=True` for both new candidates under the new `content_hash`.
- [ ] All 3 prior candidates continue to report `config_hash_match=False` (old hash) but `artifacts_failed=[]`.
- [ ] pytest count ≥ 170 passed, 4 xfailed, 0 failed.
- [ ] ruff + format + mypy + pre-commit clean.
- [ ] `docs/reports/week3.md` written with truthful numbers including the new `content_hash`.

## Readiness gate for Week 4

Week 4 brings the remaining 3 yield ratios + the value composite, all on top of Week 3's plumbing. Gate passes when:

1. `EarningsYield().compute(panel, fundamentals)` produces a clean factor parquet with sensible diagnostics.
2. `_run_factor_slice(factors=[mom_12_1, earnings_yield])` produces independently-verifiable candidates.
3. `fundamentals_at` and `ttm_at` are PIT-correct under the σ-algebra regression test.

If those three hold, Week 4 is mechanical: 3 new factor classes (BookYield, SalesYield, CashFlowYield) + 1 composite (ValueComposite) + factors.yaml entries. No new infrastructure.
