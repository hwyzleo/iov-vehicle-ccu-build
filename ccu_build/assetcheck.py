"""Platform runtime asset validators (CCU-BUILD-DSN-CR-004 §5).

Validates the BUILD-owned platform ``vsomeip`` assets declared through
``runtime.platform_assets[].validation``:

  * ``vsomeip-dropin`` - the service drop-in may only carry the two platform
    environment directives (``VSOMEIP_CONFIGURATION``,
    ``VSOMEIP_APPLICATION_NAME``). Any other section/directive
    (``ExecStart=``, ``PrivateTmp=``, SOME/IP IDs, business timeouts,
    diagnostic/security policy) is rejected.
  * ``vsomeip-json`` - the shared platform configuration must parse, declare
    exactly-typed, uniquely named applications, register every application
    required by the selected closure, and must not carry secret material.

Reports only the rule and the field path (never the value), matching the
BUILD secret-handling contract (CCU-BUILD-CONFIG-REQ-007).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import ConfigError
from .secretscan import SecretScanner

# The single authorized configuration path (CCU-BUILD-DSN-CR-004 §5.1).
VSOMEIP_CONFIGURATION_PATH = "/etc/ccu/someip/vsomeip.json"

# Application names are lowercase dash-separated ids (e.g. ccu-sec).
_APPLICATION_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")

# Directives authorized inside a platform vsomeip drop-in.
_DROPIN_AUTHORIZED_ENV = frozenset({"VSOMEIP_CONFIGURATION", "VSOMEIP_APPLICATION_NAME"})

# Field names that must never appear in the shared non-secret JSON.
_FORBIDDEN_FIELD_RE = re.compile(
    r"(private[_-]?key|secret|passw(or)?d|token|credential|certificate|"
    r"device[_-]?(id|identity|serial)|nonce|kek|seed[_-]?key)",
    re.IGNORECASE,
)


@dataclass
class AssetValidationResult:
    """Result of validating a single platform runtime asset."""

    validation: str
    status: str = "pass"  # pass | fail
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# systemd drop-in parsing
# ---------------------------------------------------------------------------


def parse_unit_file(path: Path) -> tuple[list[tuple[str, list[tuple[str, str]]]], list[str]]:
    """Parse an INI-style systemd unit file.

    Returns ``(sections, errors)`` where ``sections`` is an ordered list of
    ``(section_name, [(key, raw_value)])``. Comments (``#``/``;``), blank
    lines and line continuations are handled. Values keep their raw text.
    """
    sections: list[tuple[str, list[tuple[str, str]]]] = []
    errors: list[str] = []
    current: list[tuple[str, str]] | None = None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [], [f"cannot read {path}: {exc}"]

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            if not name:
                errors.append(f"line {lineno}: empty section name")
                current = None
                continue
            current = []
            sections.append((name, current))
            continue
        if "=" not in line:
            errors.append(f"line {lineno}: not a key=value directive")
            continue
        if current is None:
            errors.append(f"line {lineno}: directive outside any section")
            continue
        key, value = line.split("=", 1)
        current.append((key.strip(), value.strip()))
    return sections, errors


# ---------------------------------------------------------------------------
# vsomeip-dropin
# ---------------------------------------------------------------------------


def validate_vsomeip_dropin(
    path: Path,
    expected_application: str,
    expected_configuration: str = VSOMEIP_CONFIGURATION_PATH,
) -> AssetValidationResult:
    """Validate a BUILD-owned vsomeip platform drop-in (§5.1)."""
    result = AssetValidationResult(validation="vsomeip-dropin")
    sections, parse_errors = parse_unit_file(path)
    result.errors.extend(parse_errors)

    if not sections:
        result.errors.append("drop-in declares no [Service] section")
        result.status = "fail"
        return result

    if len(sections) > 1:
        result.errors.append(
            "drop-in must declare exactly one [Service] section (found: "
            + ", ".join(f"[{name}]" for name, _ in sections)
            + ")"
        )

    service_keys: list[tuple[str, str]] = []
    for name, directives in sections:
        if name != "Service":
            result.errors.append(f"unauthorized section [{name}]")
            continue
        for key, value in directives:
            if key != "Environment":
                result.errors.append(
                    f"unauthorized directive '{key}=' in [Service] "
                    f"(only Environment= is authorized)"
                )
                continue
            service_keys.append((key, value))

    parsed_env: dict[str, str] = {}
    duplicates: list[str] = []
    for _key, value in service_keys:
        if "=" not in value:
            result.errors.append(
                "Environment= entry is not in KEY=VALUE form (field path "
                "Environment)"
            )
            continue
        env_key, env_value = value.split("=", 1)
        env_key = env_key.strip()
        if env_key not in _DROPIN_AUTHORIZED_ENV:
            result.errors.append(
                f"unauthorized environment variable '{env_key}' "
                f"(Environment.{env_key})"
            )
            continue
        if env_key in parsed_env:
            duplicates.append(env_key)
            continue
        parsed_env[env_key] = env_value.strip()

    for env_key in sorted(set(duplicates)):
        result.errors.append(f"duplicate environment variable 'Environment.{env_key}'")

    if parsed_env.get("VSOMEIP_CONFIGURATION") != expected_configuration:
        if "VSOMEIP_CONFIGURATION" not in parsed_env:
            result.errors.append("missing Environment.VSOMEIP_CONFIGURATION")
        else:
            result.errors.append(
                f"Environment.VSOMEIP_CONFIGURATION must be "
                f"{expected_configuration}"
            )

    application = parsed_env.get("VSOMEIP_APPLICATION_NAME")
    if application is None:
        result.errors.append("missing Environment.VSOMEIP_APPLICATION_NAME")
    elif application != expected_application:
        result.errors.append(
            f"Environment.VSOMEIP_APPLICATION_NAME must match the service "
            f"application '{expected_application}'"
        )
    else:
        result.details["application"] = application

    result.details["configuration"] = parsed_env.get("VSOMEIP_CONFIGURATION")
    result.status = "fail" if result.errors else "pass"
    return result


# ---------------------------------------------------------------------------
# vsomeip-json
# ---------------------------------------------------------------------------


def _check_unknown_secret_fields(node: Any, prefix: str, errors: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if _FORBIDDEN_FIELD_RE.search(str(key)):
                errors.append(f"forbidden secret-bearing field '{path}'")
                continue
            _check_unknown_secret_fields(value, path, errors)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _check_unknown_secret_fields(value, f"{prefix}[{index}]", errors)


def validate_vsomeip_json(
    path: Path,
    required_applications: "list[str] | set[str]" = (),
    scanner: SecretScanner | None = None,
) -> AssetValidationResult:
    """Validate the shared platform vsomeip JSON (§5.2).

    Structural validation only: real client ids / routing / network values
    come from the controlled device baseline (CCU-BUILD-DSN-CR-004 §11) and
    are never invented here.
    """
    result = AssetValidationResult(validation="vsomeip-json")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        result.errors.append(f"cannot read {path}: {exc}")
        result.status = "fail"
        return result

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        result.errors.append(f"JSON parse error at line {exc.lineno} column {exc.colno}")
        result.status = "fail"
        return result

    if not isinstance(data, dict):
        result.errors.append("top level must be a JSON object")
        result.status = "fail"
        return result

    # Top level value types: strings or the authorized structured members.
    for key, value in data.items():
        if key == "applications":
            if not isinstance(value, list):
                result.errors.append("applications must be an array")
            continue
        if key in ("services", "payload-sizes"):
            if not isinstance(value, list):
                result.errors.append(f"{key} must be an array")
            continue
        if key in ("service-discovery", "logging", "threads"):
            if not isinstance(value, dict):
                result.errors.append(f"{key} must be an object")
            continue
        # network / payload 是 vsomeip 标准成员：受控设备基线用对象形式
        # （network.primary.name、payload.max-serialized-size），简化配置也允许
        # 字符串形式。二者内部仍逐层做秘密字段扫描（见 _check_unknown_secret_fields）。
        if key in ("network", "payload"):
            if not isinstance(value, (dict, str)):
                result.errors.append(f"{key} must be an object or string")
            continue
        if key == "max-configured-queuing-time":
            if not isinstance(value, (str, int)):
                result.errors.append(f"{key} must be a string or integer")
            continue
        if not isinstance(value, str):
            result.errors.append(f"top level field '{key}' must be a string")

    registered: list[str] = []
    applications = data.get("applications")
    if isinstance(applications, list):
        for index, entry in enumerate(applications):
            prefix = f"applications[{index}]"
            if not isinstance(entry, dict):
                result.errors.append(f"{prefix} must be an object")
                continue
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                result.errors.append(f"{prefix}.name is required")
                continue
            if not _APPLICATION_NAME_RE.match(name):
                result.errors.append(f"{prefix}.name is not a valid application id")
                continue
            if name in registered:
                result.errors.append(f"{prefix}.name duplicates application '{name}'")
                continue
            registered.append(name)
            for opt_key in ("id", "service"):
                if opt_key in entry and not isinstance(entry[opt_key], str):
                    result.errors.append(f"{prefix}.{opt_key} must be a string")

    routing = data.get("routing")
    if isinstance(routing, str) and registered and routing not in registered:
        result.errors.append(
            "routing must name a registered application (field path routing)"
        )

    required = sorted({str(a) for a in required_applications})
    missing = [app for app in required if app not in registered]
    for app in missing:
        result.errors.append(
            f"applications does not register required application '{app}' "
            f"(field path applications)"
        )

    _check_unknown_secret_fields(data, "", result.errors)
    findings = (scanner or SecretScanner()).scan_bytes(
        VSOMEIP_CONFIGURATION_PATH, raw.encode("utf-8")
    )
    for finding in findings:
        result.errors.append(
            f"secret scan rule '{finding.rule}' hit (field path "
            f"'{finding.field_path or '(root)'}')"
        )

    result.details = {
        "applications": registered,
        "required_applications": required,
    }
    result.status = "fail" if result.errors else "pass"
    return result


def run_asset_validation(
    validation: str,
    path: Path,
    expected_application: str = "",
    required_applications: "list[str] | set[str]" = (),
) -> AssetValidationResult:
    """Dispatch to the declared validator (never guessed from an extension)."""
    if validation == "vsomeip-dropin":
        return validate_vsomeip_dropin(path, expected_application)
    if validation == "vsomeip-json":
        return validate_vsomeip_json(path, required_applications)
    raise ConfigError(f"Unknown platform asset validator: {validation!r}")


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def application_from_dropin_target(target: str) -> str | None:
    """Return the service application name implied by a drop-in target path.

    ``/usr/lib/systemd/system/ccu-fota.service.d/vsomeip.conf`` ->
    ``ccu-fota``. The application name must stay identical to the baseline
    unit owner (CCU-BUILD-DSN-CR-004 §5.1).
    """
    parts = PurePosixPath(target).parts
    for part in parts:
        if part.endswith(".service.d"):
            return part[: -len(".service.d")]
    return None


def dropin_baseline_unit(target: str) -> str | None:
    """Return the baseline unit a drop-in target directory belongs to."""
    application = application_from_dropin_target(target)
    return f"{application}.service" if application else None
