# Aegis Alpha Lab

A mathematically specified, Barra-aware, long-short U.S. equity research platform.

V1 is structured around three commitments:

1. **Residual alpha before raw alpha.** Every candidate signal is neutralized against 9 Barra-lite styles and 24 GICS industries before scoring.
2. **Pre-committed multiple-testing discipline.** Promotion requires a Newey–West HAC IC t-stat > 3.0, Benjamini–Hochberg FDR at q=0.10, a Deflated Sharpe > 0.95, and FF6 α with HAC t > 2.5.
3. **Full replayability.** Every promoted signal reconstructs bit-identically from the research ledger plus the point-in-time data snapshot.

The full specification is at [docs/proposal/Aegis_Alpha_Lab_Proposal_v3.pdf](docs/proposal/Aegis_Alpha_Lab_Proposal_v3.pdf).

## Repository layout

```
configs/         Versioned YAML: gate thresholds, risk model, universe, portfolio, costs, factors
src/aegis/       Python package — six V1 modules (data, ledger, features, risk, validation, portfolio)
tests/           Unit + integration tests, including the six §6 module-acceptance stubs
docker/          Multi-stage Dockerfile + docker-compose
scripts/         One-shot operational scripts (WRDS pull, lockbox opener)
docs/proposal/   The v3 proposal (.docx / .pdf)
```

## Quickstart

Prerequisites: [uv](https://docs.astral.sh/uv/) ≥ 0.4, Python 3.11, Docker (optional), and a [Polygon.io](https://polygon.io) account (free tier works for Week 1; $29/mo Starter for Week 2+).

```bash
# 1. Install deps into a managed venv (Python 3.11 auto-downloaded by uv)
uv sync

# 2. Copy the env template and paste your Polygon API key
cp .env.example .env
# edit .env: POLYGON_API_KEY=<your key from polygon.io/dashboard/api-keys>

# 3. Quality gates — expect 103 passed, 6 xfail (Modules A–F acceptance stubs)
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
uv run pytest -m "not polygon"

# 4. See the CLI surface
uv run aegis --help
```

### Run the Week 1 vertical slice

```bash
# Initialize the research ledger (idempotent — safe to re-run)
uv run aegis ledger init

# End-to-end: panel → 12-1 momentum → ledger. ~5 min on Polygon free tier.
uv run aegis backtest week1
```

Produces:
- `data/processed/daily_panel_week1.parquet` — Module A panel (8 tickers × ~458 trading days)
- `data/processed/factor_mom_12_1_week1.parquet` — `mom_12_1` factor output
- Three new rows in `data/ledger.sqlite` (1 experiment, 1 candidate, 2 artifacts)

For the interactive smoke notebook, install the optional extras and open it:

```bash
uv sync --extra notebook
uv run jupyter lab notebooks/week1_smoke_test.ipynb
```

### Polygon-backed live smoke test

```bash
uv run pytest -m polygon -v   # ~2 min; auto-skips without POLYGON_API_KEY
```

### Docker

```bash
make docker
make docker-run
```

## V1 module build order (§12 of the proposal)

| Module | Weeks | Status | Delivers |
|---|---|---|---|
| A — Data & PIT panel | 1–5 | 🟡 Week 1 slice landed | Polygon.io panel, universe filter; S&P 500 index history deferred to Week 2 |
| B — Research ledger | alongside A | 🟢 write side done | Append-only SQLite; replay engine is Week 2 |
| C — Feature library | 9–12 | 🟡 1/~40 factors landed | `mom_12_1` computes end-to-end; ~39 factors remain |
| D — Barra-lite risk engine | 6–8 | ⚪ not started | √mcap-WLS styles + industries, EWMA covariance |
| E — Validation & gate | 13–15 | ⚪ not started | HAC IC, BH-FDR, DSR, FF6 α, decay gate |
| F — Portfolio & cost | 16–18 | ⚪ not started | Cost-aware QP (OSQP) |
| Lockbox | 19–20 | ⚪ not started | Freeze code, open 2024–2025 holdout once, post-mortem |

Each module has an acceptance test in `tests/unit/`, marked `xfail(strict=True)` until it lands. When a test stops failing, pytest forces the marker to be removed — the xfails encode the roadmap as machine-readable checkboxes.

**Data-source note:** spec §7 targets CRSP + Compustat via WRDS. Rice denied WRDS access on 2026-04-23, so V1 runs on Polygon.io ([docs/plans/week1.md](docs/plans/week1.md) documents the pivot). Core invariants (PIT discipline, corporate-action adjustment, measurability) are preserved; survivorship-bias handling and historical index constituency are deferred to Week 2.

## Quantified success criteria (§11)

Thresholds are fixed before the locked 2024–2025 sub-holdout opens and will not be relaxed afterward.

| Scope | Metric | V1 minimum | V1 target |
|---|---|---|---|
| Single factor | Residualized rank IC (mean) | 0.02 | 0.04 |
| Single factor | IC IR (annualized) | 0.50 | 0.75 |
| Single factor | HAC t(IC) | 3.0 | 5.0+ |
| Single factor | Deflated Sharpe | 0.95 | 0.99 |
| Single factor | FF6 α HAC t | 2.5 | 4.0+ |
| Composite book, gross | Annualized Sharpe | 1.20 | 2.00 |
| Composite book, net (3 bp) | Annualized Sharpe | 0.80 | 1.50 |
| Composite book | Max drawdown | ≤ 10% | ≤ 6% |
| Composite book | Turnover (GMV/yr) | ≤ 8× | ~5× |
| Composite book | Residual β to SPY | \|β\| ≤ 0.05 | \|β\| ≤ 0.02 |

Failure in the locked holdout counts as V1 failing its research hypothesis. Engineering still a success; the decision is then to change the hypothesis in V2, not retry with relaxed thresholds.

## Configuration

All numeric thresholds live in `configs/*.yaml` and are loaded + validated by `src/aegis/config.py` into frozen Pydantic models. A SHA-256 of the loaded config is stamped into every research-ledger row alongside the git SHA, so every promoted signal points to the exact code + config that produced it.

## License

MIT. See [LICENSE](LICENSE).
