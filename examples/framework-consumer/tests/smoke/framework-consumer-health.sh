#!/bin/bash
# CCU Framework Consumer health check script (CR-002 self-verification).
# Exit 0 if the service binary exists and is executable.
set -euo pipefail
BIN="/usr/bin/ccu-framework-consumer"
if [ -x "$BIN" ]; then
    echo "OK: $BIN is executable"
    exit 0
fi
echo "FAIL: $BIN not found or not executable"
exit 1
