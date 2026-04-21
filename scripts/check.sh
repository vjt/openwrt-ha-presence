#!/usr/bin/env bash
# check.sh — green-gate for eve. Chains every quality check.
# Runs all stages even on failure, exits non-zero if any failed.
# Exit 0 = ready to commit / ship. Non-zero = fix before continuing.
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$here"

status=0

echo "==> ruff check"
ruff check . || status=$?

echo "==> ruff format --check"
ruff format --check . || status=$?

echo "==> pyright"
pyright || status=$?

echo "==> pytest"
pytest -q --timeout=5 || status=$?

echo
if [[ "$status" -eq 0 ]]; then
  echo "all green"
else
  echo "FAILED (exit $status)"
fi
exit "$status"
