"""Integration test: real HOST build of the CGW framework + consumer.

CGW-BUILD-DSN-CR-002 §6/§8.6: end-to-end self-verification on the HOST
platform. Pre-stages the controlled yaml-cpp TARGET dependency, then runs
the full orchestrator pipeline:

  * framework: 5 static targets configure/build/install (sdk component with
    archives + relocatable CMake package; runtime component empty);
  * framework-consumer: find_package(CGWFramework) + link all five exported
    targets via the orchestrator-injected CMAKE_PREFIX_PATH (SDK -> dep
    staging), install runtime component;
  * artifact manifest records project/target/component/staging/SHA-256,
    the yaml-cpp dependency license, and sdk_consumers evidence.

Requires the sibling iov-vehicle-cgw-framework repo and a HOST-arch
prebuilt yaml-cpp (scripts/stage-yaml-cpp.sh); skipped otherwise.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from cgw_build.manifest import Project
from cgw_build.orchestrator import BuildConfig, BuildOrchestrator

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _framework_root() -> Path:
    return PROJECT_ROOT.parent / "iov-vehicle-cgw-framework"


@pytest.fixture(scope="module")
def built_framework():
    """Stage yaml-cpp and run the full HOST build once for the module."""
    fw_root = _framework_root()
    stage_script = fw_root / "scripts" / "stage-yaml-cpp.sh"
    if not stage_script.is_file():
        pytest.skip("framework repo / scripts/stage-yaml-cpp.sh not available")

    project = Project(PROJECT_ROOT, platform="host")
    config = BuildConfig(platform="host", profile="release", clean=False, jobs=4)
    orch = BuildOrchestrator(project, config)

    # Clean baseline (the orchestrator must not wipe our staged dep).
    if orch.staging.root.exists():
        shutil.rmtree(orch.staging.root)

    # Pre-stage controlled yaml-cpp into the orchestrator's CGW_DEP_STAGING.
    stage = subprocess.run(
        ["bash", str(stage_script)],
        env={**os.environ, "CGW_DEP_STAGING": str(orch.staging.dep_staging)},
        capture_output=True, text=True,
    )
    if stage.returncode != 0:
        pytest.skip(f"yaml-cpp HOST staging unavailable: {stage.stderr.strip()}")

    report = orch.build(set_id="cgw-dev-framework")
    yield orch, report
    if orch.staging.root.exists():
        shutil.rmtree(orch.staging.root, ignore_errors=True)


class TestFrameworkHostBuild:
    """Real HOST build of the cgw-dev-framework release set."""

    def test_framework_build_succeeds(self, built_framework):
        orch, report = built_framework
        assert report.status == "success", report.errors
        # framework builds before framework-consumer
        ids = [r.id for r in report.service_results]
        assert ids == ["framework", "framework-consumer"]
        # yaml-cpp is the staged TARGET dependency
        assert report.target_dependencies == ["yaml-cpp"]

    def test_framework_sdk_component_installed(self, built_framework):
        orch, _ = built_framework
        sdk_usr = orch.staging.sdk_dir("framework") / "usr"
        # Five static archives
        for comp in ("config", "log", "store", "hash", "someip"):
            assert (sdk_usr / "lib" / f"libcgw-framework-{comp}.a").is_file(), comp
        # Relocatable CMake package
        cmake_dir = sdk_usr / "lib" / "cmake" / "CGWFramework"
        assert (cmake_dir / "CGWFrameworkConfig.cmake").is_file()
        assert (cmake_dir / "CGWFrameworkTargets.cmake").is_file()
        # Headers
        assert (sdk_usr / "include" / "cgw" / "fw" / "hash" / "sha256.hpp").is_file()

    def test_framework_runtime_component_empty(self, built_framework):
        _, report = built_framework
        fw_result = next(r for r in report.service_results if r.id == "framework")
        # Two install components: sdk (populated) + runtime (empty, explicit)
        comp_by_name = {c.name: c for c in fw_result.components}
        assert "cgw-framework-sdk" in comp_by_name
        assert "cgw-framework-runtime" in comp_by_name
        assert len(comp_by_name["cgw-framework-sdk"].installed_files) > 0
        # 纯静态库：runtime component MUST be empty but explicitly recorded
        assert comp_by_name["cgw-framework-runtime"].installed_files == []

    def test_consumer_links_and_installs(self, built_framework):
        orch, report = built_framework
        con_result = next(r for r in report.service_results if r.id == "framework-consumer")
        assert con_result.status == "success"
        assert con_result.steps.get("configure") == "success"
        assert con_result.steps.get("build") == "success"
        # Runtime component: executable + systemd unit + config
        installed = con_result.components[0].installed_files
        assert any(f.endswith("bin/cgw-framework-consumer") for f in installed)
        assert any(f.endswith("cgw-framework-consumer.service") for f in installed)
        assert any(f.endswith("cgw-framework-consumer.conf") for f in installed)
        # The built consumer links the framework and reports OK
        bin_path = orch.staging.install_root / "usr" / "bin" / "cgw-framework-consumer"
        out = subprocess.run([str(bin_path)], capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
        assert "cgw-framework-consumer: OK" in out.stdout

    def test_artifact_manifest_records_evidence(self, built_framework):
        orch, _ = built_framework
        manifest_path = orch.staging.manifests_dir / "artifact-manifest.json"
        assert manifest_path.is_file()
        with open(manifest_path) as f:
            data = json.load(f)

        # Consumer evidence (设计 §8.6): framework is consumed by framework-consumer
        assert data["sdk_consumers"].get("framework") == ["framework-consumer"]

        # yaml-cpp dependency license/arch/linkage recorded (traceability)
        lock_summary = {d["name"]: d for d in data["dependency_lock"]}
        assert "yaml-cpp" in lock_summary
        assert lock_summary["yaml-cpp"]["license"] == "MIT"
        assert lock_summary["yaml-cpp"]["linkage"] == "static"

        # Artifact entries carry project/target/component/staging
        entries = data["artifacts"]
        fw_sdk = [e for e in entries
                  if e["owner_service"] == "framework" and e["staging"] == "sdk"]
        assert fw_sdk, "no framework sdk artifacts"
        assert all(e["install_component"] == "cgw-framework-sdk" for e in fw_sdk)
        assert any(e["path"].endswith("libcgw-framework-hash.a") for e in fw_sdk)

        con_rt = [e for e in entries
                  if e["owner_service"] == "framework-consumer" and e["staging"] == "rootfs"]
        assert con_rt, "no consumer rootfs artifacts"
        assert all(e["install_component"] == "cgw-framework-consumer-runtime" for e in con_rt)

        # Every non-symlink file artifact has a SHA-256
        assert all(e["sha256"] for e in entries if e["size"] > 0)
