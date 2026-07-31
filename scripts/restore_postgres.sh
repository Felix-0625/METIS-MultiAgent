#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Safe default: without --execute this only verifies manifest, checksum and
# pg_restore structure. The target URL must be provided through --target-env.
exec python3 "$SCRIPT_DIR/postgres_recovery_drill.py" restore "$@"
