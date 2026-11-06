"""Unit tests for release-set assembly (CCU-BUILD-DSN-CR-004 §3-§5).

Covers:
  * closure/topology ordered unit collection, deterministic de-duplication,
    illegal unit names, cross-service unit ownership and the BUILD-reserved
    ``ccu.target``;
  * template rendering: no business unit name, ``Wants=`` == ``After=``, no
    ``Requires=``, single-space canonical text, atomic write, mode 0644,
    empty-set behaviour and non-set builds;
  * ``runtime.platform_assets[]`` selection/dedup/conflict/authorization/
    required-missing/optional-missing and baseline-unit requirements;
  * stale BUILD-owned cleanup that never deletes service-owned files;
  * ``vsomeip-dropin`` / ``vsomeip-json`` validators.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from ccu_build.assetcheck import (
    validate_vsomeip_dropin,
    validate_vsomeip_json,
)
from ccu_build.assembly import (
    ASSEMBLY_REPORT_NAME,
    AssemblyError,
    ReleaseSetAssembler,
    TARGET_REL_PATH,
)
from ccu_build.config import ConfigOverlay
from ccu_build.manifest import (
    BuildConfig,
    PlatformAsset,
    Project,
    RuntimeConfig,
    Service,
    ServiceManifest,
    load_config_deployment_manifest,
    load_service_manifest,
    load_release_set_manifest,
)
from ccu_build.staging import StagingDir, sha256_file

from tests.assembly_project import (
    DROPIN,
    UNIT_DIR,
    make_assembler as assembler,
    make_project,
    make_staging,
    write_text as _text,
    write_yaml as _write_yaml,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Unit collection and aggregation target
# ---------------------------------------------------------------------------


class TestUnitCollection:
    def test_closure_topology_order(self, tmp_path: Path):
        project = make_project(tmp_path)
        staging = make_staging(project)
        plan = assembler(project, staging).plan("set-full", ["lib-a", "svc-a", "svc-b"])
        assert plan.expanded_services == ["lib-a", "svc-a", "svc-b"]
        # Libraries have no unit; order follows the service topology.
        assert plan.ordered_units == ["svc-a.service", "svc-b.service"]
        assert plan.aggregation_status == "generated"

    def test_partial_request_still_uses_full_closure(self, tmp_path: Path):
        project = make_project(tmp_path)
        staging = make_staging(project)
        plan = assembler(project, staging).plan("set-full", ["svc-b"])
        assert plan.expanded_services == ["lib-a", "svc-a", "svc-b"]
        assert plan.ordered_units == ["svc-a.service", "svc-b.service"]

    def test_library_only_set_has_empty_aggregation(self, tmp_path: Path):
        project = make_project(tmp_path)
        staging = make_staging(project)
        report = assembler(project, staging).assemble("set-lib", ["lib-a"])
        assert report.aggregation_status == "empty"
        assert report.ordered_units == []
        assert not (staging.install_root / TARGET_REL_PATH).exists()

    def test_duplicate_unit_within_service_deduplicated(self, tmp_path: Path):
        project = make_project(tmp_path)
        data = yaml.safe_load((project.root / "manifests/services.yaml").read_text())
        data["services"]["svc-a"]["runtime"]["systemd_units"] = [
            "svc-a.service", "svc-a.service"
        ]
        _write_yaml(project.root / "manifests/services.yaml", data)
        staging = make_staging(project)
        plan = assembler(project, staging).plan("set-a", ["svc-a"])
        assert plan.ordered_units == ["svc-a.service"]
        assert any("de-duplicated" in w for w in plan.warnings)

    def test_cross_service_unit_ownership_fails(self, tmp_path: Path):
        project = make_project(tmp_path)
        data = yaml.safe_load((project.root / "manifests/services.yaml").read_text())
        data["services"]["svc-b"]["runtime"]["systemd_units"] = ["svc-a.service"]
        data["services"]["svc-b"]["runtime"].pop("platform_assets")
        _write_yaml(project.root / "manifests/services.yaml", data)
        staging = make_staging(project)
        with pytest.raises(AssemblyError) as exc:
            assembler(project, staging).assemble("set-full", ["lib-a", "svc-a", "svc-b"])
        assert "declared by both" in str(exc.value)

    @pytest.mark.parametrize(
        "bad_unit",
        ["", "bad unit.service", "a/b.service", "../escape.service",
         "ccu.target", "x\ny.service"],
    )
    def test_illegal_unit_names_rejected(self, tmp_path: Path, bad_unit: str):
        project = make_project(tmp_path)
        data = yaml.safe_load((project.root / "manifests/services.yaml").read_text())
        data["services"]["svc-a"]["runtime"]["systemd_units"] = [bad_unit]
        data["services"]["svc-a"]["runtime"].pop("platform_assets")
        _write_yaml(project.root / "manifests/services.yaml", data)
        staging = make_staging(project)
        with pytest.raises(AssemblyError):
            assembler(project, staging).assemble("set-a", ["svc-a"])


class TestTargetGeneration:
    def test_rendered_target_is_canonical(self, tmp_path: Path):
        project = make_project(tmp_path)
        staging = make_staging(project)
        report = assembler(project, staging).assemble("set-full", ["lib-a", "svc-a", "svc-b"])
        target = staging.install_root / TARGET_REL_PATH
        text = target.read_text()
        assert "Requires=" not in text
        assert "svc-a.service svc-b.service" in text
        wants = [ln for ln in text.splitlines() if ln.startswith("Wants=")]
        after = [ln for ln in text.splitlines() if ln.startswith("After=")]
        assert len(wants) == len(after) == 1
        assert wants[0].split("=", 1)[1] == after[0].split("=", 1)[1]
        assert text.endswith("\n")
        assert text.splitlines()[1].startswith("Description=CCU Release Set set-full")

    def test_target_mode_is_0644(self, tmp_path: Path):
        project = make_project(tmp_path)
        staging = make_staging(project)
        assembler(project, staging).assemble("set-full", ["lib-a", "svc-a", "svc-b"])
        mode = (staging.install_root / TARGET_REL_PATH).stat().st_mode & 0o777
        assert mode == 0o644

    def test_generated_record_traces_source_and_sha(self, tmp_path: Path):
        project = make_project(tmp_path)
        staging = make_staging(project)
        report = assembler(project, staging).assemble("set-full", ["lib-a", "svc-a", "svc-b"])
        record = report.generated[0]
        assert record.path == TARGET_REL_PATH
        assert record.owner == "build"
        assert record.owner_target == "release-set-assembly"
        assert record.install_component == "ccu-release-set-runtime"
        assert record.source == "packaging/systemd/ccu.target.in"
        assert record.source_sha256
        assert record.final_sha256 == sha256_file(staging.install_root / TARGET_REL_PATH)
        assert record.mode == "0o644"
        assert record.release_set == "set-full"

    def test_deterministic_across_runs(self, tmp_path: Path):
        project = make_project(tmp_path)
        staging = make_staging(project)
        a = assembler(project, staging)
        first = a.assemble("set-full", ["lib-a", "svc-a", "svc-b"])
        second = a.assemble("set-full", ["lib-a", "svc-a", "svc-b"])
        assert first.generated[0].final_sha256 == second.generated[0].final_sha256
        assert second.removed == []

    def test_non_set_build_generates_no_target(self, tmp_path: Path):
        project = make_project(tmp_path)
        staging = make_staging(project)
        plan = assembler(project, staging).plan(None, ["svc-a"])
        assert plan.aggregation_status == "not_applicable"
        assert plan.target_rel_path is None
        assert plan.assets == []

    def test_non_set_build_removes_previous_target(self, tmp_path: Path):
        project = make_project(tmp_path)
        staging = make_staging(project)
        a = assembler(project, staging)
        a.assemble("set-full", ["lib-a", "svc-a", "svc-b"])
        assert (staging.install_root / TARGET_REL_PATH).is_file()
        removed = a.clean_previous()
        assert not (staging.install_root / TARGET_REL_PATH).exists()
        assert any(TARGET_REL_PATH in r["path"] for r in removed)
        assert not a.report_path.exists()

    def test_report_saved(self, tmp_path: Path):
        project = make_project(tmp_path)
        staging = make_staging(project)
        report = assembler(project, staging).assemble("set-full", ["lib-a", "svc-a", "svc-b"])
        path = staging.manifests_dir / ASSEMBLY_REPORT_NAME
        data = json.loads(path.read_text())
        assert data["release_set"] == "set-full"
        assert data["ordered_units"] == ["svc-a.service", "svc-b.service"]
        assert data["aggregation_status"] == "generated"


# ---------------------------------------------------------------------------
# Platform runtime assets
# ---------------------------------------------------------------------------


class TestPlatformAssets:
    def test_selected_assets_installed_once(self, tmp_path: Path):
        project = make_project(tmp_path)
        staging = make_staging(project)
        report = assembler(project, staging).assemble("set-full", ["lib-a", "svc-a", "svc-b"])
        targets = sorted(a.target for a in report.assets)
        assert targets == [
            "/etc/ccu/someip/vsomeip.json",
            f"/{UNIT_DIR}/svc-a.service.d/vsomeip.conf",
            f"/{UNIT_DIR}/svc-b.service.d/vsomeip.conf",
        ]
        # Shared JSON is delivered once and records all referencing services.
        shared = next(a for a in report.assets if a.target.endswith("vsomeip.json"))
        assert shared.referenced_by == ["svc-a", "svc-b"]
        assert sha256_file(staging.install_root / "etc/ccu/someip/vsomeip.json") == \
            shared.final_sha256
        assert shared.validation_status == "pass"

    def test_unselected_service_assets_not_installed(self, tmp_path: Path):
        project = make_project(tmp_path)
        staging = make_staging(project)
        report = assembler(project, staging).assemble("set-a", ["svc-a"])
        assert not (staging.install_root / UNIT_DIR / "svc-b.service.d" / "vsomeip.conf").exists()
        installed = sorted(a.target for a in report.assets)
        assert f"/{UNIT_DIR}/svc-b.service.d/vsomeip.conf" not in installed

    def test_required_asset_missing_fails(self, tmp_path: Path):
        project = make_project(tmp_path, with_json=False)
        staging = make_staging(project)
        with pytest.raises(AssemblyError) as exc:
            assembler(project, staging).assemble("set-a", ["svc-a"])
        assert "vsomeip.json" in str(exc.value)

    def test_dev_exemption_skips_missing_required_asset(self, tmp_path: Path):
        project = make_project(tmp_path, with_json=False)
        staging = make_staging(project)
        report = assembler(
            project, staging, allow_missing_assets=True
        ).assemble("set-a", ["svc-a"])
        assert report.assets
        assert report.missing
        assert any("vsomeip.json" in m["source"] for m in report.missing)

    def test_unauthorized_target_fails(self, tmp_path: Path):
        project = make_project(tmp_path)
        dep = load_config_deployment_manifest(
            project.config_deployment_manifest_path)
        # Remove the vsomeip.json authorization.
        files = [
            r for r in dep.get("testplat").files
            if r.path != "/etc/ccu/someip/vsomeip.json"
        ]
        _write_yaml(project.root / "manifests" / "config-deployment.yaml", {
            "version": 1,
            "platforms": {"testplat": {"files": [
                {"path": r.path, "owner": r.owner, "category": r.category,
                 "deploy_policy": r.deploy_policy} if r.path else
                {"path_glob": r.path_glob, "owner": r.owner,
                 "category": r.category, "deploy_policy": r.deploy_policy}
                for r in files
            ]}},
        })
        staging = make_staging(project)
        with pytest.raises(AssemblyError) as exc:
            assembler(project, staging).assemble("set-a", ["svc-a"])
        assert "not explicitly authorized" in str(exc.value)

    def test_conflicting_sources_fail(self, tmp_path: Path):
        project = make_project(tmp_path)
        rootfs = project.platform_configs_root()
        _text(rootfs / "etc/ccu/someip/other.json", '{"applications": []}\n')
        data = yaml.safe_load((project.root / "manifests/services.yaml").read_text())
        assets = data["services"]["svc-b"]["runtime"]["platform_assets"]
        # Same target as svc-a but a different source -> conflict.
        assets[1]["source"] = "etc/ccu/someip/other.json"
        _write_yaml(project.root / "manifests/services.yaml", data)
        staging = make_staging(project)
        with pytest.raises(AssemblyError) as exc:
            assembler(project, staging).assemble("set-full", ["lib-a", "svc-a", "svc-b"])
        assert any("conflicting sources" in e for e in exc.value.errors)

    def test_baseline_unit_must_exist_in_payload(self, tmp_path: Path):
        project = make_project(tmp_path)
        staging = make_staging(project)
        (staging.install_root / UNIT_DIR / "svc-b.service").unlink()
        with pytest.raises(AssemblyError) as exc:
            assembler(project, staging).assemble("set-full", ["lib-a", "svc-a", "svc-b"])
        assert "not present in the payload" in str(exc.value)

    def test_dropin_baseline_unit_must_belong_to_service(self, tmp_path: Path):
        project = make_project(tmp_path)
        data = yaml.safe_load((project.root / "manifests/services.yaml").read_text())
        # svc-a declares a drop-in for svc-b's unit -> invalid
        data["services"]["svc-a"]["runtime"]["platform_assets"][0] = {
            "source": f"{UNIT_DIR}/svc-b.service.d/vsomeip.conf",
            "target": f"/{UNIT_DIR}/svc-b.service.d/vsomeip.conf",
            "required": True,
            "validation": "vsomeip-dropin",
        }
        _write_yaml(project.root / "manifests/services.yaml", data)
        staging = make_staging(project)
        with pytest.raises(AssemblyError) as exc:
            assembler(project, staging).assemble("set-full", ["lib-a", "svc-a", "svc-b"])
        assert "must correspond to its own baseline unit" in str(exc.value)

    def test_source_target_must_correspond(self, tmp_path: Path):
        project = make_project(tmp_path)
        data = yaml.safe_load((project.root / "manifests/services.yaml").read_text())
        data["services"]["svc-a"]["runtime"]["platform_assets"][1]["target"] = \
            "/etc/ccu/someip/renamed.json"
        _write_yaml(project.root / "manifests/services.yaml", data)
        staging = make_staging(project)
        with pytest.raises(AssemblyError) as exc:
            assembler(project, staging).assemble("set-a", ["svc-a"])
        assert "one-to-one" in str(exc.value)

    def test_staging_escape_rejected(self, tmp_path: Path):
        project = make_project(tmp_path)
        data = yaml.safe_load((project.root / "manifests/services.yaml").read_text())
        data["services"]["svc-a"]["runtime"]["platform_assets"][0]["source"] = \
            "../outside/vsomeip.conf"
        _write_yaml(project.root / "manifests/services.yaml", data)
        staging = make_staging(project)
        with pytest.raises(AssemblyError):
            assembler(project, staging).assemble("set-a", ["svc-a"])


class TestStaleCleanup:
    def test_switching_set_removes_stale_assets_and_units(self, tmp_path: Path):
        project = make_project(tmp_path)
        staging = make_staging(project)
        a = assembler(project, staging)
        a.assemble("set-full", ["lib-a", "svc-a", "svc-b"])
        report = a.assemble("set-a", ["svc-a"])
        assert report.ordered_units == ["svc-a.service"]
        assert "svc-b.service" not in (staging.install_root / TARGET_REL_PATH).read_text()
        assert not (staging.install_root / UNIT_DIR / "svc-b.service.d" / "vsomeip.conf").exists()
        # Shared JSON is still required by svc-a -> kept.
        assert (staging.install_root / "etc/ccu/someip/vsomeip.json").is_file()
        assert any("svc-b.service.d" in r["path"] for r in report.removed)

    def test_library_only_set_removes_all_runtime_assets(self, tmp_path: Path):
        project = make_project(tmp_path)
        staging = make_staging(project)
        a = assembler(project, staging)
        a.assemble("set-full", ["lib-a", "svc-a", "svc-b"])
        report = a.assemble("set-lib", ["lib-a"])
        assert report.aggregation_status == "empty"
        assert not (staging.install_root / TARGET_REL_PATH).exists()
        assert not (staging.install_root / "etc/ccu/someip/vsomeip.json").exists()
        assert not (staging.install_root / UNIT_DIR / "svc-a.service.d" / "vsomeip.conf").exists()

    def test_service_owned_files_never_deleted(self, tmp_path: Path):
        project = make_project(tmp_path)
        staging = make_staging(project)
        a = assembler(project, staging)
        a.assemble("set-full", ["lib-a", "svc-a", "svc-b"])
        # A service overwrites a previously BUILD-owned drop-in with its own
        # content; the assembler must not delete it as "stale".
        dropin = staging.install_root / UNIT_DIR / "svc-a.service.d" / "vsomeip.conf"
        dropin.write_text("[Service]\nExecStart=/usr/bin/other\n")

        report = a.assemble("set-lib", ["lib-a"])
        assert dropin.is_file()
        assert any("kept" in r["reason"] for r in report.removed)


# ---------------------------------------------------------------------------
# Overlay integration: claimed paths are never written twice
# ---------------------------------------------------------------------------


class TestOverlayIntegration:
    def test_overlay_skips_build_claimed_paths(self, tmp_path: Path):
        project = make_project(tmp_path)
        staging = make_staging(project)
        assembly = assembler(project, staging)
        report = assembly.assemble("set-full", ["lib-a", "svc-a", "svc-b"])
        claimed = report.build_owned_paths
        overlay = ConfigOverlay(
            platform=project.platform,
            platform_rootfs=project.platform_configs_root(),
            install_root=staging.install_root,
            deployment=load_config_deployment_manifest(
                project.config_deployment_manifest_path),
            service_manifest=project.load_service_manifest(),
            project_root=project.root,
            state_dir=staging.manifests_dir,
            claimed_paths={p: "build:release-set-assembly" for p in claimed},
        )
        overlay_report = overlay.apply()
        overlaid = {r.logical_path for r in overlay_report.overlaid}
        assert "/etc/ccu/common.yaml" in overlaid
        assert "/etc/ccu/someip/vsomeip.json" not in overlaid
        assert all("service.d" not in p for p in overlaid)
        assert (staging.install_root / "etc/ccu/someip/vsomeip.json").is_file()


# ---------------------------------------------------------------------------
# vsomeip asset validators
# ---------------------------------------------------------------------------


class TestVsomeipDropinValidator:
    def test_valid_dropin(self, tmp_path: Path):
        path = tmp_path / "vsomeip.conf"
        path.write_text(DROPIN.format(app="svc-a"))
        result = validate_vsomeip_dropin(path, "svc-a")
        assert result.status == "pass"
        assert result.details["application"] == "svc-a"

    def test_application_mismatch_fails(self, tmp_path: Path):
        path = tmp_path / "vsomeip.conf"
        path.write_text(DROPIN.format(app="svc-b"))
        result = validate_vsomeip_dropin(path, "svc-a")
        assert result.status == "fail"
        assert any("must match the service application" in e for e in result.errors)

    def test_unauthorized_directive_rejected(self, tmp_path: Path):
        path = tmp_path / "vsomeip.conf"
        path.write_text(DROPIN.format(app="svc-a").replace(
            "[Service]\n", "[Service]\nPrivateTmp=no\nExecStart=/usr/bin/x\n"))
        result = validate_vsomeip_dropin(path, "svc-a")
        assert result.status == "fail"
        assert any("PrivateTmp" in e for e in result.errors)
        assert any("ExecStart" in e for e in result.errors)

    def test_missing_configuration_fails(self, tmp_path: Path):
        path = tmp_path / "vsomeip.conf"
        path.write_text("[Service]\nEnvironment=VSOMEIP_APPLICATION_NAME=svc-a\n")
        result = validate_vsomeip_dropin(path, "svc-a")
        assert result.status == "fail"
        assert any("VSOMEIP_CONFIGURATION" in e for e in result.errors)

    def test_wrong_section_rejected(self, tmp_path: Path):
        path = tmp_path / "vsomeip.conf"
        path.write_text("[Unit]\nDescription=x\n[Service]\n"
                        "Environment=VSOMEIP_CONFIGURATION=/etc/ccu/someip/vsomeip.json\n"
                        "Environment=VSOMEIP_APPLICATION_NAME=svc-a\n")
        result = validate_vsomeip_dropin(path, "svc-a")
        assert result.status == "fail"
        assert any("[Unit]" in e for e in result.errors)


class TestVsomeipJsonValidator:
    def _write(self, tmp_path: Path, data) -> Path:
        path = tmp_path / "vsomeip.json"
        path.write_text(json.dumps(data))
        return path

    def test_valid_with_required_applications(self, tmp_path: Path):
        path = self._write(tmp_path, {
            "applications": [{"name": "svc-a", "id": "0x1001"},
                             {"name": "svc-b", "id": "0x1002"}],
            "routing": "svc-a",
        })
        result = validate_vsomeip_json(path, ["svc-a", "svc-b"])
        assert result.status == "pass"

    def test_missing_required_application_fails(self, tmp_path: Path):
        path = self._write(tmp_path, {"applications": [{"name": "svc-a"}]})
        result = validate_vsomeip_json(path, ["svc-a", "svc-b"])
        assert result.status == "fail"
        assert any("does not register required application 'svc-b'" in e
                   for e in result.errors)

    def test_duplicate_application_fails(self, tmp_path: Path):
        path = self._write(tmp_path, {
            "applications": [{"name": "svc-a"}, {"name": "svc-a"}]
        })
        result = validate_vsomeip_json(path, ["svc-a"])
        assert result.status == "fail"
        assert any("duplicates application" in e for e in result.errors)

    def test_invalid_json_fails(self, tmp_path: Path):
        path = tmp_path / "vsomeip.json"
        path.write_text("{not json")
        result = validate_vsomeip_json(path, ["svc-a"])
        assert result.status == "fail"
        assert any("JSON parse error" in e for e in result.errors)

    def test_secret_field_rejected(self, tmp_path: Path):
        path = self._write(tmp_path, {
            "applications": [{"name": "svc-a"}],
            "device_private_key": "leaked",
        })
        result = validate_vsomeip_json(path, ["svc-a"])
        assert result.status == "fail"
        assert any("forbidden secret-bearing field" in e for e in result.errors)

    def test_pem_private_key_rejected(self, tmp_path: Path):
        path = tmp_path / "vsomeip.json"
        path.write_text(
            '{"applications": [{"name": "svc-a"}], "note": '
            '"-----BEGIN PRIVATE KEY-----"}'
        )
        result = validate_vsomeip_json(path, ["svc-a"])
        assert result.status == "fail"
        assert any("secret scan rule" in e for e in result.errors)

    def test_network_and_payload_objects_accepted(self, tmp_path: Path):
        """受控设备基线的 vsomeip 配置用对象形式 network/payload（CR-004 §5.2）。"""
        path = self._write(tmp_path, {
            "applications": [{"name": "svc-a", "id": "0x1001"},
                             {"name": "router", "id": "0x1002"}],
            "routing": "router",
            "service-discovery": {"enable": "true", "port": "30490"},
            "payload": {"max-serialized-size": "65535"},
            "threads": {"request": "2", "io": "2"},
            "network": {"primary": {"name": "eth0"}},
        })
        result = validate_vsomeip_json(path, ["svc-a"])
        assert result.status == "pass"
        assert result.details["applications"] == ["svc-a", "router"]

    def test_network_secret_field_still_rejected(self, tmp_path: Path):
        path = self._write(tmp_path, {
            "applications": [{"name": "svc-a"}],
            "network": {"primary": {"name": "eth0", "private_key": "leaked"}},
        })
        result = validate_vsomeip_json(path, ["svc-a"])
        assert result.status == "fail"
        assert any("forbidden secret-bearing field" in e for e in result.errors)

    def test_routing_must_name_registered_application(self, tmp_path: Path):
        path = self._write(tmp_path, {
            "applications": [{"name": "svc-a"}],
            "routing": "unknown-app",
        })
        result = validate_vsomeip_json(path, ["svc-a"])
        assert result.status == "fail"
        assert any("routing must name a registered application" in e
                   for e in result.errors)
