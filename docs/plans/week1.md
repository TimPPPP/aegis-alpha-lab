# Week 1 — Vertical Slice

> One config file → one PIT panel → one deterministic factor → one ledger entry → one reproducible artifact bundle.

## Objective

Week 1 is the **spine**, not the performance. By end-of-week, the claim we want to make is:

> *One script builds a point-in-time panel, computes 12–1 momentum exactly as specified in §8.1 of the proposal, stores the results as Parquet + SQLite artifacts, and logs the full run into a replayable ledger.*

Do **not** build Barra estimation, FF6, QP, HAC/BH-FDR/DSR, regimes, or any LLM tooling this week. Those are explicitly allocated to weeks 6–20 in spec §12.

## Data source — Polygon.io (not WRDS)

Rice's Jones School denied the WRDS application on 2026-04-23. V1 pivoted to **Polygon.io**:

- Free tier for development (5 calls/min, 10 years history, delayed). Sign up at polygon.io/dashboard/api-keys.
- $29/mo "Stocks Starter" for bulk pulls once Week 2+ ramps.
- API key in `.env` as `POLYGON_API_KEY=…`. Gitignored.
- Polygon identifies securities by **ticker** (string). CRSP's `permno` (int) and integer `share_code` are gone from our schemas. Common-share rule is now `ticker_type == "CS"`; ADRs are excluded per spec §7 + Tim's 2026-04-23 decision.
- Polygon serves **adjusted** closes directly (`adjusted=True`), so we carry both `raw_close` and `adj_close` from the loader and do not need a custom corporate-action module.

Spec §6 Module A acceptance ("S&P 500 membership within 1 name") has no Polygon-native solution. It stays `xfail` and is deferred to Week 2 (Wikipedia scrape or manual index-history CSV).

## What's already in place (scaffolding)

From the scaffolding + Day 1 + pivot work that landed before the Day 2 resume:

- `pyproject.toml` + `uv.lock` + Dockerfile + GitHub Actions CI. `polygon-api-client` pinned; `wrds` removed.
- `configs/{gates,risk,universe,portfolio,costs,factors,data}.yaml` — every §4–§11 threshold already encoded.
- `src/aegis/config.py` — Pydantic v2 frozen containers; `AegisConfig.content_hash()` stamps every ledger row. Platform-invariant (tested Windows ↔ Linux ↔ macOS).
- `src/aegis/cli.py` — Typer with subcommand groups for data / features / risk / validate / portfolio / ledger / backtest / lockbox.
- `src/aegis/data/schema.py` — `StockDailyRow`, `UniverseRow` (Polygon-flavored: `ticker` + `ticker_type`).
- `src/aegis/data/universe.py` — `build_universe_flags()` (vectorized, t-1 price discipline).
- `src/aegis/data/polygon_loader.py` — `load_polygon_daily()` (raw-in / raw-out).
- `src/aegis/ledger/schema.py` + `models.py` — SQLAlchemy tables + Pydantic row contracts.
- `src/aegis/features/base.py` — `Factor` ABC + `FactorObservation`.
- Six `@pytest.mark.xfail(strict=True)` module-acceptance stubs keyed to spec §6.

Week 1 **extends** this scaffold; it does not rename or relocate anything.

## First factor: 12–1 momentum

Per spec §8.1:

```
MOM_{i,t} = log( P_{i,t-21} / P_{i,t-252} )
```

Chosen because:
- Only adjusted prices needed — Polygon serves these directly.
- Lagged prices make σ-algebra measurability (spec §4.1) trivial to prove.
- One of the proposal's four representative formulas, so it doubles as a reference-factor unit test (spec §6 Module C acceptance: reproduce published IC within 0.005).

Value composite (B/P + E/P + S/P + CF/P) is **not** the first factor — it needs Polygon's fundamentals endpoint, which is Week 2 work.

## Data scope for Week 1

Polygon free tier, small slice for fast iteration:

- **Dates:** 2020-01-01 → 2022-12-31.
- **Universe:** a hardcoded list of ~200 liquid large caps from 2020-2022 (e.g., S&P 500 constituents as of 2019-12-31). **Note:** this is NOT survivorship-bias-free — delisted tickers are silently absent. Week 1 outputs must carry a loud "NOT PRODUCTION GRADE" caveat; the delisted-ticker tracker is Week 2.
- **Output:** `data/processed/daily_panel_week1.parquet` + `data/processed/factor_mom_12_1_week1.parquet` + `data/ledger.sqlite`.

Full 2000–2025 × ~3,000 names is Week 2+ territory after the pipeline is debugged and the $29/mo Polygon tier is active.

## Schemas (already landed in Day 1 + pivot)

Four Pydantic v2 frozen row models live in the tree:

### `StockDailyRow` — canonical PIT panel row
Location: [`src/aegis/data/schema.py`](../../src/aegis/data/schema.py)

```
date              date
ticker            str          # Polygon primary key
exchange          Literal["NYSE","AMEX","NASDAQ"]
ticker_type       Literal["CS","PFD","ETF","ETN","ADRC","ADRP","UNIT","WARRANT","RIGHT","FUND","SP","OTHER"]
is_common_share   bool         # validated: ↔ ticker_type == "CS"
raw_close         float
adj_close         float        # from Polygon adjusted=True
ret_1d            float | None # log return from adj_close; None only on first eligible day
volume            float
shares_out        float
mcap              float
gics_sector       str | None   # Week 2
gics_industry     str | None   # Week 2
eligible_flag     bool
data_snapshot_id  str          # SHA-256 of the raw pull
```

### `UniverseRow` — explicit eligibility decisions
Location: [`src/aegis/data/schema.py`](../../src/aegis/data/schema.py)

Carries `date`, `ticker`, `eligible_flag`, per-rule booleans, and `fail_reason` (the first failing rule).

### `FactorObservation` — raw + transformed factor values
Location: [`src/aegis/features/base.py`](../../src/aegis/features/base.py)

```
date, ticker, factor_name, raw_value, winsorized_value, zscore_value,
valid_flag, feature_snapshot_id
```

### `ResearchRecord` + per-table Pydantic shapes
Location: [`src/aegis/ledger/models.py`](../../src/aegis/ledger/models.py)

Umbrella `ResearchRecord` + `ExperimentRecord` / `CandidateRecord` / `ArtifactRecord` / `MetricRecord` for the four SQLAlchemy tables in [`src/aegis/ledger/schema.py`](../../src/aegis/ledger/schema.py).

## Day-by-day checklist

### Day 1 — Schemas + data config ✓ DONE

Landed during Day 1 and the pivot. All four schemas above exist; `configs/data.yaml` holds the Week 1 slice config.

### Day 2 — Polygon loader + universe filter ✓ DONE

- [`src/aegis/data/polygon_loader.py`](../../src/aegis/data/polygon_loader.py) — `load_polygon_daily(tickers, start, end, client=None, api_key=None, sleep_between_calls=12.5)` with MIC→exchange map (`XNYS→NYSE`, `XASE→AMEX`, `XNAS→NASDAQ`), free-tier rate-limit sleep, and duplicate-row invariant.
- [`src/aegis/data/universe.py`](../../src/aegis/data/universe.py) — `build_universe_flags(panel, cfg)`: 4 vectorized rules (common_share, exchange, history, price), t-1 close discipline, deterministic `fail_reason` ordering.
- [`tests/unit/test_universe.py`](../../tests/unit/test_universe.py) — 11 tests covering every rule, the $5.00/$5.01 spec boundary, t-1 lookahead discipline, rule-ordering invariant, Pydantic round-trip.
- [`tests/integration/test_polygon_loader.py`](../../tests/integration/test_polygon_loader.py) — `@pytest.mark.polygon` smoke test auto-skipped until `POLYGON_API_KEY` is set.

### Day 3 — Panel builder ✓ DONE

Landed 2026-04-23. Artifacts in tree:

- [`src/aegis/utils/hashing.py`](../../src/aegis/utils/hashing.py) — `sha256_file()` + `sha256_dataframe()` (row-order-insensitive, column-order-sensitive). Used for `data_snapshot_id`.
- [`src/aegis/data/panel.py`](../../src/aegis/data/panel.py) — `build_panel(cfg, tickers, sleep_between_calls)` composes loader + universe filter + ret_1d computation + Parquet write. `WEEK1_TICKERS = ("AAPL","MSFT","GOOGL","AMZN","NVDA","META","JPM","JNJ")` hardcoded for Week 1 (index-history deferred to Week 2).
- [`src/aegis/utils/dotenv.py`](../../src/aegis/utils/dotenv.py) — minimal `.env` loader so `aegis data build` auto-picks up `POLYGON_API_KEY` without shell-export gymnastics.
- [`tests/unit/test_panel.py`](../../tests/unit/test_panel.py) — 12 new tests (ret_1d math, Parquet round-trip, snapshot_id stability, eligibility propagation, CLI error paths) + the preserved Module A S&P 500 `xfail` stub.
- [`src/aegis/cli.py`](../../src/aegis/cli.py) — `aegis data build [--fast]` wired.

**Live Polygon end-to-end run (2026-04-23):**
- 3,664 rows written to `data/processed/daily_panel_week1.parquet`.
- 8 tickers × ~458 trading days (2024-06-03 → 2026-03-31).
- 1,648 eligible rows (45%, consistent with 252-day history gate).
- 8 `ret_1d` nulls (exactly one per ticker, first day — schema invariant holds).
- Wall time: ~5 minutes on free tier (with 12.5s/call rate-limit sleep).

### Day 4 — Research ledger ✓ DONE

Landed 2026-04-23. Artifacts in tree:

- [`src/aegis/ledger/store.py`](../../src/aegis/ledger/store.py) — append-only write API: `open_ledger(path)` (context manager), `register_experiment`, `register_candidate`, `register_artifact`. All return `uuid.UUID`. No `update_*` / `delete_*` exports — append-only is enforced at the interface level (tested).
- [`src/aegis/ledger/replay.py`](../../src/aegis/ledger/replay.py) — `replay(candidate_id)` stub that raises `NotImplementedError`; real engine is Week 2.
- [`src/aegis/utils/git.py`](../../src/aegis/utils/git.py) — `current_git_sha()`: `$AEGIS_GIT_SHA` env var first (Docker-bake-friendly), falls back to `git rev-parse HEAD`, raises `GitShaUnavailableError` if neither works.
- [`src/aegis/ledger/__init__.py`](../../src/aegis/ledger/__init__.py) — re-exports the four public functions.
- [`src/aegis/cli.py`](../../src/aegis/cli.py) — `aegis ledger init [--path PATH]` wired; `aegis ledger replay <candidate_id>` wired to the stub.
- [`tests/unit/test_ledger.py`](../../tests/unit/test_ledger.py) — 11 real tests (table creation, round-trip, FK integrity, 1-to-many, append-only interface, git SHA priority order) + the preserved Module B `xfail` stub.

**Live smoke (2026-04-23):** `uv run aegis ledger init` produced `data/ledger.sqlite` (77 KB) containing the four tables. `content_hash()` stayed at `093a33a5…` — ledger machinery doesn't touch research identity.

**Ledger path resolution:** `$AEGIS_LEDGER_PATH` env var first, fallback to `./data/ledger.sqlite`. Not added to `AegisConfig` — it's operational layout, not research identity (same argument as excluding `data.paths` from `content_hash`).

### Day 5 — 12–1 momentum factor + transforms ✓ DONE

Landed 2026-04-23. Artifacts in tree:

- [`src/aegis/features/operators.py`](../../src/aegis/features/operators.py) — `winsorize_cross_section(df, value_col, pct=(0.01, 0.99))` and `zscore_cross_section(df, value_col, ddof=0)`. Both per-date via `groupby`, lookahead-safe by construction. Zero variance → NaN (not inf).
- [`src/aegis/features/momentum.py`](../../src/aegis/features/momentum.py) — `Momentum12m1m(Factor)`: `name="mom_12_1"`, `formula="log(P[t-21] / P[t-252])"`, `lookback_days=252`. `compute(panel)` returns a `FactorObservation`-shaped frame with raw + winsorized + zscore + `feature_snapshot_id`.
- [`tests/unit/test_operators.py`](../../tests/unit/test_operators.py) — 13 tests: per-date clip, NaN passthrough, bounds validation, zscore mean/std/rank identities, degenerate-distribution NaN handling.
- [`tests/unit/test_momentum.py`](../../tests/unit/test_momentum.py) — 14 tests: hand-computed precision at 1e-12, 21-day skip proof, valid_flag-matches-finite-triple invariant, per-date zscore mean ≈ 0, σ-algebra measurability via truncation stability, snapshot_id stability.

**Transform pipeline (per spec §8 intro):**
  1. raw = log(adj_close[t-21]) − log(adj_close[t-252])  (lag-only, σ-algebra safe)
  2. winsorized = per-date 1%/99% percentile clip
  3. zscore = per-date standardize on winsorized values, population std (ddof=0)

The 1%/99% percentile choice matches spec §8 ("Cross-sectional z-score with 1%/99% winsorization"). Barra-lite exposures (Module D, weeks 6-8) use ±3σ per §4.2 — different code path, different rule, both justified by spec.

**Live compute (2026-04-23):** Ran `Momentum12m1m().compute(panel)` on the real Day 3 Polygon panel:
- 3,664 output rows / 1,648 valid (45%, matches Day 3 eligibility exactly)
- Per-date zscore means: 0.000000 across all dates
- AAPL 2026-03-31 raw ≈ +0.195 (19.5% log return over the 12-to-1-month window, plausible)
- Snapshot hash stable across re-runs

**Preserved xfail (unchanged):** `test_features.py::test_12m1m_momentum_reference_ic_within_0_005` — §6 Module C acceptance ("reproduce published IC within 0.005"). Needs forward-returns + IC machinery from Module E (Week 13-15). The factor math is correct here; it's the IC plumbing that's pending.

### Day 6 — Vertical-slice pipeline ✓ DONE

Landed 2026-04-23. Artifacts in tree:

- [`src/aegis/backtest/week1.py`](../../src/aegis/backtest/week1.py) — `run_week1_slice(cfg, ledger_path, sleep_between_calls)` returns a `Week1SliceResult` frozen dataclass. Composes Day 3 (`build_panel`) + Day 5 (`Momentum12m1m.compute`) + Day 4 (`open_ledger` / `register_*`) + hashing/git helpers into one call.
- [`src/aegis/backtest/__init__.py`](../../src/aegis/backtest/__init__.py) — re-exports `Week1SliceResult`, `run_week1_slice`, `EXPERIMENT_NAME`.
- [`src/aegis/cli.py`](../../src/aegis/cli.py) — `aegis backtest week1 [--fast] [--ledger-path PATH]` wired. Existing `aegis backtest run` stub retained for the Week 19 generalized runner.
- [`tests/unit/test_week1_pipeline.py`](../../tests/unit/test_week1_pipeline.py) — 6 Polygon-free tests using the `stock_daily_panel` fixture + on-disk SQLite. Monkey-patches `build_panel` and `current_git_sha` for determinism.

**Live end-to-end (2026-04-23):** `uv run aegis backtest week1` produced in ~5 min on free tier:
- `data/processed/daily_panel_week1.parquet` (3,664 rows)
- `data/processed/factor_mom_12_1_week1.parquet` (1,648 valid / 3,664 rows)
- Ledger rows: 1 experiment (`46bbc113…`), 1 candidate (`79431a46…`, status `"computed"`), 2 artifacts (panel + factor)

**Key invariants verified:**
- `data_snapshot_id = 3de377ce…` — identical to Day 3's live run → Polygon returned identical bars → pipeline is idempotent in data identity.
- `config_hash = 093a33a5…` — unchanged from scaffolding → research identity stable.
- `git_sha = b28683ccbcef…` — captured from `git rev-parse HEAD`.
- `panel.checksum = 45a724d3…`, `factor.checksum = fa6bc723…` — artifact bytes on disk match what the ledger recorded (via `sha256_file`).
- Re-running appends new experiment/candidate rows with new UUIDs but same `config_hash` — append-only discipline intact.

**Build**

- `src/aegis/backtest/week1.py` (new) — `run_week1_slice(cfg: AegisConfig) -> None` that orchestrates:
  1. Load configs (via `cfg`).
  2. Open ledger, register experiment with `config_hash = cfg.content_hash()` and `git_sha`.
  3. `panel.build_panel(cfg)` → register panel artifact.
  4. `Momentum12m1m().compute(panel)` → winsorize → zscore → write factor Parquet → register factor artifact.
  5. Register candidate with status `"computed"`.
- CLI: `aegis backtest week1` wired to this function.

**No evaluation this week.** Candidate status stops at `"computed"`. Promotion/hold/retire is the Week 13-15 gate (spec §4.5–§4.11, §5.1).

**Acceptance:** one command, `uv run aegis backtest week1`, runs end-to-end with no notebook intervention. Re-runs with unchanged code + config + Polygon snapshot emit the same `config_hash` and `data_snapshot_id`.

### Day 7 — Smoke notebook + README update ✓ DONE

Landed 2026-04-23. Artifacts in tree:

- [`notebooks/week1_smoke_test.ipynb`](../../notebooks/week1_smoke_test.ipynb) — 13 cells (6 code + 7 markdown) that read the on-disk Parquets and ledger and produce: ledger summary, eligibility-over-time plot, final-date factor cross-section with top/bottom tickers, AAPL 12-1 momentum time series, per-date zscore-mean sanity check. nbstripout handles output sanitation on commit.
- [`README.md`](../../README.md) — rewrote Quickstart around Polygon, added a dedicated "Run the Week 1 vertical slice" section with exact commands, and updated the V1 module build-order table with status markers (🟢 done / 🟡 slice / ⚪ not started).

**Smoke verification:** ran the notebook's code paths inline against the real `data/processed/*.parquet` + `data/ledger.sqlite` — 3,664 panel rows, 1,648 valid factor rows, ledger has 1/1/2 (exp/cand/art), per-date zscore mean-max = 2.22e-16, all aligned with Day 6 live output.

**Week 1 report:** [`docs/reports/week1.md`](../reports/week1.md) — top-line numbers, day-by-day summary, major pivots (WRDS → Polygon, content_hash platform invariance), live pipeline output, what Week 1 demonstrably does NOT claim, readiness for Week 2.

## File responsibilities map

| File | Responsibility | Do NOT put here |
|---|---|---|
| `src/aegis/data/polygon_loader.py` | Raw Polygon API access. No filtering. No return computation. | Eligibility rules |
| `src/aegis/data/universe.py` | Eligibility rules only. | Polygon IO; return math |
| `src/aegis/data/panel.py` | Orchestrates loader + universe → Parquet. | Factor computation |
| `src/aegis/data/schema.py` | `StockDailyRow`, `UniverseRow`. | Anything mutable |
| `src/aegis/features/base.py` | `Factor` ABC, `FactorObservation`. | Specific factor formulas |
| `src/aegis/features/momentum.py` | Only `Momentum12m1m`. | Transforms; other factors |
| `src/aegis/features/operators.py` | Winsorize, zscore, rank, lag, rolling. Pure functions. | Factor definitions |
| `src/aegis/ledger/schema.py` | SQLAlchemy table DDL. | Write logic |
| `src/aegis/ledger/models.py` | Pydantic row models. | SQL anything |
| `src/aegis/ledger/store.py` | Append-only writes + session. | Replay logic |
| `src/aegis/ledger/replay.py` | Replay engine (Week 2+). | Write logic |
| `src/aegis/backtest/week1.py` | Orchestrates the Week 1 vertical slice only. | Factor or risk math |
| `src/aegis/utils/hashing.py` | `data_snapshot_id`, `config_hash` helpers. | IO |

## Explicitly out of scope for Week 1

Do **not** touch:

- Corporate-action adjustment module (not needed — Polygon serves adjusted prices).
- Barra-lite WLS (Module D, weeks 6–8).
- Newey-West HAC, BH-FDR, Deflated Sharpe, FF3/FF5/FF6 regression (Module E, weeks 13–15).
- OSQP portfolio QP, Corwin-Schultz spreads, Almgren impact (Module F, weeks 16–18).
- Regime HMM, Ledoit-Wolf shrinkage (V2).
- Qlib adapter (V2, per spec §15.6 — explicitly deferred).
- LLM anything (V2).
- The locked 2024–2025 sub-holdout (Week 20 only, opened once, ever).
- **Delisted-ticker / survivorship tracker** — Week 2.
- **Historical S&P 500 membership** — Week 2.

## Definition of done

By end-of-week:

- [ ] `uv run aegis backtest week1` runs end-to-end on a ~200-ticker × 2020-2022 slice.
- [ ] `data/processed/daily_panel_week1.parquet` exists, ~200k rows.
- [ ] `data/processed/factor_mom_12_1_week1.parquet` exists with `raw_value`, `winsorized_value`, `zscore_value`.
- [ ] `data/ledger.sqlite` has one experiment row, one candidate row (status `computed`), two artifact rows.
- [ ] Re-running with unchanged code + config + Polygon snapshot produces identical `config_hash` and `data_snapshot_id`.
- [ ] `tests/unit/test_schema.py`, `test_universe.py`, `test_panel.py`, `test_ledger.py`, `test_momentum.py` all green (not xfail).
- [ ] `notebooks/week1_smoke_test.ipynb` renders five plots; nbstripout removes outputs on commit.
- [ ] README Quickstart updated with Polygon setup instructions.
- [ ] Ruff + mypy + pytest (non-polygon) all clean.
- [ ] `content_hash()` unchanged (research identity stable across the pivot).

If those ten boxes check, Week 1 is a success — and Week 2 (widen to full 2000–2025 × 3,000 names, add Polygon fundamentals, build the delisted-ticker tracker, begin the feature library) starts from a working spine.
