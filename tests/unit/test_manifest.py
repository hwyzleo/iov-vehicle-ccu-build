"""Unit tests for manifest loading and data model."""

from __future__ import annotations

from pathlib import Path

import pytest

from ccu_build.errors import ManifestError, SchemaValidationError
from ccu_build.manifest import (
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
        project = Project(project_root, platform="ccu-dev")
        assert project.platform == "ccu-dev"
        assert project.platform_manifest_path.is_file()

    def test_unknown_platform_rejected(self, project_root: Path):
        with pytest.raises(ManifestError):
            Project(project_root, platform="nonexistent")

    def test_available_platforms(self, project_root: Path):
        project = Project(project_root, platform="ccu-dev")
        platforms = project.available_platforms()
        assert "ccu-dev" in platforms

    def test_platform_manifest_path(self, project_root: Path):
        project = Project(project_root, platform="ccu-dev")
        assert project.platform_manifest_path == project_root / "manifests" / "platforms" / "ccu-dev.yaml"

    def test_sysroot_manifest_path(self, project_root: Path):
        project = Project(project_root, platform="ccu-dev")
        assert project.sysroot_manifest_path == project_root / "manifests" / "platforms" / "ccu-dev-sysroot.yaml"

    def test_dependency_lock_path(self, project_root: Path):
        project = Project(project_root, platform="ccu-dev")
        assert project.dependency_lock_path == project_root / "manifests" / "dependency-lock.yaml"

    def test_toolchain_file_path(self, project_root: Path):
        project = Project(project_root, platform="ccu-dev")
        assert project.toolchain_file_path == project_root / "cmake" / "toolchains" / "ccu-target.cmake"


class TestPlatformManifest:
    def test_load_platform_manifest(self, project_root: Path):
        pm = load_platform_manifest(project_root / "manifests" / "platforms" / "ccu-dev.yaml")
        assert pm.platform == "ccu-dev"
        assert pm.version == "0.1.0-draft"
        assert pm.sysroot_id == "ccu-dev-sysroot"
        assert pm.has_tbd is True  # TBD items present

    def test_platform_properties(self, project_root: Path):
        project = Project(project_root, platform="ccu-dev")
        pm = project.load_platform_manifest()
        assert pm.platform == "ccu-dev"
        assert pm.runtime_user == "ccu"
        assert pm.persistent_state == "/var/lib/ccu"

    def test_sysroot_manifest(self, project_root: Path):
        project = Project(project_root, platform="ccu-dev")
        sm = project.load_sysroot_manifest()
        assert sm.id == "ccu-dev-sysroot"
        assert sm.import_status == "pending"


class TestServiceManifest:
    def test_load_service_manifest(self, project_root: Path):
        project = Project(project_root, platform="ccu-dev")
        manifest = project.load_service_manifest()
        assert len(manifest) == 4
        assert "ccu-hello-lib" in manifest
        assert "ccu-hello-cli" in manifest
        assert "framework" in manifest
        assert "framework-consumer" in manifest

    def test_service_properties(self, project_root: Path):
        project = Project(project_root, platform="ccu-dev")
        manifest = project.load_service_manifest()
        lib = manifest.get("ccu-hello-lib")
        assert lib is not None
        assert lib.kind == "library"
        assert lib.is_library is True
        assert lib.build.targets == ["ccu-hello-lib"]
        assert len(lib.build.install_components) == 1
        assert lib.build.install_components[0].name == "ccu-hello-lib-runtime"
        assert lib.build.install_components[0].staging == "rootfs"

    def test_cli_service_dependencies(self, project_root: Path):
        project = Project(project_root, platform="ccu-dev")
        manifest = project.load_service_manifest()
        cli = manifest.get("ccu-hello-cli")
        assert cli is not None
        assert cli.build.service_dependencies == ["ccu-hello-lib"]
        assert cli.runtime.systemd_units == ["ccu-hello.service"]

    def test_release_set_manifest(self, project_root: Path):
        project = Project(project_root, platform="ccu-dev")
        rs = project.load_release_set_manifest()
        assert len(rs) == 2
        minimal = rs.get("ccu-dev-minimal")
        assert minimal is not None
        assert minimal.services == ["ccu-hello-lib", "ccu-hello-cli"]
        framework = rs.get("ccu-dev-framework")
        assert framework is not None
        assert framework.services == ["framework", "framework-consumer"]

    def test_dependency_lock_yaml_cpp(self, project_root: Path):
        """CCU-BUILD-DSN-CR-002: yaml-cpp is the framework's controlled TARGET dep."""
        project = Project(project_root, platform="ccu-dev")
        lock = project.load_dependency_lock()
        assert len(lock) == 1
        assert "yaml-cpp" in lock
        dep = lock.get("yaml-cpp")
        assert dep is not None
        assert dep.boundary == "TARGET"
        assert dep.linkage == "static"
        assert dep.license == "MIT"
        # Source digest is PENDING (draft); release builds must reject it.
        assert dep.is_source_pinned is False
        assert dep.is_static is True

    def test_framework_service_metadata(self, project_root: Path):
        """CCU-BUILD-DSN-CR-002 §1: framework library with five targets."""
        project = Project(project_root, platform="ccu-dev")
        manifest = project.load_service_manifest()
        fw = manifest.get("framework")
        assert fw is not None
        assert fw.kind == "library"
        assert fw.is_library is True
        assert fw.build.targets == [
            "ccu-framework-config",
            "ccu-framework-log",
            "ccu-framework-store",
            "ccu-framework-hash",
            "ccu-framework-someip",
        ]
        assert fw.build.service_dependencies == []
        assert fw.build.target_dependencies == ["yaml-cpp"]
        assert fw.runtime.systemd_units == []
        assert len(fw.build.install_components) == 2
        sdk = fw.build.install_components[0]
        assert sdk.name == "ccu-framework-sdk"
        assert sdk.staging == "sdk"
        rt = fw.build.install_components[1]
        assert rt.name == "ccu-framework-runtime"
        assert rt.staging == "rootfs"

    def test_framework_consumer_service(self, project_root: Path):
        """CCU-BUILD-DSN-CR-002 §5: consumer depends on framework + yaml-cpp."""
        project = Project(project_root, platform="ccu-dev")
        manifest = project.load_service_manifest()
        consumer = manifest.get("framework-consumer")
        assert consumer is not None
        assert consumer.kind == "daemon"
        assert consumer.build.service_dependencies == ["framework"]
        assert consumer.build.target_dependencies == ["yaml-cpp"]
        assert consumer.build.targets == ["ccu-framework-consumer"]
        assert consumer.runtime.systemd_units == ["ccu-framework-consumer.service"]


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
