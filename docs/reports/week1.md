# Week 1 Report

**Dates:** 2026-04-16 → 2026-04-23 (7 days)
**Status:** ✓ complete — all 7 Day targets met
**Spine claim:** *One command builds a point-in-time panel, computes 12-1 momentum exactly as specified in §8.1, stores the results as Parquet + SQLite artifacts, and logs the full run into a replayable ledger.* **Met.**

## Top-line numbers

| Metric | Value |
|---|---|
| Quality gates | ruff ✓ · ruff-format ✓ · mypy strict (26 files) ✓ |
| Unit tests | 103 passed, 6 xfail, 0 failed |
| Source-tree size | 26 `.py` files in `src/aegis/`, ~39 files incl. tests and configs |
| `content_hash()` | `093a33a5e150578cb3b42b63095254ce7d35d58209cf36b58e49db6d41a7b3d8` — unchanged from scaffolding through Day 7 |
| `data_snapshot_id` | `3de377ce555877f7…` — same across Day 3 and Day 6 runs (idempotent Polygon pull) |
| Live run wall time | ~5 minutes end-to-end on Polygon free tier |

## Day-by-day summary

| Day | Target | Result |
|---|---|---|
| 1 | Schemas + data config | `StockDailyRow`, `UniverseRow`, `FactorObservation`, `ResearchRecord`, `configs/data.yaml` landed. Added cross-platform `content_hash()` discipline (research identity excludes deployment paths). |
| 2 | Polygon loader + universe filter | `load_polygon_daily()` with 12.5s rate-limit sleep; `build_universe_flags()` with t-1 price discipline and deterministic fail_reason ordering. Live smoke against AAPL/MSFT/IBM passes. |
| 3 | Panel builder | `build_panel()` composes loader + filter + ret_1d + Parquet write. Real run: 3,664 rows over 2024-06 → 2026-03. `data_snapshot_id` stamped in every row. |
| 4 | Research ledger | `open_ledger` context manager, `register_experiment` / `_candidate` / `_artifact`, `current_git_sha` helper. Append-only interface with zero `update_*` / `delete_*` exports. |
| 5 | 12-1 momentum + transforms | `Momentum12m1m(Factor)` + `winsorize_cross_section` + `zscore_cross_section`. σ-algebra measurability proven by truncation-stability test. |
| 6 | Vertical-slice pipeline | `run_week1_slice()` + `aegis backtest week1` CLI. Live run: 1 experiment + 1 candidate + 2 artifacts written to `data/ledger.sqlite`. |
| 7 | Smoke notebook + README | `notebooks/week1_smoke_test.ipynb` with 6 cells over real artifacts; README rewritten around the Polygon pivot and the Week 1 quickstart. |

## Major pivots during Week 1

### 1. Data-source pivot: WRDS → Polygon.io (2026-04-23)
Rice's Jones School denied the WRDS application. Spec §7 had WRDS as primary and Sharadar ($300/mo) as backup; Polygon.io at free-tier (and $29/mo Starter for the upgrade path) was picked as the practical middle ground. Schema migration was substantive:

- Dropped `permno: int`, `share_code: int` — added `ticker: str`, `ticker_type: Literal[...]`.
- Replaced `wrds_loader.py` → `polygon_loader.py`; rewrote per-rule universe checks.
- Rebuilt the `stock_daily_panel` fixture with Polygon-flavored tickers (`T_PASS_NYSE`, `T_FAIL_SHARE`, …).
- `content_hash()` stayed at `093a33a5…` — schema change didn't touch research-identity fields.

Saved in memory: [project_data_source.md](../../C:/Users/timep/.claude/projects/c--Users-timep-OneDrive-Desktop-aegis-alpha-lab/memory/project_data_source.md) — reminds future sessions not to re-suggest WRDS.

### 2. `content_hash()` platform-invariance fix (Day 1 follow-up)
Early `content_hash()` included `DataConfig.paths`, which stored `pathlib.Path` values that serialize with platform-dependent separators (`data\raw` on Windows, `data/raw` on POSIX). Caught before any ledger rows were written; fixed by excluding `data.*` from the hash (research identity ≠ deployment layout). Verified Windows-host `093a33a5…` matches Linux-Docker `093a33a5…`.

### 3. CRSP corporate-action simplification
Spec §7 and the original Week 1 plan included a `corporate_actions.py` module using CRSP's `cfacpr`/`cfacshr` factors. Polygon serves adjusted prices directly via `adjusted=True`, so Day 3 didn't need a custom adjuster — `ret_1d = log(adj_close / adj_close.shift(1))` per ticker is the full story.

## Live pipeline output

From the Day 6 end-to-end run (`uv run aegis backtest week1`):

**Panel ([data/processed/daily_panel_week1.parquet](../../data/processed/daily_panel_week1.parquet))**
- 3,664 rows × 15 columns
- 8 tickers: AAPL, AMZN, GOOGL, JNJ, JPM, META, MSFT, NVDA
- Date range: 2024-06-03 → 2026-03-31 (458 trading days)
- 1,648 eligible rows (45% — rest are pre-252-day-history)
- `data_snapshot_id = 3de377ce555877f7…` stamped on every row

**Factor ([data/processed/factor_mom_12_1_week1.parquet](../../data/processed/factor_mom_12_1_week1.parquet))**
- Same 3,664 (date, ticker) shape
- 1,648 valid rows — exactly matches panel eligibility (factor compute is universe-agnostic by design)
- Per-date `zscore_value` mean = 0.000000 across all dates with ≥2 eligible tickers
- AAPL's 2026-03-31 raw 12-1 momentum = +0.195 (≈20% log return over March 2025 → March 2026 window, matches AAPL's actual price action)
- `feature_snapshot_id` stable across re-runs

**Ledger ([data/ledger.sqlite](../../data/ledger.sqlite))**
- 1 experiment (`week1_vertical_slice`, config_hash + git_sha stamped)
- 1 candidate (`mom_12_1`, formula `log(P[t-21] / P[t-252])`, status `computed`)
- 2 artifacts (`panel` + `factor`, each with SHA-256 checksum)
- Append-only: re-running generates new UUIDs but preserves `config_hash` and `data_snapshot_id`

## Quality discipline

- **97 Polygon-free unit tests**, **6 polygon-marked integration tests** (auto-skipped without `POLYGON_API_KEY`)
- **6 preserved xfails** — one per spec §6 module, encoding the roadmap machine-readably. `strict=True` fails CI if any module's code accidentally satisfies its acceptance test before the marker is removed.
- **Docker image builds clean** on Linux with `polygon-api-client` pinned; multi-stage, runs as non-root, `config_hash` matches host.
- **σ-algebra measurability** proven by truncation-stability test in [test_momentum.py](../../tests/unit/test_momentum.py) — no factor value at date t depends on any data at date > t.
- **Append-only ledger discipline** proven at the interface level: `store.__all__` contains no `update_*` or `delete_*`; a test asserts this.

## What's deferred (Week 2+)

| Item | Where | Why deferred |
|---|---|---|
| S&P 500 historical index membership | Week 2 | No Polygon-native source; needs Wikipedia scrape or manual CSV. Keeps Module A §6 acceptance `xfail`. |
| Delisted-ticker survivorship tracker | Week 2 | Polygon has the data via `/v3/reference/tickers?active=false`; tracker is ~half a day. Keeps Week 1 output flagged "not survivorship-bias-free". |
| Ledger replay engine | Week 2 | Day 4 lands the data model; Week 2 adds the executor that re-runs the pipeline from a ledger row and compares checksums. Keeps Module B §6 acceptance `xfail`. |
| Polygon fundamentals API | Week 2 | Needed for value composite + quality + accruals factors; Week 1 only needed prices. |
| Universe widening beyond 8 tickers | Week 2 + $29/mo Starter | Free tier's 5 calls/min bottlenecks at ~500 tickers (~5 hours). Upgrade pays for itself in one refresh. |
| Remaining ~39 factors from spec §8 | Weeks 9–12 | Module C schedule. Day 5 landed the first one end-to-end. |
| Barra-lite WLS + EWMA covariance | Weeks 6–8 | Module D. `xfail` on reconstructing mean style R² ≥ 0.25. |
| HAC IC, BH-FDR, DSR, FF6 α | Weeks 13–15 | Module E. `xfail` on BH-FDR of 1,000 null signals ≤ q=0.10. |
| Cost-aware QP optimizer | Weeks 16–18 | Module F. `xfail` on attribution residual ≤ 1 bp/day. |

## What Week 1 demonstrably does NOT claim

- **No IC, no Sharpe, no alpha.** Module E is what produces those numbers. Week 1's `status="computed"` is a statement of "factor computed without error" — not "factor has predictive power."
- **Not survivorship-bias-free.** The 8-ticker universe is hardcoded from a 2024 vantage point; all 8 were large-cap survivors. Week 2's delisted-ticker tracker fixes this.
- **Not a production research result.** The §11 Sharpe / MDD / turnover targets apply to the composite book with the full factor library, not to a single factor on a toy universe.

## Operational state

| Item | Value |
|---|---|
| Python | 3.11.15 (auto-installed by uv) |
| Venv | `.venv/` — 45+ packages incl. polygon-api-client 1.16.3 |
| uv | 0.11.7 at `C:\Users\timep\AppData\Roaming\Python\Python312\Scripts\uv.exe` |
| uv on PATH | User-PATH registry key (persistent across shells + VS Code restart) |
| Polygon | Free tier; API key in `.env` (gitignored) |
| Docker | Desktop 29.4.0; `aegis:dev` image builds and runs end-to-end |
| git SHA as of this report | `b28683ccbcef…` (matches ledger rows from Day 6 run) |

## Readiness for Week 2

**Ready:**
- Data + panel + ledger + first factor all running end-to-end against real Polygon.
- 103 tests green; 6 xfail milestones identified.
- CI, Docker, pre-commit all healthy.
- Content-hash discipline + data-snapshot-id discipline both live.

**Blocks for Week 2:**
- Polygon Starter ($29/mo) upgrade when universe widens beyond 8 tickers (likely Day 8-10 of Week 2).
- Wikipedia / manual S&P 500 index-history CSV for Module A §6 acceptance.
- Python-dotenv or pydantic-settings hooking for richer env management (minor).

**Next recommended deliverable:** Week 2 Day 1 = widen ticker universe and land the first fundamentals-based factor (value composite, since `mom_12_1` is price-only). That forces the Polygon fundamentals endpoint, the delisted-ticker tracker, and the historical-index scraper all into the same week.
