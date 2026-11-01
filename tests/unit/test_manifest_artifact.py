"""Unit tests for artifact manifest generation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ccu_build.artifact import ArtifactManifest, ArtifactEntry, get_git_commit
from ccu_build.errors import PathConflictError
from ccu_build.manifest import PlatformManifest, SysrootManifest, DependencyLock
from ccu_build.staging import StagedFile


def _make_manifest(platform: str = "ccu-dev") -> ArtifactManifest:
    pm = PlatformManifest(data={
        "platform": platform,
        "version": "0.1.0-draft",
        "architecture": "TBD",
        "sysroot": {"id": "ccu-dev-sysroot"},
        "toolchain": {"target_triple": "TBD"},
    })
    sm = SysrootManifest(data={
        "sysroot": {"id": "ccu-dev-sysroot", "digest": "TBD", "import_status": "pending"},
    })
    return ArtifactManifest(
        platform=platform,
        profile="release",
        platform_manifest=pm,
        sysroot_manifest=sm,
        dependency_lock=DependencyLock(dependencies={}),
    )


class TestArtifactManifest:
    def test_add_entry(self):
        manifest = _make_manifest()
        entry = ArtifactEntry(
            path="rootfs/usr/bin/test",
            owner_service="svc-a",
            owner_target="test",
            version="0.1.0",
            git_commit="abc123",
            profile="release",
            sha256="deadbeef",
            size=100,
            file_type="elf",
        )
        manifest.add_entry(entry)
        assert len(manifest.entries) == 1

    def test_to_dict(self):
        manifest = _make_manifest()
        entry = ArtifactEntry(
            path="rootfs/usr/bin/test",
            owner_service="svc-a",
            owner_target="test",
            version="0.1.0",
            git_commit="abc123",
            profile="release",
            sha256="deadbeef",
            size=100,
            file_type="data",
        )
        manifest.add_entry(entry)
        d = manifest.to_dict()
        assert d["platform"] == "ccu-dev"
        assert d["profile"] == "release"
        assert d["artifact_count"] == 1
        assert len(d["artifacts"]) == 1
        assert d["artifacts"][0]["path"] == "rootfs/usr/bin/test"

    def test_save_and_load(self, tmp_path: Path):
        manifest = _make_manifest()
        entry = ArtifactEntry(
            path="rootfs/usr/bin/test",
            owner_service="svc-a",
            owner_target="test",
            version="0.1.0",
            git_commit="abc123",
            profile="release",
            sha256="deadbeef",
            size=100,
            file_type="data",
        )
        manifest.add_entry(entry)
        path = tmp_path / "manifest.json"
        manifest.save(path)
        with open(path) as f:
            data = json.load(f)
        assert data["artifact_count"] == 1

    def test_check_conflicts_passes(self):
        manifest = _make_manifest()
        manifest.add_entry(ArtifactEntry(
            path="rootfs/a", owner_service="svc-a", owner_target="t",
            version="0.1", git_commit="c", profile="r",
            sha256="s", size=1, file_type="data",
        ))
        manifest.add_entry(ArtifactEntry(
            path="rootfs/b", owner_service="svc-b", owner_target="t",
            version="0.1", git_commit="c", profile="r",
            sha256="s", size=1, file_type="data",
        ))
        manifest.check_conflicts()  # should not raise

    def test_check_conflicts_fails(self):
        manifest = _make_manifest()
        manifest.add_entry(ArtifactEntry(
            path="rootfs/same", owner_service="svc-a", owner_target="t",
            version="0.1", git_commit="c", profile="r",
            sha256="s", size=1, file_type="data",
        ))
        manifest.add_entry(ArtifactEntry(
            path="rootfs/same", owner_service="svc-b", owner_target="t",
            version="0.1", git_commit="c", profile="r",
            sha256="s", size=1, file_type="data",
        ))
        with pytest.raises(PathConflictError):
            manifest.check_conflicts()

    def test_add_staged_file(self, tmp_path: Path):
        manifest = _make_manifest()
        # Create a simple data file
        file_path = tmp_path / "test.txt"
        file_path.write_text("hello")
        staged = StagedFile(
            rel_path="rootfs/test.txt",
            full_path=file_path,
            size=5,
            sha256="2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
        )
        manifest.add_staged_file(
            staged, owner_service="svc-a", owner_target="test",
            version="0.1.0", git_commit="abc",
        )
        assert len(manifest.entries) == 1
        assert manifest.entries[0].file_type == "data"

    def test_platform_manifest_summary(self):
        manifest = _make_manifest()
        d = manifest.to_dict()
        assert d["platform_manifest"]["platform"] == "ccu-dev"
        assert d["platform_manifest"]["version"] == "0.1.0-draft"
        assert d["sysroot"]["id"] == "ccu-dev-sysroot"
        assert d["sysroot"]["import_status"] == "pending"

    def test_sdk_consumers_evidence(self):
        """CCU-BUILD-DSN-CR-002 §8.6: consumer evidence in artifact manifest."""
        manifest = _make_manifest()
        manifest.sdk_consumers = {"framework": ["framework-consumer"]}
        d = manifest.to_dict()
        assert d["sdk_consumers"] == {"framework": ["framework-consumer"]}
        # Empty by default
        assert _make_manifest().to_dict()["sdk_consumers"] == {}


class TestGitCommit:
    def test_get_git_commit(self, project_root: Path):
        commit = get_git_commit(project_root)
        # Should be a hex string or "unknown"
        assert isinstance(commit, str)
        assert len(commit) > 0
