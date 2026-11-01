"""Shared pytest fixtures for CCU Build tests."""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

# Ensure ccu_build package is importable
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def project_root() -> Path:
    """Path to the ccu-build project root."""
    return PROJECT_ROOT


@pytest.fixture
def project(project_root: Path):
    """A Project instance for ccu-dev platform."""
    from ccu_build.manifest import Project
    return Project(project_root, platform="ccu-dev")


# --- ELF test fixtures ---------------------------------------------------


@pytest.fixture
def minimal_aarch64_elf(tmp_path: Path) -> Path:
    """Create a minimal 64-bit aarch64 ELF file (header only)."""
    path = tmp_path / "test_aarch64"
    e_ident = b"\x7fELF"
    e_ident += bytes([2])  # ELFCLASS64
    e_ident += bytes([1])  # ELFDATA2LSB
    e_ident += bytes([1])  # EV_CURRENT
    e_ident += bytes([0])  # ELFOSABI_NONE
    e_ident += b"\x00" * 8  # padding

    header = e_ident
    header += struct.pack("<H", 2)     # e_type = ET_EXEC
    header += struct.pack("<H", 183)   # e_machine = EM_AARCH64
    header += struct.pack("<I", 1)     # e_version
    header += struct.pack("<Q", 0)     # e_entry
    header += struct.pack("<Q", 0)     # e_phoff
    header += struct.pack("<Q", 0)     # e_shoff
    header += struct.pack("<I", 0)     # e_flags
    header += struct.pack("<H", 64)    # e_ehsize
    header += struct.pack("<H", 0)     # e_phentsize
    header += struct.pack("<H", 0)     # e_phnum
    header += struct.pack("<H", 0)     # e_shentsize
    header += struct.pack("<H", 0)     # e_shnum
    header += struct.pack("<H", 0)     # e_shstrndx

    path.write_bytes(header)
    return path


@pytest.fixture
def minimal_x86_64_elf(tmp_path: Path) -> Path:
    """Create a minimal 64-bit x86_64 ELF file (header only)."""
    path = tmp_path / "test_x86_64"
    e_ident = b"\x7fELF"
    e_ident += bytes([2])  # ELFCLASS64
    e_ident += bytes([1])  # ELFDATA2LSB
    e_ident += bytes([1])  # EV_CURRENT
    e_ident += bytes([0])  # ELFOSABI_NONE
    e_ident += b"\x00" * 8

    header = e_ident
    header += struct.pack("<H", 2)    # e_type = ET_EXEC
    header += struct.pack("<H", 62)   # e_machine = EM_X86_64
    header += struct.pack("<I", 1)    # e_version
    header += struct.pack("<Q", 0)    # e_entry
    header += struct.pack("<Q", 0)    # e_phoff
    header += struct.pack("<Q", 0)    # e_shoff
    header += struct.pack("<I", 0)    # e_flags
    header += struct.pack("<H", 64)   # e_ehsize
    header += struct.pack("<H", 0)
    header += struct.pack("<H", 0)
    header += struct.pack("<H", 0)
    header += struct.pack("<H", 0)
    header += struct.pack("<H", 0)

    path.write_bytes(header)
    return path


@pytest.fixture
def macho_binary(tmp_path: Path) -> Path:
    """Create a minimal Mach-O file (magic only)."""
    path = tmp_path / "test_macho"
    path.write_bytes(b"\xcf\xfa\xed\xfe" + b"\x00" * 28)
    return path


@pytest.fixture
def ar_archive(tmp_path: Path) -> Path:
    """Create a minimal ar archive (static library)."""
    path = tmp_path / "test.a"
    path.write_bytes(b"!<arch>\n")
    return path


@pytest.fixture
def script_file(tmp_path: Path) -> Path:
    """Create a script file with shebang."""
    path = tmp_path / "test.sh"
    path.write_text("#!/bin/bash\necho hello\n")
    return path
