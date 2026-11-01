"""Unit tests for the config overlay engine (设计 §9.3/§9.5/§9.6)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from ccu_build.config import (
    ConfigOverlay,
    ConfigValidator,
    OverlayReport,
    atomic_write,
    normalize_source_path,
)
from ccu_build.errors import ConfigError, OverlayError
from ccu_build.manifest import (
    ConfigDeploymentManifest,
    ConfigDeploymentRule,
    PlatformConfigDeployment,
    Project,
    Service,
    ServiceManifest,
    RuntimeConfig,
    ConfigValidation,
)
from ccu_build.staging import sha256_file


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_deployment(rules: list[ConfigDeploymentRule]) -> ConfigDeploymentManifest:
    return ConfigDeploymentManifest(
        version=1,
        platforms={"ccu-dev": PlatformConfigDeployment(platform="ccu-dev", files=rules)},
    )


def _make_overlay(tmp_path: Path, rootfs: Path, rules=None, services=None,
                  project_root: Path | None = None) -> ConfigOverlay:
    p = Project(project_root or tmp_path, platform="ccu-dev") if (project_root or _has_project(tmp_path)) else None
    # Use real project for schema resolution; fall back to empty manifest.
    if project_root is not None:
        project = Project(project_root, platform="ccu-dev")
        sm = services if services is not None else project.load_service_manifest()
        dep = project.load_config_deployment_manifest() if rules is None else _make_deployment(rules)
        rootfs = project.platform_configs_root() if rootfs is None else rootfs
    else:
        sm = services or ServiceManifest(services={})
        dep = _make_deployment(rules or [])
    return ConfigOverlay(
        platform="ccu-dev",
        platform_rootfs=rootfs,
        install_root=tmp_path / "install-root",
        deployment=dep,
        service_manifest=sm,
        project_root=project_root or tmp_path,
        state_dir=tmp_path / "manifests",
    )


def _has_project(tmp_path: Path) -> bool:
    return False


def _write_service_default(install_root: Path, svc_id: str, content: bytes) -> str:
    """Write a service default config into install-root and return logical path."""
    logical = f"etc/ccu/conf.d/{svc_id}.yaml"
    target = install_root / logical
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return logical


# ---------------------------------------------------------------------------
# normalize_source_path (§9.3 step 1)
# ---------------------------------------------------------------------------


class TestNormalizeSourcePath:
    def test_valid_relative(self, tmp_path: Path):
        rootfs = tmp_path / "rootfs"
        (rootfs / "etc/ccu").mkdir(parents=True)
        f = rootfs / "etc/ccu/common.yaml"
        f.write_text("a: 1\n")
        assert normalize_source_path(rootfs, f) == "etc/ccu/common.yaml"

    def test_rejects_symlink(self, tmp_path: Path):
        rootfs = tmp_path / "rootfs"
        (rootfs / "etc/ccu").mkdir(parents=True)
        target = tmp_path / "outside.txt"
        target.write_text("x")
        link = rootfs / "etc/ccu/link.yaml"
        os.symlink(target, link)
        with pytest.raises(OverlayError, match="(?i)symlink"):
            normalize_source_path(rootfs, link)

    def test_rejects_symlink_escape(self, tmp_path: Path):
        rootfs = tmp_path / "rootfs"
        (rootfs / "etc/ccu").mkdir(parents=True)
        outside = tmp_path / "secret.yaml"
        outside.write_text("password: x\n")
        link = rootfs / "etc/ccu/escape.yaml"
        os.symlink(outside, link)
        with pytest.raises(OverlayError):
            normalize_source_path(rootfs, link)

    def test_rejects_directory(self, tmp_path: Path):
        rootfs = tmp_path / "rootfs"
        d = rootfs / "etc/ccu"
        d.mkdir(parents=True)
        with pytest.raises(OverlayError, match="Non-regular"):
            normalize_source_path(rootfs, d)


# ---------------------------------------------------------------------------
# atomic_write (§9.3 step 5)
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    def test_writes_content(self, tmp_path: Path):
        target = tmp_path / "a/b/c.yaml"
        atomic_write(target, b"hello\n")
        assert target.read_bytes() == b"hello\n"

    def test_replaces_existing(self, tmp_path: Path):
        target = tmp_path / "c.yaml"
        target.write_bytes(b"old")
        atomic_write(target, b"new")
        assert target.read_bytes() == b"new"

    def test_sets_mode(self, tmp_path: Path):
        target = tmp_path / "c.yaml"
        atomic_write(target, b"x", mode=0o600)
        assert oct(target.stat().st_mode & 0o777) == "0o600"

    def test_no_temp_left(self, tmp_path: Path):
        target = tmp_path / "c.yaml"
        atomic_write(target, b"x")
        leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".ccu-overlay")]
        assert leftovers == []


# ---------------------------------------------------------------------------
# ConfigOverlay.apply (§9.3)
# ---------------------------------------------------------------------------


class TestConfigOverlayApply:
    def _setup_real(self, project_root: Path, tmp_path: Path):
        """Overlay against real ccu-dev platform configs + example schemas."""
        project = Project(project_root, platform="ccu-dev")
        dep = project.load_config_deployment_manifest()
        sm = project.load_service_manifest()
        install_root = tmp_path / "install-root"
        install_root.mkdir(parents=True)
        state_dir = tmp_path / "manifests"
        state_dir.mkdir(parents=True)
        # Seed service defaults
        d1 = (project.root / "examples/minimal/config/ccu-hello-cli.default.yaml").read_bytes()
        d2 = (project.root / "examples/framework-consumer/config/framework-consumer.default.yaml").read_bytes()
        _write_service_default(install_root, "ccu-hello-cli", d1)
        _write_service_default(install_root, "framework-consumer", d2)
        file_owners = {
            "etc/ccu/conf.d/ccu-hello-cli.yaml": ("ccu-hello-cli", "ccu-hello-cli-runtime", sha256_file(install_root / "etc/ccu/conf.d/ccu-hello-cli.yaml")),
            "etc/ccu/conf.d/framework-consumer.yaml": ("framework-consumer", "ccu-framework-consumer-runtime", sha256_file(install_root / "etc/ccu/conf.d/framework-consumer.yaml")),
        }
        overlay = ConfigOverlay(
            platform="ccu-dev",
            platform_rootfs=project.platform_configs_root(),
            install_root=install_root,
            deployment=dep,
            service_manifest=sm,
            project_root=project.root,
            state_dir=state_dir,
        )
        return overlay, install_root, file_owners

    def test_overlays_authorized_files(self, project_root: Path, tmp_path: Path):
        overlay, install_root, owners = self._setup_real(project_root, tmp_path)
        report = overlay.apply(owners)
        paths = {r.logical_path for r in report.overlaid}
        assert "/etc/ccu/common.yaml" in paths
        assert "/etc/ccu/conf.d/ccu-hello-cli.yaml" in paths
        assert "/etc/ccu/conf.d/framework-consumer.yaml" in paths

    def test_whole_file_replace(self, project_root: Path, tmp_path: Path):
        overlay, install_root, owners = self._setup_real(project_root, tmp_path)
        report = overlay.apply(owners)
        # Final content must be the platform value, not the default
        final = (install_root / "etc/ccu/conf.d/ccu-hello-cli.yaml").read_text()
        assert "ccu-dev" in final
        rec = next(r for r in report.overlaid if "ccu-hello-cli" in r.logical_path)
        assert rec.pre_overlay_source == "ccu-hello-cli"
        assert rec.pre_overlay_sha256 != rec.final_sha256

    def test_pre_overlay_source_none_for_common(self, project_root: Path, tmp_path: Path):
        overlay, install_root, owners = self._setup_real(project_root, tmp_path)
        report = overlay.apply(owners)
        rec = next(r for r in report.overlaid if r.logical_path == "/etc/ccu/common.yaml")
        assert rec.pre_overlay_source == "(none)"
        assert rec.pre_overlay_sha256 == ""

    def test_validation_and_secret_pass(self, project_root: Path, tmp_path: Path):
        overlay, install_root, owners = self._setup_real(project_root, tmp_path)
        report = overlay.apply(owners)
        for r in report.overlaid:
            assert r.validation["status"] == "pass"
            assert r.secret_scan["status"] == "pass"
        overlay.check_report(report)

    def test_incremental_consistency(self, project_root: Path, tmp_path: Path):
        overlay, install_root, owners = self._setup_real(project_root, tmp_path)
        r1 = overlay.apply(owners)
        r2 = overlay.apply(owners)
        s1 = {r.logical_path: r.final_sha256 for r in r1.overlaid}
        s2 = {r.logical_path: r.final_sha256 for r in r2.overlaid}
        assert s1 == s2

    def test_stale_build_owned_removed(self, project_root: Path, tmp_path: Path):
        overlay, install_root, owners = self._setup_real(project_root, tmp_path)
        overlay.apply(owners)
        assert (install_root / "etc/ccu/common.yaml").exists()
        # Point overlay at empty rootfs to simulate removed platform configs
        empty = tmp_path / "empty-rootfs"
        empty.mkdir()
        project = Project(project_root, platform="ccu-dev")
        overlay2 = ConfigOverlay(
            platform="ccu-dev", platform_rootfs=empty, install_root=install_root,
            deployment=project.load_config_deployment_manifest(),
            service_manifest=project.load_service_manifest(),
            project_root=project.root, state_dir=tmp_path / "manifests",
        )
        report = overlay2.apply(owners)
        assert any(r["logical_path"] == "etc/ccu/common.yaml" for r in report.removed)
        assert not (install_root / "etc/ccu/common.yaml").exists()
        # Service-owned file is NOT removed (reset by re-install)
        assert (install_root / "etc/ccu/conf.d/ccu-hello-cli.yaml").exists()

    def test_skips_unauthorized_path(self, tmp_path: Path):
        rootfs = tmp_path / "rootfs"
        (rootfs / "etc/ccu").mkdir(parents=True)
        (rootfs / "etc/ccu/random.bin").write_bytes(b"x")
        # No rules -> everything unmatched
        overlay = _make_overlay(tmp_path, rootfs, rules=[], services=ServiceManifest(services={}))
        report = overlay.apply()
        assert len(report.overlaid) == 0
        assert any("no deploy policy" in s["reason"] for s in report.skipped)

    def test_skips_device_managed(self, tmp_path: Path):
        rootfs = tmp_path / "rootfs"
        (rootfs / "etc/ccu/credentials").mkdir(parents=True)
        (rootfs / "etc/ccu/credentials/device.yaml").write_bytes(b"k: v\n")
        rules = [_r(path_glob="/etc/ccu/credentials/**", owner="provisioning",
                    category="device-managed", deploy_policy="preserve")]
        overlay = _make_overlay(tmp_path, rootfs, rules=rules, services=ServiceManifest(services={}))
        report = overlay.apply()
        assert len(report.overlaid) == 0
        assert any("device-managed" in s["reason"] for s in report.skipped)


def _r(path=None, path_glob=None, owner="build", category="release-managed",
       deploy_policy="replace") -> ConfigDeploymentRule:
    return ConfigDeploymentRule(owner=owner, category=category,
                                deploy_policy=deploy_policy, path=path, path_glob=path_glob)


# ---------------------------------------------------------------------------
# Secret scan failure in overlay (§9.6)
# ---------------------------------------------------------------------------


class TestOverlaySecretFailure:
    def test_pem_in_platform_source_fails_check(self, tmp_path: Path):
        rootfs = tmp_path / "rootfs"
        (rootfs / "etc/ccu").mkdir(parents=True)
        (rootfs / "etc/ccu/common.yaml").write_bytes(
            b"-----BEGIN RSA PRIVATE KEY-----\nMIIBOQIBAA==\n-----END RSA PRIVATE KEY-----\n")
        rules = [_r(path="/etc/ccu/common.yaml")]
        (tmp_path / "install-root").mkdir(parents=True)
        (tmp_path / "manifests").mkdir()
        overlay = ConfigOverlay(
            platform="ccu-dev", platform_rootfs=rootfs,
            install_root=tmp_path / "install-root",
            deployment=_make_deployment(rules),
            service_manifest=ServiceManifest(services={}),
            project_root=tmp_path, state_dir=tmp_path / "manifests",
        )
        report = overlay.apply()
        rec = report.overlaid[0]
        assert rec.secret_scan["status"] == "fail"
        with pytest.raises(ConfigError, match="overlay validation failed"):
            overlay.check_report(report)


# ---------------------------------------------------------------------------
# Schema validation failure in overlay (§9.5)
# ---------------------------------------------------------------------------


class TestOverlaySchemaFailure:
    def test_invalid_config_fails_validation(self, tmp_path: Path):
        # Service declaring a schema that the platform file violates.
        rootfs = tmp_path / "rootfs"
        (rootfs / "etc/ccu/conf.d").mkdir(parents=True)
        # Missing required 'service_name'
        (rootfs / "etc/ccu/conf.d/svc.yaml").write_bytes(b"greeting: hi\n")
        # service source dir with schema
        svc_dir = tmp_path / "mysvc"
        (svc_dir / "config/schema").mkdir(parents=True)
        (svc_dir / "config/schema/svc.schema.yaml").write_text(
            '$schema: "http://json-schema.org/draft-07/schema#"\n'
            'type: object\nrequired: [service_name]\n'
            'properties:\n  service_name:\n    type: string\n  greeting:\n    type: string\n')
        cv = ConfigValidation(
            schema="config/schema/svc.schema.yaml",
            target_path="/etc/ccu/conf.d/svc.yaml",
            default_source="config/svc.default.yaml",
        )
        svc = Service(id="svc", repository="mysvc",
                      build=__import__("ccu_build.manifest", fromlist=["BuildConfig"]).BuildConfig(preset="p"),
                      runtime=RuntimeConfig(config_validation=cv))
        sm = ServiceManifest(services={"svc": svc})
        rules = [_r(path_glob="/etc/ccu/conf.d/*.yaml", owner="service")]
        (tmp_path / "install-root").mkdir(parents=True)
        (tmp_path / "manifests").mkdir()
        overlay = ConfigOverlay(
            platform="ccu-dev", platform_rootfs=rootfs,
            install_root=tmp_path / "install-root",
            deployment=_make_deployment(rules),
            service_manifest=sm, project_root=tmp_path,
            state_dir=tmp_path / "manifests",
        )
        report = overlay.apply()
        rec = report.overlaid[0]
        assert rec.validation["status"] == "fail"
        with pytest.raises(ConfigError, match="overlay validation failed"):
            overlay.check_report(report)

    def test_top_level_common_rejected(self, tmp_path: Path):
        rootfs = tmp_path / "rootfs"
        (rootfs / "etc/ccu/conf.d").mkdir(parents=True)
        (rootfs / "etc/ccu/conf.d/svc.yaml").write_bytes(
            b"common:\n  x: 1\nservice_name: svc\n")
        svc_dir = tmp_path / "mysvc"
        (svc_dir / "config/schema").mkdir(parents=True)
        (svc_dir / "config/schema/svc.schema.yaml").write_text(
            '$schema: "http://json-schema.org/draft-07/schema#"\n'
            'type: object\nrequired: [service_name]\n'
            'properties:\n  service_name:\n    type: string\n')
        cv = ConfigValidation(
            schema="config/schema/svc.schema.yaml",
            target_path="/etc/ccu/conf.d/svc.yaml",
            default_source="config/svc.default.yaml",
        )
        from ccu_build.manifest import BuildConfig
        svc = Service(id="svc", repository="mysvc", build=BuildConfig(preset="p"),
                      runtime=RuntimeConfig(config_validation=cv))
        sm = ServiceManifest(services={"svc": svc})
        rules = [_r(path_glob="/etc/ccu/conf.d/*.yaml", owner="service")]
        (tmp_path / "install-root").mkdir(parents=True)
        (tmp_path / "manifests").mkdir()
        overlay = ConfigOverlay(
            platform="ccu-dev", platform_rootfs=rootfs,
            install_root=tmp_path / "install-root",
            deployment=_make_deployment(rules),
            service_manifest=sm, project_root=tmp_path,
            state_dir=tmp_path / "manifests",
        )
        report = overlay.apply()
        rec = report.overlaid[0]
        assert rec.validation["status"] == "fail"
        assert "common" in rec.validation["errors"][0]


# ---------------------------------------------------------------------------
# ConfigValidator (§9.5)
# ---------------------------------------------------------------------------


class TestConfigValidator:
    def test_common_yaml_validates(self, project_root: Path, tmp_path: Path):
        project = Project(project_root, platform="ccu-dev")
        sm = project.load_service_manifest()
        v = ConfigValidator(sm, project.root)
        content = b"log_level: info\n"
        result = v.validate("etc/ccu/common.yaml", content)
        assert result["status"] == "pass"

    def test_common_yaml_parse_error(self, project_root: Path, tmp_path: Path):
        project = Project(project_root, platform="ccu-dev")
        sm = project.load_service_manifest()
        v = ConfigValidator(sm, project.root)
        result = v.validate("etc/ccu/common.yaml", b"\t\0bad: [")
        assert result["status"] == "fail"

    def test_service_config_validates(self, project_root: Path):
        project = Project(project_root, platform="ccu-dev")
        sm = project.load_service_manifest()
        v = ConfigValidator(sm, project.root)
        content = b"service_name: ccu-hello\ngreeting: hi\n"
        result = v.validate("etc/ccu/conf.d/ccu-hello-cli.yaml", content)
        assert result["status"] == "pass"

    def test_no_schema_skips(self, project_root: Path):
        project = Project(project_root, platform="ccu-dev")
        sm = project.load_service_manifest()
        v = ConfigValidator(sm, project.root)
        result = v.validate("etc/some/other.yaml", b"a: 1\n")
        assert result["status"] == "skip"


# ---------------------------------------------------------------------------
# OverlayReport (§9.3 step 7)
# ---------------------------------------------------------------------------


class TestOverlayReport:
    def test_save_and_load(self, tmp_path: Path):
        report = OverlayReport(platform="ccu-dev")
        report.skipped.append({"logical_path": "/x", "reason": "test"})
        out = tmp_path / "report.json"
        report.save(out)
        import json
        data = json.loads(out.read_text())
        assert data["platform"] == "ccu-dev"
        assert data["skipped"][0]["reason"] == "test"
