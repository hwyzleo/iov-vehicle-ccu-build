"""Unit tests for cross-reference validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from cgw_build.errors import ValidationFailure
from cgw_build.manifest import Service, ServiceManifest, BuildConfig, DependencyLock
from cgw_build.validator import (
    validate_service_dependencies,
    validate_target_dependencies,
    validate_library_kind,
    validate_systemd_references,
    validate_all,
)


def _svc(sid: str, deps=None, target_deps=None, kind="daemon",
         units=None, after=None, health=None, smoke=None) -> Service:
    return Service(
        id=sid, repository=".",
        build=BuildConfig(
            preset="p", targets=["t"],
            service_dependencies=deps or [],
            target_dependencies=target_deps or [],
        ),
        kind=kind,
    )


class TestServiceDependencyValidation:
    def test_self_dependency_rejected(self):
        manifest = ServiceManifest(services={
            "a": _svc("a", deps=["a"]),
        })
        with pytest.raises(ValidationFailure):
            validate_service_dependencies(manifest)

    def test_missing_dependency_rejected(self):
        manifest = ServiceManifest(services={
            "a": _svc("a", deps=["nonexistent"]),
        })
        with pytest.raises(ValidationFailure):
            validate_service_dependencies(manifest)

    def test_valid_dependencies_pass(self):
        manifest = ServiceManifest(services={
            "a": _svc("a", deps=["b"]),
            "b": _svc("b"),
        })
        validate_service_dependencies(manifest)


class TestTargetDependencyValidation:
    def test_missing_target_dep_rejected(self):
        manifest = ServiceManifest(services={
            "a": _svc("a", target_deps=["unlocked"]),
        })
        lock = DependencyLock(dependencies={})
        with pytest.raises(ValidationFailure):
            validate_target_dependencies(manifest, lock)

    def test_target_dep_is_service_id_rejected(self):
        manifest = ServiceManifest(services={
            "a": _svc("a", target_deps=["b"]),
            "b": _svc("b"),
        })
        lock = DependencyLock(dependencies={})
        with pytest.raises(ValidationFailure):
            validate_target_dependencies(manifest, lock)

    def test_valid_target_dep_passes(self):
        manifest = ServiceManifest(services={
            "a": _svc("a", target_deps=["yaml-cpp"]),
        })
        from cgw_build.manifest import DependencyEntry
        lock = DependencyLock(dependencies={
            "yaml-cpp": DependencyEntry(
                name="yaml-cpp", version="0.8.0", source_url="u",
                source_sha256="s", license="MIT", boundary="TARGET",
                architecture="aarch64", linkage="static",
            )
        })
        validate_target_dependencies(manifest, lock)


class TestLibraryKindValidation:
    def test_library_with_units_rejected(self):
        from cgw_build.manifest import RuntimeConfig
        manifest = ServiceManifest(services={
            "lib": Service(
                id="lib", repository=".", kind="library",
                build=BuildConfig(preset="p", targets=["t"]),
                runtime=RuntimeConfig(systemd_units=["lib.service"]),
            ),
        })
        with pytest.raises(ValidationFailure):
            validate_library_kind(manifest)

    def test_library_without_units_passes(self):
        manifest = ServiceManifest(services={
            "lib": _svc("lib", kind="library"),
        })
        validate_library_kind(manifest)


class TestSystemdReferenceValidation:
    def test_unknown_unit_rejected(self):
        from cgw_build.manifest import RuntimeConfig
        manifest = ServiceManifest(services={
            "a": Service(
                id="a", repository=".",
                build=BuildConfig(preset="p", targets=["t"]),
                runtime=RuntimeConfig(after=["nonexistent.service"]),
            ),
        })
        with pytest.raises(ValidationFailure):
            validate_systemd_references(manifest)

    def test_well_known_target_passes(self):
        from cgw_build.manifest import RuntimeConfig
        manifest = ServiceManifest(services={
            "a": Service(
                id="a", repository=".",
                build=BuildConfig(preset="p", targets=["t"]),
                runtime=RuntimeConfig(after=["network.target"]),
            ),
        })
        validate_systemd_references(manifest)


class TestFullValidation:
    def test_validate_all_passes(self, project_root: Path):
        from cgw_build.manifest import Project
        project = Project(project_root, platform="cgw-dev")
        manifest = project.load_service_manifest()
        lock = project.load_dependency_lock()
        warnings = validate_all(manifest, project_root, lock)
        assert isinstance(warnings, list)

    def test_validate_all_with_missing_dep(self):
        manifest = ServiceManifest(services={
            "a": _svc("a", deps=["nonexistent"]),
        })
        with pytest.raises(ValidationFailure):
            validate_all(manifest, Path("/tmp"), DependencyLock(dependencies={}))
