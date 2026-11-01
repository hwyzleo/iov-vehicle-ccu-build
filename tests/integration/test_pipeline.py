"""Integration tests for CCU Build pipeline.

These tests verify the end-to-end flow:
  * Manifest parsing, schema validation and dependency ordering
  * Dry-run build plan generation
  * Full validation flow end-to-end
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ccu_build.manifest import Project
from ccu_build.schema import (
    validate_service_manifest,
    validate_release_set_manifest,
    validate_dependency_lock,
    validate_platform_manifest,
)
from ccu_build.validator import validate_all
from ccu_build.graph import DependencyGraph
from ccu_build.orchestrator import BuildOrchestrator, BuildConfig
from ccu_build.staging import StagingDir


class TestEndToEndValidation:
    """End-to-end manifest validation flow."""

    def test_full_validation_pipeline(self, project_root: Path):
        project = Project(project_root, platform="ccu-dev")

        # 1. Platform manifest schema validation
        from ccu_build.manifest import load_yaml
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
        lock = project.load_dependency_lock()
        graph = DependencyGraph(service_manifest, lock)
        order = graph.build_order()
        assert len(order) == 4
        assert order.index("ccu-hello-lib") < order.index("ccu-hello-cli")
        assert order.index("framework") < order.index("framework-consumer")

        # 8. Release set closure
        rs = release_set_manifest.get("ccu-dev-minimal")
        assert rs is not None
        closure = graph.closure(rs.services)
        assert closure == {"ccu-hello-lib", "ccu-hello-cli"}
        fw_rs = release_set_manifest.get("ccu-dev-framework")
        assert fw_rs is not None
        assert graph.closure(fw_rs.services) == {"framework", "framework-consumer"}
        assert graph.target_dependency_order(fw_rs.services) == ["yaml-cpp"]

    def test_platform_manifest_has_tbd_items(self, project_root: Path):
        """Platform manifest must have TBD items with closing gates (设计 §3)."""
        project = Project(project_root, platform="ccu-dev")
        pm = project.load_platform_manifest()
        assert pm.has_tbd is True  # TBD items present before platform confirmation

    def test_sysroot_manifest_pending(self, project_root: Path):
        """Sysroot manifest must be in pending state before import."""
        project = Project(project_root, platform="ccu-dev")
        sm = project.load_sysroot_manifest()
        assert sm.import_status == "pending"


class TestDryRunBuild:
    """Dry-run build plan generation (no actual compilation)."""

    def test_dry_run_build_plan(self, project_root: Path):
        project = Project(project_root, platform="ccu-dev")
        config = BuildConfig(
            platform="ccu-dev",
            profile="release",
            dry_run=True,
        )
        orch = BuildOrchestrator(project, config)
        report = orch.build(set_id="ccu-dev-minimal")

        # In dry-run, steps are "skipped" but order is determined
        assert report.status == "success"
        assert len(report.service_results) == 2
        assert report.service_results[0].id == "ccu-hello-lib"
        assert report.service_results[1].id == "ccu-hello-cli"
        for sr in report.service_results:
            assert sr.steps.get("configure") == "skipped"
            assert sr.steps.get("build") == "skipped"
            install_steps = [v for k, v in sr.steps.items() if k.startswith("install:")]
            assert install_steps and all(s == "skipped" for s in install_steps)

    def test_dry_run_single_service(self, project_root: Path):
        project = Project(project_root, platform="ccu-dev")
        config = BuildConfig(platform="ccu-dev", profile="release", dry_run=True)
        orch = BuildOrchestrator(project, config)
        report = orch.build(service_id="ccu-hello-cli")

        # Should include both the dependency and the service
        assert report.status == "success"
        assert len(report.service_results) == 2
        ids = [sr.id for sr in report.service_results]
        assert "ccu-hello-lib" in ids
        assert "ccu-hello-cli" in ids

    def test_dry_run_build_report_saved(self, project_root: Path):
        project = Project(project_root, platform="ccu-dev")
        config = BuildConfig(platform="ccu-dev", profile="release", dry_run=True)
        orch = BuildOrchestrator(project, config)
        report = orch.build(set_id="ccu-dev-minimal")

        # Build report should be saved
        report_path = orch.staging.manifests_dir / "build-report.json"
        assert report_path.exists()
        with open(report_path) as f:
            saved = json.load(f)
        assert saved["status"] == "success"
        assert saved["release_set"] == "ccu-dev-minimal"

    def test_dry_run_build_report_structure(self, project_root: Path):
        project = Project(project_root, platform="ccu-dev")
        config = BuildConfig(platform="ccu-dev", profile="release", dry_run=True)
        orch = BuildOrchestrator(project, config)
        report = orch.build(set_id="ccu-dev-minimal")

        assert report.platform == "ccu-dev"
        assert report.profile == "release"
        assert report.duration_seconds >= 0
        assert len(report.service_results) == 2
        for sr in report.service_results:
            assert sr.status == "success"

    def test_dry_run_target_dependencies_empty(self, project_root: Path):
        """No TARGET dependencies in the minimal example (CR-001)."""
        project = Project(project_root, platform="ccu-dev")
        config = BuildConfig(platform="ccu-dev", profile="release", dry_run=True)
        orch = BuildOrchestrator(project, config)
        report = orch.build(set_id="ccu-dev-minimal")

        assert report.target_dependencies == []

    def test_dry_run_host_build(self, project_root: Path):
        """Host platform dry-run build (native, no cross-compile)."""
        project = Project(project_root, platform="ccu-dev")
        config = BuildConfig(platform="host", profile="release", dry_run=True)
        orch = BuildOrchestrator(project, config)
        report = orch.build(set_id="ccu-dev-minimal")

        assert report.status == "success"
        assert report.platform == "host"

    def test_dry_run_framework_set(self, project_root: Path):
        """CCU-BUILD-DSN-CR-002: dry-run build of the framework release set."""
        project = Project(project_root, platform="ccu-dev")
        config = BuildConfig(platform="ccu-dev", profile="release", dry_run=True)
        orch = BuildOrchestrator(project, config)
        report = orch.build(set_id="ccu-dev-framework")

        assert report.status == "success"
        ids = [r.id for r in report.service_results]
        assert ids == ["framework", "framework-consumer"]
        # yaml-cpp TARGET dependency is collected (dry-run skips staging check)
        assert report.target_dependencies == ["yaml-cpp"]
        # framework install components recorded (sdk + runtime)
        fw = report.service_results[0]
        assert {c.name for c in fw.components} == {"ccu-framework-sdk", "ccu-framework-runtime"}


class TestDryRunPackage:
    """Dry-run packaging (staging exists from dry-run build)."""

    def test_dry_run_build_creates_staging(self, project_root: Path):
        project = Project(project_root, platform="ccu-dev")
        config = BuildConfig(platform="ccu-dev", profile="release", dry_run=True)
        orch = BuildOrchestrator(project, config)
        orch.build(set_id="ccu-dev-minimal")

        # Staging directories should exist
        assert orch.staging.root.exists()
        assert orch.staging.install_root.exists()
        assert orch.staging.manifests_dir.exists()


class TestELFCheck:
    """ELF checking with test fixtures."""

    def test_aarch64_elf_passes_check(self, minimal_aarch64_elf: Path):
        from ccu_build.elfcheck import check_file, EM_AARCH64
        result = check_file(minimal_aarch64_elf, expected_machine=EM_AARCH64)
        assert len(result.violations) == 0

    def test_macho_detected_as_violation(self, macho_binary: Path):
        from ccu_build.elfcheck import check_file, EM_AARCH64
        result = check_file(macho_binary, expected_machine=EM_AARCH64)
        assert len(result.violations) > 0

    def test_staging_check_with_fixture_files(
        self, tmp_path: Path,
        minimal_aarch64_elf: Path,
        macho_binary: Path,
    ):
        from ccu_build.elfcheck import check_staging, EM_AARCH64
        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "good.bin").write_bytes(minimal_aarch64_elf.read_bytes())
        (staging / "bad.bin").write_bytes(macho_binary.read_bytes())

        results = check_staging(staging, expected_machine=EM_AARCH64)
        assert len(results) == 2
        total_violations = sum(len(r.violations) for r in results)
        assert total_violations > 0  # Mach-O is a violation
