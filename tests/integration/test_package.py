"""Integration tests for packaging and verification."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from cgw_build.manifest import Project
from cgw_build.orchestrator import BuildOrchestrator, BuildConfig
from cgw_build.packaging import Packager
from cgw_build.staging import StagingDir, sha256_file
from cgw_build.verify import Verifier
from cgw_build.artifact import get_git_commit


class TestPackageCreation:
    """Test package creation from staging."""

    def _prepare_staging_with_artifacts(self, project_root: Path, tmp_path: Path) -> StagingDir:
        """Create a staging dir with some files and an artifact manifest."""
        staging = StagingDir(project_root, platform="cgw-dev", profile="release")
        staging.prepare(clean=True)

        # Create some install-root files
        bin_dir = staging.install_root / "usr" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "cgw-hello-cli").write_text("dummy binary")
        (bin_dir / "cgw-hello-cli").chmod(0o755)

        lib_dir = staging.install_root / "usr" / "lib"
        lib_dir.mkdir(parents=True)
        (lib_dir / "libcgw-hello.so").write_text("dummy lib")

        # Create a minimal artifact manifest
        project = Project(project_root, platform="cgw-dev")
        from cgw_build.manifest import PlatformManifest, SysrootManifest, DependencyLock
        from cgw_build.artifact import ArtifactManifest

        pm = project.load_platform_manifest()
        sm = project.load_sysroot_manifest()
        lock = project.load_dependency_lock()

        manifest = ArtifactManifest(
            platform="cgw-dev", profile="release",
            platform_manifest=pm, sysroot_manifest=sm, dependency_lock=lock,
        )

        from cgw_build.staging import StagedFile
        for f in staging.scan_files():
            manifest.add_staged_file(
                f, owner_service="cgw-hello-cli", owner_target="cgw-hello-cli",
                version="0.1.0", git_commit="abc123",
                install_component="cgw-hello-cli-runtime", staging="rootfs",
            )

        manifest_path = staging.manifests_dir / "artifact-manifest.json"
        manifest.save(manifest_path)
        return staging

    def test_create_package(self, project_root: Path):
        staging = self._prepare_staging_with_artifacts(project_root, Path("/tmp"))
        packager = Packager(staging)
        git_commit = get_git_commit(project_root)

        pkg_path = packager.create_package(
            name="cgw-dev",
            version="0.1.0-draft",
            git_commit=git_commit,
        )

        assert pkg_path.exists()
        assert pkg_path.suffix == ".gz"

        # Checksum file
        checksum_file = pkg_path.with_suffix(".sha256")
        assert checksum_file.exists()

        # Package metadata
        meta_path = staging.manifests_dir / "package.json"
        assert meta_path.exists()
        with open(meta_path) as f:
            meta = json.load(f)
        assert meta["package_name"] == "cgw-dev"
        assert meta["version"] == "0.1.0-draft"
        assert meta["platform"] == "cgw-dev"

    def test_package_checksum_matches(self, project_root: Path):
        staging = self._prepare_staging_with_artifacts(project_root, Path("/tmp"))
        packager = Packager(staging)
        git_commit = get_git_commit(project_root)

        pkg_path = packager.create_package(
            name="cgw-dev", version="0.1.0-draft", git_commit=git_commit,
        )

        checksum_file = pkg_path.with_suffix(".sha256")
        expected = checksum_file.read_text().split()[0]
        actual = sha256_file(pkg_path)
        assert expected == actual

    def test_package_structure(self, project_root: Path):
        staging = self._prepare_staging_with_artifacts(project_root, Path("/tmp"))
        packager = Packager(staging)
        git_commit = get_git_commit(project_root)

        pkg_path = packager.create_package(
            name="cgw-dev", version="0.1.0-draft", git_commit=git_commit,
        )

        with tarfile.open(pkg_path, "r:gz") as tar:
            members = tar.getnames()
            assert any("install-root" in m for m in members)
            assert any("artifact-manifest.json" in m for m in members)
            assert any("package.json" in m for m in members)


class TestPackageVerification:
    """Test package verification."""

    def test_verify_valid_package(self, project_root: Path):
        staging = StagingDir(project_root, platform="cgw-dev", profile="release")
        staging.prepare(clean=True)

        # Create minimal staging
        bin_dir = staging.install_root / "usr" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "test").write_text("test")

        # Create artifact manifest
        project = Project(project_root, platform="cgw-dev")
        from cgw_build.manifest import PlatformManifest, SysrootManifest
        from cgw_build.artifact import ArtifactManifest

        pm = project.load_platform_manifest()
        sm = project.load_sysroot_manifest()
        manifest = ArtifactManifest(
            platform="cgw-dev", profile="release",
            platform_manifest=pm, sysroot_manifest=sm,
        )
        manifest_path = staging.manifests_dir / "artifact-manifest.json"
        manifest.save(manifest_path)

        # Create package
        packager = Packager(staging)
        pkg_path = packager.create_package(
            name="cgw-dev", version="0.1.0-draft",
            git_commit=get_git_commit(project_root),
        )

        # Verify
        verifier = Verifier(staging)
        result = verifier.verify_package(pkg_path)
        assert result.status == "success"

    def test_verify_nonexistent_package(self, project_root: Path):
        staging = StagingDir(project_root, platform="cgw-dev", profile="release")
        staging.prepare(clean=True)
        verifier = Verifier(staging)
        result = verifier.verify_package(Path("/nonexistent/package.tar.gz"))
        assert result.status == "failed"

    def test_verify_staging_no_install_root(self, project_root: Path):
        staging = StagingDir(project_root, platform="cgw-dev", profile="release")
        # Don't prepare - install-root doesn't exist
        verifier = Verifier(staging)
        result = verifier.verify_staging()
        assert result.status == "failed"
