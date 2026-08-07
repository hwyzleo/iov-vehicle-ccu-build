"""Secret scanning for CGW Build config release (设计 §9.6).

Scans platform source, install-root and final package files for secret
material:

  * PEM private key blocks;
  * forbidden extensions (certificates / keystores);
  * sensitive field names in YAML;
  * provisioning-only directories.

Findings report only the rule, file and field path -- never the value
(设计 §9.6: 命中时只报告规则、文件与字段路径，不得输出值).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# PEM private key block markers (RSA / EC / DSA / OpenSSH / PGP / generic)
_PEM_PRIVATE_KEY_RE = re.compile(
    rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"
)

# Forbidden extensions: secrets and certificates must not ship as release
# files; they are a provisioning concern (设计 §9.6).
_FORBIDDEN_EXTENSIONS = frozenset({
    ".pem", ".key", ".p12", ".pfx", ".keystore", ".jks",
    ".crt", ".cert", ".gpg", ".asc",
})

# Sensitive field names (case-insensitive substring match on YAML keys).
_SENSITIVE_FIELD_NAMES = frozenset({
    "password", "passwd", "secret", "token", "api_key", "apikey",
    "private_key", "privatekey", "credential", "keystore", "kek",
    "certificate",
})

# Provisioning-only directory prefixes (normalised, no leading slash).
_PROVISIONING_DIRS = (
    "etc/cgw/credentials/",
    "etc/cgw/provisioning/",
)


@dataclass
class SecretFinding:
    """A single secret scan finding (locations only, never the value)."""

    rule: str
    file: str
    field_path: str = ""


@dataclass
class SecretScanResult:
    """Result of scanning a set of files."""

    findings: list[SecretFinding] = field(default_factory=list)

    @property
    def has_findings(self) -> bool:
        return bool(self.findings)

    def to_dict(self) -> dict:
        return {
            "status": "fail" if self.has_findings else "pass",
            "findings": [
                {"rule": f.rule, "file": f.file, "field_path": f.field_path}
                for f in self.findings
            ],
        }


class SecretScanner:
    """Scans files for secret material (设计 §9.6)."""

    def scan_bytes(self, logical_path: str, content: bytes) -> list[SecretFinding]:
        findings: list[SecretFinding] = []
        norm = logical_path.lstrip("/")

        # 1. PEM private key block
        if _PEM_PRIVATE_KEY_RE.search(content):
            findings.append(SecretFinding(rule="pem-private-key", file=logical_path))

        # 2. forbidden extension
        ext = Path(logical_path).suffix.lower()
        if ext in _FORBIDDEN_EXTENSIONS:
            findings.append(SecretFinding(
                rule="forbidden-extension", file=logical_path, field_path=ext))

        # 3. provisioning-only directory
        for prefix in _PROVISIONING_DIRS:
            if norm.startswith(prefix):
                findings.append(SecretFinding(
                    rule="provisioning-directory", file=logical_path))
                break

        # 4. sensitive field names in YAML
        if logical_path.endswith((".yaml", ".yml")):
            findings.extend(self._scan_yaml_fields(logical_path, content))

        return findings

    def scan_file(self, logical_path: str, path: Path) -> list[SecretFinding]:
        return self.scan_bytes(logical_path, path.read_bytes())

    def _scan_yaml_fields(self, logical_path: str, content: bytes) -> list[SecretFinding]:
        findings: list[SecretFinding] = []
        try:
            data = yaml.safe_load(content)
        except yaml.YAMLError:
            return findings
        self._walk_yaml(data, logical_path, "", findings)
        return findings

    def _walk_yaml(self, node, file: str, path: str, findings: list[SecretFinding]) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                key_str = str(k).lower()
                field_path = f"{path}.{k}" if path else str(k)
                if any(s in key_str for s in _SENSITIVE_FIELD_NAMES):
                    findings.append(SecretFinding(
                        rule="sensitive-field", file=file, field_path=field_path))
                self._walk_yaml(v, file, field_path, findings)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                self._walk_yaml(item, file, f"{path}[{i}]", findings)
