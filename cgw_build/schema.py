"""Schema validation for CGW Build manifests.

Validates platform, service, release-set and dependency-lock manifests
against JSON Schema definitions stored as YAML in manifests/schemas/.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import jsonschema
import yaml

from .errors import SchemaValidationError

_SCHEMA_CACHE: dict[str, dict[str, Any]] = {}


def _schemas_dir(project_root: Path | None = None) -> Path:
    if project_root is not None:
        return project_root / "manifests" / "schemas"
    return Path(__file__).resolve().parent.parent / "manifests" / "schemas"


def load_schema(name: str, project_root: Path | None = None) -> dict[str, Any]:
    """Load a JSON Schema (stored as YAML) by name.

    Looks for ``manifests/schemas/<name>.schema.yaml``.
    """
    if name in _SCHEMA_CACHE:
        return _SCHEMA_CACHE[name]
    path = _schemas_dir(project_root) / f"{name}.schema.yaml"
    if not path.is_file():
        raise SchemaValidationError(f"Schema file not found: {path}")
    with open(path, encoding="utf-8") as f:
        schema = yaml.safe_load(f)
    _SCHEMA_CACHE[name] = schema
    return schema


def _format_errors(errors: list[jsonschema.ValidationError]) -> list[str]:
    return [f"{'.'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message}" for e in errors]


def _validate(data: dict[str, Any], schema_name: str, label: str,
              project_root: Path | None = None) -> None:
    schema = load_schema(schema_name, project_root)
    validator = jsonschema.Draft7Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
    if errors:
        raise SchemaValidationError(
            f"{label} schema validation failed ({len(errors)} error(s))",
            _format_errors(errors),
        )


def validate_platform_manifest(data: dict[str, Any], project_root: Path | None = None) -> None:
    """Validate a platform manifest dict against the platform schema."""
    _validate(data, "platform", "Platform manifest", project_root)


def validate_service_manifest(data: dict[str, Any], project_root: Path | None = None) -> None:
    """Validate a service manifest dict against the service schema."""
    _validate(data, "service", "Service manifest", project_root)


def validate_release_set_manifest(data: dict[str, Any], project_root: Path | None = None) -> None:
    """Validate a release-set manifest dict against the release-set schema."""
    _validate(data, "release-set", "Release-set manifest", project_root)


def validate_dependency_lock(data: dict[str, Any], project_root: Path | None = None) -> None:
    """Validate a dependency lock dict against the lock schema."""
    _validate(data, "lock", "Dependency lock", project_root)
