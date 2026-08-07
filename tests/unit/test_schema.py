"""Unit tests for schema validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from cgw_build.errors import SchemaValidationError
from cgw_build.schema import (
    validate_service_manifest,
    validate_release_set_manifest,
    validate_dependency_lock,
    validate_platform_manifest,
    load_schema,
)


class TestServiceSchema:
    def test_valid_service_manifest(self, project_root: Path):
        from cgw_build.manifest import load_yaml
        data = load_yaml(project_root / "manifests" / "services.yaml")
        validate_service_manifest(data, project_root)

    def test_missing_required_field_repository(self, project_root: Path):
        data = {
            "services": {
                "svc-a": {
                    "build": {"target": "a", "preset": "cgw-dev-release"},
                }
            }
        }
        with pytest.raises(SchemaValidationError):
            validate_service_manifest(data, project_root)

    def test_missing_required_field_build(self, project_root: Path):
        data = {
            "services": {
                "svc-a": {
                    "repository": ".",
                }
            }
        }
        with pytest.raises(SchemaValidationError):
            validate_service_manifest(data, project_root)

    def test_missing_required_field_target(self, project_root: Path):
        data = {
            "services": {
                "svc-a": {
                    "repository": ".",
                    "build": {"preset": "cgw-dev-release"},
                }
            }
        }
        with pytest.raises(SchemaValidationError):
            validate_service_manifest(data, project_root)

    def test_unknown_field_rejected(self, project_root: Path):
        data = {
            "services": {
                "svc-a": {
                    "repository": ".",
                    "build": {"target": "a", "preset": "cgw-dev-release"},
                    "unknown_field": "bad",
                }
            }
        }
        with pytest.raises(SchemaValidationError):
            validate_service_manifest(data, project_root)

    def test_empty_services_rejected(self, project_root: Path):
        data = {"services": {}}
        with pytest.raises(SchemaValidationError):
            validate_service_manifest(data, project_root)

    def test_invalid_service_id_uppercase(self, project_root: Path):
        data = {
            "services": {
                "ServiceA": {
                    "repository": ".",
                    "build": {"target": "a", "preset": "cgw-dev-release"},
                }
            }
        }
        with pytest.raises(SchemaValidationError):
            validate_service_manifest(data, project_root)

    def test_target_and_targets_mutually_exclusive(self, project_root: Path):
        data = {
            "services": {
                "svc-a": {
                    "repository": ".",
                    "build": {
                        "target": "a",
                        "targets": ["a"],
                        "preset": "cgw-dev-release",
                    },
                }
            }
        }
        with pytest.raises(SchemaValidationError):
            validate_service_manifest(data, project_root)

    def test_optional_runtime_fields(self, project_root: Path):
        data = {
            "services": {
                "svc-a": {
                    "repository": ".",
                    "build": {"target": "a", "preset": "cgw-dev-release"},
                    "runtime": {
                        "systemd_units": ["svc-a.service"],
                        "after": ["network.target"],
                        "config_paths": ["/etc/cgw/svc-a"],
                    },
                }
            }
        }
        validate_service_manifest(data, project_root)


class TestReleaseSetSchema:
    def test_valid_release_set(self, project_root: Path):
        from cgw_build.manifest import load_yaml
        data = load_yaml(project_root / "manifests" / "release-sets.yaml")
        validate_release_set_manifest(data, project_root)

    def test_missing_services(self, project_root: Path):
        data = {
            "release_sets": {
                "test-set": {
                    "description": "test",
                }
            }
        }
        with pytest.raises(SchemaValidationError):
            validate_release_set_manifest(data, project_root)

    def test_empty_services_list(self, project_root: Path):
        data = {
            "release_sets": {
                "test-set": {
                    "services": [],
                }
            }
        }
        with pytest.raises(SchemaValidationError):
            validate_release_set_manifest(data, project_root)


class TestDependencyLockSchema:
    def test_valid_empty_lock(self, project_root: Path):
        from cgw_build.manifest import load_yaml
        data = load_yaml(project_root / "manifests" / "dependency-lock.yaml")
        validate_dependency_lock(data, project_root)

    def test_valid_entry(self, project_root: Path):
        data = {
            "dependencies": {
                "yaml-cpp": {
                    "version": "0.8.0",
                    "source": {"url": "https://example.com/yaml.tar.gz", "sha256": "abc123"},
                    "license": "MIT",
                    "boundary": "TARGET",
                    "architecture": "aarch64",
                    "linkage": "static",
                }
            }
        }
        validate_dependency_lock(data, project_root)

    def test_invalid_boundary(self, project_root: Path):
        data = {
            "dependencies": {
                "yaml-cpp": {
                    "version": "0.8.0",
                    "source": {"url": "https://example.com/yaml.tar.gz", "sha256": "abc123"},
                    "license": "MIT",
                    "boundary": "HOST",
                    "architecture": "aarch64",
                    "linkage": "static",
                }
            }
        }
        with pytest.raises(SchemaValidationError):
            validate_dependency_lock(data, project_root)


class TestPlatformSchema:
    def test_valid_platform_manifest(self, project_root: Path):
        from cgw_build.manifest import load_yaml
        data = load_yaml(project_root / "manifests" / "platforms" / "cgw-dev.yaml")
        validate_platform_manifest(data, project_root)

    def test_missing_platform(self, project_root: Path):
        data = {"version": "0.1.0", "architecture": "aarch64"}
        with pytest.raises(SchemaValidationError):
            validate_platform_manifest(data, project_root)

    def test_missing_sysroot_in_toolchain(self, project_root: Path):
        data = {
            "platform": "test",
            "version": "0.1.0",
            "architecture": "aarch64",
            "toolchain": {"cross_cc": "gcc"},
        }
        with pytest.raises(SchemaValidationError):
            validate_platform_manifest(data, project_root)


class TestSchemaLoading:
    def test_load_service_schema(self, project_root: Path):
        schema = load_schema("service", project_root)
        assert schema["title"] == "CGW Build Service Manifest"
        assert "services" in schema["properties"]

    def test_load_release_set_schema(self, project_root: Path):
        schema = load_schema("release-set", project_root)
        assert schema["title"] == "CGW Build Release Set Manifest"

    def test_load_lock_schema(self, project_root: Path):
        schema = load_schema("lock", project_root)
        assert schema["title"] == "CGW Build Dependency Lock"

    def test_load_platform_schema(self, project_root: Path):
        schema = load_schema("platform", project_root)
        assert schema["title"] == "CGW Build Platform Manifest"
