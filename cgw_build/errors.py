"""Custom exception classes for CGW Build."""

from __future__ import annotations


class CgwBuildError(Exception):
    """Base class for all CGW Build errors."""


class ManifestError(CgwBuildError):
    """Manifest loading or parsing error."""


class SchemaValidationError(CgwBuildError):
    """Schema validation failed."""

    def __init__(self, message: str, errors: list | None = None):
        super().__init__(message)
        self.errors = errors or []


class DependencyError(CgwBuildError):
    """Dependency graph error (missing dependency)."""


class CycleError(CgwBuildError):
    """Dependency cycle detected."""

    def __init__(self, message: str, cycles: list | None = None):
        super().__init__(message)
        self.cycles = cycles or []


class ValidationFailure(CgwBuildError):
    """Manifest validation failure (unique ID, systemd refs, health/smoke)."""

    def __init__(self, message: str, details: list | None = None):
        super().__init__(message)
        self.details = details or []


class PathConflictError(CgwBuildError):
    """Installation path conflict between services."""

    def __init__(self, message: str, conflicts: list | None = None):
        super().__init__(message)
        self.conflicts = conflicts or []


class ElfCheckError(CgwBuildError):
    """ELF architecture or host pollution check failure."""

    def __init__(self, message: str, violations: list | None = None):
        super().__init__(message)
        self.violations = violations or []


class BuildFailure(CgwBuildError):
    """Build step failure."""


class PackageError(CgwBuildError):
    """Packaging error."""
