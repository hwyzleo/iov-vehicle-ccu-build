"""systemd unit verification for CCU Build install-root.

CCU-BUILD-RELEASE-REQ-004 requires the final install-root to pass
``systemd-analyze verify`` *or an equivalent check*. This module implements
the equivalent static check (always available, also on HOST/CI without
systemd) and additionally runs ``systemd-analyze verify`` when the binary is
present.

Equivalent static checks:

  * every ``*.service`` parses, declares ``[Service]`` and ``ExecStart=``;
  * every ``[Unit]`` ordering/dependency reference resolves to a unit in the
    payload or to a well-known systemd target;
  * ``usr/lib/systemd/system/ccu.target`` (the BUILD release-set aggregation
    target) never uses ``Requires=`` and keeps ``Wants=`` == ``After=``;
  * drop-ins under ``*.service.d/`` parse and declare a ``[Service]`` section;
  * a drop-in target directory must correspond to a baseline unit present in
    the same payload (CCU-BUILD-DSN-CR-004 §5.1).
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from .assetcheck import parse_unit_file
from .manifest import RELEASE_SET_AGGREGATION_TARGET

SYSTEM_UNIT_DIR = "usr/lib/systemd/system"

# Well-known units/targets a payload unit may legitimately order/depend on.
KNOWN_EXTERNAL_UNITS = frozenset({
    "basic.target",
    "default.target",
    "multi-user.target",
    "network.target",
    "network-online.target",
    "sockets.target",
    "sysinit.target",
    "local-fs.target",
    "remote-fs.target",
    "systemd-journald.service",
    "systemd-tmpfiles-setup.service",
})

_REFERENCE_DIRECTIVES = frozenset({
    "Wants", "After", "Requires", "Requisite", "BindsTo", "PartOf", "Upholds",
})


@dataclass
class UnitCheckResult:
    """Result of the install-root unit verification."""

    engine: str = "equivalent-static-check"
    checked: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        return "fail" if self.errors else "pass"

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "status": self.status,
            "checked": self.checked,
            "errors": self.errors,
            "warnings": self.warnings,
        }


def _unit_references(sections) -> list[str]:
    refs: list[str] = []
    for name, directives in sections:
        if name != "Unit":
            continue
        for key, value in directives:
            if key not in _REFERENCE_DIRECTIVES:
                continue
            refs.extend(part for part in value.split() if part)
    return refs


def check_install_root_units(
    install_root: Path,
    run_systemd_analyze: bool = True,
) -> UnitCheckResult:
    """Verify units and drop-ins in an install-root."""
    result = UnitCheckResult()
    unit_dir = Path(install_root) / SYSTEM_UNIT_DIR
    if not unit_dir.is_dir():
        result.warnings.append(f"no unit directory in install-root: {unit_dir}")
        return result

    units = sorted(
        p for p in unit_dir.iterdir()
        if p.is_file() and p.suffix in (".service", ".target", ".socket", ".timer")
    )
    payload_units = {unit.name for unit in units}

    for unit in units:
        rel = f"{SYSTEM_UNIT_DIR}/{unit.name}"
        result.checked.append(rel)
        sections, parse_errors = parse_unit_file(unit)
        for err in parse_errors:
            result.errors.append(f"{rel}: {err}")
        if not sections:
            result.errors.append(f"{rel}: declares no section")
            continue

        section_names = [name for name, _ in sections]
        if unit.suffix == ".service":
            if "Service" not in section_names:
                result.errors.append(f"{rel}: missing [Service] section")
            service_directives = dict(
                (k, v) for name, dirs in sections if name == "Service" for k, v in dirs
            )
            if "ExecStart" not in service_directives:
                result.errors.append(f"{rel}: missing ExecStart= in [Service]")
        elif unit.suffix == ".target" and "Unit" not in section_names:
            result.errors.append(f"{rel}: target declares no [Unit] section")

        refs = _unit_references(sections)
        for ref in refs:
            if ref in payload_units or ref in KNOWN_EXTERNAL_UNITS:
                continue
            result.errors.append(
                f"{rel}: references unknown unit/service '{ref}' in [Unit] ordering"
            )
        # R-004 §4.2: the release-set aggregation target must render the same
        # ordered unit set into Wants= and After=; service units keep their own
        # service-owned ordering semantics.
        if unit.name == RELEASE_SET_AGGREGATION_TARGET:
            wants = _directive_values(sections, "Wants")
            after = _directive_values(sections, "After")
            if wants != after:
                result.errors.append(
                    f"{rel}: Wants= and After= must contain the same ordered "
                    f"unit set"
                )
            if _directive_values(sections, "Requires"):
                result.errors.append(
                    f"{rel}: release-set aggregation target must not use Requires="
                )

    # Drop-ins: target directory must correspond to an existing baseline unit.
    for dropin_dir in sorted(unit_dir.glob("*.service.d")):
        stem = dropin_dir.name[: -len(".d")]
        baseline = stem if stem.endswith(".service") else f"{stem}.service"
        if baseline not in payload_units:
            result.errors.append(
                f"{SYSTEM_UNIT_DIR}/{dropin_dir.name}: baseline unit "
                f"'{baseline}' is not present in the payload"
            )
        for conf in sorted(dropin_dir.glob("*.conf")):
            rel = f"{SYSTEM_UNIT_DIR}/{dropin_dir.name}/{conf.name}"
            result.checked.append(rel)
            sections, parse_errors = parse_unit_file(conf)
            for err in parse_errors:
                result.errors.append(f"{rel}: {err}")
            if not sections:
                result.errors.append(f"{rel}: declares no section")
                continue
            if [name for name, _ in sections] != ["Service"]:
                result.errors.append(
                    f"{rel}: platform drop-in must declare exactly one [Service] "
                    f"section"
                )

    if run_systemd_analyze and shutil.which("systemd-analyze"):
        result.engine = "systemd-analyze verify + equivalent-static-check"
        try:
            proc = subprocess.run(
                ["systemd-analyze", "verify", str(unit_dir)],
                capture_output=True,
                text=True,
                timeout=60,
            )
            output = (proc.stdout + proc.stderr).strip()
            if proc.returncode != 0:
                result.errors.append(
                    "systemd-analyze verify failed: " + (output[:800] or "unknown")
                )
        except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
            result.warnings.append(f"systemd-analyze verify not run: {exc}")

    return result


def _directive_values(sections, key: str) -> list[str]:
    values: list[str] = []
    for name, directives in sections:
        if name != "Unit":
            continue
        for directive_key, value in directives:
            if directive_key == key:
                values.extend(part for part in value.split() if part)
    return values


def load_aggregation_target_units(install_root: Path) -> tuple[list[str], list[str]]:
    """Return ``(wants, after)`` ordered unit lists from the install-root target."""
    target = Path(install_root) / SYSTEM_UNIT_DIR / RELEASE_SET_AGGREGATION_TARGET
    if not target.is_file():
        return [], []
    sections, _ = parse_unit_file(target)
    return _directive_values(sections, "Wants"), _directive_values(sections, "After")
