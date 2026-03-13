#!/bin/bash
# Builds the strands-agents Lambda layer locally.
# Run before `cdk deploy` to populate lambdas/layers/strands-agents/python/

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_DIR="${SCRIPT_DIR}/python"

echo "[build] Cleaning previous build..."
rm -rf "$PYTHON_DIR"
mkdir -p "$PYTHON_DIR"

echo "[build] Installing packages..."
pip install --quiet -r "${SCRIPT_DIR}/requirements.txt" -t "$PYTHON_DIR"

echo "[build] Layer built at: ${PYTHON_DIR}"
echo "[build] Layer size: $(du -sh "$PYTHON_DIR" | cut -f1)"
