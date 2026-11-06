"""Unit tests for cross-reference validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from ccu_build.errors import ValidationFailure
from ccu_build.manifest import Service, ServiceManifest, BuildConfig, DependencyLock
from ccu_build.validator import (
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
        from ccu_build.manifest import DependencyEntry
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
        from ccu_build.manifest import RuntimeConfig
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
        from ccu_build.manifest import RuntimeConfig
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
        from ccu_build.manifest import RuntimeConfig
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
        from ccu_build.manifest import Project
        project = Project(project_root, platform="ccu-dev")
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


class TestReleaseSetAssetsValidation:
    """CCU-BUILD-DSN-CR-004 §3.2/§3.3/§4.2 manifest-side rules."""

    def _svc_with_runtime(self, units=None, assets=None) -> Service:
        from ccu_build.manifest import PlatformAsset, RuntimeConfig
        svc = _svc("svc-a")
        svc.runtime = RuntimeConfig(
            systemd_units=list(units or []),
            platform_assets=[
                PlatformAsset(
                    source=a[0], target=a[1],
                    required=a[2], validation=a[3],
                ) for a in (assets or [])
            ],
        )
        return svc

    def test_reserved_aggregation_target_rejected(self):
        from ccu_build.validator import validate_release_set_assets
        manifest = ServiceManifest(services={
            "svc-a": self._svc_with_runtime(units=["ccu.target"]),
        })
        with pytest.raises(ValidationFailure) as exc:
            validate_release_set_assets(manifest)
        assert "BUILD-reserved" in str(exc.value.details)

    @pytest.mark.parametrize(
        "unit", ["bad unit.service", "a/b.service", "../x.service", "x\ny.service"]
    )
    def test_illegal_unit_name_rejected(self, unit: str):
        from ccu_build.validator import validate_release_set_assets
        manifest = ServiceManifest(services={
            "svc-a": self._svc_with_runtime(units=[unit]),
        })
        with pytest.raises(ValidationFailure):
            validate_release_set_assets(manifest)

    def test_target_must_correspond_one_to_one(self):
        from ccu_build.validator import validate_release_set_assets
        manifest = ServiceManifest(services={
            "svc-a": self._svc_with_runtime(
                units=["svc-a.service"],
                assets=[("etc/ccu/someip/vsomeip.json",
                         "/etc/ccu/someip/renamed.json", True,
                         "vsomeip-json")],
            ),
        })
        with pytest.raises(ValidationFailure) as exc:
            validate_release_set_assets(manifest)
        assert any("one-to-one" in e for e in exc.value.details)

    def test_dropin_must_match_own_baseline_unit(self):
        from ccu_build.validator import validate_release_set_assets
        manifest = ServiceManifest(services={
            "svc-a": self._svc_with_runtime(
                units=["svc-a.service"],
                assets=[("usr/lib/systemd/system/svc-b.service.d/vsomeip.conf",
                         "/usr/lib/systemd/system/svc-b.service.d/vsomeip.conf",
                         True, "vsomeip-dropin")],
            ),
        })
        with pytest.raises(ValidationFailure) as exc:
            validate_release_set_assets(manifest)
        assert any("own baseline unit" in e for e in exc.value.details)

    def test_absolute_source_rejected(self):
        from ccu_build.validator import validate_release_set_assets
        manifest = ServiceManifest(services={
            "svc-a": self._svc_with_runtime(
                assets=[("/etc/ccu/someip/vsomeip.json",
                         "/etc/ccu/someip/vsomeip.json", True,
                         "vsomeip-json")],
            ),
        })
        with pytest.raises(ValidationFailure):
            validate_release_set_assets(manifest)

    def test_vsomeip_json_target_scope(self):
        from ccu_build.validator import validate_release_set_assets
        manifest = ServiceManifest(services={
            "svc-a": self._svc_with_runtime(
                assets=[("opt/vsomeip.json", "/opt/vsomeip.json", True,
                         "vsomeip-json")],
            ),
        })
        with pytest.raises(ValidationFailure) as exc:
            validate_release_set_assets(manifest)
        assert any("/etc/ccu/" in e for e in exc.value.details)

    def test_valid_declarations_pass(self):
        from ccu_build.validator import validate_release_set_assets
        manifest = ServiceManifest(services={
            "svc-a": self._svc_with_runtime(
                units=["svc-a.service"],
                assets=[
                    ("usr/lib/systemd/system/svc-a.service.d/vsomeip.conf",
                     "/usr/lib/systemd/system/svc-a.service.d/vsomeip.conf",
                     True, "vsomeip-dropin"),
                    ("etc/ccu/someip/vsomeip.json",
                     "/etc/ccu/someip/vsomeip.json", True, "vsomeip-json"),
                ],
            ),
        })
        validate_release_set_assets(manifest)  # no raise
