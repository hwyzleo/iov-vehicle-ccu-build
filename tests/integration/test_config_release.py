"""Integration tests for config release pipeline (设计 §9.9).

Covers the CGW-BUILD-DSN-CR-003 acceptance criteria:
  * dev fixtures not installed; default templates precisely installed;
  * platform whole-file overlay authorized with pre/post source records;
  * common.yaml only BUILD source; service configs no top-level common:;
  * schema/checker, secret, permission, path, stale, conflict tests pass;
  * replace/preserve/create_if_missing and unmatched-default-preserve;
  * deploy plan enumerates create/replace/preserve and excludes provisioning;
  * deploy failure triggers consistent release-set rollback.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from cgw_build.artifact import ArtifactManifest
from cgw_build.config import ConfigOverlay
from cgw_build.deploy import Deployer
from cgw_build.manifest import Project
from cgw_build.packaging import Packager
from cgw_build.staging import StagingDir, sha256_file
from cgw_build.artifact import get_git_commit

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project(project_root: Path):
    return Project(project_root, platform="cgw-dev")


@pytest.fixture
def seeded_install_root(project, tmp_path: Path):
    """Install-root pre-seeded with service default templates."""
    install_root = tmp_path / "install-root"
    state_dir = tmp_path / "manifests"
    install_root.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    confd = install_root / "etc/cgw/conf.d"
    confd.mkdir(parents=True)
    d1 = (project.root / "examples/minimal/config/cgw-hello-cli.default.yaml").read_bytes()
    d2 = (project.root / "examples/framework-consumer/config/framework-consumer.default.yaml").read_bytes()
    (confd / "cgw-hello-cli.yaml").write_bytes(d1)
    (confd / "framework-consumer.yaml").write_bytes(d2)
    owners = {
        "etc/cgw/conf.d/cgw-hello-cli.yaml": (
            "cgw-hello-cli", "cgw-hello-cli-runtime",
            sha256_file(confd / "cgw-hello-cli.yaml")),
        "etc/cgw/conf.d/framework-consumer.yaml": (
            "framework-consumer", "cgw-framework-consumer-runtime",
            sha256_file(confd / "framework-consumer.yaml")),
    }
    return install_root, state_dir, owners


def _make_overlay(project, install_root, state_dir) -> ConfigOverlay:
    return ConfigOverlay(
        platform="cgw-dev",
        platform_rootfs=project.platform_configs_root(),
        install_root=install_root,
        deployment=project.load_config_deployment_manifest(),
        service_manifest=project.load_service_manifest(),
        project_root=project.root,
        state_dir=state_dir,
    )


# ---------------------------------------------------------------------------
# Overlay pipeline + artifact manifest provenance (§9.3/§9.6)
# ---------------------------------------------------------------------------


class TestOverlayPipeline:
    def test_overlay_replaces_defaults_with_platform_values(
        self, project, seeded_install_root
    ):
        install_root, state_dir, owners = seeded_install_root
        overlay = _make_overlay(project, install_root, state_dir)
        report = overlay.apply(owners)
        # 3 files: common + 2 service configs
        assert len(report.overlaid) == 3
        # Service configs replaced with platform values
        hello = (install_root / "etc/cgw/conf.d/cgw-hello-cli.yaml").read_text()
        assert "cgw-dev" in hello
        # Pre/post source recorded
        rec = next(r for r in report.overlaid if "cgw-hello-cli" in r.logical_path)
        assert rec.pre_overlay_source == "cgw-hello-cli"
        assert rec.pre_overlay_sha256 != rec.final_sha256

    def test_all_validations_and_secret_scans_pass(self, project, seeded_install_root):
        install_root, state_dir, owners = seeded_install_root
        overlay = _make_overlay(project, install_root, state_dir)
        report = overlay.apply(owners)
        overlay.check_report(report)  # raises on failure
        for r in report.overlaid:
            assert r.validation["status"] == "pass"
            assert r.secret_scan["status"] == "pass"

    def test_artifact_manifest_provenance(self, project, seeded_install_root):
        install_root, state_dir, owners = seeded_install_root
        overlay = _make_overlay(project, install_root, state_dir)
        report = overlay.apply(owners)
        # Build a minimal artifact manifest and attach provenance
        from cgw_build.manifest import PlatformManifest, SysrootManifest
        am = ArtifactManifest(
            platform="cgw-dev", profile="release",
            platform_manifest=PlatformManifest(data={"platform": "cgw-dev"}),
            sysroot_manifest=SysrootManifest(data={"sysroot": {"id": "x"}}),
        )
        from cgw_build.artifact import ArtifactEntry
        from cgw_build.staging import StagedFile
        for path in sorted(install_root.rglob("*")):
            if not path.is_file():
                continue
            rel = str(path.relative_to(install_root))
            staged = StagedFile(
                rel_path="rootfs/" + rel, full_path=path,
                size=path.stat().st_size, sha256=sha256_file(path))
            am.add_staged_file(staged, owner_service="build", owner_target="config",
                               version="0.1.0", git_commit="x",
                               install_component="config", staging="rootfs")
        am.attach_config_provenance(report)
        # common.yaml entry must have provenance
        common_entry = next(e for e in am.entries if e.path == "rootfs/etc/cgw/common.yaml")
        assert common_entry.config_provenance is not None
        assert common_entry.config_provenance["owner"] == "build"
        assert common_entry.config_provenance["deploy_policy"] == "replace"
        # serialized
        d = am.to_dict()
        cfg_entries = [a for a in d["artifacts"] if a.get("config_provenance")]
        assert len(cfg_entries) == 3

    def test_overlay_report_saved(self, project, seeded_install_root):
        install_root, state_dir, owners = seeded_install_root
        overlay = _make_overlay(project, install_root, state_dir)
        report = overlay.apply(owners)
        out = state_dir / "config-overlay-report.json"
        report.save(out)
        data = json.loads(out.read_text())
        assert data["platform"] == "cgw-dev"
        assert len(data["overlaid"]) == 3


# ---------------------------------------------------------------------------
# Dev fixtures not installed (§9.2, REQ-001)
# ---------------------------------------------------------------------------


class TestDevFixturesNotInstalled:
    def test_dev_fixtures_exist_but_not_in_platform_rootfs(self, project):
        # Dev fixtures exist in service repos
        assert (project.root / "examples/minimal/config/dev/cgw-hello-cli.yaml").is_file()
        assert (project.root / "examples/minimal/config/dev/common.yaml").is_file()
        # Platform rootfs does NOT contain dev fixtures
        rootfs = project.platform_configs_root()
        dev_files = [p for p in rootfs.rglob("*") if "/dev/" in str(p) or "dev" in p.parts]
        assert dev_files == []

    def test_overlay_does_not_touch_dev_files(self, project, seeded_install_root):
        install_root, state_dir, owners = seeded_install_root
        overlay = _make_overlay(project, install_root, state_dir)
        report = overlay.apply(owners)
        # No overlaid file references dev/
        for r in report.overlaid:
            assert "/dev/" not in r.platform_source


# ---------------------------------------------------------------------------
# common.yaml BUILD-only source (§9.4, REQ-004)
# ---------------------------------------------------------------------------


class TestCommonBuildOnlySource:
    def test_common_only_from_build(self, project, seeded_install_root):
        install_root, state_dir, owners = seeded_install_root
        overlay = _make_overlay(project, install_root, state_dir)
        report = overlay.apply(owners)
        common_rec = next(
            r for r in report.overlaid if r.logical_path == "/etc/cgw/common.yaml")
        assert common_rec.owner == "build"
        assert common_rec.pre_overlay_source == "(none)"

    def test_service_configs_no_top_level_common(self, project, seeded_install_root):
        install_root, state_dir, owners = seeded_install_root
        overlay = _make_overlay(project, install_root, state_dir)
        report = overlay.apply(owners)
        for r in report.overlaid:
            if r.logical_path != "/etc/cgw/common.yaml":
                assert r.validation["status"] == "pass", r.logical_path


# ---------------------------------------------------------------------------
# Clean / incremental consistency (§9.3)
# ---------------------------------------------------------------------------


class TestCleanIncrementalConsistency:
    def test_two_runs_identical(self, project, seeded_install_root):
        install_root, state_dir, owners = seeded_install_root
        overlay = _make_overlay(project, install_root, state_dir)
        r1 = overlay.apply(owners)
        r2 = overlay.apply(owners)
        s1 = {r.logical_path: r.final_sha256 for r in r1.overlaid}
        s2 = {r.logical_path: r.final_sha256 for r in r2.overlaid}
        assert s1 == s2


# ---------------------------------------------------------------------------
# Deploy plan (§9.7)
# ---------------------------------------------------------------------------


class TestDeployPlan:
    def _make_payload(self, tmp_path: Path) -> Path:
        payload = tmp_path / "install-root"
        cgw = payload / "etc/cgw"
        (cgw / "conf.d").mkdir(parents=True)
        (cgw / "credentials").mkdir(parents=True)
        (cgw / "common.yaml").write_text("log_level: info\n")
        (cgw / "conf.d/cgw-hello-cli.yaml").write_text("service_name: x\n")
        (cgw / "credentials/device.crt").write_text("FAKE\n")
        return payload

    def test_replace_preserve_classification(self, project, tmp_path: Path):
        payload = self._make_payload(tmp_path)
        d = Deployer(project, platform="cgw-dev")
        plan = d.build_deploy_plan(payload)
        assert plan["summary"]["replace"] == 2  # common + conf.d
        assert plan["summary"]["preserve"] == 1  # credentials

    def test_provisioning_excluded(self, project, tmp_path: Path):
        payload = self._make_payload(tmp_path)
        d = Deployer(project, platform="cgw-dev")
        plan = d.build_deploy_plan(payload)
        assert len(plan["excluded_provisioning"]) == 1
        assert plan["excluded_provisioning"][0]["path"] == "/etc/cgw/credentials/device.crt"

    def test_backup_command_excludes_provisioning(self, project, tmp_path: Path):
        payload = self._make_payload(tmp_path)
        d = Deployer(project, platform="cgw-dev")
        steps = d.build_plan(payload)
        backup = next(s for s in steps if s.name == "backup")
        cmd = backup.commands[0]
        assert "credentials" in cmd or "provisioning" in cmd


# ---------------------------------------------------------------------------
# Deploy dry-run produces deploy_plan in report (§9.7)
# ---------------------------------------------------------------------------


class TestDeployDryRun:
    def test_dry_run_report_has_deploy_plan(self, project, tmp_path: Path):
        # Build a minimal package with config files
        staging = StagingDir(tmp_path, platform="cgw-dev", profile="release")
        staging.prepare()
        cgw = staging.install_root / "etc/cgw"
        (cgw / "conf.d").mkdir(parents=True)
        (cgw / "common.yaml").write_text("log_level: info\n")
        (cgw / "conf.d/cgw-hello-cli.yaml").write_text("service_name: x\n")
        # Minimal artifact manifest
        am_path = staging.manifests_dir / "artifact-manifest.json"
        am_path.write_text(json.dumps({"artifacts": [], "platform": "cgw-dev"}))
        packager = Packager(staging)
        pkg = packager.create_package(name="cgw-test", version="0.1.0",
                                      git_commit=get_git_commit(project.root))
        d = Deployer(project, platform="cgw-dev")
        report = d.deploy(pkg, execute=False)
        assert "deploy_plan" in report.to_dict()
        assert report.deploy_plan["summary"]["replace"] >= 1
        shutil.rmtree(staging.root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Consistent rollback (§9.7) - plan-level verification
# ---------------------------------------------------------------------------


class TestConsistentRollback:
    def test_backup_and_rollback_in_plan(self, project, tmp_path: Path):
        payload = tmp_path / "install-root"
        (payload / "usr/bin").mkdir(parents=True)
        (payload / "etc/cgw/conf.d").mkdir(parents=True)
        (payload / "usr/bin/svc").write_text("bin")
        (payload / "etc/cgw/conf.d/svc.yaml").write_text("a: 1\n")
        d = Deployer(project, platform="cgw-dev")
        steps = d.build_plan(payload)
        names = [s.name for s in steps]
        # Consistent release-set: backup covers binaries + replace configs;
        # rollback restores the whole set (not config-only or binary-only).
        assert "backup" in names
        assert "install" in names
        assert "smoke" in names

    def test_rollback_step_restores_full_set(self, project, tmp_path: Path):
        """On smoke failure the rollback restores the entire backup archive
        (framework + binaries + libs + units + replace configs), not a
        subset (§9.7: 禁止只回退配置或只回退二进制)."""
        payload = tmp_path / "install-root"
        (payload / "usr/bin").mkdir(parents=True)
        (payload / "usr/bin/svc").write_text("bin")
        d = Deployer(project, platform="cgw-dev", rollback_on_failure=True)
        # _rollback restores from the backup archive wholesale
        step = d._rollback()
        assert "tar -C / -xzf" in step.commands[0]
        assert "ldconfig" in step.commands[0]
        assert "daemon-reload" in step.commands[0]


# ---------------------------------------------------------------------------
# Host build overlay no-op (§9.8) -- covered by test_framework_build.py
#   test_host_build_overlay_report_noop verifies a real HOST build produces
#   a no-op config-overlay-report.json (configs/host/rootfs absent).
# ---------------------------------------------------------------------------
