# CGW Build TARGET Dependency Recipes

This directory holds BUILD-controlled recipes for TARGET dependencies declared in
`manifests/dependency-lock.yaml`. Per CGW-BUILD-DSN-CR-002 §3 / 设计 §8.3, every
TARGET dependency is classified as one of: **sysroot**, **BUILD recipe**,
**vendor SDK**, or **源码内自包含 (source-internal)**, and the recipe lock
records version, source/digest, license, patch, profile, architecture, linkage
and artifact digest.

## Status (CR-002)

Real recipe execution (building locked deps from source into `CGW_DEP_STAGING`)
is a **future CR**. The orchestrator's `_prepare_target_dependencies` currently
**verifies controlled presence and fails closed** (CR-002 option (a)): if a
service declares `target_dependencies` but `CGW_DEP_STAGING` is empty, the build
is rejected before configure. Each project's own `find_package(... CONFIG
REQUIRED)` then fails closed on a missing package.

### yaml-cpp (framework's controlled TARGET dependency)

`yaml-cpp` is the CGW framework's first controlled TARGET dependency
(`cgw-framework-config` / `cgw-framework-log` link it PRIVATEly as a static
archive). It is recorded in `manifests/dependency-lock.yaml`.

- **HOST dev/test self-verification**: the sibling `iov-vehicle-cgw-framework`
  repo ships `scripts/stage-yaml-cpp.sh`, which stages a HOST-arch prebuilt
  `libyaml-cpp.a` + headers + a relocatable `yaml-cpp` CMake package into
  `CGW_DEP_STAGING`. This is a HOST-only dev/test artifact and MUST NOT be used
  for aarch64-linux target linking. The integration test
  (`tests/integration/test_framework_build.py`) pre-stages yaml-cpp this way
  before running the orchestrator.
- **TARGET build**: a BUILD controlled source-build recipe (build yaml-cpp from
  the pinned upstream source into `CGW_DEP_STAGING` with the target architecture
  and linkage) is implemented by a future CR once the dependency lock source
  digest is acquired (currently `PENDING`; release builds reject `PENDING`
  entries via `DependencyEntry.is_source_pinned == False`).

See `yaml-cpp.yaml` for the recipe stub.
