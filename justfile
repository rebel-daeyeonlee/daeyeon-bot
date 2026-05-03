# daeyeon-bot — task runner.
# `just <recipe>` to run. `just` (no args) prints the list.

set shell := ["bash", "-cu"]
set dotenv-load := true

default:
    @just --list

# ─── Setup ─────────────────────────────────────────────────────────────────────

sync:
    uv sync --all-extras --dev

# ─── Code quality ──────────────────────────────────────────────────────────────

lint:
    uv run ruff check src tests
    uv run ruff format --check src tests

format:
    uv run ruff format src tests
    uv run ruff check --fix src tests

typecheck:
    uv run pyright src tests

# ─── Tests ─────────────────────────────────────────────────────────────────────

test *args:
    uv run pytest {{args}}

test-unit:
    uv run pytest tests/unit

test-integration:
    uv run pytest tests/integration -m integration

# ─── Run / inspect ─────────────────────────────────────────────────────────────

run:
    uv run daeyeon-bot run

doctor:
    uv run daeyeon-bot ops doctor

status:
    uv run daeyeon-bot inspect status

# ─── Maintenance ───────────────────────────────────────────────────────────────

prune:
    uv run daeyeon-bot ops prune

migrate:
    uv run daeyeon-bot ops migrate

# ─── Deployment ────────────────────────────────────────────────────────────────

setup-token:
    ./scripts/setup-token.sh

install-mac:
    ./scripts/install-mac.sh

install-linux credential:
    ./scripts/install-linux.sh {{credential}}

# ─── Pre-commit aggregate ──────────────────────────────────────────────────────

check: lint typecheck test
