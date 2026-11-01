"""Unit tests for BuildOrchestrator configure-command and TARGET dep preparation.

CCU-BUILD-DSN-CR-002 §4/§8.4: HOST self-verification find-root injection.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ccu_build.errors import BuildFailure
from ccu_build.graph import DependencyGraph
from ccu_build.manifest import Project
from ccu_build.orchestrator import BuildConfig, BuildOrchestrator
from ccu_build.staging import StagingDir


def _service(project_root: Path, sid: str):
    project = Project(project_root, platform="ccu-dev")
    return project.load_service_manifest().get(sid)


def _graph(project_root: Path) -> DependencyGraph:
    project = Project(project_root, platform="ccu-dev")
    manifest = project.load_service_manifest()
    lock = project.load_dependency_lock()
    return DependencyGraph(manifest, lock)


class TestHostConfigureInjection:
    """HOST builds inject an equivalent find-root set (no toolchain loaded)."""

    def test_host_framework_injects_dep_find_roots(self, project_root: Path):
        orch = BuildOrchestrator(
            Project(project_root, platform="host"),
            BuildConfig(platform="host", profile="release"),
        )
        cmd = orch._configure_cmd(_service(project_root, "framework"))
        dep_usr = str(orch.staging.dep_staging / "usr")
        # framework: target_dependencies=[yaml-cpp], no service_dependencies
        assert f"-DCMAKE_PREFIX_PATH={dep_usr}" in cmd
        assert f"-DCMAKE_FIND_ROOT_PATH={orch.staging.dep_staging}" in cmd
        assert "-DCMAKE_FIND_ROOT_PATH_MODE_PROGRAM=NEVER" in cmd
        assert "-DCMAKE_FIND_ROOT_PATH_MODE_LIBRARY=ONLY" in cmd
        assert "-DCMAKE_FIND_ROOT_PATH_MODE_INCLUDE=ONLY" in cmd
        assert "-DCMAKE_FIND_ROOT_PATH_MODE_PACKAGE=ONLY" in cmd
        # No toolchain file for HOST
        assert not any("CMAKE_TOOLCHAIN_FILE" in c for c in cmd)

    def test_host_consumer_injects_sdk_then_dep(self, project_root: Path):
        orch = BuildOrchestrator(
            Project(project_root, platform="host"),
            BuildConfig(platform="host", profile="release"),
        )
        cmd = orch._configure_cmd(_service(project_root, "framework-consumer"))
        sdk_usr = str(orch.staging.sdk_dir("framework") / "usr")
        dep_usr = str(orch.staging.dep_staging / "usr")
        prefix = next(c for c in cmd if c.startswith("-DCMAKE_PREFIX_PATH="))
        # 设计 §4 order: SDK staging -> TARGET dependency staging
        assert prefix == f"-DCMAKE_PREFIX_PATH={sdk_usr};{dep_usr}"

    def test_host_no_deps_no_injection(self, project_root: Path):
        orch = BuildOrchestrator(
            Project(project_root, platform="host"),
            BuildConfig(platform="host", profile="release"),
        )
        cmd = orch._configure_cmd(_service(project_root, "ccu-hello-lib"))
        assert not any(c.startswith("-DCMAKE_PREFIX_PATH=") for c in cmd)
        assert not any("CMAKE_FIND_ROOT_PATH_MODE" in c for c in cmd)

    def test_target_uses_toolchain_not_prefix_path(self, project_root: Path):
        orch = BuildOrchestrator(
            Project(project_root, platform="ccu-dev"),
            BuildConfig(platform="ccu-dev", profile="release"),
        )
        cmd = orch._configure_cmd(_service(project_root, "framework"))
        assert any("CMAKE_TOOLCHAIN_FILE" in c for c in cmd)
        # TARGET relies on the toolchain for find roots, not -DCMAKE_PREFIX_PATH
        assert not any(c.startswith("-DCMAKE_PREFIX_PATH=") for c in cmd)

    def test_host_env_records_dep_staging(self, project_root: Path):
        orch = BuildOrchestrator(
            Project(project_root, platform="host"),
            BuildConfig(platform="host", profile="release"),
        )
        env = orch._cmake_env()
        assert env["CCU_DEP_STAGING"] == str(orch.staging.dep_staging)


class TestTargetDependencyPreparation:
    """CR-002 option (a): BUILD verifies controlled dep presence (fail-closed)."""

    def test_dry_run_skips_staging_check(self, project_root: Path):
        orch = BuildOrchestrator(
            Project(project_root, platform="host"),
            BuildConfig(platform="host", profile="release", dry_run=True),
        )
        deps = orch._prepare_target_dependencies(_graph(project_root), ["framework-consumer"])
        assert deps == ["yaml-cpp"]

    def test_fail_closed_when_dep_staging_empty(self, project_root: Path, tmp_path: Path):
        orch = BuildOrchestrator(
            Project(project_root, platform="host"),
            BuildConfig(platform="host", profile="release"),
        )
        # Point staging at an empty tmp tree: no deps staged.
        orch.staging = StagingDir(tmp_path, "host", "release")
        orch.staging.prepare(clean=True)
        with pytest.raises(BuildFailure, match="CCU_DEP_STAGING"):
            orch._prepare_target_dependencies(_graph(project_root), ["framework"])
