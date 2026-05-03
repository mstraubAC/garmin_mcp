#!/usr/bin/env bash
# One-command developer environment setup.
#
# Usage:
#   ./setup.sh
#
# What it does:
#   1. Installs uv if not already present
#   2. Creates a Python virtual environment (3.13) via uv
#   3. Installs all dependencies (including dev tools: pytest, ruff, mypy, coverage)
#   4. Installs pre-commit hooks (ruff, mypy, whitespace, YAML/JSON checks)
#
# After running this, you're ready to:
#   uv run pytest                     # run tests
#   uv run ruff check src/ tests/     # lint
#   uv run mypy src/garmin_mcp        # type check
#   uv run coverage run -m pytest && uv run coverage report  # coverage

set -euo pipefail

echo "==> Checking for uv..."
if ! command -v uv &>/dev/null; then
    echo "    uv not found — installing..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # shellcheck source=/dev/null
    source "$HOME/.local/env" 2>/dev/null || source "$HOME/.cargo/env" 2>/dev/null || true
    echo "    uv installed: $(uv --version)"
else
    echo "    uv found: $(uv --version)"
fi

echo ""
echo "==> Installing Python 3.13..."
uv python install 3.13

echo ""
echo "==> Syncing dependencies..."
uv sync

echo ""
echo "==> Installing pre-commit hooks..."
uv run pre-commit install --install-hooks

echo ""
echo "==> Running pre-commit on all files (first-time check)..."
uv run pre-commit run --all-files || echo "    (some hooks may need adjustment — review output above)"

echo ""
echo "==> Setup complete!"
echo ""
echo "    Run the tests:       uv run pytest"
echo "    Run linting:         uv run ruff check src/ tests/"
echo "    Run type checking:    uv run mypy src/garmin_mcp"
echo "    Run coverage:        uv run coverage run -m pytest && uv run coverage report"
echo "    Run pre-commit:      uv run pre-commit run --all-files"
