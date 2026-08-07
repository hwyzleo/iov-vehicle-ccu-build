#!/bin/bash
# CGW Hello smoke test script (minimal example)
# Run the CLI and check the output contains the expected greeting.
set -euo pipefail
BIN="/usr/bin/cgw-hello-cli"
OUTPUT=$("$BIN" CGW 2>&1 || true)
if echo "$OUTPUT" | grep -q "CGW-Hello"; then
    echo "OK: smoke test passed"
    exit 0
fi
echo "FAIL: unexpected output: $OUTPUT"
exit 1
