"""Manifest ↔ install-root reconciliation (CCU-BUILD-RELEASE-REQ-001).

After a release-set / single-service build, the declared contract and the
installed payload must agree in both directions:

  * every declared daemon, unit and config path of the built closure is
    present in the install-root (missing declaration = failure);
  * no release asset in the install-root is unowned (``unknown`` owner =
    failure), so package/traceability cannot silently degrade;
  * leftovers of a previous release-set are reported as stale evidence
    instead of being mistaken for part of the current closure.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable

from .manifest import ServiceManifest
from .staging import StagedFile


@dataclass
class ReconcileReport:
    """Result of the bidirectional declaration/install-root check."""

    services: list[str] = field(default_factory=list)
    checked_units: list[str] = field(default_factory=list)
    checked_configs: list[str] = field(default_factory=list)
    missing_units: list[str] = field(default_factory=list)
    missing_configs: list[str] = field(default_factory=list)
    unknown_assets: list[str] = field(default_factory=list)
    stale_service_assets: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        return "fail" if self.errors else "pass"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status
        return data


def reconcile_install_root(
    service_manifest: ServiceManifest,
    services: Iterable[str],
    install_root: Path,
    staged_files: list[StagedFile],
    owned_paths: set[str],
    previous_owned_paths: set[str] | None = None,
    skip_config_paths: set[str] | None = None,
) -> ReconcileReport:
    """Check declared contract against the install-root payload.

    ``staged_files`` are the scanned install-root/sdk files (``rel_path``
    like ``rootfs/usr/bin/x``); ``owned_paths`` are the install-root relative
    paths the current build can attribute to a service, BUILD or the config
    overlay. ``previous_owned_paths`` allows previously owned assets of
    services that left the closure to be reported as stale instead of unowned.
    ``skip_config_paths`` names logical config paths whose provider is a
    documented no-op for this build (e.g. BUILD-only ``common.yaml`` on the
    native HOST platform where ``configs/host/rootfs`` does not exist).
    """
    report = ReconcileReport(services=sorted(set(services)))
    install_root = Path(install_root)
    unit_dir = install_root / "usr/lib/systemd/system"

    for service_id in report.services:
        svc = service_manifest.get(service_id)
        if svc is None:
            report.errors.append(
                f"Built service '{service_id}' is not declared in the manifest"
            )
            continue
        for unit in svc.runtime.systemd_units:
            report.checked_units.append(unit)
            if not (unit_dir / unit).is_file():
                report.missing_units.append(unit)
                report.errors.append(
                    f"Service '{service_id}' declares unit '{unit}' but it is "
                    f"not present in the install-root"
                )
        for logical in svc.runtime.config_paths:
            if logical in (skip_config_paths or set()):
                continue
            rel = logical.lstrip("/")
            report.checked_configs.append(logical)
            if not (install_root / rel).is_file():
                report.missing_configs.append(logical)
                report.errors.append(
                    f"Service '{service_id}' declares config path '{logical}' "
                    f"but it is not present in the install-root"
                )

    previous = previous_owned_paths or set()
    for staged in staged_files:
        if not staged.rel_path.startswith("rootfs/"):
            continue  # SDK staging is covered by the artifact manifest
        rel = staged.rel_path[len("rootfs/"):]
        if rel in owned_paths:
            continue
        if rel in previous:
            report.stale_service_assets.append(rel)
            report.warnings.append(
                f"'{rel}' is a leftover release asset from a previous build "
                f"(not part of the current closure)"
            )
            continue
        report.unknown_assets.append(rel)
        report.errors.append(
            f"Install-root path '{rel}' has no owner (declared manifest, "
            f"BUILD release-set output or authorized config overlay)"
        )
    return report
