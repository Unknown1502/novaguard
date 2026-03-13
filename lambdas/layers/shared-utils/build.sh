#!/bin/bash
# Builds the shared-utils Lambda layer.
# Copies NovaGuard shared Python modules into the python/ directory so all agents
# can import them from /opt/python at runtime.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_DIR="${SCRIPT_DIR}/python"
SHARED_DIR="${SCRIPT_DIR}/../../shared"

echo "[build] Cleaning previous build..."
rm -rf "$PYTHON_DIR"
mkdir -p "$PYTHON_DIR"

echo "[build] Installing base dependencies..."
pip install --quiet -r "${SCRIPT_DIR}/requirements.txt" -t "$PYTHON_DIR"

echo "[build] Copying shared utility modules..."
cp "${SHARED_DIR}/models.py"           "$PYTHON_DIR/"
cp "${SHARED_DIR}/bedrock_client.py"   "$PYTHON_DIR/"
cp "${SHARED_DIR}/mcp_tools.py"        "$PYTHON_DIR/"
cp "${SHARED_DIR}/observability.py"    "$PYTHON_DIR/"
cp "${SHARED_DIR}/opensearch_client.py" "$PYTHON_DIR/"

echo "[build] Shared-utils layer built at: ${PYTHON_DIR}"
echo "[build] Contents: $(ls "$PYTHON_DIR")"
