"""Release-set assembly for CCU Build (CCU-BUILD-DSN-CR-004 §4/§5).

The ``ReleaseSetAssembler`` is the single BUILD component that owns:

  * the release-set aggregation target ``usr/lib/systemd/system/ccu.target``,
    rendered from the versioned template ``packaging/systemd/ccu.target.in``
    using the *expanded* service dependency closure, and
  * the authorized platform runtime assets declared by selected services
    through ``runtime.platform_assets[]``.

Design guarantees implemented here:

  * closure, not the raw release-set service list, drives the result;
  * order = service topology order, then each service's ``systemd_units``
    declaration order, deterministically de-duplicated;
  * ``Wants=`` == ``After=``, never ``Requires=``;
  * no business unit name and no device value lives in the template or the
    generator;
  * only ``build --set <id>`` writes an aggregation target; empty unit sets
    are reported (``aggregation_status=empty``) and never written;
  * stale BUILD-owned files from the previous assembly report are removed,
    and nothing owned by a service or another owner is ever deleted;
  * every produced file is recorded with owner ``build`` for the Artifact
    Manifest (no ``unknown`` owner for BUILD-owned output).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable

from .assetcheck import (
    AssetValidationResult,
    application_from_dropin_target,
    dropin_baseline_unit,
    run_asset_validation,
)
from .config import DeployPolicyResolver, atomic_write, normalize_source_path
from .errors import AssemblyError, OverlayError
from .graph import DependencyGraph
from .manifest import (
    RELEASE_SET_AGGREGATION_TARGET,
    ConfigDeploymentManifest,
    Project,
    Service,
    ServiceManifest,
)
from .staging import StagingDir, sha256_file

# BUILD-owned aggregation target (never a service asset).
TARGET_REL_PATH = f"usr/lib/systemd/system/{RELEASE_SET_AGGREGATION_TARGET}"
TARGET_TEMPLATE_REL = "packaging/systemd/ccu.target.in"
OWNER_TARGET = "release-set-assembly"
INSTALL_COMPONENT = "ccu-release-set-runtime"
ASSEMBLY_REPORT_NAME = "assembly-report.json"
_DEFAULT_MODE = 0o644

# Unit names: basename only, no whitespace/newline, no path separators and no
# parent traversal - prevents unit-config injection / path escape (§4.2).
_UNIT_NAME_RE = re.compile(r"^[A-Za-z0-9@:_.+-]+$")


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass
class GeneratedFileRecord:
    """A BUILD-generated file (target now, extensible) (§4.2 step 8)."""

    path: str  # install-root relative
    owner: str = "build"
    owner_target: str = OWNER_TARGET
    install_component: str = INSTALL_COMPONENT
    staging: str = "rootfs"
    source: str = TARGET_TEMPLATE_REL
    source_sha256: str = ""
    final_sha256: str = ""
    mode: str = oct(_DEFAULT_MODE)
    release_set: str | None = None


@dataclass
class AssetRecord:
    """A BUILD-delivered platform runtime asset (§5)."""

    target: str  # logical rootfs path, e.g. /usr/lib/systemd/system/x.d/vsomeip.conf
    source: str  # relative to configs/<platform>/rootfs
    required: bool
    validation: str
    source_sha256: str = ""
    final_sha256: str = ""
    mode: str = oct(_DEFAULT_MODE)
    validation_status: str = "pass"
    validation_errors: list[str] = field(default_factory=list)
    referenced_by: list[str] = field(default_factory=list)
    status: str = "installed"  # installed | missing_optional

    @property
    def rel_path(self) -> str:
        return self.target.lstrip("/")


@dataclass
class AssemblyPlan:
    """Pure (no-write) assembly plan."""

    release_set: str | None = None
    requested_services: list[str] = field(default_factory=list)
    expanded_services: list[str] = field(default_factory=list)
    ordered_units: list[str] = field(default_factory=list)
    aggregation_status: str = "not_applicable"
    target_rel_path: str | None = None
    target_text: str | None = None
    assets: list[AssetRecord] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    missing: list[dict[str, str]] = field(default_factory=list)
    conflicts: list[dict[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def claimed_paths(self) -> dict[str, str]:
        """Logical install-root paths claimed by this plan (overlay must skip)."""
        claimed: dict[str, str] = {}
        if self.target_rel_path:
            claimed[self.target_rel_path] = "build:release-set-assembly"
        for asset in self.assets:
            claimed[asset.rel_path] = "build:release-set-assembly"
        return claimed


@dataclass
class AssemblyReport:
    """Machine-readable assembly report (§5.3/§7.2)."""

    version: str = "0.1.0"
    release_set: str | None = None
    requested_services: list[str] = field(default_factory=list)
    expanded_services: list[str] = field(default_factory=list)
    ordered_units: list[str] = field(default_factory=list)
    aggregation_status: str = "not_applicable"
    generated: list[GeneratedFileRecord] = field(default_factory=list)
    assets: list[AssetRecord] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    missing: list[dict[str, str]] = field(default_factory=list)
    conflicts: list[dict[str, str]] = field(default_factory=list)
    removed: list[dict[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def build_owned_paths(self) -> dict[str, dict[str, str]]:
        """install-root relative path -> provenance for the Artifact Manifest."""
        result: dict[str, dict[str, str]] = {}
        for rec in self.generated:
            result[rec.path] = {
                "source": rec.source,
                "source_sha256": rec.source_sha256,
                "final_sha256": rec.final_sha256,
                "mode": rec.mode,
                "staging": rec.staging,
                "install_component": rec.install_component,
                "owner_target": rec.owner_target,
                "referenced_by": "",
            }
        for asset in self.assets:
            if asset.status != "installed":
                continue
            result[asset.rel_path] = {
                "source": asset.source,
                "source_sha256": asset.source_sha256,
                "final_sha256": asset.final_sha256,
                "mode": asset.mode,
                "staging": "rootfs",
                "install_component": INSTALL_COMPONENT,
                "owner_target": OWNER_TARGET,
                "referenced_by": ",".join(asset.referenced_by),
            }
        return result

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)


# ---------------------------------------------------------------------------
# Assembler
# ---------------------------------------------------------------------------


class ReleaseSetAssembler:
    """Owns the release-set aggregation target and platform runtime assets."""

    def __init__(
        self,
        project: Project,
        staging: StagingDir,
        service_manifest: ServiceManifest,
        deployment: ConfigDeploymentManifest | None = None,
        allow_missing_assets: bool = False,
    ):
        self.project = project
        self.staging = staging
        self.service_manifest = service_manifest
        self.deployment = (
            deployment
            if deployment is not None
            else project.load_config_deployment_manifest()
        )
        plat = self.deployment.get(project.platform)
        self._resolver = DeployPolicyResolver(plat.files if plat is not None else [])
        self.allow_missing_assets = allow_missing_assets
        self._require_baseline_in_payload = True
        self.template_path = project.root / TARGET_TEMPLATE_REL
        self.report_path = staging.manifests_dir / ASSEMBLY_REPORT_NAME

    # -- planning ---------------------------------------------------------

    def plan(
        self,
        release_set_id: str | None,
        requested_services: Iterable[str],
        require_baseline_in_payload: bool = True,
    ) -> AssemblyPlan:
        """Compute the aggregation plan without touching the install-root."""
        self._require_baseline_in_payload = require_baseline_in_payload
        requested = list(requested_services)
        plan = AssemblyPlan(
            release_set=release_set_id,
            requested_services=requested,
        )
        graph = DependencyGraph(self.service_manifest)
        order = graph.build_order(requested)
        plan.expanded_services = order

        if release_set_id is None:
            # Single-service / all-services builds never produce (or carry)
            # a release-set aggregation result (§4.2 step 1).
            plan.aggregation_status = "not_applicable"
            return plan

        # --- ordered units -------------------------------------------------
        unit_owners: dict[str, str] = {}
        duplicates_within_service: list[tuple[str, str]] = []
        for service_id in order:
            service = self.service_manifest.get(service_id)
            if service is None:
                continue
            seen_in_service: set[str] = set()
            for unit in service.runtime.systemd_units:
                if not self._validate_unit_name(unit, service_id, plan):
                    continue
                if unit in seen_in_service:
                    duplicates_within_service.append((service_id, unit))
                    continue
                seen_in_service.add(unit)
                owner = unit_owners.get(unit)
                if owner is not None and owner != service_id:
                    plan.conflicts.append({
                        "unit": unit,
                        "reason": f"unit owned by both '{owner}' and '{service_id}'",
                    })
                    plan.errors.append(
                        f"Unit '{unit}' is declared by both '{owner}' and "
                        f"'{service_id}'"
                    )
                    continue
                unit_owners[unit] = service_id
                plan.ordered_units.append(unit)
        for service_id, unit in duplicates_within_service:
            plan.warnings.append(
                f"Service '{service_id}' declares unit '{unit}' more than once; "
                f"de-duplicated"
            )

        # A service must not own the BUILD aggregation target (§3.3).
        if RELEASE_SET_AGGREGATION_TARGET in unit_owners:
            plan.errors.append(
                f"'{RELEASE_SET_AGGREGATION_TARGET}' is a BUILD-reserved "
                f"release-set aggregation target and must not be declared by "
                f"a service"
            )

        if plan.ordered_units:
            plan.aggregation_status = "generated"
            plan.target_rel_path = TARGET_REL_PATH
            plan.target_text = self._render_target(release_set_id, plan.ordered_units)
        else:
            plan.aggregation_status = "empty"

        # --- platform runtime assets --------------------------------------
        self._plan_assets(plan)
        return plan

    def _validate_unit_name(self, unit: str, service_id: str, plan: AssemblyPlan) -> bool:
        if not unit:
            plan.errors.append(f"Service '{service_id}' declares an empty unit name")
            return False
        if not _UNIT_NAME_RE.match(unit) or "/" in unit or "\\" in unit or ".." in unit:
            plan.errors.append(
                f"Service '{service_id}' declares illegal unit name '{unit}' "
                f"(basename only, no whitespace/path/..)"
            )
            return False
        return True

    def _render_target(self, release_set_id: str, units: list[str]) -> str:
        if not self.template_path.is_file():
            raise AssemblyError(
                f"Aggregation target template not found: {self.template_path}"
            )
        template = self.template_path.read_text(encoding="utf-8")
        # Strip the template's documentation header so the generated unit is
        # only the fixed structure + substituted placeholders (deterministic
        # content independent of template comments, §4.1).
        body = [
            line for line in template.splitlines()
            if not line.startswith("#")
        ]
        while body and not body[0].strip():
            body.pop(0)
        rendered = "\n".join(body).rstrip("\n") + "\n"
        # Deterministic canonical rendering: single space separation, the
        # template supplies the terminating newline.
        rendered = rendered.replace("@RELEASE_SET_ID@", release_set_id)
        rendered = rendered.replace("@ORDERED_UNITS@", " ".join(units))
        return rendered

    def _plan_assets(self, plan: AssemblyPlan) -> None:
        """Resolve, authorize, de-duplicate and validate platform assets (§5)."""
        rootfs = self.project.platform_configs_root()
        by_target: dict[str, AssetRecord] = {}
        declaring_services: dict[str, list[str]] = {}

        for service_id in plan.expanded_services:
            service = self.service_manifest.get(service_id)
            if service is None:
                continue
            self.validate_service_assets(service, plan)
            for asset in service.runtime.platform_assets:
                target = asset.target
                declaring_services.setdefault(target, []).append(service_id)
                existing = by_target.get(target)
                if existing is not None:
                    if existing.source != asset.source:
                        plan.conflicts.append({
                            "target": target,
                            "reason": (
                                f"same target declared with different sources: "
                                f"'{existing.source}' vs '{asset.source}'"
                            ),
                        })
                        plan.errors.append(
                            f"Platform asset target '{target}' declared with "
                            f"conflicting sources '{existing.source}' and "
                            f"'{asset.source}'"
                        )
                    if asset.required:
                        existing.required = True
                    if service_id not in existing.referenced_by:
                        existing.referenced_by.append(service_id)
                    continue

                record = AssetRecord(
                    target=target,
                    source=asset.source,
                    required=asset.required,
                    validation=asset.validation,
                    referenced_by=[service_id],
                )
                by_target[target] = record

                rule_error = self._authorized_rule_error(target)
                if rule_error:
                    plan.errors.append(rule_error)
                source_path = rootfs / asset.source
                if not source_path.is_file():
                    if asset.required and not self.allow_missing_assets:
                        plan.missing.append({
                            "target": target,
                            "source": asset.source,
                            "reason": "required platform asset missing",
                        })
                        plan.errors.append(
                            f"Required platform asset '{asset.source}' for "
                            f"service '{service_id}' is missing "
                            f"(configs/{self.project.platform}/rootfs)"
                        )
                    else:
                        record.status = "missing_optional"
                        plan.missing.append({
                            "target": target,
                            "source": asset.source,
                            "reason": "platform asset missing (exemption/optional)",
                        })
                        plan.warnings.append(
                            f"Platform asset '{asset.source}' missing "
                            f"(service '{service_id}'); not installed"
                        )
                        by_target.pop(target, None)
                    continue
                try:
                    normalized = normalize_source_path(rootfs, source_path)
                except OverlayError as exc:
                    plan.errors.append(
                        f"Platform asset source '{asset.source}' rejected: {exc}"
                    )
                    by_target.pop(target, None)
                    continue
                if normalized != asset.source:
                    plan.errors.append(
                        f"Platform asset source '{asset.source}' must equal its "
                        f"normalized rootfs path '{normalized}'"
                    )
                    by_target.pop(target, None)
                    continue
                record.source_sha256 = sha256_file(source_path)

        plan.assets = list(by_target.values())

        # Baseline unit must exist in the current payload for drop-in assets.
        for asset in plan.assets:
            baseline = dropin_baseline_unit(asset.target)
            if baseline is None:
                continue
            if baseline not in plan.ordered_units:
                plan.errors.append(
                    f"Platform drop-in target '{asset.target}' does not "
                    f"correspond to a unit in the release-set closure "
                    f"(expected '{baseline}')"
                )
            elif self.staging.install_root.is_dir() and self._require_baseline_in_payload:
                installed = self.staging.install_root / "usr/lib/systemd/system" / baseline
                if not installed.is_file():
                    plan.errors.append(
                        f"Platform drop-in target '{asset.target}' corresponds to "
                        f"baseline unit '{baseline}' which is not present in the "
                        f"payload install-root"
                    )

        # Content validation (declared validator, never extension-based).
        required_applications = sorted({
            application_from_dropin_target(asset.target)
            for asset in plan.assets
            if asset.validation == "vsomeip-dropin"
        } - {None})
        for asset in plan.assets:
            if not asset.source_sha256:
                continue
            source_path = rootfs / asset.source
            application = application_from_dropin_target(asset.target) or ""
            result: AssetValidationResult = run_asset_validation(
                asset.validation,
                source_path,
                expected_application=application,
                required_applications=required_applications,
            )
            asset.validation_status = result.status
            asset.validation_errors = list(result.errors)
            if result.status == "fail":
                plan.errors.extend(
                    f"Platform asset '{asset.target}' ({asset.validation}): {err}"
                    for err in result.errors
                )

        # Explicit record of every selected service's declared assets.
        for service_id in plan.expanded_services:
            service = self.service_manifest.get(service_id)
            if service is None:
                continue
            for asset in service.runtime.platform_assets:
                if asset.target not in by_target:
                    plan.skipped.append({
                        "target": asset.target,
                        "source": asset.source,
                        "service": service_id,
                        "reason": "asset not installed (missing/exemption)",
                    })

    def validate_service_assets(self, service: Service, plan: AssemblyPlan) -> None:
        """Validate a service's platform asset declarations (§3.2 contract)."""
        seen_targets: set[str] = set()
        for asset in service.runtime.platform_assets:
            source = asset.source
            target = asset.target
            if source.startswith("/"):
                plan.errors.append(
                    f"Service '{service.id}' platform asset source must be "
                    f"relative to configs rootfs: '{source}'"
                )
            if "\\" in source or ".." in source.split("/"):
                plan.errors.append(
                    f"Service '{service.id}' platform asset source '{source}' "
                    f"is not a safe POSIX path"
                )
            if target in seen_targets:
                plan.errors.append(
                    f"Service '{service.id}' declares platform asset target "
                    f"'{target}' more than once"
                )
            seen_targets.add(target)
            if not target.startswith("/"):
                plan.errors.append(
                    f"Service '{service.id}' platform asset target must be an "
                    f"absolute rootfs logical path: '{target}'"
                )
            elif target != "/" + source.lstrip("/"):
                plan.errors.append(
                    f"Service '{service.id}' platform asset target '{target}' "
                    f"must correspond one-to-one with source '{source}'"
                )
            if asset.validation == "vsomeip-dropin":
                baseline = dropin_baseline_unit(target)
                if baseline is None:
                    plan.errors.append(
                        f"Service '{service.id}' vsomeip drop-in target "
                        f"'{target}' is not under a '*.service.d' directory"
                    )
                elif baseline not in service.runtime.systemd_units:
                    plan.errors.append(
                        f"Service '{service.id}' vsomeip drop-in target "
                        f"'{target}' must correspond to its own baseline unit "
                        f"'{baseline}'"
                    )
            if asset.validation == "vsomeip-json" and not target.startswith("/etc/ccu/"):
                plan.errors.append(
                    f"Service '{service.id}' vsomeip-json asset target must be "
                    f"under /etc/ccu/: '{target}'"
                )

    def _authorized_rule_error(self, target: str) -> str | None:
        try:
            rule = self._resolver.resolve(target)
        except OverlayError as exc:
            return f"Platform asset target '{target}' policy conflict: {exc}"
        if rule is None:
            return (
                f"Platform asset target '{target}' is not explicitly authorized "
                f"in manifests/config-deployment.yaml (owner=build, "
                f"release-managed, replace)"
            )
        if (rule.owner, rule.category, rule.deploy_policy) != (
            "build", "release-managed", "replace"
        ):
            return (
                f"Platform asset target '{target}' must be authorized as "
                f"owner=build / category=release-managed / deploy_policy=replace "
                f"(got {rule.owner}/{rule.category}/{rule.deploy_policy})"
            )
        return None

    # -- writing ----------------------------------------------------------

    def write(self, plan: AssemblyPlan) -> AssemblyReport:
        """Apply a plan to the install-root and emit the assembly report."""
        report = AssemblyReport(
            release_set=plan.release_set,
            requested_services=list(plan.requested_services),
            expanded_services=list(plan.expanded_services),
            ordered_units=list(plan.ordered_units),
            aggregation_status=plan.aggregation_status,
            skipped=list(plan.skipped),
            missing=list(plan.missing),
            conflicts=list(plan.conflicts),
            errors=list(plan.errors),
            warnings=list(plan.warnings),
        )
        if plan.errors:
            raise AssemblyError(
                f"Release-set assembly failed ({len(plan.errors)} error(s)): "
                + "; ".join(plan.errors[:3]),
                plan.errors,
            )

        written: set[str] = set()
        install_root = self.staging.install_root
        install_root.mkdir(parents=True, exist_ok=True)

        if plan.target_rel_path and plan.target_text is not None:
            target_path = install_root / plan.target_rel_path
            atomic_write(target_path, plan.target_text.encode("utf-8"), _DEFAULT_MODE)
            written.add(plan.target_rel_path)
            report.generated.append(GeneratedFileRecord(
                path=plan.target_rel_path,
                source=TARGET_TEMPLATE_REL,
                source_sha256=sha256_file(self.template_path),
                final_sha256=sha256_file(target_path),
                release_set=plan.release_set,
            ))

        rootfs = self.project.platform_configs_root()
        for asset in plan.assets:
            if asset.status != "installed" and not (rootfs / asset.source).is_file():
                continue
            source_path = rootfs / asset.source
            target_path = install_root / asset.rel_path
            atomic_write(target_path, source_path.read_bytes(), _DEFAULT_MODE)
            asset.final_sha256 = sha256_file(target_path)
            written.add(asset.rel_path)
            report.assets.append(asset)

        report.removed = self._remove_stale(written)
        report.save(self.report_path)
        return report

    def assemble(
        self, release_set_id: str | None, requested_services: Iterable[str]
    ) -> AssemblyReport:
        plan = self.plan(release_set_id, requested_services)
        return self.write(plan)
    def _remove_stale(self, written: set[str]) -> list[dict[str, str]]:
        """Remove BUILD-owned files from the previous report (§4.2 step 9)."""
        removed: list[dict[str, str]] = []
        previous = self.previous_report()
        if previous is None:
            return removed
        install_root = self.staging.install_root
        previous_paths: list[tuple[str, str]] = []
        for rec in previous.get("generated", []) or []:
            previous_paths.append((str(rec.get("path", "")), str(rec.get("final_sha256", ""))))
        for rec in previous.get("assets", []) or []:
            target = str(rec.get("target", "")).lstrip("/")
            previous_paths.append((target, str(rec.get("final_sha256", ""))))
        for rel_path, previous_sha in previous_paths:
            if not rel_path or rel_path in written:
                continue
            path = install_root / rel_path
            if not path.is_file():
                continue
            # Never delete a file that is no longer BUILD-owned content: only
            # remove it when it still matches the recorded BUILD output SHA.
            if previous_sha and sha256_file(path) != previous_sha:
                removed.append({
                    "path": rel_path,
                    "reason": "kept: content no longer matches BUILD-owned output",
                })
                continue
            path.unlink()
            removed.append({"path": rel_path, "reason": "stale BUILD-owned output"})
        return removed

    def previous_report(self) -> dict[str, Any] | None:
        if not self.report_path.is_file():
            return None
        try:
            return json.loads(self.report_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def clean_previous(self) -> list[dict[str, str]]:
        """Remove BUILD-owned output for non-release-set builds (§4.2 step 1).

        A single-service / all-services build must not carry a previous
        release-set aggregation result or its platform assets.
        """
        removed = self._remove_stale(written=set())
        if self.report_path.is_file():
            self.report_path.unlink()
        return removed

    def claimed_paths_from_report(self) -> dict[str, str]:
        """Logical paths claimed by the previous assembly run.

        Used by the generic config overlay to skip BUILD-claimed paths even
        when the assembler does not run (e.g. ``ccu_build config``).
        """
        previous = self.previous_report()
        claimed: dict[str, str] = {}
        if previous is None:
            return claimed
        for rec in previous.get("generated", []) or []:
            claimed[str(rec.get("path", ""))] = "build:release-set-assembly"
        for rec in previous.get("assets", []) or []:
            claimed[str(rec.get("target", "")).lstrip("/")] = "build:release-set-assembly"
        return claimed
