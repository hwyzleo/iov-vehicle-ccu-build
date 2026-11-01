#!/bin/bash
# CCU Framework Consumer smoke test script (CR-002 self-verification).
# Run the consumer and check it links the framework and reports OK.
set -euo pipefail
BIN="/usr/bin/ccu-framework-consumer"
OUTPUT=$("$BIN" 2>&1 || true)
if echo "$OUTPUT" | grep -q "ccu-framework-consumer: OK"; then
    echo "OK: smoke test passed"
    exit 0
fi
echo "FAIL: unexpected output: $OUTPUT"
exit 1
