"""Unit tests for config-deployment manifest and deploy policy (设计 §9.4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cgw_build.config import DeployPolicyResolver, _glob_match
from cgw_build.errors import OverlayError, SchemaValidationError
from cgw_build.manifest import (
    ConfigDeploymentManifest,
    ConfigDeploymentRule,
    Project,
    load_config_deployment_manifest,
)
from cgw_build.schema import validate_config_deployment_manifest


def _rule(path=None, path_glob=None, owner="build", category="release-managed",
          deploy_policy="replace") -> ConfigDeploymentRule:
    return ConfigDeploymentRule(
        owner=owner, category=category, deploy_policy=deploy_policy,
        path=path, path_glob=path_glob,
    )


class TestConfigDeploymentSchema:
    def test_valid_manifest(self, project_root: Path):
        from cgw_build.manifest import load_yaml
        p = Project(project_root)
        data = load_yaml(p.config_deployment_manifest_path)
        validate_config_deployment_manifest(data, project_root)

    def test_invalid_deploy_policy(self):
        data = {"version": 1, "platforms": {"cgw-dev": {"files": [
            {"path": "/etc/cgw/x.yaml", "owner": "build",
             "category": "release-managed", "deploy_policy": "delete"},
        ]}}}
        with pytest.raises(SchemaValidationError):
            validate_config_deployment_manifest(data)

    def test_missing_owner(self):
        data = {"version": 1, "platforms": {"cgw-dev": {"files": [
            {"path": "/etc/cgw/x.yaml", "category": "release-managed",
             "deploy_policy": "replace"},
        ]}}}
        with pytest.raises(SchemaValidationError):
            validate_config_deployment_manifest(data)

    def test_path_and_path_glob_mutually_exclusive(self):
        data = {"version": 1, "platforms": {"cgw-dev": {"files": [
            {"path": "/etc/cgw/x.yaml", "path_glob": "/etc/cgw/*.yaml",
             "owner": "build", "category": "release-managed",
             "deploy_policy": "replace"},
        ]}}}
        with pytest.raises(SchemaValidationError):
            validate_config_deployment_manifest(data)

    def test_neither_path_nor_glob(self):
        data = {"version": 1, "platforms": {"cgw-dev": {"files": [
            {"owner": "build", "category": "release-managed",
             "deploy_policy": "replace"},
        ]}}}
        with pytest.raises(SchemaValidationError):
            validate_config_deployment_manifest(data)


class TestLoadConfigDeployment:
    def test_load_real_manifest(self, project_root: Path):
        from cgw_build.manifest import Project
        p = Project(project_root)
        m = p.load_config_deployment_manifest()
        assert m.version == 1
        assert "cgw-dev" in m
        plat = m.get("cgw-dev")
        assert plat is not None
        assert len(plat.files) == 3
        owners = {r.owner for r in plat.files}
        assert owners == {"build", "service", "provisioning"}

    def test_load_returns_empty_when_absent(self, tmp_path: Path):
        # Build a minimal project root without config-deployment.yaml
        (tmp_path / "manifests" / "platforms").mkdir(parents=True)
        (tmp_path / "manifests" / "platforms" / "cgw-dev.yaml").write_text(
            "platform: cgw-dev\nversion: '1'\narchitecture: x86_64\n")
        (tmp_path / "manifests" / "services.yaml").write_text("services: {}")
        (tmp_path / "manifests" / "release-sets.yaml").write_text("release_sets: {}")
        (tmp_path / "manifests" / "dependency-lock.yaml").write_text("dependencies: {}")
        p = Project(tmp_path, platform="cgw-dev")
        m = p.load_config_deployment_manifest()
        assert m.version == 0
        assert len(m) == 0


class TestGlobMatch:
    def test_double_star_cross_dir(self):
        assert _glob_match("/etc/cgw/credentials/**", "/etc/cgw/credentials/a/b/c")

    def test_double_star_zero_segments(self):
        assert _glob_match("/etc/cgw/credentials/**", "/etc/cgw/credentials")

    def test_single_star_no_cross_dir(self):
        assert _glob_match("/etc/cgw/conf.d/*.yaml", "/etc/cgw/conf.d/foo.yaml")
        assert not _glob_match("/etc/cgw/conf.d/*.yaml", "/etc/cgw/conf.d/sub/foo.yaml")

    def test_exact_no_star(self):
        assert _glob_match("/etc/cgw/common.yaml", "/etc/cgw/common.yaml")
        assert not _glob_match("/etc/cgw/common.yaml", "/etc/cgw/other.yaml")


class TestDeployPolicyResolver:
    def test_exact_path_precedence_over_glob(self):
        rules = [
            _rule(path="/etc/cgw/conf.d/special.yaml", deploy_policy="preserve"),
            _rule(path_glob="/etc/cgw/conf.d/*.yaml", deploy_policy="replace"),
        ]
        r = DeployPolicyResolver(rules)
        assert r.resolve("/etc/cgw/conf.d/special.yaml").deploy_policy == "preserve"
        assert r.resolve("/etc/cgw/conf.d/normal.yaml").deploy_policy == "replace"

    def test_unmatched_returns_none_default_preserve(self):
        r = DeployPolicyResolver([_rule(path="/etc/cgw/common.yaml")])
        assert r.resolve("/etc/cgw/random.bin") is None

    def test_glob_conflict_different_policies_fails(self):
        rules = [
            _rule(path_glob="/etc/cgw/conf.d/*.yaml", deploy_policy="replace"),
            _rule(path_glob="/etc/cgw/conf.d/*", deploy_policy="preserve"),
        ]
        r = DeployPolicyResolver(rules)
        with pytest.raises(OverlayError, match="Conflicting"):
            r.resolve("/etc/cgw/conf.d/foo.yaml")

    def test_glob_same_policy_no_conflict(self):
        rules = [
            _rule(path_glob="/etc/cgw/conf.d/*.yaml", deploy_policy="replace"),
            _rule(path_glob="/etc/cgw/c*", deploy_policy="replace"),
        ]
        r = DeployPolicyResolver(rules)
        assert r.resolve("/etc/cgw/conf.d/foo.yaml").deploy_policy == "replace"

    def test_device_managed_replace_rejected(self):
        with pytest.raises(OverlayError, match="device-managed"):
            DeployPolicyResolver([_rule(
                path_glob="/etc/cgw/credentials/**", owner="provisioning",
                category="device-managed", deploy_policy="replace")])

    def test_duplicate_exact_path_rejected(self):
        with pytest.raises(OverlayError, match="Duplicate"):
            DeployPolicyResolver([
                _rule(path="/etc/cgw/common.yaml"),
                _rule(path="/etc/cgw/common.yaml", deploy_policy="preserve"),
            ])

    def test_device_managed_preserve_allowed(self):
        r = DeployPolicyResolver([_rule(
            path_glob="/etc/cgw/credentials/**", owner="provisioning",
            category="device-managed", deploy_policy="preserve")])
        assert r.resolve("/etc/cgw/credentials/x").deploy_policy == "preserve"
