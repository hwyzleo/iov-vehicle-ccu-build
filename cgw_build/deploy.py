"""Deploy framework for CGW Build.

Automated deployment of a release package to a CGW target device over SSH
(设计 §7 部署事务):

    pre-check -> extract -> upload -> backup -> install -> ldconfig
    -> daemon-reload -> enable/restart (dependency order) -> smoke
    (with automatic rollback to the pre-deploy backup on failure)

The pipeline is *dry-run by default*: it prints the exact ssh/rsync/systemctl
command plan without touching the device. Pass ``execute=True`` (CLI:
``--execute``) with a ``--host`` to perform the real deployment. Nothing about
a specific device is hard-coded; host/user/identity/port/sudo are provided at
invocation time.

/var/lib/cgw、设备身份和 provisioning 秘密默认保留。配置、二进制、framework、
动态库及 unit 不得部分回退或混装。
"""

from __future__ import annotations

import shlex
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import CgwBuildError
from .manifest import Project
from .graph import DependencyGraph
from .staging import sha256_file


@dataclass
class DeployStep:
    name: str
    status: str = "pending"
    message: str = ""
    commands: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0


@dataclass
class DeployReport:
    package_path: str = ""
    target_host: str = ""
    steps: list[DeployStep] = field(default_factory=list)
    status: str = "pending"
    errors: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Deployer:
    """Deploys a release package to a target device over SSH."""

    def __init__(
        self,
        project: Project,
        target_host: str | None = None,
        target_user: str = "cgw",
        identity: str | None = None,
        port: int = 22,
        use_sudo: bool = True,
        rollback_on_failure: bool = True,
        smoke_settle_seconds: int = 3,
        platform: str = "cgw-dev",
    ):
        self.project = project
        self.target_host = target_host
        self.target_user = target_user
        self.identity = identity
        self.port = port
        self.use_sudo = use_sudo
        self.rollback_on_failure = rollback_on_failure
        self.smoke_settle_seconds = smoke_settle_seconds
        self.staging_platform = platform
        self._units: list[str] = []
        self._stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.remote_stage = f"/tmp/cgw-deploy-{self._stamp}"
        self.backup_path = f"/var/lib/cgw/backups/cgw-{self._stamp}.tar.gz"

    # -- ssh / rsync helpers ----------------------------------------------

    def _ssh_opts(self) -> list[str]:
        opts = ["-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes"]
        if self.identity:
            opts += ["-i", self.identity]
        if self.port and self.port != 22:
            opts += ["-p", str(self.port)]
        return opts

    def _target(self) -> str:
        return f"{self.target_user}@{self.target_host}"

    def _ssh_cmd(self, remote_command: str) -> list[str]:
        return ["ssh", *self._ssh_opts(), self._target(), remote_command]

    def _rsync_cmd(self, local_src: str, remote_dst: str) -> list[str]:
        ssh = "ssh " + " ".join(self._ssh_opts())
        return ["rsync", "-a", "--delete", "-e", ssh,
                local_src, f"{self._target()}:{remote_dst}"]

    def _sudo(self, cmd: str) -> str:
        if not self.use_sudo:
            return cmd
        return f"sudo {cmd}"

    def _sudo_sh(self, script: str) -> str:
        if not self.use_sudo:
            return f"sh -c {shlex.quote(script)}"
        return "sudo sh -c " + shlex.quote(script)

    def _ssh_str(self, remote_command: str) -> str:
        return " ".join(shlex.quote(c) for c in self._ssh_cmd(remote_command))

    # -- restart order ----------------------------------------------------

    def _ordered_units(self, payload_root: Path) -> list[str]:
        """Return systemd units present in the payload, in dependency order."""
        unit_dir = payload_root / "usr" / "lib" / "systemd" / "system"
        present = {p.name for p in unit_dir.glob("*.service")} if unit_dir.is_dir() else set()
        if not present:
            return []
        try:
            sm = self.project.load_service_manifest()
            graph = DependencyGraph(sm)
            svc_units = {
                sid: list(svc.runtime.systemd_units)
                for sid, svc in sm.services.items()
            }
            ordered: list[str] = []
            for sid in graph.build_order():
                for unit in svc_units.get(sid, []):
                    if unit in present and unit not in ordered:
                        ordered.append(unit)
            for unit in sorted(present):
                if unit not in ordered:
                    ordered.append(unit)
            return ordered
        except Exception:
            return sorted(present)

    # -- plan -------------------------------------------------------------

    def build_plan(self, payload_root: Path) -> list[DeployStep]:
        """Build the ordered deploy step plan (command lists only)."""
        units = self._ordered_units(payload_root)
        self._units = units
        steps: list[DeployStep] = []

        steps.append(DeployStep("upload", commands=[
            " ".join(shlex.quote(c) for c in
                     self._ssh_cmd(f"mkdir -p {self.remote_stage}/rootfs")),
            " ".join(shlex.quote(c) for c in
                     self._rsync_cmd(f"{payload_root}/", f"{self.remote_stage}/rootfs/")),
        ]))
        steps.append(DeployStep("backup", commands=[
            " ".join(shlex.quote(c) for c in self._ssh_cmd(self._sudo_sh(
                f"mkdir -p /var/lib/cgw/backups && "
                f"tar -C / -czf {self.backup_path} "
                f"$(cd {self.remote_stage}/rootfs && find . -type f -o -type l | sed 's#^\\./##') "
                f"2>/dev/null || true"))),
        ]))
        steps.append(DeployStep("install", commands=[
            " ".join(shlex.quote(c) for c in self._ssh_cmd(
                self._sudo(f"rsync -a {self.remote_stage}/rootfs/ /"))),
        ]))
        steps.append(DeployStep("ldconfig", commands=[
            " ".join(shlex.quote(c) for c in self._ssh_cmd(self._sudo("ldconfig"))),
        ]))
        steps.append(DeployStep("daemon-reload", commands=[
            " ".join(shlex.quote(c) for c in self._ssh_cmd(self._sudo("systemctl daemon-reload"))),
        ]))
        restart_cmds = []
        for unit in units:
            restart_cmds.append(" ".join(shlex.quote(c) for c in self._ssh_cmd(
                self._sudo_sh(f"systemctl enable --now {unit} && systemctl restart {unit}"))))
        steps.append(DeployStep("restart", commands=restart_cmds,
                                message=f"units (dependency order): {', '.join(units) or '(none)'}"))
        smoke_cmds = [" ".join(shlex.quote(c) for c in self._ssh_cmd(
            f"systemctl is-active {unit}")) for unit in units]
        steps.append(DeployStep("smoke", commands=smoke_cmds,
                                message="per-unit is-active"))
        steps.append(DeployStep("cleanup", commands=[
            " ".join(shlex.quote(c) for c in self._ssh_cmd(f"rm -rf {self.remote_stage}")),
        ]))
        return steps

    # -- pre-check --------------------------------------------------------

    def _pre_check(self, package_path: Path) -> DeployStep:
        step = DeployStep(name="pre-check")
        start = time.time()
        if not package_path.is_file():
            step.status = "failed"
            step.message = f"Package not found: {package_path}"
            step.duration_seconds = time.time() - start
            return step
        checksum_file = package_path.with_suffix(".sha256")
        if checksum_file.is_file():
            expected = checksum_file.read_text().split()[0]
            actual = sha256_file(package_path)
            if expected != actual:
                step.status = "failed"
                step.message = f"Checksum mismatch: expected {expected}, got {actual}"
                step.duration_seconds = time.time() - start
                return step
        step.status = "success"
        step.message = f"Package verified ({package_path.name})"
        step.duration_seconds = time.time() - start
        return step

    def _extract(self, package_path: Path, dest: Path) -> Path:
        with tarfile.open(package_path, "r:gz") as tar:
            try:
                tar.extractall(dest, filter="data")
            except TypeError:
                tar.extractall(dest)
        candidates = list(dest.rglob("install-root"))
        for c in candidates:
            if c.is_dir():
                return c
        raise CgwBuildError(f"Package has no install-root: {package_path}")

    # -- run --------------------------------------------------------------

    def _run(self, cmd: str) -> tuple[int, str]:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        return proc.returncode, (proc.stdout + proc.stderr)

    def deploy(self, package_path: Path, execute: bool = False) -> DeployReport:
        """Execute (or plan) the deploy pipeline."""
        report = DeployReport(
            package_path=str(package_path),
            target_host=self.target_host or "(none)",
            started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

        pre = self._pre_check(package_path)
        report.steps.append(pre)
        if pre.status == "failed":
            report.status = "failed"
            report.errors.append(pre.message)
            report.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            return report

        do_execute = execute and bool(self.target_host)

        with tempfile.TemporaryDirectory(prefix="cgw-deploy-") as tmp:
            payload_root = self._extract(package_path, Path(tmp))
            plan = self.build_plan(payload_root)

            if not do_execute:
                for step in plan:
                    step.status = "planned"
                    if not step.message:
                        step.message = f"{len(step.commands)} command(s) (dry-run)"
                    report.steps.append(step)
                if execute and not self.target_host:
                    report.errors.append("--execute requires --host")
                report.status = "success (dry-run)"
                report.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                return report

            backed_up = False
            try:
                for step in plan:
                    if step.name == "smoke":
                        smoke = self._run_smoke(self._units)
                        report.steps.append(smoke)
                        if smoke.status == "failed":
                            report.status = "failed"
                            report.errors.append(f"smoke: {smoke.message}")
                            if self.rollback_on_failure and backed_up:
                                report.steps.append(self._rollback())
                            report.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                            return report
                        continue
                    start = time.time()
                    failed = False
                    for cmd in step.commands:
                        rc, out = self._run(cmd)
                        if rc != 0:
                            step.message = (step.message + " " if step.message else "") + \
                                f"command failed (rc={rc}): {out.strip()[:400]}"
                            failed = True
                            break
                    step.duration_seconds = time.time() - start
                    step.status = "failed" if failed else "success"
                    report.steps.append(step)
                    if step.name == "backup" and not failed:
                        backed_up = True
                    if failed:
                        report.status = "failed"
                        report.errors.append(f"{step.name}: {step.message}")
                        if self.rollback_on_failure and backed_up:
                            report.steps.append(self._rollback())
                        report.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                        return report
            except Exception as exc:
                report.status = "failed"
                report.errors.append(f"Unexpected error: {exc}")

        report.status = "success"
        report.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return report

    def _run_smoke(self, units: list[str]) -> DeployStep:
        step = DeployStep(name="smoke")
        if self.smoke_settle_seconds > 0:
            time.sleep(self.smoke_settle_seconds)
        states: list[str] = []
        failing: list[str] = []
        for unit in units:
            rc, out = self._run(self._ssh_str(f"systemctl is-active {unit}"))
            state = out.strip() or ("active" if rc == 0 else "unknown")
            states.append(f"{unit}={state}")
            if rc != 0:
                failing.append(unit)
        step.message = "unit states: " + ", ".join(states)
        if failing:
            step.status = "failed"
            step.message += "\nnot active: " + ", ".join(failing)
        else:
            step.status = "success"
        return step

    def _rollback(self) -> DeployStep:
        step = DeployStep(name="rollback")
        start = time.time()
        cmds = [
            self._ssh_cmd(self._sudo_sh(
                f"tar -C / -xzf {self.backup_path} && ldconfig && systemctl daemon-reload")),
        ]
        step.commands = [" ".join(shlex.quote(c) for c in cmds[0])]
        rc, out = self._run(step.commands[0])
        step.status = "success" if rc == 0 else "failed"
        step.message = f"restored from {self.backup_path}" if rc == 0 else out.strip()[:400]
        step.duration_seconds = time.time() - start
        return step

    def rollback(self, backup_path: Path | str) -> DeployReport:
        """Manually roll back to a specific on-device backup archive."""
        report = DeployReport(
            target_host=self.target_host or "(none)",
            started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        self.backup_path = str(backup_path)
        if not self.target_host:
            report.steps.append(DeployStep(
                name="rollback", status="planned", message="requires --host"))
            report.status = "success (dry-run)"
        else:
            report.steps.append(self._rollback())
            report.status = "success" if report.steps[-1].status == "success" else "failed"
        report.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return report
