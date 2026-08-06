#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
VENV_PATH="$PROJECT_ROOT/.venv"

if [ ! -d "$VENV_PATH" ]; then
  echo "Error: Virtual environment not found at $VENV_PATH"
  echo "Run setup-venv.sh first"
  exit 1
fi

source "$VENV_PATH/bin/activate"

if [ ! -f "$PROJECT_ROOT/.env" ]; then
  echo "Warning: .env file not found at $PROJECT_ROOT/.env"
  echo "The backend may not start correctly without configuration"
fi

cd "$PROJECT_ROOT"

echo "Starting ArchiveX backend..."
uvicorn archivex.main:create_app --factory --reload
