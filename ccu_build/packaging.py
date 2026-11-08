"""Packaging for CCU Build.

Creates release packages (tar + artifact manifest) from the staging
directory (设计 §6/§10).  Each package includes:

  * install-root/  - all target files
  * manifest.json  - artifact manifest with per-file metadata
  * package.json   - package metadata (version, git commit, rollback info)
"""

from __future__ import annotations

import json
import tarfile
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import PackageError
from .staging import StagingDir, find_forbidden_payload, sha256_file


@dataclass
class PackageMetadata:
    """Metadata embedded in a release package."""

    package_name: str
    version: str
    git_commit: str
    platform: str
    profile: str
    created_at: str
    artifact_manifest_path: str
    artifact_count: int
    package_sha256: str = ""
    rollback_info: dict[str, Any] | None = None
    # CCU-BUILD-DSN-CR-004 §7.2: release-set evidence (None for non-set builds).
    release_set: dict[str, Any] | None = None


class Packager:
    """Creates release packages from staging output."""

    def __init__(self, staging: StagingDir):
        self.staging = staging

    def create_package(
        self,
        name: str,
        version: str,
        git_commit: str,
        artifact_manifest_path: Path | None = None,
    ) -> Path:
        """Create a tar.gz package from the staging install-root.

        Returns the path to the created package file.
        """
        if not self.staging.install_root.exists():
            raise PackageError(
                f"Staging install-root does not exist: {self.staging.install_root}"
            )

        # Development fixtures and device instance state never enter a release
        # package (CCU-BUILD-REQ-CR-005, RELEASE-REQ-001).
        forbidden = find_forbidden_payload(self.staging.install_root)
        if forbidden:
            raise PackageError(
                "Refusing to package forbidden payload file(s): "
                + ", ".join(forbidden)
            )

        if artifact_manifest_path is None:
            artifact_manifest_path = self.staging.manifests_dir / "artifact-manifest.json"

        if not artifact_manifest_path.exists():
            raise PackageError(
                f"Artifact manifest not found: {artifact_manifest_path}"
            )

        with open(artifact_manifest_path, encoding="utf-8") as f:
            artifact_manifest = json.load(f)

        release_set = artifact_manifest.get("release_set")
        self._check_release_set_consistency(artifact_manifest, release_set)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        pkg_filename = f"{name}-{version}-{timestamp}.tar.gz"
        pkg_path = self.staging.packages_dir / pkg_filename

        metadata = PackageMetadata(
            package_name=name,
            version=version,
            git_commit=git_commit,
            platform=self.staging.platform,
            profile=self.staging.profile,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            artifact_manifest_path=str(artifact_manifest_path.relative_to(self.staging.root)),
            artifact_count=len(artifact_manifest.get("artifacts", [])),
            release_set=release_set,
            rollback_info={
                "previous_version": None,
                "backup_path": None,
            },
        )

        self.staging.packages_dir.mkdir(parents=True, exist_ok=True)

        with tarfile.open(pkg_path, "w:gz") as tar:
            for path in sorted(self.staging.install_root.rglob("*")):
                if path.is_file() or path.is_symlink():
                    arcname = str(path.relative_to(self.staging.root))
                    tar.add(path, arcname=arcname)

            arcname = str(artifact_manifest_path.relative_to(self.staging.root))
            tar.add(artifact_manifest_path, arcname=arcname)

            meta_path = self.staging.manifests_dir / "package.json"
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(asdict(metadata), f, indent=2)
            tar.add(meta_path, arcname=str(meta_path.relative_to(self.staging.root)))

        metadata.package_sha256 = sha256_file(pkg_path)

        with open(self.staging.manifests_dir / "package.json", "w", encoding="utf-8") as f:
            json.dump(asdict(metadata), f, indent=2)

        checksum_path = pkg_path.with_suffix(".sha256")
        with open(checksum_path, "w", encoding="utf-8") as f:
            f.write(f"{metadata.package_sha256}  {pkg_path.name}\n")

        print(f"Package created: {pkg_path}")
        print(f"  Size: {pkg_path.stat().st_size} bytes")
        print(f"  SHA-256: {metadata.package_sha256}")
        print(f"  Artifacts: {metadata.artifact_count}")

        return pkg_path

    def _check_release_set_consistency(
        self, artifact_manifest: dict[str, Any], release_set: dict[str, Any] | None
    ) -> None:
        """Refuse to package an inconsistent/stale release-set (§7.2)."""
        target = self.staging.install_root / "usr/lib/systemd/system/ccu.target"
        artifacts = artifact_manifest.get("artifacts", []) or []
        unowned = [
            a.get("path", "") for a in artifacts
            if str(a.get("owner_service", "")) in ("", "unknown")
        ]
        if unowned:
            raise PackageError(
                "Refusing to package artifacts with unknown owner: "
                + ", ".join(sorted(unowned))
            )
        if not release_set:
            if target.is_file():
                raise PackageError(
                    "Non-release-set build but staging still contains the "
                    "release-set aggregation target usr/lib/systemd/system/ccu.target"
                )
            return
        units = list(release_set.get("systemd_units") or [])
        status = release_set.get("aggregation_status")
        if units:
            if not target.is_file():
                raise PackageError(
                    "Artifact manifest records release-set units "
                    f"{units} but the aggregation target is missing from staging"
                )
        elif status != "empty":
            raise PackageError(
                f"Artifact manifest release-set aggregation status '{status}' "
                f"records no units; expected 'empty'"
            )
        evidence_paths = {
            "usr/lib/systemd/system/ccu.target" if units else ""
        }
        recorded = {
            str(a.get("path", "")) for a in artifacts
            if str(a.get("owner_service", "")) == "build"
        }
        missing = {
            p for p in evidence_paths
            if p and f"rootfs/{p}" not in recorded
        }
        if missing:
            raise PackageError(
                "BUILD-owned release-set file(s) not recorded in the artifact "
                "manifest: " + ", ".join(sorted(missing))
            )
