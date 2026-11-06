"""Unit tests for config-deployment manifest and deploy policy (设计 §9.4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ccu_build.config import DeployPolicyResolver, _glob_match
from ccu_build.errors import OverlayError, SchemaValidationError
from ccu_build.manifest import (
    ConfigDeploymentManifest,
    ConfigDeploymentRule,
    Project,
    load_config_deployment_manifest,
)
from ccu_build.schema import validate_config_deployment_manifest


def _rule(path=None, path_glob=None, owner="build", category="release-managed",
          deploy_policy="replace") -> ConfigDeploymentRule:
    return ConfigDeploymentRule(
        owner=owner, category=category, deploy_policy=deploy_policy,
        path=path, path_glob=path_glob,
    )


class TestConfigDeploymentSchema:
    def test_valid_manifest(self, project_root: Path):
        from ccu_build.manifest import load_yaml
        p = Project(project_root)
        data = load_yaml(p.config_deployment_manifest_path)
        validate_config_deployment_manifest(data, project_root)

    def test_invalid_deploy_policy(self):
        data = {"version": 1, "platforms": {"ccu-dev": {"files": [
            {"path": "/etc/ccu/x.yaml", "owner": "build",
             "category": "release-managed", "deploy_policy": "delete"},
        ]}}}
        with pytest.raises(SchemaValidationError):
            validate_config_deployment_manifest(data)

    def test_missing_owner(self):
        data = {"version": 1, "platforms": {"ccu-dev": {"files": [
            {"path": "/etc/ccu/x.yaml", "category": "release-managed",
             "deploy_policy": "replace"},
        ]}}}
        with pytest.raises(SchemaValidationError):
            validate_config_deployment_manifest(data)

    def test_path_and_path_glob_mutually_exclusive(self):
        data = {"version": 1, "platforms": {"ccu-dev": {"files": [
            {"path": "/etc/ccu/x.yaml", "path_glob": "/etc/ccu/*.yaml",
             "owner": "build", "category": "release-managed",
             "deploy_policy": "replace"},
        ]}}}
        with pytest.raises(SchemaValidationError):
            validate_config_deployment_manifest(data)

    def test_neither_path_nor_glob(self):
        data = {"version": 1, "platforms": {"ccu-dev": {"files": [
            {"owner": "build", "category": "release-managed",
             "deploy_policy": "replace"},
        ]}}}
        with pytest.raises(SchemaValidationError):
            validate_config_deployment_manifest(data)


class TestLoadConfigDeployment:
    def test_load_real_manifest(self, project_root: Path):
        from ccu_build.manifest import Project
        p = Project(project_root)
        m = p.load_config_deployment_manifest()
        assert m.version == 1
        assert "ccu-dev" in m
        plat = m.get("ccu-dev")
        assert plat is not None
        assert len(plat.files) == 5
        owners = {r.owner for r in plat.files}
        assert owners == {"build", "service", "provisioning"}
        # CCU-BUILD-DSN-CR-004 §6: BUILD owns the platform vsomeip assets.
        vsomeip = [r for r in plat.files if "vsomeip" in r.pattern or "someip" in r.pattern]
        assert len(vsomeip) == 2
        for rule in vsomeip:
            assert rule.owner == "build"
            assert rule.category == "release-managed"
            assert rule.deploy_policy == "replace"

    def test_load_returns_empty_when_absent(self, tmp_path: Path):
        # Build a minimal project root without config-deployment.yaml
        (tmp_path / "manifests" / "platforms").mkdir(parents=True)
        (tmp_path / "manifests" / "platforms" / "ccu-dev.yaml").write_text(
            "platform: ccu-dev\nversion: '1'\narchitecture: x86_64\n")
        (tmp_path / "manifests" / "services.yaml").write_text("services: {}")
        (tmp_path / "manifests" / "release-sets.yaml").write_text("release_sets: {}")
        (tmp_path / "manifests" / "dependency-lock.yaml").write_text("dependencies: {}")
        p = Project(tmp_path, platform="ccu-dev")
        m = p.load_config_deployment_manifest()
        assert m.version == 0
        assert len(m) == 0


class TestGlobMatch:
    def test_double_star_cross_dir(self):
        assert _glob_match("/etc/ccu/credentials/**", "/etc/ccu/credentials/a/b/c")

    def test_double_star_zero_segments(self):
        assert _glob_match("/etc/ccu/credentials/**", "/etc/ccu/credentials")

    def test_single_star_no_cross_dir(self):
        assert _glob_match("/etc/ccu/conf.d/*.yaml", "/etc/ccu/conf.d/foo.yaml")
        assert not _glob_match("/etc/ccu/conf.d/*.yaml", "/etc/ccu/conf.d/sub/foo.yaml")

    def test_exact_no_star(self):
        assert _glob_match("/etc/ccu/common.yaml", "/etc/ccu/common.yaml")
        assert not _glob_match("/etc/ccu/common.yaml", "/etc/ccu/other.yaml")


class TestDeployPolicyResolver:
    def test_exact_path_precedence_over_glob(self):
        rules = [
            _rule(path="/etc/ccu/conf.d/special.yaml", deploy_policy="preserve"),
            _rule(path_glob="/etc/ccu/conf.d/*.yaml", deploy_policy="replace"),
        ]
        r = DeployPolicyResolver(rules)
        assert r.resolve("/etc/ccu/conf.d/special.yaml").deploy_policy == "preserve"
        assert r.resolve("/etc/ccu/conf.d/normal.yaml").deploy_policy == "replace"

    def test_unmatched_returns_none_default_preserve(self):
        r = DeployPolicyResolver([_rule(path="/etc/ccu/common.yaml")])
        assert r.resolve("/etc/ccu/random.bin") is None

    def test_glob_conflict_different_policies_fails(self):
        rules = [
            _rule(path_glob="/etc/ccu/conf.d/*.yaml", deploy_policy="replace"),
            _rule(path_glob="/etc/ccu/conf.d/*", deploy_policy="preserve"),
        ]
        r = DeployPolicyResolver(rules)
        with pytest.raises(OverlayError, match="Conflicting"):
            r.resolve("/etc/ccu/conf.d/foo.yaml")

    def test_glob_same_policy_no_conflict(self):
        rules = [
            _rule(path_glob="/etc/ccu/conf.d/*.yaml", deploy_policy="replace"),
            _rule(path_glob="/etc/ccu/c*", deploy_policy="replace"),
        ]
        r = DeployPolicyResolver(rules)
        assert r.resolve("/etc/ccu/conf.d/foo.yaml").deploy_policy == "replace"

    def test_device_managed_replace_rejected(self):
        with pytest.raises(OverlayError, match="device-managed"):
            DeployPolicyResolver([_rule(
                path_glob="/etc/ccu/credentials/**", owner="provisioning",
                category="device-managed", deploy_policy="replace")])

    def test_duplicate_exact_path_rejected(self):
        with pytest.raises(OverlayError, match="Duplicate"):
            DeployPolicyResolver([
                _rule(path="/etc/ccu/common.yaml"),
                _rule(path="/etc/ccu/common.yaml", deploy_policy="preserve"),
            ])

    def test_device_managed_preserve_allowed(self):
        r = DeployPolicyResolver([_rule(
            path_glob="/etc/ccu/credentials/**", owner="provisioning",
            category="device-managed", deploy_policy="preserve")])
        assert r.resolve("/etc/ccu/credentials/x").deploy_policy == "preserve"
