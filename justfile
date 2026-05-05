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

backup:
    uv run daeyeon-bot ops backup

migrate:
    uv run daeyeon-bot ops migrate

# ─── Deployment ────────────────────────────────────────────────────────────────

setup-token:
    ./scripts/setup-token.sh

# Rotate token: store new OAuth token + restart launchd agent.
# Rolls Keychain back to the previous token if the restart fails.
rotate-token:
    ./scripts/setup-token.sh --rotate

install-mac:
    ./scripts/install-mac.sh

install-linux credential:
    ./scripts/install-linux.sh {{credential}}

# ─── Pre-commit aggregate ──────────────────────────────────────────────────────

check: lint typecheck test
