#!/usr/bin/env bash
# ci/pipeline.sh - CGW Build CI pipeline
#
# Executes the full build verification pipeline (设计 §8 CI 分层 - 提交级):
#   1. Lint (manifest/schema validation)
#   2. Configure (cross-compile configure)
#   3. Build (compile + link)
#   4. Install (staging)
#   5. ELF/pollution check
#   6. Package + manifest
#   7. Verify package
#
# Usage:
#   ./ci/pipeline.sh [OPTIONS]
#
# OPTIONS:
#   --set <id>         Release set to build/package (default: cgw-dev-minimal).
#   --service <id>     Build/package a single service instead of a set.
#   --platform <p>     Target platform (default: cgw-dev).
#   --profile <p>      debug | release (default: release).
#   -j, --jobs <n>     Parallel build jobs (default: 4).
#   --build-only       Run build + package but skip the final verify step.
#   --dry-run          Dry-run only (no actual compilation; schema + plan).
#   -h, --help         Show this help and exit.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Defaults
PLATFORM="cgw-dev"
PROFILE="release"
SET="cgw-dev-minimal"
SERVICE=""
JOBS=4
BUILD_ONLY=0
DRY_RUN=0

usage() {
    sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --set)          [ $# -ge 2 ] || { echo "ERROR: --set requires an argument" >&2; exit 2; }
                        SET="$2"; SERVICE=""; shift 2 ;;
        --service)      [ $# -ge 2 ] || { echo "ERROR: --service requires an argument" >&2; exit 2; }
                        SERVICE="$2"; SET=""; shift 2 ;;
        --platform)     [ $# -ge 2 ] || { echo "ERROR: --platform requires an argument" >&2; exit 2; }
                        PLATFORM="$2"; shift 2 ;;
        --profile)      [ $# -ge 2 ] || { echo "ERROR: --profile requires an argument" >&2; exit 2; }
                        PROFILE="$2"; shift 2 ;;
        -j|--jobs)      [ $# -ge 2 ] || { echo "ERROR: $1 requires an argument" >&2; exit 2; }
                        JOBS="$2"; shift 2 ;;
        --build-only)   BUILD_ONLY=1; shift ;;
        --dry-run)      DRY_RUN=1; shift ;;
        -h|--help)      usage; exit 0 ;;
        *)              echo "ERROR: unknown argument: $1" >&2; exit 2 ;;
    esac
done

cd "$PROJECT_ROOT"
export CGW_ROOT="$PROJECT_ROOT"

if [ -n "$SERVICE" ]; then
    SCOPE_ARGS=(--service "$SERVICE")
    SCOPE_DESC="service=$SERVICE"
else
    SCOPE_ARGS=(--set "$SET")
    SCOPE_DESC="set=$SET"
fi

echo "================================================"
echo " CGW Build CI Pipeline"
echo " Platform: $PLATFORM, Profile: $PROFILE, $SCOPE_DESC, Jobs: $JOBS"
echo "================================================"

# Step 1: Validate manifests (host-side, no Docker needed)
echo ""
echo "[1/7] Lint - manifest/schema validation"
python3 -m cgw_build validate --platform "$PLATFORM"

# Steps 2-6: Build, install, ELF check, package
DRY_FLAG=""
if [ "$DRY_RUN" -eq 1 ]; then
    DRY_FLAG="--dry-run"
    echo ""
    echo "[2-6/7] Build, install, ELF check, package (DRY-RUN)"
    python3 -m cgw_build build "${SCOPE_ARGS[@]}" --platform "$PLATFORM" --profile "$PROFILE" --jobs "$JOBS" $DRY_FLAG
    echo ""
    echo " CI Pipeline: SUCCESS (dry-run)"
    exit 0
fi

echo ""
echo "[2-6/7] Build, install, ELF check, package"
# For target builds, run inside Docker; for host builds, run natively.
if [ "$PLATFORM" = "host" ]; then
    python3 -m cgw_build build "${SCOPE_ARGS[@]}" --platform "$PLATFORM" --profile "$PROFILE" --jobs "$JOBS"
    python3 -m cgw_build package --platform "$PLATFORM" --profile "$PROFILE"
else
    # Target build: requires Docker with cross-compile environment.
    # Container image is TBD per platform manifest (close_gate required).
    IMAGE_NAME="cgw-build:0.1-draft"
    if ! docker image inspect "$IMAGE_NAME" >/dev/null 2>&1; then
        echo "  Building $IMAGE_NAME..."
        docker build -t "$IMAGE_NAME" -f ci/Dockerfile . || {
            echo "ERROR: Docker build failed. Use --platform host for native builds."
            exit 1
        }
    fi
    WORKSPACE_PARENT="$(cd "$PROJECT_ROOT/.." && pwd)"
    BUILD_SUBDIR="$(basename "$PROJECT_ROOT")"
    CONTAINER_CGW_ROOT="/workspace/${BUILD_SUBDIR}"
    docker run --rm \
        -v "$WORKSPACE_PARENT":/workspace \
        -e CGW_ROOT="$CONTAINER_CGW_ROOT" \
        --platform linux/arm64 \
        "$IMAGE_NAME" \
        bash -c "cd '$CONTAINER_CGW_ROOT' && \
            python3 -m cgw_build build ${SCOPE_ARGS[*]} --profile $PROFILE --jobs $JOBS && \
            python3 -m cgw_build package --platform $PLATFORM --profile $PROFILE"
fi

# Step 7: Verify package
PKG=$(ls -t out/$PLATFORM/$PROFILE/packages/*.tar.gz 2>/dev/null | head -1)
if [ -z "$PKG" ]; then
    echo "ERROR: No package found"
    exit 1
fi

if [ "$BUILD_ONLY" -eq 1 ]; then
    echo ""
    echo "[7/7] Verify package - SKIPPED (--build-only)"
else
    echo ""
    echo "[7/7] Verify package"
    python3 -m cgw_build verify --platform "$PLATFORM" --profile "$PROFILE" --package "$PKG"
fi

echo ""
echo "================================================"
echo " CI Pipeline: SUCCESS"
echo " Package: $PKG"
echo "================================================"
