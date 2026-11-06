"""Verification for CCU Build.

Provides package verification (checksum, manifest integrity, structure)
and staging verification (ELF check, artifact manifest, path conflicts).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from .elfcheck import check_staging
from .staging import StagingDir, sha256_file
from .unitcheck import check_install_root_units, load_aggregation_target_units


@dataclass
class VerifyResult:
    """Result of a verification run."""

    checks: list[dict[str, Any]] = field(default_factory=list)
    status: str = "pending"
    errors: list[str] = field(default_factory=list)

    def add_check(self, name: str, passed: bool, message: str = "") -> None:
        self.checks.append({
            "name": name,
            "status": "success" if passed else "failed",
            "message": message,
        })
        if not passed:
            self.status = "failed"
            self.errors.append(f"{name}: {message}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Verifier:
    """Verifies packages and staging output."""

    def __init__(self, staging: StagingDir, project=None):
        self.staging = staging
        self.project = project

    def verify_staging(self, expected_machine: int | None = None) -> VerifyResult:
        """Verify the staging directory: ELF check + artifact manifest."""
        from .elfcheck import _ELFCLASS64

        result = VerifyResult()

        # 1. Install-root exists
        if not self.staging.install_root.exists():
            result.add_check(
                "install-root-exists", False,
                f"Install-root not found: {self.staging.install_root}",
            )
            result.status = "failed"
            return result
        result.add_check("install-root-exists", True)

        # 2. ELF / pollution check (skipped for native HOST builds: the host
        #    object format is not the TARGET format).
        is_host = getattr(self.project, "platform", None) == "host"
        try:
            elf_results = []
            if is_host:
                result.add_check(
                    "elf-pollution-check", True,
                    "skipped for native HOST build (no TARGET object format)",
                )
            elif expected_machine is not None:
                elf_results = check_staging(
                    self.staging.install_root,
                    expected_machine=expected_machine,
                    expected_class=_ELFCLASS64,
                )
            else:
                # Machine unknown (TBD platform): check for Mach-O only
                from .elfcheck import ElfCheckResult, classify_file
                for path in sorted(self.staging.install_root.rglob("*")):
                    if not path.is_file():
                        continue
                    r = ElfCheckResult(path=path)
                    r.classification = classify_file(path)
                    if r.classification.file_type == "macho":
                        r.violations.append(
                            f"Mach-O binary detected: {path}"
                        )
                    elf_results.append(r)
            if not is_host:
                violations = sum(len(r.violations) for r in elf_results)
                warnings = sum(len(r.warnings) for r in elf_results)
                result.add_check(
                    "elf-pollution-check",
                    violations == 0,
                    f"{len(elf_results)} files checked, "
                    f"{violations} violation(s), {warnings} warning(s)",
                )
        except Exception as exc:
            result.add_check("elf-pollution-check", False, str(exc))

        # 3. Artifact manifest exists and is valid JSON
        manifest_path = self.staging.manifests_dir / "artifact-manifest.json"
        if not manifest_path.exists():
            result.add_check(
                "artifact-manifest", False,
                f"Artifact manifest not found: {manifest_path}",
            )
        else:
            try:
                with open(manifest_path, encoding="utf-8") as f:
                    manifest = json.load(f)
                artifact_count = len(manifest.get("artifacts", []))
                result.add_check(
                    "artifact-manifest", True,
                    f"{artifact_count} artifact(s) recorded",
                )
            except (json.JSONDecodeError, KeyError) as exc:
                result.add_check("artifact-manifest", False, str(exc))

        # 4. Check for path conflicts in manifest
        if manifest_path.exists():
            try:
                with open(manifest_path, encoding="utf-8") as f:
                    manifest = json.load(f)
                paths: dict[str, str] = {}
                conflicts = 0
                for artifact in manifest.get("artifacts", []):
                    path = artifact.get("path", "")
                    owner = artifact.get("owner_service", "")
                    if path in paths and paths[path] != owner:
                        conflicts += 1
                    else:
                        paths[path] = owner
                result.add_check(
                    "path-conflicts",
                    conflicts == 0,
                    f"{conflicts} conflict(s)" if conflicts else "No conflicts",
                )
                self._check_release_set(manifest, result)
                self._check_traceability(manifest, result)
                self._check_stale(manifest, result)
            except Exception as exc:
                result.add_check("path-conflicts", False, str(exc))

        # 5. Equivalent systemd unit check (systemd-analyze when available).
        unit_result = check_install_root_units(self.staging.install_root)
        result.add_check(
            "systemd-units",
            unit_result.status == "pass",
            f"{unit_result.engine}: {len(unit_result.checked)} file(s)"
            + ("; " + "; ".join(unit_result.errors) if unit_result.errors else ""),
        )

        # 6. Platform runtime asset revalidation on the final install-root.
        if self.project is not None and manifest_path.exists():
            try:
                self._check_platform_assets(manifest, result)
            except Exception as exc:
                result.add_check("platform-assets", False, str(exc))

        if result.status == "pending":
            result.status = "success"

        return result

    def _check_release_set(self, manifest: dict[str, Any], result: VerifyResult) -> None:
        """Verify release-set evidence against the install-root payload (§7.2)."""
        release_set = manifest.get("release_set")
        install_root = self.staging.install_root
        target = install_root / "usr/lib/systemd/system/ccu.target"
        unit_dir = install_root / "usr/lib/systemd/system"
        payload_units = sorted(
            p.name for p in unit_dir.glob("*.service")
        ) if unit_dir.is_dir() else []
        if not release_set:
            result.add_check(
                "release-set-evidence",
                not target.is_file(),
                "no release-set evidence (single-service/all build); "
                + ("aggregation target present" if target.is_file() else "no target"),
            )
            return
        units = list(release_set.get("systemd_units") or [])
        if not units:
            ok = release_set.get("aggregation_status") == "empty" and not target.is_file()
            result.add_check(
                "release-set-evidence", ok,
                f"empty aggregation ({release_set.get('aggregation_status')})",
            )
            return
        wants, after = load_aggregation_target_units(install_root)
        missing = [u for u in units if u not in payload_units]
        ok = (
            target.is_file()
            and wants == units
            and after == units
            and not missing
        )
        result.add_check(
            "release-set-evidence",
            ok,
            f"release-set '{release_set.get('id')}': units={units} "
            f"wants={wants} after={after} missing={missing}",
        )

    def _check_traceability(self, manifest: dict[str, Any], result: VerifyResult) -> None:
        """Every artifact must have an owner; BUILD-owned files full evidence (§7.2)."""
        unknown = [
            a.get("path", "") for a in manifest.get("artifacts", []) or []
            if str(a.get("owner_service", "")) in ("", "unknown")
        ]
        result.add_check(
            "artifact-ownership",
            not unknown,
            "no unknown owner" if not unknown
            else "unknown owner: " + ", ".join(sorted(unknown)),
        )
        incomplete: list[str] = []
        for artifact in manifest.get("artifacts", []) or []:
            if str(artifact.get("owner_service")) != "build":
                continue
            if artifact.get("config_provenance"):
                # BUILD platform overlay output: provenance is recorded in
                # config_provenance (default/platform source, SHA, policy).
                continue
            if not artifact.get("source") or not artifact.get("source_sha256"):
                incomplete.append(str(artifact.get("path", "")))
            elif not artifact.get("mode"):
                incomplete.append(str(artifact.get("path", "")))
        result.add_check(
            "build-owned-evidence",
            not incomplete,
            "BUILD-owned files recorded with source/SHA/mode"
            if not incomplete
            else "incomplete BUILD-owned evidence: " + ", ".join(sorted(incomplete)),
        )

    def _check_stale(self, manifest: dict[str, Any], result: VerifyResult) -> None:
        """No install-root file may be missing from the artifact manifest (§9.9)."""
        recorded = {
            str(a.get("path", "")) for a in manifest.get("artifacts", []) or []
        }
        stale: list[str] = []
        for path in sorted(self.staging.install_root.rglob("*")):
            if not path.is_file() and not path.is_symlink():
                continue
            rel = str(path.relative_to(self.staging.install_root)).replace(os.sep, "/")
            if f"rootfs/{rel}" not in recorded:
                stale.append(rel)
        result.add_check(
            "stale-assets",
            not stale,
            "install-root matches the artifact manifest" if not stale
            else "unrecorded install-root file(s): " + ", ".join(stale),
        )

    def _check_platform_assets(self, manifest: dict[str, Any], result: VerifyResult) -> None:
        """Revalidate delivered platform runtime assets on the install-root."""
        from .assetcheck import application_from_dropin_target, run_asset_validation

        service_manifest = self.project.load_service_manifest()
        release_set = manifest.get("release_set") or {}
        services = list(release_set.get("expanded_services") or [])
        if not services:
            services = list(service_manifest.services.keys())
        declared = []
        for service_id in services:
            svc = service_manifest.get(service_id)
            if svc is None:
                continue
            for asset in svc.runtime.platform_assets:
                declared.append(asset)
        installed = []
        missing: list[str] = []
        for asset in declared:
            target = self.staging.install_root / asset.target.lstrip("/")
            if target.is_file():
                installed.append((asset, target))
            elif asset.required:
                missing.append(asset.target)
        dropin_apps = sorted({
            app for asset, _ in installed
            if asset.validation == "vsomeip-dropin"
            for app in [application_from_dropin_target(asset.target)] if app
        })
        failures: list[str] = []
        for asset, target in installed:
            app = application_from_dropin_target(asset.target) or ""
            outcome = run_asset_validation(
                asset.validation, target,
                expected_application=app,
                required_applications=dropin_apps if asset.validation == "vsomeip-json" else (),
            )
            if outcome.status == "fail":
                failures.append(
                    f"{asset.target}: " + "; ".join(outcome.errors)
                )
        result.add_check(
            "platform-assets",
            not failures and not missing,
            f"{len(installed)} asset(s) revalidated"
            + (f"; failures: {'; '.join(failures)}" if failures else "")
            + (f"; missing required: {', '.join(missing)}" if missing else ""),
        )

    def verify_package(self, package_path: Path) -> VerifyResult:
        """Verify a release package: checksum + structure."""
        result = VerifyResult()

        if not package_path.is_file():
            result.add_check("package-exists", False, f"Package not found: {package_path}")
            result.status = "failed"
            return result
        result.add_check("package-exists", True)

        # Checksum file
        checksum_file = package_path.with_suffix(".sha256")
        if not checksum_file.is_file():
            result.add_check("checksum-file", False, "Checksum file not found")
        else:
            with open(checksum_file) as f:
                expected = f.read().split()[0]
            actual = sha256_file(package_path)
            result.add_check(
                "checksum-match",
                expected == actual,
                f"expected={expected[:16]}..., actual={actual[:16]}..."
                if expected != actual else "Checksum matches",
            )

        # Package structure
        import tarfile
        try:
            with tarfile.open(package_path, "r:gz") as tar:
                members = tar.getnames()
                has_install_root = any("install-root" in m for m in members)
                has_manifest = any("artifact-manifest.json" in m for m in members)
                has_package_info = any("package.json" in m for m in members)
                result.add_check(
                    "package-structure",
                    has_install_root and has_manifest and has_package_info,
                    f"install_root={has_install_root}, manifest={has_manifest}, "
                    f"package_info={has_package_info}",
                )
        except Exception as exc:
            result.add_check("package-structure", False, str(exc))

        if result.status == "pending":
            result.status = "success"

        return result
