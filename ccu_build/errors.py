"""Custom exception classes for CCU Build."""

from __future__ import annotations


class CcuBuildError(Exception):
    """Base class for all CCU Build errors."""


class ManifestError(CcuBuildError):
    """Manifest loading or parsing error."""


class SchemaValidationError(CcuBuildError):
    """Schema validation failed."""

    def __init__(self, message: str, errors: list | None = None):
        super().__init__(message)
        self.errors = errors or []


class DependencyError(CcuBuildError):
    """Dependency graph error (missing dependency)."""


class CycleError(CcuBuildError):
    """Dependency cycle detected."""

    def __init__(self, message: str, cycles: list | None = None):
        super().__init__(message)
        self.cycles = cycles or []


class ValidationFailure(CcuBuildError):
    """Manifest validation failure (unique ID, systemd refs, health/smoke)."""

    def __init__(self, message: str, details: list | None = None):
        super().__init__(message)
        self.details = details or []


class PathConflictError(CcuBuildError):
    """Installation path conflict between services."""

    def __init__(self, message: str, conflicts: list | None = None):
        super().__init__(message)
        self.conflicts = conflicts or []


class ElfCheckError(CcuBuildError):
    """ELF architecture or host pollution check failure."""

    def __init__(self, message: str, violations: list | None = None):
        super().__init__(message)
        self.violations = violations or []


class BuildFailure(CcuBuildError):
    """Build step failure."""


class PackageError(CcuBuildError):
    """Packaging error."""


class ConfigError(CcuBuildError):
    """Configuration release error (overlay, validation, ownership)."""

    def __init__(self, message: str, details: list | None = None):
        super().__init__(message)
        self.details = details or []


class OverlayError(CcuBuildError):
    """Platform config overlay error (unauthorized path, stale file)."""

    def __init__(self, message: str, details: list | None = None):
        super().__init__(message)
        self.details = details or []


class SecretScanError(CcuBuildError):
    """Secret material detected in build output."""

    def __init__(self, message: str, findings: list | None = None):
        super().__init__(message)
        self.findings = findings or []


class ConfigValidationError(CcuBuildError):
    """Configuration schema/checker validation failure."""

    def __init__(self, message: str, errors: list | None = None):
        super().__init__(message)
        self.errors = errors or []
