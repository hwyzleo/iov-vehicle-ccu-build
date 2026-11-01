"""Unit tests for ELF architecture and pollution checking."""

from __future__ import annotations

from pathlib import Path

import pytest

from ccu_build.elfcheck import (
    parse_elf,
    classify_file,
    check_file,
    check_staging,
    check_archive_members,
    ElfInfo,
    EM_AARCH64,
    EM_X86_64,
    _ELFCLASS64,
    _ELFCLASS32,
)


class TestElfParsing:
    def test_parse_aarch64_elf(self, minimal_aarch64_elf: Path):
        info = parse_elf(minimal_aarch64_elf)
        assert info.is_elf
        assert info.elf_machine == EM_AARCH64
        assert info.elf_class == _ELFCLASS64
        assert info.is_64bit

    def test_parse_x86_64_elf(self, minimal_x86_64_elf: Path):
        info = parse_elf(minimal_x86_64_elf)
        assert info.is_elf
        assert info.elf_machine == EM_X86_64
        assert info.elf_class == _ELFCLASS64

    def test_parse_non_elf(self, tmp_path: Path):
        path = tmp_path / "not_elf"
        path.write_bytes(b"not an elf file")
        info = parse_elf(path)
        assert not info.is_elf


class TestFileClassification:
    def test_classify_elf(self, minimal_aarch64_elf: Path):
        cls = classify_file(minimal_aarch64_elf)
        assert cls.file_type == "elf"
        assert cls.elf_info is not None
        assert cls.elf_info.is_elf

    def test_classify_macho(self, macho_binary: Path):
        cls = classify_file(macho_binary)
        assert cls.file_type == "macho"
        assert cls.macho_description is not None

    def test_classify_ar_archive(self, ar_archive: Path):
        cls = classify_file(ar_archive)
        assert cls.file_type == "ar_archive"

    def test_classify_script(self, script_file: Path):
        cls = classify_file(script_file)
        assert cls.file_type == "script"

    def test_classify_data(self, tmp_path: Path):
        path = tmp_path / "data"
        path.write_bytes(b"\x00\x01\x02\x03")
        cls = classify_file(path)
        assert cls.file_type == "data"


class TestPollutionCheck:
    def test_aarch64_elf_passes(self, minimal_aarch64_elf: Path):
        result = check_file(minimal_aarch64_elf, expected_machine=EM_AARCH64)
        assert len(result.violations) == 0

    def test_x86_64_elf_in_target_staging_fails(self, minimal_x86_64_elf: Path):
        result = check_file(minimal_x86_64_elf, expected_machine=EM_AARCH64)
        assert len(result.violations) > 0
        assert any("x86_64" in v or "machine mismatch" in v for v in result.violations)

    def test_macho_is_violation(self, macho_binary: Path):
        result = check_file(macho_binary, expected_machine=EM_AARCH64)
        assert len(result.violations) > 0
        assert any("Mach-O" in v for v in result.violations)

    def test_script_not_checked(self, script_file: Path):
        result = check_file(script_file, expected_machine=EM_AARCH64)
        assert len(result.violations) == 0

    def test_data_file_not_checked(self, tmp_path: Path):
        path = tmp_path / "data"
        path.write_bytes(b"\x00\x01\x02\x03")
        result = check_file(path, expected_machine=EM_AARCH64)
        assert len(result.violations) == 0


class TestStagingCheck:
    def test_check_empty_staging(self, tmp_path: Path):
        staging = tmp_path / "staging"
        staging.mkdir()
        results = check_staging(staging, expected_machine=EM_AARCH64)
        assert results == []

    def test_check_staging_with_mixed_files(
        self, tmp_path: Path,
        minimal_aarch64_elf: Path,
        macho_binary: Path,
        script_file: Path,
    ):
        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "good").write_bytes(minimal_aarch64_elf.read_bytes())
        (staging / "bad").write_bytes(macho_binary.read_bytes())
        (staging / "script.sh").write_bytes(script_file.read_bytes())

        results = check_staging(staging, expected_machine=EM_AARCH64)
        assert len(results) == 3
        macho_result = next(r for r in results if r.path.name == "bad")
        assert len(macho_result.violations) > 0


class TestArArchive:
    def test_empty_archive(self, ar_archive: Path):
        checked, violations = check_archive_members(ar_archive)
        assert checked == 0
        assert len(violations) == 0
