"""Pre-build manifest validation for CCU Build.

Performs filesystem-dependent and cross-reference checks that the
schema validator and pure-graph analyser cannot cover:

  * service_dependencies: self-dependency, missing service, cycles;
  * target_dependencies: must hit a lock entry;
  * kind: library services must not declare runtime units / health /
    smoke / after ordering;
  * source_dir: resolved path exists, contains a CMake project and stays
    within the approved workspace;
  * systemd unit reference consistency (``after`` -> declared units);
  * health/smoke script existence in service repositories;
  * full manifest validation entry point.
"""

from __future__ import annotations

import re
import stat
from pathlib import Path

from .assetcheck import dropin_baseline_unit
from .errors import ValidationFailure
from .graph import DependencyGraph
from .manifest import (
    RELEASE_SET_AGGREGATION_TARGET,
    DependencyLock,
    Project,
    ServiceManifest,
)

_UNIT_NAME_RE = re.compile(r"^[A-Za-z0-9@:_.+-]+$")

# Well-known systemd targets that services may legitimately depend on
_SYSTEMD_EXTERNAL_TARGETS = frozenset({
    "basic.target",
    "default.target",
    "multi-user.target",
    "network.target",
    "network-online.target",
    "sockets.target",
    "sysinit.target",
    "systemd-journald.service",
})


def validate_service_dependencies(manifest: ServiceManifest) -> None:
    """Check service_dependencies: no self-dep, all hit declared services."""
    violations: list[str] = []
    for svc in manifest:
        for dep in svc.build.service_dependencies:
            if dep == svc.id:
                violations.append(
                    f"Service '{svc.id}' must not depend on itself in "
                    f"service_dependencies"
                )
            elif dep not in manifest:
                violations.append(
                    f"Service '{svc.id}' has missing service dependency "
                    f"'{dep}' (not declared in services)"
                )
    if violations:
        raise ValidationFailure(
            f"service_dependencies validation failed ({len(violations)} violation(s))",
            violations,
        )


def validate_target_dependencies(
    manifest: ServiceManifest, lock: DependencyLock
) -> None:
    """Check target_dependencies: each must hit a lock entry; must not be a service id."""
    lock_names = lock.dependency_names()
    service_ids = set(manifest.services)
    violations: list[str] = []
    for svc in manifest:
        for dep in svc.build.target_dependencies:
            if dep in service_ids:
                violations.append(
                    f"Service '{svc.id}' target_dependencies entry '{dep}' is a "
                    f"service id; use service_dependencies for service ordering"
                )
            elif dep not in lock_names:
                violations.append(
                    f"Service '{svc.id}' has missing target dependency '{dep}' "
                    f"(not declared in dependency-lock.yaml)"
                )
    if violations:
        raise ValidationFailure(
            f"target_dependencies validation failed ({len(violations)} violation(s))",
            violations,
        )


def validate_library_kind(manifest: ServiceManifest) -> None:
    """kind: library services must not declare daemon/runtime artefacts."""
    violations: list[str] = []
    for svc in manifest:
        if not svc.is_library:
            continue
        if svc.runtime.systemd_units:
            violations.append(
                f"Service '{svc.id}' is kind: library but declares "
                f"runtime.systemd_units (libraries have no daemon units)"
            )
        if svc.runtime.after:
            violations.append(
                f"Service '{svc.id}' is kind: library but declares "
                f"runtime.after ordering (libraries have no daemon ordering)"
            )
        if svc.runtime.health_check:
            violations.append(
                f"Service '{svc.id}' is kind: library but declares "
                f"runtime.health_check (libraries have no daemon health)"
            )
        if svc.runtime.smoke_test:
            violations.append(
                f"Service '{svc.id}' is kind: library but declares "
                f"runtime.smoke_test (libraries have no daemon smoke test)"
            )
    if violations:
        raise ValidationFailure(
            f"library kind validation failed ({len(violations)} violation(s))",
            violations,
        )


def validate_source_dirs(manifest: ServiceManifest, project_root: Path) -> None:
    """Check source_dir resolves within the workspace and has a CMake project."""
    workspace_root = project_root.parent.resolve()
    violations: list[str] = []
    for svc in manifest:
        source_dir = project_root / svc.effective_source_dir
        try:
            resolved = source_dir.resolve()
        except OSError:
            violations.append(
                f"Service '{svc.id}' source_dir '{svc.effective_source_dir}' "
                f"could not be resolved"
            )
            continue
        try:
            resolved.relative_to(workspace_root)
        except ValueError:
            violations.append(
                f"Service '{svc.id}' source_dir '{svc.effective_source_dir}' "
                f"resolves outside the approved workspace ({workspace_root})"
            )
            continue
        if not resolved.is_dir():
            violations.append(
                f"Service '{svc.id}' source_dir '{svc.effective_source_dir}' "
                f"does not exist (resolved: {resolved})"
            )
            continue
        if not (resolved / "CMakeLists.txt").is_file():
            violations.append(
                f"Service '{svc.id}' source_dir '{svc.effective_source_dir}' "
                f"has no CMakeLists.txt (not a CMake project)"
            )
    if violations:
        raise ValidationFailure(
            f"source_dir validation failed ({len(violations)} violation(s))",
            violations,
        )


def validate_systemd_references(manifest: ServiceManifest) -> None:
    """Ensure every ``after`` entry references a declared or well-known unit."""
    declared_units: set[str] = set()
    for svc in manifest:
        declared_units.update(svc.runtime.systemd_units)

    violations: list[str] = []
    for svc in manifest:
        for after_unit in svc.runtime.after:
            if after_unit in declared_units:
                continue
            if after_unit in _SYSTEMD_EXTERNAL_TARGETS:
                continue
            violations.append(
                f"Service '{svc.id}' references unknown systemd unit "
                f"'{after_unit}' in runtime.after"
            )
    if violations:
        raise ValidationFailure(
            f"systemd unit reference validation failed ({len(violations)} violation(s))",
            violations,
        )


def validate_health_smoke(manifest: ServiceManifest, project_root: Path) -> None:
    """Check that declared health_check / smoke_test scripts exist on disk."""
    violations: list[str] = []
    for svc in manifest:
        svc_root = project_root / svc.effective_source_dir
        for check_type, rel_path in (
            ("health_check", svc.runtime.health_check),
            ("smoke_test", svc.runtime.smoke_test),
        ):
            if rel_path is None:
                continue
            full_path = svc_root / rel_path
            if not full_path.is_file():
                violations.append(
                    f"Service '{svc.id}' {check_type} script not found: {full_path}"
                )
                continue
            st = full_path.stat()
            if not (st.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)):
                violations.append(
                    f"Service '{svc.id}' {check_type} script not executable: {full_path}"
                )
    if violations:
        raise ValidationFailure(
            f"health/smoke script validation failed ({len(violations)} violation(s))",
            violations,
        )


def validate_config_validation(manifest: ServiceManifest) -> None:
    """Check config_validation declarations (设计 §9.5).

    A service that declares ``config_validation`` must provide schema,
    target_path and default_source. ``target_path`` must be under
    ``/etc/ccu/conf.d/`` (never ``common.yaml``, which is BUILD-only per
    REQ-004) and must be listed in ``runtime.config_paths``.
    """
    violations: list[str] = []
    for svc in manifest:
        cv = svc.runtime.config_validation
        if cv is None:
            continue
        if not cv.schema:
            violations.append(
                f"Service '{svc.id}' config_validation.schema is required"
            )
        if not cv.target_path:
            violations.append(
                f"Service '{svc.id}' config_validation.target_path is required"
            )
        if not cv.default_source:
            violations.append(
                f"Service '{svc.id}' config_validation.default_source is required"
            )
        if cv.target_path:
            if cv.target_path == "/etc/ccu/common.yaml":
                violations.append(
                    f"Service '{svc.id}' config_validation.target_path must not "
                    f"be /etc/ccu/common.yaml (common.yaml is BUILD-only, REQ-004)"
                )
            elif not cv.target_path.startswith("/etc/ccu/conf.d/"):
                violations.append(
                    f"Service '{svc.id}' config_validation.target_path must be "
                    f"under /etc/ccu/conf.d/"
                )
            if cv.target_path not in svc.runtime.config_paths:
                violations.append(
                    f"Service '{svc.id}' config_validation.target_path "
                    f"'{cv.target_path}' not listed in runtime.config_paths"
                )
    if violations:
        raise ValidationFailure(
            f"config_validation validation failed ({len(violations)} violation(s))",
            violations,
        )


def validate_release_set_assets(manifest: ServiceManifest) -> None:
    """Validate release-set aggregation units and platform assets.

    Covers CCU-BUILD-DSN-CR-004 §3.3/§4.2/§3.2 manifest-side rules:
      * ``ccu.target`` is BUILD-reserved and must not be service-declared;
      * unit names must be safe basenames (no whitespace/path/``..``);
      * ``runtime.platform_assets[]`` entries must be machine-readable,
        path-safe, one-to-one (target == "/" + source), use a declared
        validator and map drop-ins to the service's own baseline unit.
    """
    violations: list[str] = []
    for svc in manifest:
        for unit in svc.runtime.systemd_units:
            if unit == RELEASE_SET_AGGREGATION_TARGET:
                violations.append(
                    f"Service '{svc.id}' must not declare "
                    f"'{RELEASE_SET_AGGREGATION_TARGET}' (BUILD-reserved "
                    f"release-set aggregation target)"
                )
                continue
            if not _UNIT_NAME_RE.match(unit) or "/" in unit or ".." in unit:
                violations.append(
                    f"Service '{svc.id}' declares illegal unit name '{unit}' "
                    f"(basename only, no whitespace/path/'..')"
                )

        seen_targets: set[str] = set()
        for asset in svc.runtime.platform_assets:
            if asset.source.startswith("/"):
                violations.append(
                    f"Service '{svc.id}' platform asset source must be relative "
                    f"to configs/<platform>/rootfs: '{asset.source}'"
                )
            if "\\" in asset.source or ".." in asset.source.split("/"):
                violations.append(
                    f"Service '{svc.id}' platform asset source '{asset.source}' "
                    f"is not a safe POSIX path"
                )
            if asset.target in seen_targets:
                violations.append(
                    f"Service '{svc.id}' declares platform asset target "
                    f"'{asset.target}' more than once"
                )
            seen_targets.add(asset.target)
            if not asset.target.startswith("/"):
                violations.append(
                    f"Service '{svc.id}' platform asset target must be an "
                    f"absolute rootfs logical path: '{asset.target}'"
                )
            elif asset.target != "/" + asset.source.lstrip("/"):
                violations.append(
                    f"Service '{svc.id}' platform asset target '{asset.target}' "
                    f"must correspond one-to-one with source '{asset.source}'"
                )
            if asset.validation == "vsomeip-dropin":
                baseline = dropin_baseline_unit(asset.target)
                if baseline is None:
                    violations.append(
                        f"Service '{svc.id}' vsomeip drop-in target "
                        f"'{asset.target}' is not under a '*.service.d' directory"
                    )
                elif baseline not in svc.runtime.systemd_units:
                    violations.append(
                        f"Service '{svc.id}' vsomeip drop-in target "
                        f"'{asset.target}' must correspond to its own baseline "
                        f"unit '{baseline}'"
                    )
            if asset.validation == "vsomeip-json" and not asset.target.startswith(
                "/etc/ccu/"
            ):
                violations.append(
                    f"Service '{svc.id}' vsomeip-json asset target must be under "
                    f"/etc/ccu/: '{asset.target}'"
                )
    if violations:
        raise ValidationFailure(
            f"release-set unit/asset validation failed "
            f"({len(violations)} violation(s))",
            violations,
        )


def validate_all(
    manifest: ServiceManifest, project_root: Path, lock: DependencyLock | None = None
) -> list[str]:
    """Run all pre-build validations.

    Raises :class:`ValidationFailure`, :class:`DependencyError` or
    :class:`CycleError` on failure.  Returns a list of non-fatal warning
    strings.
    """
    warnings: list[str] = []

    if lock is None:
        try:
            project = Project(project_root)
            lock = project.load_dependency_lock()
        except Exception:
            from .manifest import DependencyLock
            lock = DependencyLock(dependencies={})

    # 1. service_dependencies: self-dep, missing
    validate_service_dependencies(manifest)

    # 2. target_dependencies: must hit lock, must not be service id
    validate_target_dependencies(manifest, lock)

    # 3. Dependency graph: cycles
    graph = DependencyGraph(manifest, lock)
    cycles = graph.detect_cycles()
    if cycles:
        raise ValidationFailure(
            f"Dependency cycles detected ({len(cycles)} cycle(s))",
            [f"{' -> '.join(c)}" for c in cycles],
        )

    # 4. kind: library runtime rules
    validate_library_kind(manifest)

    # 5. source_dir existence / workspace boundary / CMake project
    validate_source_dirs(manifest, project_root)

    # 6. systemd unit references
    validate_systemd_references(manifest)

    # 7. health/smoke script existence
    validate_health_smoke(manifest, project_root)

    # 8. config_validation declarations (CR-003 §9.5)
    validate_config_validation(manifest)

    # 9. release-set aggregation units and platform assets (CR-004 §3-§5)
    validate_release_set_assets(manifest)

    return warnings
