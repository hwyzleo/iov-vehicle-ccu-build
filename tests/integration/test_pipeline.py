"""Integration tests for CGW Build pipeline.

These tests verify the end-to-end flow:
  * Manifest parsing, schema validation and dependency ordering
  * Dry-run build plan generation
  * Full validation flow end-to-end
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cgw_build.manifest import Project
from cgw_build.schema import (
    validate_service_manifest,
    validate_release_set_manifest,
    validate_dependency_lock,
    validate_platform_manifest,
)
from cgw_build.validator import validate_all
from cgw_build.graph import DependencyGraph
from cgw_build.orchestrator import BuildOrchestrator, BuildConfig
from cgw_build.staging import StagingDir


class TestEndToEndValidation:
    """End-to-end manifest validation flow."""

    def test_full_validation_pipeline(self, project_root: Path):
        project = Project(project_root, platform="cgw-dev")

        # 1. Platform manifest schema validation
        from cgw_build.manifest import load_yaml
        pm_data = load_yaml(project.platform_manifest_path)
        validate_platform_manifest(pm_data, project_root)

        # 2. Service manifest schema validation
        svc_data = load_yaml(project.service_manifest_path)
        validate_service_manifest(svc_data, project_root)

        # 3. Release-set manifest schema validation
        rs_data = load_yaml(project.release_set_manifest_path)
        validate_release_set_manifest(rs_data, project_root)

        # 4. Dependency lock schema validation
        lock_data = load_yaml(project.dependency_lock_path)
        validate_dependency_lock(lock_data, project_root)

        # 5. Load manifests
        service_manifest = project.load_service_manifest()
        release_set_manifest = project.load_release_set_manifest()

        # 6. Cross-reference validation
        validate_all(service_manifest, project_root)

        # 7. Dependency graph
        graph = DependencyGraph(service_manifest)
        order = graph.build_order()
        assert len(order) == 2
        assert order.index("cgw-hello-lib") < order.index("cgw-hello-cli")

        # 8. Release set closure
        rs = release_set_manifest.get("cgw-dev-minimal")
        assert rs is not None
        closure = graph.closure(rs.services)
        assert closure == {"cgw-hello-lib", "cgw-hello-cli"}

    def test_platform_manifest_has_tbd_items(self, project_root: Path):
        """Platform manifest must have TBD items with closing gates (设计 §3)."""
        project = Project(project_root, platform="cgw-dev")
        pm = project.load_platform_manifest()
        assert pm.has_tbd is True  # TBD items present before platform confirmation

    def test_sysroot_manifest_pending(self, project_root: Path):
        """Sysroot manifest must be in pending state before import."""
        project = Project(project_root, platform="cgw-dev")
        sm = project.load_sysroot_manifest()
        assert sm.import_status == "pending"


class TestDryRunBuild:
    """Dry-run build plan generation (no actual compilation)."""

    def test_dry_run_build_plan(self, project_root: Path):
        project = Project(project_root, platform="cgw-dev")
        config = BuildConfig(
            platform="cgw-dev",
            profile="release",
            dry_run=True,
        )
        orch = BuildOrchestrator(project, config)
        report = orch.build(set_id="cgw-dev-minimal")

        # In dry-run, steps are "skipped" but order is determined
        assert report.status == "success"
        assert len(report.service_results) == 2
        assert report.service_results[0].id == "cgw-hello-lib"
        assert report.service_results[1].id == "cgw-hello-cli"
        for sr in report.service_results:
            assert sr.steps.get("configure") == "skipped"
            assert sr.steps.get("build") == "skipped"
            install_steps = [v for k, v in sr.steps.items() if k.startswith("install:")]
            assert install_steps and all(s == "skipped" for s in install_steps)

    def test_dry_run_single_service(self, project_root: Path):
        project = Project(project_root, platform="cgw-dev")
        config = BuildConfig(platform="cgw-dev", profile="release", dry_run=True)
        orch = BuildOrchestrator(project, config)
        report = orch.build(service_id="cgw-hello-cli")

        # Should include both the dependency and the service
        assert report.status == "success"
        assert len(report.service_results) == 2
        ids = [sr.id for sr in report.service_results]
        assert "cgw-hello-lib" in ids
        assert "cgw-hello-cli" in ids

    def test_dry_run_build_report_saved(self, project_root: Path):
        project = Project(project_root, platform="cgw-dev")
        config = BuildConfig(platform="cgw-dev", profile="release", dry_run=True)
        orch = BuildOrchestrator(project, config)
        report = orch.build(set_id="cgw-dev-minimal")

        # Build report should be saved
        report_path = orch.staging.manifests_dir / "build-report.json"
        assert report_path.exists()
        with open(report_path) as f:
            saved = json.load(f)
        assert saved["status"] == "success"
        assert saved["release_set"] == "cgw-dev-minimal"

    def test_dry_run_build_report_structure(self, project_root: Path):
        project = Project(project_root, platform="cgw-dev")
        config = BuildConfig(platform="cgw-dev", profile="release", dry_run=True)
        orch = BuildOrchestrator(project, config)
        report = orch.build(set_id="cgw-dev-minimal")

        assert report.platform == "cgw-dev"
        assert report.profile == "release"
        assert report.duration_seconds >= 0
        assert len(report.service_results) == 2
        for sr in report.service_results:
            assert sr.status == "success"

    def test_dry_run_target_dependencies_empty(self, project_root: Path):
        """No TARGET dependencies in the minimal example (CR-001)."""
        project = Project(project_root, platform="cgw-dev")
        config = BuildConfig(platform="cgw-dev", profile="release", dry_run=True)
        orch = BuildOrchestrator(project, config)
        report = orch.build(set_id="cgw-dev-minimal")

        assert report.target_dependencies == []

    def test_dry_run_host_build(self, project_root: Path):
        """Host platform dry-run build (native, no cross-compile)."""
        project = Project(project_root, platform="cgw-dev")
        config = BuildConfig(platform="host", profile="release", dry_run=True)
        orch = BuildOrchestrator(project, config)
        report = orch.build(set_id="cgw-dev-minimal")

        assert report.status == "success"
        assert report.platform == "host"


class TestDryRunPackage:
    """Dry-run packaging (staging exists from dry-run build)."""

    def test_dry_run_build_creates_staging(self, project_root: Path):
        project = Project(project_root, platform="cgw-dev")
        config = BuildConfig(platform="cgw-dev", profile="release", dry_run=True)
        orch = BuildOrchestrator(project, config)
        orch.build(set_id="cgw-dev-minimal")

        # Staging directories should exist
        assert orch.staging.root.exists()
        assert orch.staging.install_root.exists()
        assert orch.staging.manifests_dir.exists()


class TestELFCheck:
    """ELF checking with test fixtures."""

    def test_aarch64_elf_passes_check(self, minimal_aarch64_elf: Path):
        from cgw_build.elfcheck import check_file, EM_AARCH64
        result = check_file(minimal_aarch64_elf, expected_machine=EM_AARCH64)
        assert len(result.violations) == 0

    def test_macho_detected_as_violation(self, macho_binary: Path):
        from cgw_build.elfcheck import check_file, EM_AARCH64
        result = check_file(macho_binary, expected_machine=EM_AARCH64)
        assert len(result.violations) > 0

    def test_staging_check_with_fixture_files(
        self, tmp_path: Path,
        minimal_aarch64_elf: Path,
        macho_binary: Path,
    ):
        from cgw_build.elfcheck import check_staging, EM_AARCH64
        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "good.bin").write_bytes(minimal_aarch64_elf.read_bytes())
        (staging / "bad.bin").write_bytes(macho_binary.read_bytes())

        results = check_staging(staging, expected_machine=EM_AARCH64)
        assert len(results) == 2
        total_violations = sum(len(r.violations) for r in results)
        assert total_violations > 0  # Mach-O is a violation
