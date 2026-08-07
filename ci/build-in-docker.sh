#!/usr/bin/env bash
# ci/build-in-docker.sh - Build inside Docker container
#
# Wraps the build commands for execution inside the CI container.
# Called by ci/pipeline.sh when a target (non-host) platform is selected.
set -euo pipefail

cd "${CGW_ROOT:-$(pwd}"

echo "=== CGW Build (in Docker) ==="
echo "CGW_ROOT: ${CGW_ROOT:-$(pwd)}"
echo "Platform: ${CGW_PLATFORM:-cgw-dev}"
echo "Profile: ${CGW_PROFILE:-release}"

# Validate manifests
python3 -m cgw_build validate --platform "${CGW_PLATFORM:-cgw-dev}"

# Build
python3 -m cgw_build build \
    --set "${CGW_SET:-cgw-dev-minimal}" \
    --platform "${CGW_PLATFORM:-cgw-dev}" \
    --profile "${CGW_PROFILE:-release}" \
    --jobs "${CGW_JOBS:-4}"

# Package
python3 -m cgw_build package \
    --platform "${CGW_PLATFORM:-cgw-dev}" \
    --profile "${CGW_PROFILE:-release}"

echo "=== Build complete ==="
