#!/bin/bash
# CGW Hello health check script (minimal example)
# Exit 0 if the service binary exists and is executable.
set -euo pipefail
BIN="/usr/bin/cgw-hello-cli"
if [ -x "$BIN" ]; then
    echo "OK: $BIN is executable"
    exit 0
fi
echo "FAIL: $BIN not found or not executable"
exit 1
