"""Integration tests for release-set assembly (CCU-BUILD-DSN-CR-004).

Covers the release-set acceptance criteria:

  * ``ccu-dev-minimal`` / ``ccu-dev-framework`` / ``ccu-dev-fota`` produce a
    ``ccu.target`` consistent with their own expanded closure;
  * repeated, clean and incremental builds produce identical file sets,
    content and SHA-256; switching release sets removes stale BUILD-owned
    target items and drop-ins while shared JSON follows the selected services;
  * package and Artifact Manifest carry complete release-set evidence with no
    ``unknown`` owner; mismatched package/manifest evidence is refused;
  * the deploy plan enumerates explicitly authorized runtime assets, keeps
    provisioning preserved, rejects unauthorized runtime assets and verifies
    the package/target/payload topology before touching a device;
  * a real HOST build of ``ccu-dev-minimal`` produces the expected target.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ccu_build.artifact import ArtifactManifest, get_git_commit
from ccu_build.assembly import (
    ASSEMBLY_REPORT_NAME,
    TARGET_REL_PATH,
    ReleaseSetAssembler,
)
from ccu_build.config import ConfigOverlay
from ccu_build.deploy import Deployer
from ccu_build.manifest import (
    PlatformManifest,
    Project,
    SysrootManifest,
    load_config_deployment_manifest,
)
from ccu_build.orchestrator import (
    BuildConfig,
    BuildOrchestrator,
    ComponentInstallResult,
    ServiceResult,
)
from ccu_build.errors import PackageError
from ccu_build.packaging import Packager
from ccu_build.staging import StagingDir, format_mode, sha256_file
from ccu_build.verify import Verifier
from tests.assembly_project import (
    make_assembler,
    make_project,
    make_staging,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
UNIT_DIR = "usr/lib/systemd/system"
SERVICE_FILES = {
    "svc-a": [
        f"{UNIT_DIR}/svc-a.service",
        "usr/bin/svc-a",
        "etc/ccu/conf.d/svc-a.yaml",
    ],
    "svc-b": [
        f"{UNIT_DIR}/svc-b.service",
        "usr/bin/svc-b",
        "etc/ccu/conf.d/svc-b.yaml",
    ],
}


# ---------------------------------------------------------------------------
# Pipeline helper (assembler -> overlay -> artifact -> package)
# ---------------------------------------------------------------------------


def run_pipeline(project: Project, staging: StagingDir, set_id: str, requested: list[str]):
    """Run the full post-build pipeline on a seeded staging tree."""
    assembly = make_assembler(project, staging).assemble(set_id, requested)
    overlay = ConfigOverlay(
        platform=project.platform,
        platform_rootfs=project.platform_configs_root(),
        install_root=staging.install_root,
        deployment=load_config_deployment_manifest(
            project.config_deployment_manifest_path),
        service_manifest=project.load_service_manifest(),
        project_root=project.root,
        state_dir=staging.manifests_dir,
        claimed_paths={
            path: "build:release-set-assembly"
            for path in assembly.build_owned_paths
        },
    )
    overlay_report = overlay.apply()
    overlay.check_report(overlay_report)
    overlay_report.save(staging.manifests_dir / "config-overlay-report.json")

    service_manifest = project.load_service_manifest()
    results: list[ServiceResult] = []
    for service_id, files in SERVICE_FILES.items():
        if service_id not in assembly.expanded_services:
            continue
        component = next(
            (c for c in service_manifest.get(service_id).build.install_components),
        ) if service_manifest.get(service_id) else None
        results.append(ServiceResult(
            id=service_id,
            status="success",
            components=[ComponentInstallResult(
                name=component.name if component else "unknown",
                staging="rootfs",
                destdir=str(staging.install_root),
                installed_files=[f for f in files
                                 if (staging.install_root / f).exists()],
            )],
            installed_files=[f for f in files
                             if (staging.install_root / f).exists()],
        ))

    orchestrator = BuildOrchestrator(project, BuildConfig(platform=project.platform))
    orchestrator.staging = staging
    manifest = orchestrator._generate_artifact_manifest(
        service_manifest,
        project.load_dependency_lock(),
        "testcommit",
        results,
        overlay_report,
        assembly,
    )
    manifest.save(staging.manifests_dir / "artifact-manifest.json")
    return assembly, overlay_report, manifest


def make_package(staging: StagingDir) -> Path:
    return Packager(staging).create_package(
        name="ccu-test", version="0.1.0", git_commit="testcommit"
    )


# ---------------------------------------------------------------------------
# Real manifests: expected closure -> target mapping (_dev_ plan only)
# ---------------------------------------------------------------------------


class TestRealReleaseSetPlans:
    def test_minimal_and_framework_sets(self):
        project = Project(PROJECT_ROOT, platform="ccu-dev")
        staging = StagingDir(PROJECT_ROOT, "ccu-dev", "release")
        assembler = ReleaseSetAssembler(
            project, staging, project.load_service_manifest()
        )
        minimal = assembler.plan(
            "ccu-dev-minimal", ["ccu-hello-lib", "ccu-hello-cli"],
            require_baseline_in_payload=False,
        )
        assert minimal.expanded_services == ["ccu-hello-lib", "ccu-hello-cli"]
        assert minimal.ordered_units == ["ccu-hello.service"]
        assert minimal.errors == []

        framework = assembler.plan(
            "ccu-dev-framework", ["framework", "framework-consumer"],
            require_baseline_in_payload=False,
        )
        assert framework.ordered_units == ["ccu-framework-consumer.service"]
        assert framework.errors == []

    def test_real_service_set_aggregates_topology_order(self):
        project = Project(PROJECT_ROOT, platform="ccu-dev")
        staging = StagingDir(PROJECT_ROOT, "ccu-dev", "release")
        assembler = ReleaseSetAssembler(
            project, staging, project.load_service_manifest()
        )
        plan = assembler.plan(
            "ccu-dev-fota",
            ["framework", "ccu-sec", "ccu-diag", "ccu-fota"],
            require_baseline_in_payload=False,
        )
        assert plan.expanded_services == [
            "framework", "ccu-sec", "ccu-diag", "ccu-fota"
        ]
        assert plan.ordered_units == [
            "ccu-sec.service", "ccu-diag.service", "ccu-fota.service"
        ]
        # 受控基线已落库（configs/ccu-dev/rootfs/etc/ccu/someip/vsomeip.json）：
        # release profile 不再 fail-closed，共享 JSON 进入计划且三服务去重一次。
        # （缺基线时的 fail-closed 语义由 tests/unit/test_assembly.py 的
        #   合成工程用例覆盖，CCU-BUILD-DSN-CR-004 §11。）
        assert plan.errors == []
        assert "/etc/ccu/someip/vsomeip.json" in {a.target for a in plan.assets}

    def test_dev_exemption_allows_plan_without_device_json(self):
        project = Project(PROJECT_ROOT, platform="ccu-dev")
        staging = StagingDir(PROJECT_ROOT, "ccu-dev", "release")
        assembler = ReleaseSetAssembler(
            project, staging, project.load_service_manifest(),
            allow_missing_assets=True,
        )
        plan = assembler.plan(
            "ccu-dev-fota",
            ["framework", "ccu-sec", "ccu-diag", "ccu-fota"],
            require_baseline_in_payload=False,
        )
        assert plan.errors == []
        installed = sorted(a.target for a in plan.assets)
        # 三个服务共同声明同一份共享 JSON -> 只安装一次（§5.3 去重）。
        assert installed == [
            "/etc/ccu/someip/vsomeip.json",
            "/usr/lib/systemd/system/ccu-diag.service.d/vsomeip.conf",
            "/usr/lib/systemd/system/ccu-fota.service.d/vsomeip.conf",
            "/usr/lib/systemd/system/ccu-sec.service.d/vsomeip.conf",
        ]

    def test_static_target_replaced_by_template(self):
        template = PROJECT_ROOT / "packaging/systemd/ccu.target.in"
        assert template.is_file()
        assert not (PROJECT_ROOT / "packaging/systemd/ccu.target").exists()
        text = template.read_text()
        for business_unit in (
            "ccu-hello.service", "ccu-fota.service",
            "ccu-diag.service", "ccu-sec.service",
        ):
            assert business_unit not in text
        assert "@RELEASE_SET_ID@" in text
        assert "@ORDERED_UNITS@" in text


# ---------------------------------------------------------------------------
# Synthetic project: full pipeline + traceability + package + deploy
# ---------------------------------------------------------------------------


class TestFullPipeline:
    def _run(self, tmp_path: Path):
        project = make_project(tmp_path)
        staging = make_staging(project)
        return project, staging

    def test_artifact_manifest_records_release_set_evidence(self, tmp_path: Path):
        project, staging = self._run(tmp_path)
        assembly, _, manifest = run_pipeline(
            project, staging, "set-full", ["lib-a", "svc-a", "svc-b"]
        )
        data = manifest.to_dict()
        assert data["release_set"]["id"] == "set-full"
        assert data["release_set"]["requested_services"] == [
            "lib-a", "svc-a", "svc-b"
        ]
        assert data["release_set"]["systemd_units"] == [
            "svc-a.service", "svc-b.service"
        ]
        assert data["release_set"]["aggregation_status"] == "generated"
        assert [a["path"] for a in data["artifacts"]
                if a["owner_service"] == "unknown"] == []

        target = next(a for a in data["artifacts"] if a["path"] == f"rootfs/{TARGET_REL_PATH}")
        assert target["owner_service"] == "build"
        assert target["owner_target"] == "release-set-assembly"
        assert target["install_component"] == "ccu-release-set-runtime"
        assert target["source"] == "packaging/systemd/ccu.target.in"
        assert target["source_sha256"] and target["mode"] == "0o644"
        assert target["sha256"] == sha256_file(staging.install_root / TARGET_REL_PATH)

        shared = next(
            a for a in data["artifacts"]
            if a["path"] == "rootfs/etc/ccu/someip/vsomeip.json"
        )
        assert shared["owner_service"] == "build"
        assert shared["referenced_by"] == ["svc-a", "svc-b"]

    def test_overlay_never_writes_build_claimed_paths(self, tmp_path: Path):
        project, staging = self._run(tmp_path)
        _, overlay_report, _ = run_pipeline(
            project, staging, "set-full", ["lib-a", "svc-a", "svc-b"]
        )
        overlaid = {r.logical_path for r in overlay_report.overlaid}
        assert "/etc/ccu/common.yaml" in overlaid
        assert "/etc/ccu/someip/vsomeip.json" not in overlaid
        skipped = {s["logical_path"] for s in overlay_report.skipped}
        assert "/etc/ccu/someip/vsomeip.json" in skipped
        assert all("service.d" not in p for p in overlaid)

    def test_staging_verification_passes(self, tmp_path: Path):
        project, staging = self._run(tmp_path)
        run_pipeline(project, staging, "set-full", ["lib-a", "svc-a", "svc-b"])
        result = Verifier(staging, project=project).verify_staging()
        assert result.status == "success", result.errors
        names = {c["name"] for c in result.checks}
        assert {"systemd-units", "release-set-evidence", "artifact-ownership",
                "build-owned-evidence", "stale-assets", "platform-assets"} <= names

    def test_package_and_deploy_dry_run(self, tmp_path: Path):
        project, staging = self._run(tmp_path)
        run_pipeline(project, staging, "set-full", ["lib-a", "svc-a", "svc-b"])
        package = make_package(staging)
        metadata = json.loads(
            (staging.manifests_dir / "package.json").read_text()
        )
        assert metadata["release_set"]["id"] == "set-full"
        assert metadata["release_set"]["systemd_units"] == [
            "svc-a.service", "svc-b.service"
        ]

        deployer = Deployer(project, platform=project.platform)
        report = deployer.deploy(package, execute=False)
        assert "success" in report.status, report.errors
        steps = {s.name: s for s in report.steps}
        assert steps["release-set-check"].status == "success"
        # Runtime assets appear in the replace plan (release-managed).
        replaced = {e["path"] for e in report.deploy_plan["replace"]}
        assert "/etc/ccu/someip/vsomeip.json" in replaced
        assert f"/{UNIT_DIR}/svc-a.service.d/vsomeip.conf" in replaced
        assert f"/{UNIT_DIR}/svc-b.service.d/vsomeip.conf" in replaced
        # Dependency-ordered restart uses .services only, never the target.
        assert steps["restart"].message == (
            "units (dependency order): svc-a.service, svc-b.service"
        )
        assert report.deploy_plan["unauthorized"] == []

    def test_unauthorized_runtime_asset_never_replaced(self, tmp_path: Path):
        project = make_project(tmp_path)
        payload = tmp_path / "payload"
        (payload / "usr/lib/systemd/system/svc-a.service.d").mkdir(parents=True)
        (payload / "usr/lib/systemd/system/svc-a.service.d/vsomeip-extra.conf").write_text(
            "[Service]\nEnvironment=X=1\n"
        )
        (payload / "etc/ccu/provisioning").mkdir(parents=True)
        (payload / "etc/ccu/provisioning/device.yaml").write_text("device: 1\n")
        (payload / "etc/ccu/common.yaml").write_text("log: {}\n")

        deployer = Deployer(project, platform=project.platform)
        plan = deployer.build_deploy_plan(payload)
        unauthorized = {e["path"] for e in plan["unauthorized"]}
        assert "/usr/lib/systemd/system/svc-a.service.d/vsomeip-extra.conf" in unauthorized
        replaced = {e["path"] for e in plan["replace"]}
        assert "/usr/lib/systemd/system/svc-a.service.d/vsomeip-extra.conf" not in replaced
        assert "/etc/ccu/provisioning/device.yaml" not in replaced
        assert "/etc/ccu/common.yaml" in replaced


class TestManifestInstallRootParity:
    """CCU-BUILD-DSN-CR-005 §2: bidirectional manifest <-> install-root check.

    Every recorded artifact must exist with the recorded SHA-256/mode; dev
    fixtures and instance state must never be released.
    """

    def _run(self, tmp_path: Path):
        project = make_project(tmp_path)
        staging = make_staging(project)
        run_pipeline(project, staging, "set-full", ["lib-a", "svc-a", "svc-b"])
        return project, staging

    def _check(self, project, staging, name: str) -> dict:
        result = Verifier(staging, project=project).verify_staging()
        return {c["name"]: c for c in result.checks}[name]

    def test_mode_and_sha256_recorded_and_matching(self, tmp_path: Path):
        project, staging = self._run(tmp_path)
        data = json.loads(
            (staging.manifests_dir / "artifact-manifest.json").read_text()
        )
        for artifact in data["artifacts"]:
            if not artifact["path"].startswith("rootfs/"):
                continue
            target = staging.install_root / artifact["path"][len("rootfs/"):]
            assert artifact["mode"] == format_mode(target, follow_symlinks=True)
            assert artifact["sha256"] == sha256_file(target)
        check = self._check(project, staging, "manifest-install-root-parity")
        assert check["status"] == "success", check["message"]
        assert self._check(project, staging, "forbidden-payload")["status"] == "success"

    def test_missing_artifact_fails(self, tmp_path: Path):
        project, staging = self._run(tmp_path)
        (staging.install_root / "usr/bin/svc-b").unlink()
        check = self._check(project, staging, "manifest-install-root-parity")
        assert check["status"] == "failed"
        assert "missing from install-root" in check["message"]
        assert "rootfs/usr/bin/svc-b" in check["message"]

    def test_sha256_mismatch_fails(self, tmp_path: Path):
        project, staging = self._run(tmp_path)
        (staging.install_root / "usr/bin/svc-b").write_text("tampered")
        check = self._check(project, staging, "manifest-install-root-parity")
        assert check["status"] == "failed"
        assert "sha256 mismatch" in check["message"]

    def test_mode_mismatch_fails(self, tmp_path: Path):
        project, staging = self._run(tmp_path)
        (staging.install_root / "usr/bin/svc-b").chmod(0o600)
        check = self._check(project, staging, "manifest-install-root-parity")
        assert check["status"] == "failed"
        assert "mode" in check["message"]

    def test_instance_state_and_dev_fixture_refused(self, tmp_path: Path):
        project, staging = self._run(tmp_path)
        vin = staging.install_root / "var/lib/ccu/diag/vin.dat"
        vin.parent.mkdir(parents=True)
        vin.write_text("VIN")
        fixture = staging.install_root / "usr/bin/config/dev/diag.yaml"
        fixture.parent.mkdir(parents=True)
        fixture.write_text("dev: true\n")

        check = self._check(project, staging, "forbidden-payload")
        assert check["status"] == "failed"
        assert "var/lib/ccu/diag/vin.dat" in check["message"]
        assert "config/dev/diag.yaml" in check["message"]
        with pytest.raises(PackageError, match="forbidden payload"):
            make_package(staging)


class TestDeterminismAndSwitching:
    def test_repeated_and_clean_builds_identical(self, tmp_path: Path):
        project = make_project(tmp_path)
        staging = make_staging(project)
        run_pipeline(project, staging, "set-full", ["lib-a", "svc-a", "svc-b"])
        first = {
            str(p.relative_to(staging.install_root)): sha256_file(p)
            for p in sorted(staging.install_root.rglob("*")) if p.is_file()
        }
        first_modes = {
            str(p.relative_to(staging.install_root)): format_mode(p, follow_symlinks=True)
            for p in sorted(staging.install_root.rglob("*")) if p.is_file()
        }
        mode_first = (staging.install_root / TARGET_REL_PATH).stat().st_mode & 0o777

        # Incremental rebuild of the same input.
        run_pipeline(project, staging, "set-full", ["lib-a", "svc-a", "svc-b"])
        second = {
            str(p.relative_to(staging.install_root)): sha256_file(p)
            for p in sorted(staging.install_root.rglob("*")) if p.is_file()
        }
        second_modes = {
            str(p.relative_to(staging.install_root)): format_mode(p, follow_symlinks=True)
            for p in sorted(staging.install_root.rglob("*")) if p.is_file()
        }
        assert first == second
        assert first_modes == second_modes

        # Clean rebuild into a fresh staging directory.
        clean_staging = make_staging(project, profile="clean")
        run_pipeline(project, clean_staging, "set-full", ["lib-a", "svc-a", "svc-b"])
        third = {
            str(p.relative_to(clean_staging.install_root)): sha256_file(p)
            for p in sorted(clean_staging.install_root.rglob("*")) if p.is_file()
        }
        third_modes = {
            str(p.relative_to(clean_staging.install_root)): format_mode(p, follow_symlinks=True)
            for p in sorted(clean_staging.install_root.rglob("*")) if p.is_file()
        }
        assert first == third
        assert first_modes == third_modes
        assert mode_first == 0o644

    def test_switch_to_smaller_set_cleans_stale_build_assets(self, tmp_path: Path):
        project = make_project(tmp_path)
        staging = make_staging(project)
        run_pipeline(project, staging, "set-full", ["lib-a", "svc-a", "svc-b"])

        assembly, _, _ = run_pipeline(project, staging, "set-a", ["svc-a"])
        target = (staging.install_root / TARGET_REL_PATH).read_text()
        assert "svc-a.service" in target
        assert "svc-b.service" not in target
        # svc-b drop-in is BUILD-owned and stale -> removed.
        assert not (staging.install_root / UNIT_DIR / "svc-b.service.d" / "vsomeip.conf").exists()
        # Shared JSON is still required by svc-a -> kept.
        assert (staging.install_root / "etc/ccu/someip/vsomeip.json").is_file()
        assert any("svc-b.service.d" in r["path"] for r in assembly.removed)
        # Service-owned files are never deleted by the assembler.
        assert (staging.install_root / "usr/bin/svc-b").is_file()

        result = Verifier(staging, project=project).verify_staging()
        assert result.status == "success", result.errors

    def test_switch_to_library_only_set_removes_target_and_shared_json(
        self, tmp_path: Path
    ):
        project = make_project(tmp_path)
        staging = make_staging(project)
        run_pipeline(project, staging, "set-full", ["lib-a", "svc-a", "svc-b"])
        assembly, _, _ = run_pipeline(project, staging, "set-lib", ["lib-a"])
        assert assembly.aggregation_status == "empty"
        assert not (staging.install_root / TARGET_REL_PATH).exists()
        assert not (staging.install_root / "etc/ccu/someip/vsomeip.json").exists()
        assert not (staging.install_root / UNIT_DIR / "svc-a.service.d" / "vsomeip.conf").exists()


# ---------------------------------------------------------------------------
# Deploy pre-check: package / target / payload topology
# ---------------------------------------------------------------------------


class TestDeployReleaseSetPreCheck:
    def _payload(self, tmp_path: Path, project: Project) -> Path:
        staging = make_staging(project, profile="pkg")
        run_pipeline(project, staging, "set-full", ["lib-a", "svc-a", "svc-b"])
        package = make_package(staging)
        import tarfile
        dest = tmp_path / "extract"
        with tarfile.open(package) as tar:
            tar.extractall(dest, filter="data")
        return dest / "install-root"

    def test_missing_target_fails(self, tmp_path: Path):
        project = make_project(tmp_path)
        payload = self._payload(tmp_path, project)
        (payload / TARGET_REL_PATH).unlink()
        step = Deployer(project, platform=project.platform)._check_release_set(payload)
        assert step.status == "failed"
        assert "ccu.target is missing" in step.message

    def test_tampered_wants_fails(self, tmp_path: Path):
        project = make_project(tmp_path)
        payload = self._payload(tmp_path, project)
        target = payload / TARGET_REL_PATH
        target.write_text(target.read_text().replace(
            "Wants=svc-a.service", "Wants=svc-a.service svc-b.service"
        ))
        step = Deployer(project, platform=project.platform)._check_release_set(payload)
        assert step.status == "failed"
        assert "Wants=" in step.message

    def test_metadata_mismatch_fails(self, tmp_path: Path):
        project = make_project(tmp_path)
        payload = self._payload(tmp_path, project)
        metadata_path = payload.parent / "manifests" / "package.json"
        metadata = json.loads(metadata_path.read_text())
        metadata["release_set"]["id"] = "other-set"
        metadata_path.write_text(json.dumps(metadata))
        step = Deployer(project, platform=project.platform)._check_release_set(payload)
        assert step.status == "failed"
        assert "does not match" in step.message

    def test_non_set_package_with_target_fails(self, tmp_path: Path):
        project = make_project(tmp_path)
        payload = self._payload(tmp_path, project)
        manifest_path = payload.parent / "manifests" / "artifact-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["release_set"] = None
        manifest_path.write_text(json.dumps(manifest))
        step = Deployer(project, platform=project.platform)._check_release_set(payload)
        assert step.status == "failed"
        assert "Non-release-set package carries" in step.message


# ---------------------------------------------------------------------------
# Packager: refuse inconsistent release-set evidence
# ---------------------------------------------------------------------------


class TestPackagerRefusesInconsistentEvidence:
    def test_unknown_owner_refused(self, tmp_path: Path):
        project = make_project(tmp_path)
        staging = make_staging(project)
        run_pipeline(project, staging, "set-a", ["svc-a"])
        manifest_path = staging.manifests_dir / "artifact-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["artifacts"].append({
            "path": "rootfs/usr/bin/orphan", "owner_service": "unknown",
            "owner_target": "unknown", "version": "?", "git_commit": "?",
            "profile": "?", "sha256": "x", "size": 1, "file_type": "text",
        })
        manifest_path.write_text(json.dumps(manifest))
        with pytest.raises(Exception) as exc:
            make_package(staging)
        assert "unknown owner" in str(exc.value)

    def test_stale_target_without_release_set_refused(self, tmp_path: Path):
        project = make_project(tmp_path)
        staging = make_staging(project)
        run_pipeline(project, staging, "set-full", ["lib-a", "svc-a", "svc-b"])
        manifest_path = staging.manifests_dir / "artifact-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["release_set"] = None
        manifest_path.write_text(json.dumps(manifest))
        with pytest.raises(Exception) as exc:
            make_package(staging)
        assert "still contains the release-set aggregation target" in str(exc.value)


# ---------------------------------------------------------------------------
# Real HOST build of ccu-dev-minimal
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def host_build():
    project = Project(PROJECT_ROOT, platform="host")
    config = BuildConfig(platform="host", profile="cr004-test", jobs=4, clean=True)
    staging_root = StagingDir(PROJECT_ROOT, "host", "cr004-test").root
    report = BuildOrchestrator(project, config).build(set_id="ccu-dev-minimal")
    yield project, config, report
    shutil.rmtree(staging_root, ignore_errors=True)


class TestRealMinimalHostBuild:

    def test_target_generated_from_closure(self, host_build):
        project, config, report = host_build
        assert report.status == "success", report.errors
        staging = StagingDir(PROJECT_ROOT, "host", config.profile)
        target = staging.install_root / TARGET_REL_PATH
        assert target.is_file()
        text = target.read_text()
        assert "Wants=ccu-hello.service" in text
        assert "After=ccu-hello.service" in text
        assert "Requires=" not in text
        assert report.systemd_units == ["ccu-hello.service"]
        assert report.aggregation_status == "generated"

    def test_manifest_evidence_and_verify(self, host_build):
        project, config, report = host_build
        staging = StagingDir(PROJECT_ROOT, "host", config.profile)
        data = json.loads(
            (staging.manifests_dir / "artifact-manifest.json").read_text()
        )
        assert data["release_set"]["id"] == "ccu-dev-minimal"
        assert data["release_set"]["systemd_units"] == ["ccu-hello.service"]
        build_owned = [a for a in data["artifacts"]
                       if a["owner_service"] == "build"]
        assert [a["path"] for a in build_owned] == [f"rootfs/{TARGET_REL_PATH}"]
        result = Verifier(staging, project=project).verify_staging()
        assert result.status == "success", result.errors

    def test_package_and_ordered_restart_plan(self, host_build):
        project, config, report = host_build
        staging = StagingDir(PROJECT_ROOT, "host", config.profile)
        package = Packager(staging).create_package(
            name="ccu-hello", version="0.1.0",
            git_commit=get_git_commit(PROJECT_ROOT),
        )
        deployer = Deployer(project, platform="host")
        deploy_report = deployer.deploy(package, execute=False)
        assert "success" in deploy_report.status, deploy_report.errors
        steps = {s.name: s for s in deploy_report.steps}
        assert steps["release-set-check"].status == "success"
        assert steps["restart"].message == (
            "units (dependency order): ccu-hello.service"
        )


# ---------------------------------------------------------------------------
# Real ccu-dev-fota release set assembled from the real manifests
# ---------------------------------------------------------------------------


REAL_SERVICE_FILES = {
    "framework": [],
    "ccu-sec": [
        "usr/bin/ccu_sec",
        f"{UNIT_DIR}/ccu-sec.service",
        "etc/ccu/conf.d/sec.yaml",
        "usr/share/ccu-sec/schema/sec.schema.yaml",
        "etc/ccu/common.yaml",
    ],
    "ccu-diag": [
        "usr/bin/ccu_diag",
        f"{UNIT_DIR}/ccu-diag.service",
        "etc/ccu/conf.d/diag.yaml",
        "usr/share/ccu-diag/schema/diag.schema.yaml",
        # CCU-BUILD-DSN-CR-005 §1: deployment seed is part of the exact set.
        "usr/share/ccu-diag/ecu-inventory.seed.json",
    ],
    "ccu-fota": [
        "usr/bin/ccu-fota",
        f"{UNIT_DIR}/ccu-fota.service",
        "etc/ccu/conf.d/fota.yaml",
        "usr/share/ccu-fota/schema/fota.schema.yaml",
    ],
}


class TestRealServiceSetStaging:
    """Assemble the real ccu-dev-fota set from the real manifests.

    The controlled device ``vsomeip.json`` is not landed yet (CR-004 §11),
    so the assembler runs with the development exemption. The aggregation
    target, drop-in delivery, artifact evidence, packaging and deploy
    pre-check are still fully exercised; the platform-asset revalidation
    check is expected to report the missing required JSON until the controlled
    input lands.
    """

    def test_real_set_target_and_traceability(self):
        project = Project(PROJECT_ROOT, platform="ccu-dev")
        staging = StagingDir(PROJECT_ROOT, "ccu-dev", "cr004-fota")
        try:
            staging.prepare()
            unit_dir = staging.install_root / UNIT_DIR
            for service_id, files in REAL_SERVICE_FILES.items():
                for rel in files:
                    path = staging.install_root / rel
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if rel.endswith(".service"):
                        path.write_text(
                            f"[Unit]\nDescription={service_id}\n"
                            "After=network.target\n[Service]\n"
                            f"ExecStart=/usr/bin/{service_id}\n"
                        )
                    else:
                        path.write_text(f"{service_id}: fixture\n")
                    path.chmod(0o644)

            service_manifest = project.load_service_manifest()
            assembler = ReleaseSetAssembler(
                project, staging, service_manifest, allow_missing_assets=True
            )
            assembly = assembler.assemble(
                "ccu-dev-fota",
                ["framework", "ccu-sec", "ccu-diag", "ccu-fota"],
            )
            assert assembly.expanded_services == [
                "framework", "ccu-sec", "ccu-diag", "ccu-fota"
            ]
            assert assembly.ordered_units == [
                "ccu-sec.service", "ccu-diag.service", "ccu-fota.service"
            ]
            assert assembly.aggregation_status == "generated"
            target = (staging.install_root / TARGET_REL_PATH).read_text()
            assert ("Wants=ccu-sec.service ccu-diag.service ccu-fota.service"
                    in target)
            assert "Requires=" not in target
            installed = {a.target for a in assembly.assets}
            assert installed == {
                "/etc/ccu/someip/vsomeip.json",
                f"/{UNIT_DIR}/ccu-sec.service.d/vsomeip.conf",
                f"/{UNIT_DIR}/ccu-diag.service.d/vsomeip.conf",
                f"/{UNIT_DIR}/ccu-fota.service.d/vsomeip.conf",
            }
            # 基线已落库 -> 不再记录 missing。
            assert assembly.missing == []

            service_results = [
                ServiceResult(
                    id=service_id,
                    status="success",
                    components=[ComponentInstallResult(
                        name=f"{service_id}-runtime",
                        staging="rootfs",
                        destdir=str(staging.install_root),
                        installed_files=list(files),
                    )],
                    installed_files=list(files),
                )
                for service_id, files in REAL_SERVICE_FILES.items()
                if files
            ]
            orchestrator = BuildOrchestrator(
                project, BuildConfig(platform="ccu-dev")
            )
            orchestrator.staging = staging
            manifest = orchestrator._generate_artifact_manifest(
                service_manifest,
                project.load_dependency_lock(),
                "testcommit",
                service_results,
                None,
                assembly,
            )
            manifest.save(staging.manifests_dir / "artifact-manifest.json")
            data = manifest.to_dict()
            assert data["release_set"]["id"] == "ccu-dev-fota"
            assert data["release_set"]["systemd_units"] == [
                "ccu-sec.service", "ccu-diag.service", "ccu-fota.service"
            ]
            build_owned = {
                a["path"] for a in data["artifacts"]
                if a["owner_service"] == "build"
            }
            assert f"rootfs/{TARGET_REL_PATH}" in build_owned
            assert build_owned >= {
                f"rootfs/{UNIT_DIR}/ccu-sec.service.d/vsomeip.conf",
                f"rootfs/{UNIT_DIR}/ccu-diag.service.d/vsomeip.conf",
                f"rootfs/{UNIT_DIR}/ccu-fota.service.d/vsomeip.conf",
            }
            assert [a["path"] for a in data["artifacts"]
                    if a["owner_service"] == "unknown"] == []

            # CCU-BUILD-DSN-CR-005 §3: seed ownership, provenance and SHA-256.
            diag = service_manifest.get("ccu-diag")
            seed_rel = "usr/share/ccu-diag/ecu-inventory.seed.json"
            seed = next(
                a for a in data["artifacts"] if a["path"] == f"rootfs/{seed_rel}"
            )
            assert seed["owner_service"] == "ccu-diag"
            assert seed["owner_target"] == "ccu_diag"
            assert seed["install_component"] == "ccu-diag-runtime"
            assert seed["staging"] == "rootfs"
            assert seed["mode"] == "0o644"
            assert seed["source"] == f"ccu-diag@{diag.revision}:{seed_rel}"
            assert seed["sha256"] == sha256_file(staging.install_root / seed_rel)
            # Five-file exact set: daemon, unit, config, schema, seed.
            diag_files = {
                a["path"] for a in data["artifacts"]
                if a["owner_service"] == "ccu-diag"
            }
            assert diag_files == {
                f"rootfs/{rel}" for rel in REAL_SERVICE_FILES["ccu-diag"]
            }

            # Unit check passes; platform assets keep reporting the open gate.
            verify = Verifier(staging, project=project).verify_staging()
            checks = {c["name"]: c for c in verify.checks}
            assert checks["systemd-units"]["status"] == "success"
            assert checks["release-set-evidence"]["status"] == "success"
            assert checks["artifact-ownership"]["status"] == "success"
            assert checks["build-owned-evidence"]["status"] == "success"
            assert checks["manifest-install-root-parity"]["status"] == "success", (
                checks["manifest-install-root-parity"]["message"]
            )
            assert checks["forbidden-payload"]["status"] == "success"
            # 受控基线已落库：平台资产校验通过（缺基线的失败路径由
            # tests/unit/test_assembly.py 的合成工程用例覆盖）。
            assert checks["platform-assets"]["status"] == "success"
            assert "revalidated" in checks["platform-assets"]["message"]

            # Package + deploy dry-run: release-set topology pre-check passes.
            package = Packager(staging).create_package(
                name="ccu-fota", version="0.1.0", git_commit="testcommit"
            )
            deployer = Deployer(project, platform="ccu-dev")
            report = deployer.deploy(package, execute=False)
            assert "success" in report.status, report.errors
            steps = {s.name: s for s in report.steps}
            assert steps["release-set-check"].status == "success"
            assert steps["restart"].message == (
                "units (dependency order): ccu-sec.service, ccu-diag.service, "
                "ccu-fota.service"
            )
            replaced = {e["path"] for e in report.deploy_plan["replace"]}
            assert f"/{UNIT_DIR}/ccu-fota.service.d/vsomeip.conf" in replaced
            # Seed is release-managed: replaced with the rest of the set.
            assert f"/{seed_rel}" in replaced
            # Instance state is never part of the payload or the plan.
            planned = {
                e["path"]
                for key in ("create", "replace", "preserve", "unauthorized")
                for e in report.deploy_plan[key]
            }
            assert not any(p.startswith("/var/lib/ccu/") for p in planned)
        finally:
            shutil.rmtree(staging.root, ignore_errors=True)
