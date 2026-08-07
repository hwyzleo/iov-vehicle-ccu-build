"""Unit tests for staging directory management."""

from __future__ import annotations

from pathlib import Path

import pytest

from cgw_build.staging import StagingDir, sha256_file, StagedFile


class TestStagingDir:
    def test_staging_paths(self, tmp_path: Path):
        staging = StagingDir(tmp_path, platform="cgw-dev", profile="release")
        assert staging.root == tmp_path / "out" / "cgw-dev" / "release"
        assert staging.install_root == staging.root / "install-root"
        assert staging.sdk_root == staging.root / "sdk"
        assert staging.deps_root == staging.root / "deps"
        assert staging.build_dir == staging.root / "build"
        assert staging.manifests_dir == staging.root / "manifests"
        assert staging.logs_dir == staging.root / "logs"
        assert staging.packages_dir == staging.root / "packages"

    def test_prepare_creates_dirs(self, tmp_path: Path):
        staging = StagingDir(tmp_path, platform="cgw-dev", profile="release")
        staging.prepare()
        assert staging.root.is_dir()
        assert staging.install_root.is_dir()
        assert staging.sdk_root.is_dir()
        assert staging.deps_root.is_dir()
        assert staging.build_dir.is_dir()
        assert staging.manifests_dir.is_dir()
        assert staging.logs_dir.is_dir()
        assert staging.packages_dir.is_dir()

    def test_prepare_clean(self, tmp_path: Path):
        staging = StagingDir(tmp_path, platform="cgw-dev", profile="release")
        staging.prepare()
        (staging.install_root / "test.txt").write_text("test")
        assert (staging.install_root / "test.txt").exists()
        staging.prepare(clean=True)
        assert not (staging.install_root / "test.txt").exists()

    def test_component_destdir_rootfs(self, tmp_path: Path):
        staging = StagingDir(tmp_path, platform="cgw-dev", profile="release")
        destdir = staging.component_destdir("svc-a", "rootfs")
        assert destdir == staging.install_root

    def test_component_destdir_sdk(self, tmp_path: Path):
        staging = StagingDir(tmp_path, platform="cgw-dev", profile="release")
        destdir = staging.component_destdir("svc-a", "sdk")
        assert destdir == staging.sdk_root / "svc-a"

    def test_component_destdir_invalid(self, tmp_path: Path):
        staging = StagingDir(tmp_path, platform="cgw-dev", profile="release")
        with pytest.raises(ValueError):
            staging.component_destdir("svc-a", "invalid")

    def test_dep_staging_usr(self, tmp_path: Path):
        staging = StagingDir(tmp_path, platform="cgw-dev", profile="release")
        assert staging.dep_staging_usr() == staging.deps_root / "usr"

    def test_service_build_dir(self, tmp_path: Path):
        staging = StagingDir(tmp_path, platform="cgw-dev", profile="release")
        assert staging.service_build_dir("svc-a") == staging.build_dir / "svc-a"

    def test_service_log_file(self, tmp_path: Path):
        staging = StagingDir(tmp_path, platform="cgw-dev", profile="release")
        assert staging.service_log_file("svc-a", "configure") == staging.logs_dir / "svc-a-configure.log"


class TestSha256:
    def test_sha256_file(self, tmp_path: Path):
        path = tmp_path / "test.txt"
        path.write_text("hello")
        digest = sha256_file(path)
        assert len(digest) == 64
        assert digest == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


class TestScanFiles:
    def test_scan_empty(self, tmp_path: Path):
        staging = StagingDir(tmp_path, platform="cgw-dev", profile="release")
        staging.prepare()
        files = staging.scan_files()
        assert files == []

    def test_scan_with_files(self, tmp_path: Path):
        staging = StagingDir(tmp_path, platform="cgw-dev", profile="release")
        staging.prepare()
        (staging.install_root / "usr").mkdir(parents=True)
        (staging.install_root / "usr" / "bin").mkdir()
        (staging.install_root / "usr" / "bin" / "test").write_text("test")
        files = staging.scan_files()
        assert len(files) == 1
        assert files[0].rel_path == "rootfs/usr/bin/test"
        assert files[0].size == 4
        assert len(files[0].sha256) == 64

    def test_scan_sdk_files(self, tmp_path: Path):
        staging = StagingDir(tmp_path, platform="cgw-dev", profile="release")
        staging.prepare()
        sdk = staging.sdk_dir("lib-a")
        (sdk / "usr").mkdir(parents=True)
        (sdk / "usr" / "include").mkdir()
        (sdk / "usr" / "include" / "test.h").write_text("header")
        files = staging.scan_files()
        assert len(files) == 1
        assert files[0].rel_path == "sdk/lib-a/usr/include/test.h"


class TestSnapshotPaths:
    def test_snapshot_empty(self, tmp_path: Path):
        staging = StagingDir(tmp_path, platform="cgw-dev", profile="release")
        staging.prepare()
        assert staging.snapshot_paths() == set()

    def test_snapshot_with_files(self, tmp_path: Path):
        staging = StagingDir(tmp_path, platform="cgw-dev", profile="release")
        staging.prepare()
        (staging.install_root / "usr").mkdir(parents=True)
        (staging.install_root / "usr" / "bin").mkdir()
        (staging.install_root / "usr" / "bin" / "test").write_text("test")
        paths = staging.snapshot_paths()
        assert "usr/bin/test" in paths
