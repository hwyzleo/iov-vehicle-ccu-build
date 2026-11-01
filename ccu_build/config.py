"""Configuration release overlay for CCU Build (设计 §9).

Implements the platform config overlay algorithm (§9.3):

  1. Normalize ``configs/<platform>/rootfs``; reject absolute paths, ``..``,
     symlink escape, device files and non-regular files.
  2. Clean previous overlay state to prevent stale files in incremental
     staging.
  3. Map relative source paths to target logical path ``/<rel>``.
  4. Overlay only ``config-deployment.yaml``-authorized targets; record
     pre-overlay project/component/source/SHA-256.
  5. Atomic write with permission normalization.
  6. Schema/checker, ownership conflict, path, permission and secret scan.
  7. Emit overlay report for the Artifact Manifest.

BUILD overlay uses *whole-file replace*; it does not participate in the
CCU-FW runtime six-stage deep merge (§9.8). ``clean`` and ``incremental``
builds MUST produce the same final configuration set and digests.
"""

from __future__ import annotations

import fnmatch
import json
import os
import stat
import tempfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from .errors import ConfigError, ConfigValidationError, OverlayError
from .manifest import (
    ConfigDeploymentManifest,
    ConfigDeploymentRule,
    ConfigValidation,
    Service,
    ServiceManifest,
    load_yaml,
)
from .secretscan import SecretScanResult, SecretScanner
from .staging import sha256_file

# Default mode for non-secret YAML config files (设计 §9.6).
_DEFAULT_MODE = 0o644


# ---------------------------------------------------------------------------
# Glob matching
# ---------------------------------------------------------------------------


def _glob_match(pattern: str, path: str) -> bool:
    """Match a glob pattern against a logical path.

    Supports ``**`` (cross-directory, zero or more segments) and ``*``
    (within a single path segment). Leading slashes are ignored.
    """
    pat_segs = pattern.lstrip("/").split("/")
    tgt_segs = path.lstrip("/").split("/")
    return _match_segments(pat_segs, tgt_segs)


def _match_segments(pat_segs: list[str], tgt_segs: list[str]) -> bool:
    if not pat_segs:
        return not tgt_segs
    seg = pat_segs[0]
    if seg == "**":
        # ** matches zero or more remaining segments
        for i in range(len(tgt_segs) + 1):
            if _match_segments(pat_segs[1:], tgt_segs[i:]):
                return True
        return False
    if not tgt_segs:
        return False
    if not fnmatch.fnmatchcase(tgt_segs[0], seg):
        return False
    return _match_segments(pat_segs[1:], tgt_segs[1:])


# ---------------------------------------------------------------------------
# Deploy policy resolution (设计 §9.4)
# ---------------------------------------------------------------------------


class DeployPolicyResolver:
    """Resolves deploy policy for a logical path (设计 §9.4).

    Exact ``path`` rules take precedence over ``path_glob`` rules.
    Same-priority (glob) rules with conflicting ``deploy_policy`` fail.
    Unmatched ``/etc/ccu/**`` paths default to ``preserve``.
    ``device-managed`` files must not use ``replace`` (validated at load).
    """

    DEFAULT_POLICY = "preserve"

    def __init__(self, rules: list[ConfigDeploymentRule]):
        self._exact: dict[str, ConfigDeploymentRule] = {}
        self._globs: list[ConfigDeploymentRule] = []
        for r in rules:
            if r.is_exact:
                if r.path in self._exact:
                    raise OverlayError(
                        f"Duplicate exact path rule for '{r.path}' in "
                        f"config-deployment.yaml"
                    )
                self._exact[r.path] = r  # type: ignore[index]
            else:
                self._globs.append(r)
        # Validate device-managed must not replace (设计 §9.4)
        for r in rules:
            if r.category == "device-managed" and r.deploy_policy == "replace":
                raise OverlayError(
                    f"device-managed path '{r.pattern}' must not use "
                    f"deploy_policy: replace"
                )

    def resolve(self, logical_path: str) -> ConfigDeploymentRule | None:
        """Return the matching rule, or ``None`` for unmatched (default preserve)."""
        if logical_path in self._exact:
            return self._exact[logical_path]
        matches = [r for r in self._globs if r.path_glob and _glob_match(r.path_glob, logical_path)]
        if not matches:
            return None
        if len({r.deploy_policy for r in matches}) > 1:
            raise OverlayError(
                f"Conflicting deploy policies for '{logical_path}': "
                + ", ".join(f"{r.pattern}={r.deploy_policy}" for r in matches)
            )
        return matches[0]


# ---------------------------------------------------------------------------
# Path normalization (设计 §9.3 step 1)
# ---------------------------------------------------------------------------


def normalize_source_path(rootfs: Path, src: Path) -> str:
    """Validate a platform source file and return its logical path.

    Rejects absolute paths, ``..``, symlink escape, device files and
    non-regular files. Returns the logical path relative to rootfs
    (e.g. ``etc/ccu/common.yaml``).
    """
    try:
        rel = src.relative_to(rootfs)
    except ValueError:
        raise OverlayError(f"Source path outside rootfs: {src}")
    rel_str = str(rel).replace(os.sep, "/")
    if rel_str.startswith("/"):
        raise OverlayError(f"Absolute path in platform source: {rel_str}")
    parts = rel_str.split("/")
    if ".." in parts:
        raise OverlayError(f"Parent traversal in platform source: {rel_str}")
    # Symlink escape: resolve and confirm within rootfs
    try:
        resolved = src.resolve()
        resolved.relative_to(rootfs.resolve())
    except ValueError:
        raise OverlayError(f"Symlink escape in platform source: {src}")
    st = src.lstat()
    if stat.S_ISLNK(st.st_mode):
        raise OverlayError(f"Symlink not allowed in platform source: {src}")
    if not stat.S_ISREG(st.st_mode):
        raise OverlayError(f"Non-regular/special file in platform source: {src}")
    return rel_str


# ---------------------------------------------------------------------------
# Atomic write (设计 §9.3 step 5)
# ---------------------------------------------------------------------------


def atomic_write(target: Path, content: bytes, mode: int = _DEFAULT_MODE) -> None:
    """Write *content* to *target* via a temp file + atomic replace."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".ccu-overlay-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Overlay report
# ---------------------------------------------------------------------------


@dataclass
class OverlayFileRecord:
    """A single file record in the overlay report."""

    logical_path: str
    platform_source: str
    platform_source_sha256: str
    pre_overlay_source: str
    pre_overlay_component: str
    pre_overlay_sha256: str
    final_sha256: str
    owner: str
    category: str
    deploy_policy: str
    mode: str
    validation: dict[str, Any]
    secret_scan: dict[str, Any]


@dataclass
class OverlayReport:
    """Report of a platform config overlay run (设计 §9.3 step 7)."""

    platform: str
    overlaid: list[OverlayFileRecord] = field(default_factory=list)
    removed: list[dict[str, str]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)


# ---------------------------------------------------------------------------
# Config validation (设计 §9.5)
# ---------------------------------------------------------------------------


class ConfigValidator:
    """Validates final config files against service-declared schemas (§9.5).

    Service configs are validated with the JSON Schema declared in
    ``services.yaml`` ``runtime.config_validation.schema`` (loaded from the
    release-set-locked service source directory). ``common.yaml`` uses a
    BUILD-side structural validator; the full CCU-FW common validator is an
    upstream concern. Service config files must not declare a top-level
    ``common:`` key (§9.9).
    """

    def __init__(self, service_manifest: ServiceManifest, project_root: Path):
        self.service_manifest = service_manifest
        self.project_root = Path(project_root)
        # target_path -> (service_id, ConfigValidation)
        self._by_target: dict[str, tuple[str, ConfigValidation]] = {}
        for svc in service_manifest:
            cv = svc.runtime.config_validation
            if cv is not None and cv.target_path:
                self._by_target[cv.target_path] = (svc.id, cv)

    def validate(self, logical_path: str, content: bytes) -> dict[str, Any]:
        """Validate a final config file. Returns a result dict."""
        target = "/" + logical_path.lstrip("/")
        entry = self._by_target.get(target)
        if entry is not None:
            return self._validate_with_schema(logical_path, content, entry)
        if logical_path.endswith("common.yaml"):
            return self._validate_common(logical_path, content)
        # No declared schema and not common.yaml: nothing to validate.
        return {"status": "skip", "schema": None, "errors": []}

    def _validate_with_schema(
        self, logical_path: str, content: bytes, entry: tuple[str, ConfigValidation]
    ) -> dict[str, Any]:
        svc_id, cv = entry
        if not cv.schema:
            return {"status": "skip", "schema": None, "errors": []}
        svc = self.service_manifest.get(svc_id)
        if svc is None:
            return {"status": "skip", "schema": None, "errors": []}
        schema_path = self.project_root / svc.effective_source_dir / cv.schema
        if not schema_path.is_file():
            return {
                "status": "fail",
                "schema": cv.schema,
                "errors": [f"schema file not found: {schema_path}"],
            }
        try:
            schema = yaml.safe_load(schema_path.read_bytes())
        except yaml.YAMLError as exc:
            return {
                "status": "fail",
                "schema": cv.schema,
                "errors": [f"schema parse error: {exc}"],
            }
        try:
            data = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            return {
                "status": "fail",
                "schema": cv.schema,
                "errors": [f"config parse error: {exc}"],
            }
        # Service configs must not declare a top-level common: key (§9.9)
        if isinstance(data, dict) and "common" in data:
            return {
                "status": "fail",
                "schema": cv.schema,
                "errors": [
                    f"service config '{logical_path}' must not declare a "
                    f"top-level 'common:' key"
                ],
            }
        validator = jsonschema.Draft7Validator(schema)
        errors = sorted(
            validator.iter_errors(data),
            key=lambda e: list(e.absolute_path),
        )
        if errors:
            return {
                "status": "fail",
                "schema": cv.schema,
                "errors": [
                    f"{'.'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message}"
                    for e in errors
                ],
            }
        return {"status": "pass", "schema": cv.schema, "errors": []}

    def _validate_common(self, logical_path: str, content: bytes) -> dict[str, Any]:
        """BUILD-side common.yaml structural validator (§9.5)."""
        try:
            yaml.safe_load(content)
        except yaml.YAMLError as exc:
            return {
                "status": "fail",
                "schema": "ccu-common-validator",
                "errors": [f"common.yaml parse error: {exc}"],
            }
        return {"status": "pass", "schema": "ccu-common-validator", "errors": []}


# ---------------------------------------------------------------------------
# Config overlay engine (设计 §9.3)
# ---------------------------------------------------------------------------


class ConfigOverlay:
    """Applies the BUILD platform config overlay (设计 §9.3)."""

    def __init__(
        self,
        platform: str,
        platform_rootfs: Path,
        install_root: Path,
        deployment: ConfigDeploymentManifest,
        service_manifest: ServiceManifest,
        project_root: Path,
        state_dir: Path,
    ):
        self.platform = platform
        self.platform_rootfs = Path(platform_rootfs)
        self.install_root = Path(install_root)
        self.deployment = deployment
        self.service_manifest = service_manifest
        self.project_root = Path(project_root)
        self.state_dir = Path(state_dir)
        self.state_file = self.state_dir / "config-overlay-state.json"
        plat_dep = deployment.get(platform)
        self._resolver = DeployPolicyResolver(
            plat_dep.files if plat_dep is not None else []
        )
        self._scanner = SecretScanner()
        self._validator = ConfigValidator(service_manifest, project_root)

    # -- state persistence ------------------------------------------------

    def _load_state(self) -> dict[str, dict[str, Any]]:
        if not self.state_file.is_file():
            return {}
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return {e["logical_path"]: e for e in data.get("overlaid", [])}

    def _save_state(self, records: list[dict[str, Any]]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = {"platform": self.platform, "overlaid": records}
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

    def _clear_state(self) -> None:
        if self.state_file.is_file():
            self.state_file.unlink()

    # -- overlay ----------------------------------------------------------

    def apply(
        self, file_owners: dict[str, tuple[str, str, str]] | None = None
    ) -> OverlayReport:
        """Run the overlay algorithm.

        ``file_owners`` maps install-root-relative logical paths (e.g.
        ``etc/ccu/conf.d/foo.yaml``) to ``(service_id, component, sha256)``
        describing the pre-overlay installer of each file.
        """
        file_owners = file_owners or {}
        report = OverlayReport(platform=self.platform)

        # Step 1: enumerate and normalize platform source files
        sources: list[tuple[str, Path]] = []
        if self.platform_rootfs.is_dir():
            for src in sorted(self.platform_rootfs.rglob("*")):
                if src.is_dir():
                    continue
                logical = normalize_source_path(self.platform_rootfs, src)
                sources.append((logical, src))
        current_paths = {logical for logical, _ in sources}

        # Step 2: clean previous overlay state (stale handling)
        prev_state = self._load_state()
        stale = set(prev_state.keys()) - current_paths
        for sp in sorted(stale):
            record = prev_state[sp]
            # Only build-owned files (no service default to reset them) are
            # removed; service-owned files are reset by service re-install.
            if record.get("owner") == "build":
                target = self.install_root / sp
                if target.is_file():
                    target.unlink()
                    report.removed.append({
                        "logical_path": sp,
                        "reason": "stale build-owned overlay",
                    })
        self._clear_state()

        # Steps 3-7: overlay each source file
        state_records: list[dict[str, Any]] = []
        for logical, src in sources:
            target_logical = "/" + logical
            # Step 4: authorization
            try:
                rule = self._resolver.resolve(target_logical)
            except OverlayError as exc:
                report.skipped.append({
                    "logical_path": target_logical, "reason": str(exc)})
                continue
            if rule is None:
                report.skipped.append({
                    "logical_path": target_logical,
                    "reason": "no deploy policy (default preserve)",
                })
                continue
            if rule.category == "device-managed":
                # BUILD never overlays device-managed (provisioning) content
                report.skipped.append({
                    "logical_path": target_logical,
                    "reason": "device-managed (preserve)",
                })
                continue

            target = self.install_root / logical
            # record pre-overlay source
            owner_info = file_owners.get(logical)
            if owner_info:
                pre_source, pre_component, _ = owner_info
            else:
                pre_source, pre_component = "(none)", "(none)"
            pre_sha = sha256_file(target) if target.is_file() else ""

            # scan platform source for secrets before overlaying
            src_findings = self._scanner.scan_file(target_logical, src)

            # Step 5: atomic write with permission normalization
            content = src.read_bytes()
            atomic_write(target, content, _DEFAULT_MODE)
            final_sha = sha256_file(target)

            # Step 6: schema/checker validation on the final file
            validation = self._validator.validate(logical, content)
            # Step 6: secret scan on the final file
            final_findings = self._scanner.scan_bytes(target_logical, content)
            # merge source + final findings (dedup by rule/file/field_path)
            all_findings = src_findings + final_findings
            seen: set[tuple[str, str, str]] = set()
            deduped = []
            for f in all_findings:
                key = (f.rule, f.file, f.field_path)
                if key not in seen:
                    seen.add(key)
                    deduped.append(f)
            scan_result = SecretScanResult(findings=deduped)

            record = OverlayFileRecord(
                logical_path=target_logical,
                platform_source=logical,
                platform_source_sha256=sha256_file(src),
                pre_overlay_source=pre_source,
                pre_overlay_component=pre_component,
                pre_overlay_sha256=pre_sha,
                final_sha256=final_sha,
                owner=rule.owner,
                category=rule.category,
                deploy_policy=rule.deploy_policy,
                mode=oct(_DEFAULT_MODE),
                validation=validation,
                secret_scan=scan_result.to_dict(),
            )
            report.overlaid.append(record)
            state_records.append({
                "logical_path": logical,
                "owner": rule.owner,
                "pre_overlay_source": pre_source,
                "platform_source_sha256": record.platform_source_sha256,
            })

        # Step 7: persist state + report (saved by caller via report.save)
        self._save_state(state_records)
        return report

    # -- post-overlay failure checks --------------------------------------

    def check_report(self, report: OverlayReport) -> None:
        """Raise on validation/secret failures in an overlay report."""
        failures: list[str] = []
        for rec in report.overlaid:
            if rec.validation.get("status") == "fail":
                failures.append(
                    f"config validation failed for '{rec.logical_path}': "
                    + "; ".join(rec.validation.get("errors", []))
                )
            if rec.secret_scan.get("status") == "fail":
                findings = rec.secret_scan.get("findings", [])
                failures.append(
                    f"secret material detected in '{rec.logical_path}': "
                    + ", ".join(
                        f"{f['rule']}@{f['field_path']}" for f in findings
                    )
                )
        if failures:
            raise ConfigError(
                f"config overlay validation failed ({len(failures)} issue(s))",
                failures,
            )
