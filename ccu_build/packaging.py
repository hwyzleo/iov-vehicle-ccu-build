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
from .staging import StagingDir, sha256_file


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

        if artifact_manifest_path is None:
            artifact_manifest_path = self.staging.manifests_dir / "artifact-manifest.json"

        if not artifact_manifest_path.exists():
            raise PackageError(
                f"Artifact manifest not found: {artifact_manifest_path}"
            )

        with open(artifact_manifest_path, encoding="utf-8") as f:
            artifact_manifest = json.load(f)

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
