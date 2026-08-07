"""Unit tests for manifest loading and data model."""

from __future__ import annotations

from pathlib import Path

import pytest

from cgw_build.errors import ManifestError, SchemaValidationError
from cgw_build.manifest import (
    Project,
    Service,
    ServiceManifest,
    BuildConfig,
    InstallComponent,
    PlatformManifest,
    load_service_manifest,
    load_platform_manifest,
    load_dependency_lock,
    resolve_legacy_dependencies,
)


class TestProjectLayout:
    def test_project_root_validation(self, project_root: Path):
        project = Project(project_root, platform="cgw-dev")
        assert project.platform == "cgw-dev"
        assert project.platform_manifest_path.is_file()

    def test_unknown_platform_rejected(self, project_root: Path):
        with pytest.raises(ManifestError):
            Project(project_root, platform="nonexistent")

    def test_available_platforms(self, project_root: Path):
        project = Project(project_root, platform="cgw-dev")
        platforms = project.available_platforms()
        assert "cgw-dev" in platforms

    def test_platform_manifest_path(self, project_root: Path):
        project = Project(project_root, platform="cgw-dev")
        assert project.platform_manifest_path == project_root / "manifests" / "platforms" / "cgw-dev.yaml"

    def test_sysroot_manifest_path(self, project_root: Path):
        project = Project(project_root, platform="cgw-dev")
        assert project.sysroot_manifest_path == project_root / "manifests" / "platforms" / "cgw-dev-sysroot.yaml"

    def test_dependency_lock_path(self, project_root: Path):
        project = Project(project_root, platform="cgw-dev")
        assert project.dependency_lock_path == project_root / "manifests" / "dependency-lock.yaml"

    def test_toolchain_file_path(self, project_root: Path):
        project = Project(project_root, platform="cgw-dev")
        assert project.toolchain_file_path == project_root / "cmake" / "toolchains" / "cgw-target.cmake"


class TestPlatformManifest:
    def test_load_platform_manifest(self, project_root: Path):
        pm = load_platform_manifest(project_root / "manifests" / "platforms" / "cgw-dev.yaml")
        assert pm.platform == "cgw-dev"
        assert pm.version == "0.1.0-draft"
        assert pm.sysroot_id == "cgw-dev-sysroot"
        assert pm.has_tbd is True  # TBD items present

    def test_platform_properties(self, project_root: Path):
        project = Project(project_root, platform="cgw-dev")
        pm = project.load_platform_manifest()
        assert pm.platform == "cgw-dev"
        assert pm.runtime_user == "cgw"
        assert pm.persistent_state == "/var/lib/cgw"

    def test_sysroot_manifest(self, project_root: Path):
        project = Project(project_root, platform="cgw-dev")
        sm = project.load_sysroot_manifest()
        assert sm.id == "cgw-dev-sysroot"
        assert sm.import_status == "pending"


class TestServiceManifest:
    def test_load_service_manifest(self, project_root: Path):
        project = Project(project_root, platform="cgw-dev")
        manifest = project.load_service_manifest()
        assert len(manifest) == 2
        assert "cgw-hello-lib" in manifest
        assert "cgw-hello-cli" in manifest

    def test_service_properties(self, project_root: Path):
        project = Project(project_root, platform="cgw-dev")
        manifest = project.load_service_manifest()
        lib = manifest.get("cgw-hello-lib")
        assert lib is not None
        assert lib.kind == "library"
        assert lib.is_library is True
        assert lib.build.targets == ["cgw-hello-lib"]
        assert len(lib.build.install_components) == 1
        assert lib.build.install_components[0].name == "cgw-hello-lib-runtime"
        assert lib.build.install_components[0].staging == "rootfs"

    def test_cli_service_dependencies(self, project_root: Path):
        project = Project(project_root, platform="cgw-dev")
        manifest = project.load_service_manifest()
        cli = manifest.get("cgw-hello-cli")
        assert cli is not None
        assert cli.build.service_dependencies == ["cgw-hello-lib"]
        assert cli.runtime.systemd_units == ["cgw-hello.service"]

    def test_release_set_manifest(self, project_root: Path):
        project = Project(project_root, platform="cgw-dev")
        rs = project.load_release_set_manifest()
        assert len(rs) == 1
        minimal = rs.get("cgw-dev-minimal")
        assert minimal is not None
        assert minimal.services == ["cgw-hello-lib", "cgw-hello-cli"]

    def test_dependency_lock_empty(self, project_root: Path):
        project = Project(project_root, platform="cgw-dev")
        lock = project.load_dependency_lock()
        assert len(lock) == 0


class TestLegacyDependencyResolution:
    def test_legacy_dependency_classified_as_service(self):
        manifest = ServiceManifest(services={
            "a": Service(
                id="a", repository=".",
                build=BuildConfig(preset="p", targets=["t"], _legacy_dependencies=["b"]),
            ),
            "b": Service(
                id="b", repository=".",
                build=BuildConfig(preset="p", targets=["t"]),
            ),
        })
        resolve_legacy_dependencies(manifest, set())
        assert manifest.get("a").build.service_dependencies == ["b"]
        assert not manifest.get("a").build.has_legacy_dependencies

    def test_legacy_dependency_classified_as_target(self):
        manifest = ServiceManifest(services={
            "a": Service(
                id="a", repository=".",
                build=BuildConfig(preset="p", targets=["t"], _legacy_dependencies=["yaml-cpp"]),
            ),
        })
        resolve_legacy_dependencies(manifest, {"yaml-cpp"})
        assert manifest.get("a").build.target_dependencies == ["yaml-cpp"]

    def test_legacy_dependency_ambiguous_rejected(self):
        manifest = ServiceManifest(services={
            "a": Service(
                id="a", repository=".",
                build=BuildConfig(preset="p", targets=["t"], _legacy_dependencies=["b"]),
            ),
            "b": Service(
                id="b", repository=".",
                build=BuildConfig(preset="p", targets=["t"]),
            ),
        })
        with pytest.raises(SchemaValidationError):
            resolve_legacy_dependencies(manifest, {"b"})

    def test_legacy_dependency_unclassifiable_rejected(self):
        manifest = ServiceManifest(services={
            "a": Service(
                id="a", repository=".",
                build=BuildConfig(preset="p", targets=["t"], _legacy_dependencies=["unknown"]),
            ),
        })
        with pytest.raises(SchemaValidationError):
            resolve_legacy_dependencies(manifest, set())
