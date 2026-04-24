"""Typer CLI entrypoint.

Subcommand surface mirrors the §6 modules. Every command is a thin wrapper:
it loads config, calls into a library function, and logs a ledger row. No
research logic lives here.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

import typer
from rich.console import Console

from aegis import __version__
from aegis.backtest import run_week1_slice
from aegis.config import load_all
from aegis.data.panel import WEEK1_TICKERS, build_panel
from aegis.ledger import open_ledger, replay
from aegis.utils.dotenv import load_dotenv_if_present

# Load .env at CLI import so POLYGON_API_KEY is visible to subcommands without
# requiring users to shell-export it. Silently no-ops if no .env is found.
load_dotenv_if_present()

app = typer.Typer(
    name="aegis",
    help="Aegis Alpha Lab — long-short U.S. equity research platform.",
    add_completion=False,
)

data_app = typer.Typer(help="Module A — PIT panel assembly.", no_args_is_help=True)
features_app = typer.Typer(help="Module C — Feature library.", no_args_is_help=True)
risk_app = typer.Typer(help="Module D — Barra-lite risk engine.", no_args_is_help=True)
validate_app = typer.Typer(help="Module E — Validation & gate.", no_args_is_help=True)
portfolio_app = typer.Typer(help="Module F — Portfolio & cost model.", no_args_is_help=True)
ledger_app = typer.Typer(help="Module B — Research ledger.", no_args_is_help=True)
backtest_app = typer.Typer(help="End-to-end backtest runner.", no_args_is_help=True)
lockbox_app = typer.Typer(help="Single-use holdout opener (spec §5.2).", no_args_is_help=True)

app.add_typer(data_app, name="data")
app.add_typer(features_app, name="features")
app.add_typer(risk_app, name="risk")
app.add_typer(validate_app, name="validate")
app.add_typer(portfolio_app, name="portfolio")
app.add_typer(ledger_app, name="ledger")
app.add_typer(backtest_app, name="backtest")
app.add_typer(lockbox_app, name="lockbox")

console = Console()


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", "-V", help="Print version and exit."),
) -> None:
    if version:
        console.print(f"aegis {__version__}")
        raise typer.Exit(code=0)
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
        raise typer.Exit(code=0)


# --- Module A -----------------------------------------------------------------
@data_app.command("build")
def data_build(
    fast: bool = typer.Option(
        False,
        "--fast",
        help="Skip the 12.5s-per-call Polygon free-tier rate-limit sleep. "
        "Only safe on Polygon paid tiers (Stocks Starter $29/mo+).",
    ),
) -> None:
    """Build the point-in-time panel from Polygon.io.

    Pulls the Week 1 hardcoded ticker list (WEEK1_TICKERS) over the date
    window in configs/data.yaml, applies the universe filter, computes
    ret_1d from adj_close, and writes data/processed/daily_panel_week1.parquet.
    Requires POLYGON_API_KEY in the environment (or .env).
    """
    cfg = load_all()
    sleep_s = 0.0 if fast else 12.5
    out_path = build_panel(cfg, tickers=WEEK1_TICKERS, sleep_between_calls=sleep_s)
    console.print(f"[green]OK[/] wrote {out_path}")


# --- Module C -----------------------------------------------------------------
@features_app.command("compute")
def features_compute() -> None:
    """Materialize the factor panel from configs/factors.yaml."""
    raise NotImplementedError("Module C — scheduled for weeks 9-12 (spec §12).")


# --- Module D -----------------------------------------------------------------
@risk_app.command("fit")
def risk_fit() -> None:
    """Estimate Barra-lite cross-sectional factor returns and covariance."""
    raise NotImplementedError("Module D — scheduled for weeks 6-8 (spec §12).")


# --- Module E -----------------------------------------------------------------
@validate_app.command("run")
def validate_run() -> None:
    """Run HAC IC, BH-FDR, DSR, FF6 α, and decay gate; emit Promote/Hold/Retire."""
    raise NotImplementedError("Module E — scheduled for weeks 13-15 (spec §12).")


# --- Module F -----------------------------------------------------------------
@portfolio_app.command("solve")
def portfolio_solve() -> None:
    """Solve the daily cost-aware QP over the promoted-signal panel."""
    raise NotImplementedError("Module F — scheduled for weeks 16-18 (spec §12).")


# --- Module B -----------------------------------------------------------------
def _default_ledger_path() -> Path:
    """Env var first, then ``./data/ledger.sqlite`` relative to CWD."""
    if env := os.environ.get("AEGIS_LEDGER_PATH"):
        return Path(env)
    return Path("data") / "ledger.sqlite"


@ledger_app.command("init")
def ledger_init(
    path: Path | None = typer.Option(  # noqa: B008 — typer convention
        None,
        help="Ledger file location. Defaults to AEGIS_LEDGER_PATH env var, "
        "else ./data/ledger.sqlite.",
    ),
) -> None:
    """Initialize an empty research-ledger SQLite with the four tables.

    Idempotent — running it against an existing ledger does nothing (tables
    are only created if missing).
    """
    final_path = path or _default_ledger_path()
    with open_ledger(final_path):
        pass  # the context-manager entry runs create_all; no rows written
    console.print(f"[green]OK[/] ledger at {final_path}")


@ledger_app.command("replay")
def ledger_replay(candidate_id: str = typer.Argument(...)) -> None:
    """Bit-identical replay of a promoted candidate from the ledger.

    Not implemented yet — Week 2 deliverable. The ledger write-side landed
    Day 4; the replay executor follows.
    """
    replay(UUID(candidate_id))


# --- Backtest / Lockbox -------------------------------------------------------
@backtest_app.command("run")
def backtest_run() -> None:
    """End-to-end: build panel → fit risk → features → validate → portfolio.

    Generalized runner for V1's full module set. Stubbed until Week 19 when
    Modules A–F are all assembled. For Week 1, use ``aegis backtest week1``.
    """
    raise NotImplementedError(
        "Generalized end-to-end runner lands in week 19 (spec §12). "
        "For the Week 1 vertical slice, use `aegis backtest week1`."
    )


@backtest_app.command("week1")
def backtest_week1(
    fast: bool = typer.Option(
        False,
        "--fast",
        help="Skip Polygon free-tier rate-limit sleep. Only safe on paid tiers.",
    ),
    ledger_path: Path | None = typer.Option(  # noqa: B008 — typer convention
        None,
        "--ledger-path",
        help="Ledger file location. Defaults to AEGIS_LEDGER_PATH, else ./data/ledger.sqlite.",
    ),
) -> None:
    """Run the Week 1 vertical slice: panel → 12-1 momentum → ledger.

    Produces daily_panel_week1.parquet, factor_mom_12_1_week1.parquet, and
    three rows in the research ledger (1 experiment, 1 candidate, 2 artifacts).
    Re-runs append new ledger rows — the ledger is append-only.
    Requires POLYGON_API_KEY in the environment (or .env).
    """
    cfg = load_all()
    final_ledger = ledger_path or _default_ledger_path()
    sleep_s = 0.0 if fast else 12.5

    result = run_week1_slice(cfg, final_ledger, sleep_between_calls=sleep_s)

    console.print("[green]OK[/] week1 slice complete")
    console.print(f"  experiment_id    {result.experiment_id}")
    console.print(f"  candidate_id     {result.candidate_id}  (mom_12_1, computed)")
    console.print(f"  panel            {result.panel_path}  [dim]{result.panel_rows} rows[/]")
    console.print(
        f"  factor           {result.factor_path}  "
        f"[dim]{result.factor_valid_rows} valid / {result.panel_rows}[/]"
    )
    console.print(f"  config_hash      {result.config_hash[:16]}…")
    console.print(f"  git_sha          {result.git_sha[:16]}")
    console.print(f"  data_snapshot    {result.data_snapshot_id[:16]}…")
    console.print(f"  ledger           {final_ledger}")


@lockbox_app.command("open")
def lockbox_open() -> None:
    """Open the 2024-2025 locked sub-holdout exactly once (spec §5.2)."""
    raise NotImplementedError("Lockbox — opened in week 20 (spec §12).")


if __name__ == "__main__":
    app()
