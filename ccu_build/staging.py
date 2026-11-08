"""Staging directory management for CCU Build.

Manages the ``out/<platform>/<profile>/`` output tree (设计 §6):

    out/<platform>/<profile>/
    ├── build/<project>/        per-service CMake build dirs
    ├── deps/usr/               TARGET dependency staging
    ├── sdk/<project>/usr/      per-service SDK staging
    ├── install-root/           merged DESTDIR install tree (rootfs)
    ├── manifests/              artifact manifest, build report
    ├── logs/                   per-service step logs
    └── packages/               release packages

Provides file ownership tracking and path-conflict detection.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

# Payload paths that MUST never enter a release staging tree or package
# (CCU-BUILD-REQ-CR-005 / RELEASE-REQ-001): development fixtures, persistent
# instance state (VIN / ECU inventory) and any /var/lib/ccu/** content.
FORBIDDEN_PAYLOAD_NAMES = frozenset({"vin.dat", "ecu_inventory.json"})


def find_forbidden_payload(root: Path) -> list[str]:
    """Return install-root-relative paths that must not be released.

    Rules (CR-005 §2):
      * any ``config/dev/**`` segment sequence (development fixtures);
      * any ``var/lib/ccu/**`` path (persistent / instance state);
      * instance-state file names (``vin.dat``, ``ecu_inventory.json``)
        wherever they appear.
    """
    hits: list[str] = []
    if not root.exists():
        return hits
    for path in sorted(root.rglob("*")):
        if not path.is_file() and not path.is_symlink():
            continue
        rel = str(path.relative_to(root)).replace(os.sep, "/")
        parts = rel.split("/")
        dev_fixture = any(
            parts[i] == "config" and parts[i + 1] == "dev"
            for i in range(len(parts) - 1)
        )
        if (
            dev_fixture
            or "/var/lib/ccu/" in f"/{rel}"
            or path.name in FORBIDDEN_PAYLOAD_NAMES
        ):
            hits.append(rel)
    return hits


def format_mode(path: Path, *, follow_symlinks: bool = False) -> str:
    """Return a file mode as ``0o<perms>`` (e.g. ``0o644``).

    Symlinks are inspected with ``lstat`` by default: the recorded mode is the
    link's own permission bits, never the target's (设计 §9/§9.6).
    """
    st = path.stat() if follow_symlinks else path.lstat()
    return oct(stat.S_IMODE(st.st_mode))


@dataclass
class StagedFile:
    """A file in the staging install-root."""

    rel_path: str  # relative to install-root or sdk root
    full_path: Path
    size: int
    sha256: str
    owner_service: str | None = None
    # Final permission bits recorded in the Artifact Manifest (设计 §9,
    # CCU-BUILD-DSN-CR-005 §3). Empty only for legacy callers that never
    # resolved a file on disk.
    mode: str = ""


def sha256_file(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class StagingDir:
    """Manages the staging output directory tree."""

    def __init__(self, project_root: Path, platform: str = "ccu-dev", profile: str = "release"):
        self.project_root = Path(project_root)
        self.platform = platform
        self.profile = profile
        self.root = self.project_root / "out" / platform / profile
        self.install_root = self.root / "install-root"
        self.sdk_root = self.root / "sdk"
        self.deps_root = self.root / "deps"
        self.build_dir = self.root / "build"
        self.manifests_dir = self.root / "manifests"
        self.logs_dir = self.root / "logs"
        self.packages_dir = self.root / "packages"

    def prepare(self, clean: bool = False) -> None:
        """Create the staging directory structure."""
        if clean and self.root.exists():
            shutil.rmtree(self.root)
        for d in (
            self.root,
            self.install_root,
            self.sdk_root,
            self.deps_root,
            self.build_dir,
            self.manifests_dir,
            self.logs_dir,
            self.packages_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    @property
    def dep_staging(self) -> Path:
        """TARGET dependency staging prefix (CCU_DEP_STAGING)."""
        return self.deps_root

    def sdk_dir(self, service_id: str) -> Path:
        """SDK staging root for a service (staging: sdk components)."""
        return self.sdk_root / service_id

    def component_destdir(self, service_id: str, staging: str) -> Path:
        """Compute the DESTDIR for a component based on its staging class."""
        if staging == "sdk":
            return self.sdk_dir(service_id)
        if staging == "rootfs":
            return self.install_root
        raise ValueError(f"Unknown staging class: {staging!r}")

    def dep_staging_usr(self) -> Path:
        """The ``usr`` prefix under the dependency staging root."""
        return self.deps_root / "usr"

    def service_build_dir(self, service_id: str) -> Path:
        return self.build_dir / service_id

    def service_log_file(self, service_id: str, step: str) -> Path:
        return self.logs_dir / f"{service_id}-{step}.log"

    def snapshot_paths(self) -> set[str]:
        """Return a set of relative paths currently in install-root."""
        result: set[str] = set()
        if not self.install_root.exists():
            return result
        for path in self.install_root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                result.add(str(path.relative_to(self.install_root)))
            elif path.is_symlink():
                result.add(str(path.relative_to(self.install_root)))
        return result

    def scan_files(self) -> list[StagedFile]:
        """Scan install-root and sdk root, return all files with metadata."""
        files: list[StagedFile] = []
        roots = [self.install_root, self.sdk_root]
        for root in roots:
            if not root.exists():
                continue
            for path in sorted(root.rglob("*")):
                if not path.is_file() and not path.is_symlink():
                    continue
                rel = str(path.relative_to(root))
                staging_prefix = "sdk/" if root == self.sdk_root else "rootfs/"
                rel_key = staging_prefix + rel
                if path.is_symlink():
                    files.append(StagedFile(
                        rel_path=rel_key, full_path=path, size=0, sha256="",
                        mode=format_mode(path),
                    ))
                else:
                    stat_result = path.stat()
                    files.append(StagedFile(
                        rel_path=rel_key,
                        full_path=path,
                        size=stat_result.st_size,
                        sha256=sha256_file(path),
                        mode=format_mode(path, follow_symlinks=True),
                    ))
        return files

    def new_files_after_install(
        self, before: set[str], service_id: str
    ) -> set[str]:
        """Return paths that appeared after install, attributed to *service_id*."""
        after = self.snapshot_paths()
        return after - before
