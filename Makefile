# Aegis Alpha Lab — convenience targets. All wrap `uv run ...`.

.PHONY: help sync lock lint fmt typecheck test test-all cov clean \
        panel risk features validate portfolio backtest lockbox \
        docker docker-run hooks

help:
	@echo "Setup"
	@echo "  sync         Install deps from uv.lock"
	@echo "  lock         Re-resolve and refresh uv.lock"
	@echo "  hooks        Install pre-commit hooks"
	@echo ""
	@echo "Quality"
	@echo "  lint         ruff check"
	@echo "  fmt          ruff format"
	@echo "  typecheck    mypy on src/aegis"
	@echo "  test         pytest, skipping @wrds-marked"
	@echo "  test-all     pytest including @wrds-marked"
	@echo "  cov          pytest with coverage report"
	@echo ""
	@echo "Pipeline (V1 modules)"
	@echo "  panel        Build PIT panel (Module A)"
	@echo "  risk         Fit Barra-lite risk model (Module D)"
	@echo "  features     Compute feature panel (Module C)"
	@echo "  validate     Run gate: HAC IC, BH-FDR, DSR, FF6 (Module E)"
	@echo "  portfolio    Solve daily QP (Module F)"
	@echo "  backtest     End-to-end run across all modules"
	@echo "  lockbox      Single-use 2024-2025 holdout opener"
	@echo ""
	@echo "Docker"
	@echo "  docker       Build image"
	@echo "  docker-run   Run container with mounted data dir + .env"

# --- Setup ------------------------------------------------------------------
sync:
	uv sync

lock:
	uv lock

hooks:
	uv run pre-commit install

# --- Quality ----------------------------------------------------------------
lint:
	uv run ruff check src tests

fmt:
	uv run ruff format src tests
	uv run ruff check --fix src tests

typecheck:
	uv run mypy src

test:
	uv run pytest -m "not wrds"

test-all:
	uv run pytest

cov:
	uv run pytest --cov=aegis --cov-report=term-missing --cov-report=html -m "not wrds"

# --- Pipeline (stubs — subcommands wired as modules land) -------------------
panel:
	uv run aegis data build

features:
	uv run aegis features compute

risk:
	uv run aegis risk fit

validate:
	uv run aegis validate run

portfolio:
	uv run aegis portfolio solve

backtest:
	uv run aegis backtest run

lockbox:
	uv run aegis lockbox open

# --- Docker -----------------------------------------------------------------
docker:
	docker build -f docker/Dockerfile -t aegis:dev \
		--build-arg GIT_SHA=$$(git rev-parse --short HEAD) .

docker-run:
	docker run --rm -it \
		--env-file .env \
		-v "$$PWD/data:/app/data" \
		aegis:dev aegis --help

# --- Housekeeping -----------------------------------------------------------
clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache .hypothesis htmlcov .coverage
	find . -type d -name __pycache__ -exec rm -rf {} +
