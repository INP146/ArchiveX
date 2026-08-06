#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
VENV_PATH="$PROJECT_ROOT/.venv"

echo "Setting up Python virtual environment..."

if [ -d "$VENV_PATH" ]; then
  echo "Virtual environment already exists at $VENV_PATH"
  echo "To recreate, delete it first: rm -rf $VENV_PATH"
  exit 0
fi

python3 -m venv "$VENV_PATH"
echo "Created virtual environment at $VENV_PATH"

source "$VENV_PATH/bin/activate"

echo "Installing dependencies..."
pip install --upgrade pip
pip install -e "$PROJECT_ROOT"

echo ""
echo "✓ Setup complete!"
echo ""
echo "To activate the environment, run:"
echo "  source .venv/bin/activate"
