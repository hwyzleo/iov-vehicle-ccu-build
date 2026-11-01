#!/usr/bin/env bash
# ci/build-in-docker.sh - Build inside Docker container
#
# Wraps the build commands for execution inside the CI container.
# Called by ci/pipeline.sh when a target (non-host) platform is selected.
set -euo pipefail

cd "${CCU_ROOT:-$(pwd}"

echo "=== CCU Build (in Docker) ==="
echo "CCU_ROOT: ${CCU_ROOT:-$(pwd)}"
echo "Platform: ${CCU_PLATFORM:-ccu-dev}"
echo "Profile: ${CCU_PROFILE:-release}"

# Validate manifests
python3 -m ccu_build validate --platform "${CCU_PLATFORM:-ccu-dev}"

# Build
python3 -m ccu_build build \
    --set "${CCU_SET:-ccu-dev-minimal}" \
    --platform "${CCU_PLATFORM:-ccu-dev}" \
    --profile "${CCU_PROFILE:-release}" \
    --jobs "${CCU_JOBS:-4}"

# Package
python3 -m ccu_build package \
    --platform "${CCU_PLATFORM:-ccu-dev}" \
    --profile "${CCU_PROFILE:-release}"

echo "=== Build complete ==="
