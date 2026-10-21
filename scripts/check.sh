#!/usr/bin/env bash
# Единый гейт проекта (то же гоняет CI).
set -euo pipefail
cd "$(dirname "$0")/.."

uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q
