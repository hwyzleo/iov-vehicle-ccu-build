"""Unit tests for secret scanning (设计 §9.6)."""

from __future__ import annotations

from pathlib import Path

from ccu_build.secretscan import SecretScanner, SecretScanResult


class TestSecretScanner:
    def setup_method(self):
        self.scanner = SecretScanner()

    def test_clean_yaml_passes(self):
        content = b"service_name: ccu-hello\ngreeting: hello\n"
        findings = self.scanner.scan_bytes("/etc/ccu/conf.d/svc.yaml", content)
        assert findings == []

    def test_pem_private_key_detected(self):
        content = b"-----BEGIN RSA PRIVATE KEY-----\nMIIBOQIBAAJBALW0gA==\n-----END RSA PRIVATE KEY-----\n"
        findings = self.scanner.scan_bytes("/etc/ccu/conf.d/svc.yaml", content)
        assert any(f.rule == "pem-private-key" for f in findings)

    def test_pem_ec_private_key_detected(self):
        content = b"-----BEGIN EC PRIVATE KEY-----\nMHQCAQEE...\n-----END EC PRIVATE KEY-----\n"
        findings = self.scanner.scan_bytes("/etc/ccu/x.yaml", content)
        assert any(f.rule == "pem-private-key" for f in findings)

    def test_forbidden_extension_pem(self):
        findings = self.scanner.scan_bytes("/etc/ccu/certs/server.pem", b"not a key")
        assert any(f.rule == "forbidden-extension" for f in findings)

    def test_forbidden_extension_key(self):
        findings = self.scanner.scan_bytes("/etc/ccu/certs/server.key", b"x")
        assert any(f.rule == "forbidden-extension" and f.field_path == ".key" for f in findings)

    def test_allowed_extension_passes(self):
        findings = self.scanner.scan_bytes("/etc/ccu/conf.d/svc.yaml", b"a: 1\n")
        assert findings == []

    def test_provisioning_directory_detected(self):
        findings = self.scanner.scan_bytes(
            "/etc/ccu/credentials/device.crt", b"cert")
        assert any(f.rule == "provisioning-directory" for f in findings)

    def test_provisioning_nested_detected(self):
        findings = self.scanner.scan_bytes(
            "/etc/ccu/credentials/sub/device.key", b"x")
        rules = {f.rule for f in findings}
        assert "provisioning-directory" in rules
        assert "forbidden-extension" in rules

    def test_sensitive_field_password(self):
        content = b"password: hunter2\n"
        findings = self.scanner.scan_bytes("/etc/ccu/conf.d/svc.yaml", content)
        assert any(f.rule == "sensitive-field" and f.field_path == "password" for f in findings)

    def test_sensitive_field_token_nested(self):
        content = b"db:\n  token: abc123\n"
        findings = self.scanner.scan_bytes("/etc/ccu/conf.d/svc.yaml", content)
        assert any(
            f.rule == "sensitive-field" and f.field_path == "db.token" for f in findings
        )

    def test_sensitive_field_in_list(self):
        content = b"secrets:\n  - name: a\n    api_key: x\n"
        findings = self.scanner.scan_bytes("/etc/ccu/conf.d/svc.yaml", content)
        # "secrets" key matches sensitive-field, "api_key" matches too
        rules = {f.rule for f in findings}
        assert "sensitive-field" in rules

    def test_findings_report_no_value(self):
        content = b"password: supersecret-value\n"
        findings = self.scanner.scan_bytes("/etc/ccu/conf.d/svc.yaml", content)
        # Findings must not contain the value, only rule/file/field_path
        for f in findings:
            assert "supersecret-value" not in (f.rule + f.file + f.field_path)

    def test_scan_file(self, tmp_path: Path):
        path = tmp_path / "svc.yaml"
        path.write_bytes(b"token: xyz\n")
        findings = self.scanner.scan_file("/etc/ccu/conf.d/svc.yaml", path)
        assert any(f.rule == "sensitive-field" for f in findings)

    def test_invalid_yaml_no_crash(self):
        content = b"\t\0bad: [unclosed"
        findings = self.scanner.scan_bytes("/etc/ccu/conf.d/svc.yaml", content)
        # Should not raise; sensitive-field scan skipped on parse error
        assert all(f.rule != "sensitive-field" for f in findings)


class TestSecretScanResult:
    def test_empty_result_passes(self):
        result = SecretScanResult()
        assert not result.has_findings
        assert result.to_dict()["status"] == "pass"

    def test_findings_fail(self):
        from ccu_build.secretscan import SecretFinding
        result = SecretScanResult(findings=[SecretFinding("pem-private-key", "x")])
        assert result.has_findings
        d = result.to_dict()
        assert d["status"] == "fail"
        assert d["findings"][0]["rule"] == "pem-private-key"
        assert "value" not in d["findings"][0]
