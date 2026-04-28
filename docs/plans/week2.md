# Week 2 — Universe realism + Replayability

> **Dates:** 2026-04-24 → 2026-04-30 (Days 8–14)
> **Spine claim:** *The pipeline runs against a **historically reconstructed** U.S. large-cap universe with **materially reduced** survivorship bias, and any ledger row can be **verified** against its stored artifact checksums, config hash, and git SHA.*

## Week 2 priorities

Week 2 has two hard objectives and one hard operational deliverable with a synthetic fallback if Polygon has an outage.

1. **Date-aware universe realism** — replace the Week 1 survivor-only hardcoded list with a reconstructed S&P 500 universe that respects additions, removals, and delisting boundaries. Built from historical index membership + delisting-aware Polygon metadata + a small manual ticker-rename reconciliation table.
2. **Ledger verification** — a working `verify()` engine that proves any ledger row still matches its on-disk artifacts (checksum + config hash + git SHA). Unblocks spec §6 Module B acceptance.
3. **Scale test — live** — build the widened-universe code path AND run it end-to-end on ~500 tickers against real Polygon data. Polygon Starter is active (user upgraded 2026-04-24), so the live run is must-land, not conditional. Expected wall time ~20 min at 100 calls/min.

Everything else (sector enrichment, Polygon fundamentals, Barra-lite, validation gate, optimizer) is explicitly out of scope and lives in its own week.

## Core objective, written once

> Replace the survivor-only Week 1 universe with a **date-aware, historically reconstructed universe that respects delisting boundaries** closely enough to support later IC/Sharpe validation. Make the ledger's replayability claim **operational** — at end of week, `aegis ledger replay <id>` returns a truthful report that can distinguish "artifacts intact" from "tampered" from "config has drifted" from "git SHA is gone."

## Context

Week 1 proved the spine works on 8 blue-chips. Week 2 fixes two distinct classes of debt from that run:

**Universe-realism debt**
1. Hardcoded `WEEK1_TICKERS` (8 survivor names, no delisted exposure)
2. No notion of "was this ticker a member of a tradable index on date *t*"
3. No reconciliation for ticker renames (FB→META, GOOG/GOOGL classes, etc.)

**Replayability debt**
4. Ledger writes candidates + artifacts, but nothing reads them back and verifies

By end of Week 2, two `xfail` markers flip to `pass` — Module A (universe reconstruction) and Module B (verify-mode replay). Two of six spec §6 module milestones cleared.

## Terminology discipline

Three precise claims Week 2 makes — and three overclaims it avoids:

| Precise claim (Week 2 ships) | Imprecise claim to avoid |
|---|---|
| "Historically reconstructed U.S. large-cap universe with materially reduced survivorship bias" | "Survivorship-bias-free" — our sources (Wikipedia + Polygon) are derivative; institutional-grade index history sits behind S&P Dow Jones paywalls |
| "Delisting-aware tradability check" — `is_active_on(ticker, date)` respects the delisting date | "Perfect point-in-time membership" — we reconcile the top-20 ticker renames manually; long-tail cases may drift |
| "**Verify mode** — checksum + config-hash + git-sha match" | "Bit-identical replay" — true replay requires git-worktree checkout + full pipeline re-run; V2 scope |

The spec §6 Module B acceptance (*"every promoted factor replays bit-identical from the ledger"*) is reasonably interpreted as verify-mode for Week 2: if the artifact is on disk and matches its recorded checksum AND the config hash and git SHA check out, the ledger row faithfully represents a reproducible output. Full rebuild-from-source stays on the V2 roadmap.

## Upstream decision: Polygon Starter — resolved (upgraded 2026-04-24)

**Status:** Upgrade complete. `POLYGON_API_KEY` is on a Stocks Starter plan (100 calls/min, 10+ years history). The Week 2 scale-test *live* run is now a must-land deliverable, not conditional.

Wall-time budget at Starter's 100 calls/min, ~500 tickers × ~3 calls each (ticker details + 2 aggs calls) = ~1,500 calls → ~15 minutes minimum, ~25 minutes with network overhead. That's the target for Day 13's live run.

## Workstream A — Universe realism (Days 8–10)

### Day 8 — Historical S&P 500 membership

**Build**
- `scripts/fetch_sp500_history.py` (new) — scrapes Wikipedia's S&P 500 current table + Changes table via `pandas.read_html` + BeautifulSoup. One-shot; rerun quarterly.
- `data/reference/sp500_membership.csv` (new, checked in) — schema: `ticker, name, wiki_sector, wiki_sub_industry, date_added, date_removed, cik_code`. `date_removed` empty for current members. ~650 rows (500 current + ~150 historical changes back to 2000).
- `src/aegis/data/index_membership.py` (new):
  ```python
  def load_sp500_membership(path: Path) -> pd.DataFrame: ...
  def active_on(date: date, membership: pd.DataFrame) -> set[str]: ...
  def membership_window(start: date, end: date, membership: pd.DataFrame) -> pd.DataFrame: ...
  ```

**Tests** — `tests/unit/test_index_membership.py` (new, 5 tests)
- `test_active_on_2020_returns_500_names` — size in [485, 510]
- `test_active_on_excludes_post_delisting` — known pre-2020 delisting not in 2020-active
- `test_active_on_includes_post_addition` — TSLA in 2021-active, not 2020-pre-addition
- `test_membership_window_shape` — ~500 × 21 ≈ 10.5K rows
- `test_load_sp500_membership_schema` — expected columns, non-empty

**Acceptance:** `active_on(date(2020, 6, 15), …)` returns 500 ± 1 tickers.

---

### Day 9 — Ticker history + delisting-aware tradability cache

This is the **load-bearing survivorship-tracker day**. Priority 1's central deliverable.

**Build**
- `scripts/fetch_polygon_ticker_reference.py` (new) — paginates Polygon `/v3/reference/tickers` with both `active=true` and `active=false`. Filters to tickers that ever appeared in `sp500_membership.csv`. Caches to `data/reference/ticker_metadata.parquet`.
- `data/reference/ticker_metadata.parquet` (new, gitignored) — columns: `ticker, name, primary_exchange, ticker_type, list_date, delisted_date, sic_code, sic_description, cik`. ~650 rows, ≥ 50 with non-null `delisted_date`.
- `data/reference/ticker_aliases.csv` (new, checked in, ~10 rows) — hand-curated rename reconciliation: `canonical_ticker, alias, effective_from, effective_to, note`. Seed entries: `FB↔META` (2022-06-09), `GOOG/GOOGL` class-share handling, known post-split relists. **Scope: small by design — known high-impact reconciliation cases only.** This file is explicitly NOT an attempt to solve every historical symbol-mapping problem; long-tail cases (e.g. obscure 1990s mergers, pre-2000 rename trails) are out of scope. **Implementation discipline:** cap at ~20 entries for Week 2; if the file starts ballooning during implementation, stop and document the pattern instead — the alias table is meant to patch high-impact cases, not become a historical security master. The Week 2 report restates this expectation so downstream users aren't surprised by edge-case misses.
- `src/aegis/data/ticker_reference.py` (new):
  ```python
  def load_ticker_metadata(path: Path) -> pd.DataFrame: ...
  def is_active_on(ticker: str, date: date, metadata: pd.DataFrame) -> bool: ...
  def canonicalize_ticker(ticker: str, date: date, aliases: pd.DataFrame) -> str: ...
  def sector_for(ticker: str, metadata: pd.DataFrame) -> str | None: ...
  ```

**Tests** — `tests/unit/test_ticker_reference.py` (new, 6 tests)
- `test_known_delisted_tickers_have_delisted_date` — GE (2020 reverse-split), TWTR (2022 acquisition)
- `test_known_active_tickers_have_null_delisted_date` — AAPL, MSFT
- `test_is_active_on_boundary_day` — on the exact delisting date, `is_active_on` returns False (spec §4.1 t-1 discipline)
- `test_canonicalize_ticker_resolves_rename` — `canonicalize_ticker("FB", date(2023, 1, 1), …) == "META"`
- `test_canonicalize_ticker_identity` — AAPL → AAPL on any date
- `test_known_delisted_ticker_excluded_from_universe_post_inactive_date` — explicit survivorship-tracker acceptance per the reviewer's note: for a sample of known delisted names, the universe loader excludes them on and after their inactive date, includes them before that date if they were S&P 500 members.

**Acceptance (load-bearing):** For a test sample of ≥ 3 known delisted S&P 500 members, `is_active_on(ticker, date)` returns the correct boolean across each ticker's delisting boundary. At least **one** of the three must be a **clean one-way delisting** — acquired-and-delisted with no rename, split, or restructuring trail — so the acceptance test isn't vulnerable to corporate-history ambiguity. Recommended anchors:

- **TWTR** — Twitter, acquired by Musk 2022-10-28, delisted from NYSE. No rename, no split. The cleanest well-known example. (Must include.)
- **ATVI** — Activision Blizzard, acquired by Microsoft 2023-10-13. Clean.
- **CELG** — Celgene, acquired by Bristol-Myers Squibb 2019-11-20. Clean.

Messier cases (rename / restructuring) can be added once the TWTR-class tests pass, but are not required to clear the gate:
- CTL/Lumen (2021 rename), LINTA/Liberty Interactive (2017 restructuring), etc.

---

### Day 10 — Module A §6 acceptance flip

**Build**
- `src/aegis/data/panel.py` — add `build_panel_for_date(cfg, date, membership_df)` that pulls just that day's S&P 500 constituents (for downstream use; not required for the Module A flip itself).
- Two ground-truth fixtures, from **two different market regimes** so the test doesn't rely on a single curated snapshot:
  - `tests/fixtures/sp500_20180615.txt` (new) — pre-Tesla-era (Tesla joined S&P 500 on 2020-12-21), curated from iShares IVV holdings XLSX or archived SPY composition CSV.
  - `tests/fixtures/sp500_20210104.txt` (new) — first full trading day of 2021, i.e. after Tesla's addition and the Q4 2020 rebalance. Straddles a known large constituent change vs. 2018.

**Tests** — flip the existing `xfail`. The single test asserts the within-1-name bound on *both* dates, so accidental drift on one era doesn't get hidden by a curated snapshot on the other:
```python
@pytest.mark.parametrize(
    "check_date, fixture_file",
    [
        (date(2018, 6, 15), "sp500_20180615.txt"),  # pre-Tesla era
        (date(2021, 1, 4), "sp500_20210104.txt"),   # post-Tesla, post-Q4-2020 rebalance
    ],
)
def test_sp500_reconstruction_within_1_name(check_date, fixture_file) -> None:
    membership = load_sp500_membership(REFERENCE / "sp500_membership.csv")
    reconstructed = active_on(check_date, membership)
    ground_truth = set((Path("tests/fixtures") / fixture_file).read_text().split())
    diff = reconstructed.symmetric_difference(ground_truth)
    assert len(diff) <= 1, f"[{check_date}] Membership mismatch: {sorted(diff)}"
```

**Acceptance:** Both parametrized calls pass. xfail count drops 6 → 5. **Spec §6 Module A acceptance cleared** against two independent ground-truth dates.

---

## Workstream B — Replayability (Days 11–12)

### Day 11 — Replay engine (`verify()` mode)

**Build**
- `src/aegis/ledger/replay.py` — replace the stub:
  ```python
  @dataclass(frozen=True)
  class ReplayReport:
      candidate_id: UUID
      artifacts_verified: int                        # count passed
      artifacts_failed: list[tuple[str, str]]        # (path, failure_mode)
      config_hash_recorded: str                      # from ledger row
      config_hash_current: str                       # from live cfg
      config_hash_match: bool
      git_sha_recorded: str
      git_sha_available: bool                        # git resolves the SHA
      all_ok: bool                                   # aggregate predicate

  def verify(candidate_id: UUID, ledger_path: Path, cfg: AegisConfig | None = None) -> ReplayReport:
      """Checksum-only replay. Non-throwing — always returns a report."""
  ```
- Failure modes enumerated in `artifacts_failed`:
  - `"file_missing"` — artifact path doesn't exist on disk
  - `"checksum_mismatch"` — sha256_file ≠ stored
- Other signals surface in top-level ReplayReport fields:
  - `config_hash_match=False` — current config content_hash differs from stored
  - `git_sha_available=False` — `git cat-file -e <sha>` fails (SHA pruned or detached)
- CLI: `aegis ledger replay <candidate_id>` — pretty-prints the ReplayReport using rich.

**Non-mutation guarantee.** `verify()` MUST NOT mutate ledger state or artifacts. No inserts, updates, or deletes on any of the four ledger tables; no writes, renames, or touch-modifies on any artifact file. This reinforces the append-only philosophy at the reader layer — verification is a pure read. Enforced by test (see Day 12) and documented in the `verify()` docstring.

**Tests** — deferred to Day 12 so each day has exactly one flip target.

**Acceptance:** `uv run aegis ledger replay <id>` returns a ReplayReport in stdout. `all_ok == True` for a freshly-written candidate, `all_ok == False` with informative `artifacts_failed` for a tampered one. **Zero mutation of ledger or artifacts during a verify call, even in the failure path.**

---

### Day 12 — Module B §6 acceptance flip

Pure test work — no new source.

**Tests** — flip the `xfail` and add coverage for the four mismatch modes:
```python
def test_promoted_factor_replays_bit_identical(tmp_path, pipeline_fixture):
    result = run_week1_slice(cfg, ledger_path)
    report = verify(result.candidate_id, ledger_path, cfg)

    # 1. all artifact checksums match
    assert report.artifacts_verified == 2
    assert report.artifacts_failed == []
    # 2. stored config hash matches current resolved config
    assert report.config_hash_match is True
    assert report.config_hash_recorded == cfg.content_hash()
    # 3. stored git SHA exists and is readable
    assert report.git_sha_available is True
    # 4. aggregate predicate
    assert report.all_ok is True
```
Plus four tests covering mismatch modes and the non-mutation invariant:
- `test_replay_detects_modified_artifact` — corrupt one byte in panel Parquet, assert `("checksum_mismatch", panel_path)` in `artifacts_failed`, no exception raised
- `test_replay_detects_missing_artifact` — delete factor Parquet, assert `("file_missing", factor_path)`, no exception raised
- `test_replay_detects_config_hash_drift` — tweak `gates.promotion.t_ic_min` in a copied config, re-load cfg, assert `config_hash_match == False` but report still returned
- `test_verify_does_not_mutate_ledger_or_artifacts` — enforces the non-mutation guarantee:
  1. Snapshot ledger row counts (`experiments`, `candidates`, `artifacts`, `metrics`) and artifact-file sha256 hashes pre-call.
  2. Run `verify()` against both the happy path AND a corrupted-artifact path.
  3. Assert post-call: ledger row counts unchanged, artifact sha256s unchanged, on-disk timestamps unchanged. Applies even when `all_ok=False`.

**Acceptance:** `test_promoted_factor_replays_bit_identical` passes. xfail count drops 5 → 4. **Spec §6 Module B acceptance cleared** (verify-mode scope).

---

## Workstream C — Scale test (Day 13)

### Day 13 — Widened-universe code path

**Build**
- `src/aegis/backtest/full.py` (new) — `run_full_slice(cfg, ledger_path, sample_date, sleep_between_calls)`:
  1. `active_on(sample_date, membership)` → ~500 tickers
  2. Build panel over `cfg.data.date_range` for those tickers
  3. Compute momentum
  4. Ledger: experiment name `week2_full_universe_<YYYY-MM-DD>`
- CLI: `aegis backtest full [--date YYYY-MM-DD] [--fast] [--ledger-path PATH]`.

**Tests** — `tests/unit/test_full_pipeline.py` (new, 3 tests) — all monkey-patched:
- `test_full_slice_uses_date_aware_universe` — different `sample_date` values produce different ticker sets
- `test_full_slice_ledger_records_universe_date` — experiment name encodes the `sample_date`; different dates yield different experiment rows
- `test_full_slice_synthetic_500_ticker_fixture_is_sensible` — the concrete acceptance for Day 13 (see criteria below)

**Live run:** must-land (Polygon Starter active).
- `aegis backtest full --date 2025-06-15` end-to-end on ~500 tickers, ~15–25 min wall time. Produces ~200k-row panel and factor Parquets, three ledger rows stamped.
- If the live run breaks (Polygon outage, rate limit misconfig, etc.), fall back to the synthetic acceptance gate below so Day 13 still closes cleanly — and re-run against Polygon on Day 14 before the report.

**Synthetic-run acceptance criteria (concrete):** Day 13 lands with a monkey-patched `load_polygon_daily` that returns a synthetic ~500-ticker × ~500-day panel. The success gates, all asserted in `test_full_slice_synthetic_500_ticker_fixture_is_sensible`:

| Criterion | Threshold |
|---|---|
| Synthetic panel size | ≥ 250,000 rows (500 tickers × ≥ 500 trading days) |
| No memory blowup | Peak process RSS < 1.5 GB during the call |
| Ledger row stamping | 1 experiment + 1 candidate + 2 artifacts written; experiment `name` contains the `sample_date` as ISO string |
| Panel shape | `(rows, 15)` — matches Day 3's `_PANEL_COLUMNS` |
| Factor shape | `(rows, 8)` — matches Day 5's FactorObservation column set |
| Wall time | < 30 seconds on a dev laptop (Windows 11, 32 GB RAM) — no API calls, only Parquet IO + in-memory compute |
| Non-mutation | Re-running with the same seed produces identical panel checksums (sha256_file) |

These numbers are the floor; exceeding them is fine. Failing any of them means the code path isn't ready to point at real Polygon data.

**Live-run acceptance (if Starter is active):** same seven criteria scaled to real data, plus wall time < 25 minutes and panel-row check ≥ 200,000 (~500 tickers × 458 trading days from the current date window).

---

## Must-land Day 14 — Week 2 report

The report is **always written**, regardless of whether Day 13's live run happened.

**Build**
- `docs/reports/week2.md` (new) — structured like `week1.md`:
  - Top-line numbers (xfails flipped, rows if full-universe ran, otherwise synthetic-only)
  - Day-by-day summary
  - Major learnings (Wikipedia index-history drift, Polygon pagination quirks, ticker-rename edge cases)
  - **Terminology discipline restated** (universe realism language, verify-mode framing)
  - Deferred items for Week 3+ (fundamentals, sector-proxy enrichment, rebuild-mode replay)
- Update `README.md` Module table: A 🟡 → 🟢, B partial → 🟢 (verify-mode).

**Acceptance:** pytest count ≥ 105 passed, 4 xfailed, 0 failed. Week 2 report exists with truthful numbers reflecting what actually ran.

## Priority-ordered deliverables

| Rank | Workstream | Required | Days |
|---|---|---|---|
| 1 | Universe realism (membership + delisted + aliases + Module A flip) | ✅ must land | 8–10 |
| 2 | Replayability (`verify()` + Module B flip) | ✅ must land | 11–12 |
| 3 | Widened-universe code path (synthetic-tested) | ✅ must land | 13 |
| 3b | Live widened-universe smoke run (~500 tickers against Polygon) | ✅ must land (Starter active) | 13 |
| 4 | Week 2 report | ✅ must land | 14 |
| 5 | Sector/industry proxy (SIC-derived) | ⚪ **deferred to Week 3** | — |

Sector enrichment moved OUT of Week 2 per the reviewer's priority ranking. If it lands, the `gics_sector`/`gics_industry` columns need renaming to `sector_proxy`/`industry_proxy` to avoid conceptual debt — but that's a bundled decision, punted together. Week 2 ships with those columns unchanged (None).

## Xfail → pass milestones

| Test | File | Unblocks |
|---|---|---|
| `test_sp500_reconstruction_within_1_name` | `tests/unit/test_panel.py` | Spec §6 Module A acceptance |
| `test_promoted_factor_replays_bit_identical` | `tests/unit/test_ledger.py` | Spec §6 Module B acceptance (verify-mode) |

Remaining xfails after Week 2: C (IC within 0.005 — Module E, Week 13–15), D (Barra-lite R² — Week 6–8), E (BH-FDR null-signal — Week 13–15), F (QP attribution — Week 16–18).

## Files to add / modify

### Create (must land)
| Path | Day | Purpose |
|---|---|---|
| `scripts/fetch_sp500_history.py` | 8 | Wikipedia scraper |
| `data/reference/sp500_membership.csv` | 8 | Checked in, ~650 rows |
| `src/aegis/data/index_membership.py` | 8 | `load_*`, `active_on`, `membership_window` |
| `tests/unit/test_index_membership.py` | 8 | 5 tests |
| `scripts/fetch_polygon_ticker_reference.py` | 9 | Polygon reference scraper |
| `src/aegis/data/ticker_reference.py` | 9 | `load_*`, `is_active_on`, `canonicalize_ticker`, `sector_for` |
| `data/reference/ticker_aliases.csv` | 9 | Checked in, ≥10 entries |
| `tests/unit/test_ticker_reference.py` | 9 | 6 tests |
| `tests/fixtures/sp500_20180615.txt` | 10 | Module A ground truth, pre-Tesla era |
| `tests/fixtures/sp500_20210104.txt` | 10 | Module A ground truth, post-Q4-2020 rebalance |
| `src/aegis/ledger/replay.py` body | 11 | `ReplayReport` + `verify()` |
| `src/aegis/backtest/full.py` | 13 | `run_full_slice` |
| `tests/unit/test_full_pipeline.py` | 13 | 3 tests |
| `docs/reports/week2.md` | 14 | Week 2 report |

### Create (generated, gitignored)
| Path | Day | Purpose |
|---|---|---|
| `data/reference/ticker_metadata.parquet` | 9 | Polygon cache, regenerated monthly |

### Modify (must land)
| Path | Day | Change |
|---|---|---|
| `src/aegis/ledger/__init__.py` | 11 | Export `ReplayReport`, `verify` |
| `src/aegis/cli.py` | 11,13 | Functional `ledger replay`; new `backtest full` |
| `src/aegis/backtest/__init__.py` | 13 | Export `run_full_slice` |
| `src/aegis/data/panel.py` | 10 | Add `build_panel_for_date()` |
| `tests/unit/test_panel.py` | 10 | Flip `test_sp500_reconstruction_within_1_name` |
| `tests/unit/test_ledger.py` | 12 | Flip `test_promoted_factor_replays_bit_identical` + 4 aux tests (3 mismatch-mode + 1 non-mutation) |
| `README.md` | 14 | Module status table, Week 2 note |

### Not touched
- `configs/*.yaml`
- `src/aegis/config.py`
- `src/aegis/features/momentum.py`, `operators.py`, `base.py`
- `src/aegis/data/schema.py` — **no column renames in Week 2** (see Priority 5 deferral)
- `tests/conftest.py` `stock_daily_panel` fixture
- `notebooks/week1_smoke_test.ipynb` — still works as-is
- Spec document

## Verification — end-of-week gates

1. **Xfail count dropped by 2**
   ```bash
   uv run pytest -m "not polygon" 2>&1 | grep "xfailed"
   ```
   Expect: `4 xfailed` (down from 6).

2. **Module A live check** — both ground-truth dates, matching the parametrized test:
   ```bash
   uv run python -c "
   from datetime import date
   from pathlib import Path
   from aegis.data.index_membership import active_on, load_sp500_membership
   m = load_sp500_membership(Path('data/reference/sp500_membership.csv'))
   for check_date in (date(2018, 6, 15), date(2021, 1, 4)):
       print(f'{check_date}: {len(active_on(check_date, m))} names')
   "
   ```
   Expect: 500 ± 1 on each date.

3. **Delisting-aware tradability check (Priority 1 acceptance)** — uses a clean one-way delisting:
   ```bash
   uv run python -c "
   from datetime import date
   from pathlib import Path
   from aegis.data.ticker_reference import load_ticker_metadata, is_active_on
   m = load_ticker_metadata(Path('data/reference/ticker_metadata.parquet'))
   # TWTR acquired by Musk and delisted 2022-10-28. Clean boundary, no rename.
   print('TWTR 2022-06-01:', is_active_on('TWTR', date(2022, 6, 1), m))   # expect True
   print('TWTR 2023-01-01:', is_active_on('TWTR', date(2023, 1, 1), m))   # expect False
   "
   ```

4. **Module B live check**
   ```bash
   uv run aegis ledger replay <candidate_id_from_a_fresh_week1_run>
   ```
   Expect: `all_ok=True, artifacts_verified=2, artifacts_failed=[], config_hash_match=True, git_sha_available=True`.

5. **Quality gates**
   ```bash
   uv run ruff check src tests && uv run ruff format --check src tests
   uv run mypy src
   uv run pytest -m "not polygon"
   ```
   Expect: clean, 105+ passed, 4 xfailed, 0 failed.

6. **content_hash discipline holds** — data paths stay excluded, but the data sample definition is part of research identity so sample-window drift is detected.

## Explicitly out of scope for Week 2

- **Sector/industry proxy enrichment** — Priority 5, deferred to Week 3 (bundled with fundamentals day since both touch ticker metadata).
- **Polygon fundamentals API** — Week 3 (value composite + quality + accruals).
- **Real GICS codes** — Week 6+ Barra-lite.
- **Full rebuild-from-source replay** — V2.
- **Module C/D/E/F acceptance flips** — respective module weeks.
- **International data** — US only per V1 spec.
- **Revisiting WRDS** — closed.

## Definition of done

Must-land (Week 2 counts as successful with all nine):

- [ ] `data/reference/sp500_membership.csv` checked in, ≥ 640 rows
- [ ] `data/reference/ticker_metadata.parquet` generated, ≥ 640 rows, ≥ 50 delisted
- [ ] `data/reference/ticker_aliases.csv` checked in, ≥ 10 entries
- [ ] 5 new test files + 2 modified test files; **2 xfail milestones flipped** (A, B)
- [ ] `aegis ledger replay <id>` returns truthful `ReplayReport` across all four failure modes
- [ ] `aegis backtest full --date 2025-06-15` runs end-to-end against live Polygon (~500 tickers). **Target wall time 15–25 min on Starter**; a mild miss (say, up to ~40 min) is a reportable operational issue (Polygon congestion, local network, etc.) — not an automatic week failure. What matters for must-land: the pipeline *completes* end-to-end and writes all expected artifacts + ledger rows.
- [ ] Ledger has a `week2_full_universe_<date>` experiment row with ~500 tickers
- [ ] `docs/reports/week2.md` written with real-scale numbers from the live run
- [ ] Ruff + mypy + pytest green; `content_hash()` is stable for unchanged research identity and changes on sample-window drift

## Readiness gate for Week 3

Week 3 brings Polygon fundamentals + the value composite factor + sector-proxy enrichment (all deferred from here). The gate passes when:

1. `active_on(any_date_back_to_2010, membership)` works.
2. `is_active_on(delisted_ticker, pre_delisting_date, metadata)` correctly returns True; `post_delisting_date` returns False.
3. `aegis ledger replay <any_recent_candidate>` returns `all_ok=True`.

If those three hold, Week 3's fundamentals layer composes cleanly onto the hardened data module. If any fail, Week 3 starts with debt.
