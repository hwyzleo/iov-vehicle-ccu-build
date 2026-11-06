"""Build orchestration for CCU Build.

Ties together manifest loading, validation, dependency-graph ordering,
TARGET dependency recipes, CMake configure/build/install (multi-target,
multi-component with SDK/rootfs staging), ELF checking and artifact
manifest generation into a single pipeline.

平台参数化：platform 从配置传入，toolchain/sysroot 从平台清单动态
确定，不硬编码任何平台常量。

Usage::

    orch = BuildOrchestrator(project, config=BuildConfig(platform="ccu-dev"))
    report = orch.build(set_id="ccu-dev-minimal")
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .artifact import ArtifactManifest, get_git_commit
from .assembly import (
    INSTALL_COMPONENT as RELEASE_SET_COMPONENT,
    OWNER_TARGET as RELEASE_SET_OWNER_TARGET,
    AssemblyReport,
    ReleaseSetAssembler,
)
from .config import ConfigOverlay
from .elfcheck import check_staging, ElfCheckResult, EM_AARCH64, EM_X86_64, _ELFCLASS64, _MACHINE_NAMES
from .errors import AssemblyError, BuildFailure, CcuBuildError, ConfigError
from .graph import DependencyGraph
from .reconcile import ReconcileReport, reconcile_install_root
from .unitcheck import check_install_root_units
from .manifest import (
    Project,
    Service,
    ServiceManifest,
    ReleaseSetManifest,
    InstallComponent,
)
from .schema import validate_service_manifest, validate_release_set_manifest, validate_dependency_lock
from .staging import StagingDir, sha256_file
from .validator import validate_all


# ---------------------------------------------------------------------------
# Architecture -> ELF machine mapping
# ---------------------------------------------------------------------------

_ARCH_TO_ELF_MACHINE: dict[str, int] = {
    "aarch64": EM_AARCH64,
    "arm64": EM_AARCH64,
    "x86_64": EM_X86_64,
    "amd64": EM_X86_64,
}


# ---------------------------------------------------------------------------
# Build configuration and report
# ---------------------------------------------------------------------------


@dataclass
class BuildConfig:
    """Build configuration parameters."""

    platform: str = "ccu-dev"
    profile: str = "release"
    jobs: int = 1
    clean: bool = False
    dry_run: bool = False
    skip_recipes: bool = False
    # CCU-BUILD-DSN-CR-004 §11: explicit development exemption for builds
    # without a real vsomeip backend. Never allowed for a release profile.
    allow_missing_platform_assets: bool = False

    @property
    def build_type(self) -> str:
        return "Debug" if "debug" in self.profile.lower() else "Release"

    @property
    def is_host(self) -> bool:
        """True when building natively on the host (no cross-toolchain)."""
        return self.platform == "host"

    @property
    def is_target(self) -> bool:
        """True when cross-compiling for a target platform."""
        return not self.is_host

    @property
    def generator(self) -> str:
        return "Unix Makefiles" if self.is_host else "Ninja"

    @property
    def is_release(self) -> bool:
        return "release" in self.profile.lower()


@dataclass
class ComponentInstallResult:
    """Install result for a single install component."""

    name: str
    staging: str
    destdir: str
    status: str = "pending"
    installed_files: list[str] = field(default_factory=list)


@dataclass
class ServiceResult:
    """Build result for a single service."""

    id: str
    status: str = "pending"
    steps: dict[str, str] = field(default_factory=dict)
    targets: list[str] = field(default_factory=list)
    components: list[ComponentInstallResult] = field(default_factory=list)
    installed_files: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    error: str | None = None


@dataclass
class BuildReport:
    """Machine-readable build report."""

    version: str = "0.1.0"
    platform: str = ""
    profile: str = ""
    release_set: str | None = None
    services_requested: list[str] = field(default_factory=list)
    expanded_services: list[str] = field(default_factory=list)
    systemd_units: list[str] = field(default_factory=list)
    aggregation_status: str = "not_applicable"
    assembly_report_path: str | None = None
    reconcile: dict[str, Any] | None = None
    unit_check: dict[str, Any] | None = None
    target_dependencies: list[str] = field(default_factory=list)
    start_time: str = ""
    end_time: str = ""
    duration_seconds: float = 0.0
    status: str = "pending"
    service_results: list[ServiceResult] = field(default_factory=list)
    artifact_manifest_path: str | None = None
    config_overlay_path: str | None = None
    elf_check_violations: int = 0
    elf_check_warnings: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.to_json())


# ---------------------------------------------------------------------------
# Build orchestrator
# ---------------------------------------------------------------------------


class BuildOrchestrator:
    """Orchestrates the full CCU build pipeline."""

    def __init__(self, project: Project, config: BuildConfig | None = None):
        self.project = project
        self.config = config or BuildConfig()
        self.staging = StagingDir(project.root, self.config.platform, self.config.profile)

    # -- manifest loading and validation ----------------------------------

    def load_and_validate(self) -> tuple[ServiceManifest, ReleaseSetManifest]:
        """Load all manifests and run pre-build validation."""
        from .manifest import load_yaml

        svc_raw = load_yaml(self.project.service_manifest_path)
        rs_raw = load_yaml(self.project.release_set_manifest_path)
        lock_raw = load_yaml(self.project.dependency_lock_path)

        validate_service_manifest(svc_raw, self.project.root)
        validate_release_set_manifest(rs_raw, self.project.root)
        validate_dependency_lock(lock_raw, self.project.root)

        service_manifest = self.project.load_service_manifest()
        release_set_manifest = self.project.load_release_set_manifest()
        lock = self.project.load_dependency_lock()

        validate_all(service_manifest, self.project.root, lock)

        return service_manifest, release_set_manifest

    # -- ELF machine resolution -------------------------------------------

    def _expected_elf_machine(self) -> int | None:
        """Determine expected ELF machine from platform manifest architecture."""
        if self.config.is_host:
            import platform as _platform
            machine = _platform.machine().lower()
            return _ARCH_TO_ELF_MACHINE.get(machine)
        try:
            pm = self.project.load_platform_manifest()
            arch = (pm.architecture or "").lower()
            return _ARCH_TO_ELF_MACHINE.get(arch)
        except Exception:
            return None

    # -- CMake command builders -------------------------------------------

    def _configure_cmd(self, service: Service) -> list[str]:
        source_dir = self.project.root / service.effective_source_dir
        build_dir = self.staging.service_build_dir(service.id)
        build_dir.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            "cmake",
            "-S", str(source_dir),
            "-B", str(build_dir),
            "-G", self.config.generator,
            f"-DCMAKE_BUILD_TYPE={self.config.build_type}",
            "-DCMAKE_INSTALL_PREFIX=/usr",
            "-DCMAKE_INSTALL_SYSCONFDIR=/etc",
            "-DCMAKE_INSTALL_LOCALSTATEDIR=/var",
            f"-DCCU_ROOT={self.project.root}",
        ]

        if self.config.is_target:
            toolchain = self.project.toolchain_file_path
            cmd.append(f"-DCMAKE_TOOLCHAIN_FILE={toolchain}")
            sysroot = self.project.sysroot_path
            cmd.append(f"-DCCU_SYSROOT={sysroot}")
        else:
            # HOST self-verification (设计 §4/§8.4, CCU-BUILD-DSN-CR-002): no
            # TARGET toolchain is loaded. Inject an equivalent find-root set so
            # the build resolves installed packages from BUILD-managed staging
            # only -- SDK staging -> TARGET dependency staging -- with
            # PROGRAM=NEVER and LIBRARY/INCLUDE/PACKAGE=ONLY, so the build does
            # not fall back to host /usr/local, personal dirs or undeclared
            # packages.
            prefix_paths: list[str] = []
            root_paths: list[str] = []
            for dep_id in service.build.service_dependencies:
                sdk = self.staging.sdk_dir(dep_id)
                prefix_paths.append(str(sdk / "usr"))
                root_paths.append(str(sdk))
            if service.build.target_dependencies:
                dep = self.staging.dep_staging
                prefix_paths.append(str(dep / "usr"))
                root_paths.append(str(dep))
            if prefix_paths:
                cmd.append(f"-DCMAKE_PREFIX_PATH={';'.join(prefix_paths)}")
                cmd.append(f"-DCMAKE_FIND_ROOT_PATH={';'.join(root_paths)}")
                cmd.append("-DCMAKE_FIND_ROOT_PATH_MODE_PROGRAM=NEVER")
                cmd.append("-DCMAKE_FIND_ROOT_PATH_MODE_LIBRARY=ONLY")
                cmd.append("-DCMAKE_FIND_ROOT_PATH_MODE_INCLUDE=ONLY")
                cmd.append("-DCMAKE_FIND_ROOT_PATH_MODE_PACKAGE=ONLY")

        for key, value in service.build.cmake_cache_variables.items():
            cmd.append(f"-D{key}={value}")

        return cmd

    def _build_cmd(self, service: Service) -> list[str]:
        build_dir = self.staging.service_build_dir(service.id)
        cmd = [
            "cmake", "--build", str(build_dir),
            "--target", *service.build.targets,
        ]
        cmd.extend(["--", f"-j{self.config.jobs}"])
        return cmd

    def _install_cmds(self, service: Service) -> list[tuple[InstallComponent, list[str], Path]]:
        """Return [(component, cmd, destdir)] for each install component."""
        build_dir = self.staging.service_build_dir(service.id)
        result: list[tuple[InstallComponent, list[str], Path]] = []
        for component in service.build.install_components:
            destdir = self.staging.component_destdir(service.id, component.staging)
            cmd = [
                "cmake", "--install", str(build_dir),
                "--prefix", "/usr",
                "--component", component.name,
            ]
            result.append((component, cmd, destdir))
        return result

    def _cmake_env(self, service: Service | None = None) -> dict[str, str]:
        """Return environment variables required by CMake and the toolchain."""
        env: dict[str, str] = {
            "CCU_ROOT": str(self.project.root),
        }
        # CCU_DEP_STAGING is always set: the TARGET toolchain consumes it, and
        # HOST build logs record it (HOST find-root injection happens via
        # CMAKE_PREFIX_PATH in _configure_cmd, 设计 §4/§8.4).
        env["CCU_DEP_STAGING"] = str(self.staging.dep_staging)
        if self.config.is_target:
            env["CCU_SYSROOT"] = str(self.project.sysroot_path)
            env["PKG_CONFIG_SYSROOT_DIR"] = str(self.staging.dep_staging)
            # Platform manifest architecture for toolchain CMAKE_SYSTEM_PROCESSOR
            try:
                pm = self.project.load_platform_manifest()
                if pm.architecture and pm.architecture != "TBD":
                    env["CCU_TARGET_ARCH"] = pm.architecture
                if pm.cross_cc and pm.cross_cc != "TBD":
                    env["CCU_CROSS_CC"] = pm.cross_cc
                if pm.cross_cxx and pm.cross_cxx != "TBD":
                    env["CCU_CROSS_CXX"] = pm.cross_cxx
            except Exception:
                pass
        if service is not None and service.build.service_dependencies:
            first_dep = service.build.service_dependencies[0]
            sdk = self.staging.sdk_dir(first_dep)
            env["CCU_SDK_STAGING"] = str(sdk)
            sdk_dirs = [str(self.staging.sdk_dir(d)) for d in service.build.service_dependencies]
            sdk_dirs.reverse()
            env["CCU_SDK_STAGING_DIRS"] = ":".join(sdk_dirs)
        return env

    # -- subprocess execution ---------------------------------------------

    def _run(
        self,
        cmd: list[str],
        service_id: str,
        step: str,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        log_file = self.staging.service_log_file(service_id, step)
        log_file.parent.mkdir(parents=True, exist_ok=True)

        full_env = os.environ.copy()
        if env:
            full_env.update(env)

        with open(log_file, "w", encoding="utf-8") as log:
            log.write(f"$ {' '.join(cmd)}\n")
            log.write(f"env: DESTDIR={full_env.get('DESTDIR', '(unset)')}\n")
            log.write("---\n")
            log.flush()
            result = subprocess.run(
                cmd,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=full_env,
                cwd=str(self.project.root),
            )
        return result

    def _run_step(
        self,
        cmd: list[str],
        service: Service,
        step: str,
        env: dict[str, str] | None = None,
    ) -> str:
        if self.config.dry_run:
            print(f"  [DRY-RUN] {step}: {' '.join(cmd)}")
            return "skipped"
        print(f"  {step}: {' '.join(cmd[:3])}...")
        result = self._run(cmd, service.id, step, env=env)
        if result.returncode != 0:
            return "failed"
        return "success"

    # -- per-service build ------------------------------------------------

    def _build_service(
        self,
        service: Service,
        git_commit: str,
    ) -> ServiceResult:
        """Configure, build and install a single service."""
        result = ServiceResult(id=service.id, targets=list(service.build.targets))
        start = time.time()
        print(f"\n[{service.id}]")

        if self.config.clean:
            build_dir = self.staging.service_build_dir(service.id)
            if build_dir.exists() and not self.config.dry_run:
                shutil.rmtree(build_dir)

        # 1. Configure
        status = self._run_step(
            self._configure_cmd(service), service, "configure",
            env=self._cmake_env(service),
        )
        result.steps["configure"] = status
        if status == "failed":
            result.status = "failed"
            result.error = f"Configure failed, see {self.staging.service_log_file(service.id, 'configure')}"
            result.duration_seconds = time.time() - start
            return result

        # 2. Build (all targets)
        status = self._run_step(
            self._build_cmd(service), service, "build",
            env=self._cmake_env(service),
        )
        result.steps["build"] = status
        if status == "failed":
            result.status = "failed"
            result.error = f"Build failed, see {self.staging.service_log_file(service.id, 'build')}"
            result.duration_seconds = time.time() - start
            return result

        # 3. Install each component (DESTDIR computed from staging class)
        base_env = self._cmake_env(service)
        all_installed: list[str] = []
        for component, cmd, destdir in self._install_cmds(service):
            comp_result = ComponentInstallResult(
                name=component.name,
                staging=component.staging,
                destdir=str(destdir),
            )
            destdir.mkdir(parents=True, exist_ok=True)
            before = self._snapshot_under(destdir)
            env = dict(base_env)
            env["DESTDIR"] = str(destdir)
            step_name = f"install:{component.name}"
            status = self._run_step(cmd, service, step_name, env=env)
            result.steps[step_name] = status
            comp_result.status = status
            if status == "failed":
                result.status = "failed"
                result.error = (
                    f"Install component '{component.name}' failed, see "
                    f"{self.staging.service_log_file(service.id, step_name)}"
                )
                result.components.append(comp_result)
                result.duration_seconds = time.time() - start
                return result
            if not self.config.dry_run:
                new_files = self._new_files_under(destdir, before)
                manifest_files = self._component_install_manifest_files(
                    self.staging.service_build_dir(service.id), component, destdir
                )
                # CMake records the exact per-component install set in
                # install_manifest_<component>.txt. Using it (instead of only
                # the pre/post diff) keeps incremental builds correctly
                # attributed, so no install-root file degrades to an
                # "unknown" owner (CCU-BUILD-RELEASE-REQ-004).
                comp_result.installed_files = sorted(
                    set(new_files) | set(manifest_files)
                )
                all_installed.extend(comp_result.installed_files)
            result.components.append(comp_result)

        result.installed_files = sorted(all_installed)
        result.status = "success"
        result.duration_seconds = time.time() - start
        return result

    def _snapshot_under(self, root: Path) -> set[str]:
        result: set[str] = set()
        if not root.exists():
            return result
        for path in root.rglob("*"):
            if path.is_file() or path.is_symlink():
                result.add(str(path.relative_to(root)))
        return result

    def _new_files_under(self, root: Path, before: set[str]) -> list[str]:
        after = self._snapshot_under(root)
        return sorted(after - before)

    def _component_install_manifest_files(
        self, build_dir: Path, component: InstallComponent, destdir: Path
    ) -> list[str]:
        """Return the per-component install set recorded by CMake."""
        manifest = build_dir / f"install_manifest_{component.name}.txt"
        if not manifest.is_file():
            return []
        files: list[str] = []
        for line in manifest.read_text(encoding="utf-8").splitlines():
            entry = line.strip()
            if not entry:
                continue
            rel = entry.lstrip("/")
            if (destdir / rel).exists():
                files.append(rel)
        return sorted(set(files))

    # -- full pipeline ----------------------------------------------------

    def build(
        self,
        set_id: str | None = None,
        service_id: str | None = None,
    ) -> BuildReport:
        """Run the full build pipeline."""
        report = BuildReport(
            platform=self.config.platform,
            profile=self.config.profile,
            release_set=set_id,
            start_time=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        pipeline_start = time.time()

        try:
            # 1. Load and validate manifests
            print("=== Loading and validating manifests ===")
            service_manifest, release_set_manifest = self.load_and_validate()
            print(f"  Loaded {len(service_manifest)} service(s), "
                  f"{len(release_set_manifest)} release set(s)")

            # 2. Determine build order
            lock = self.project.load_dependency_lock()
            graph = DependencyGraph(service_manifest, lock)
            if service_id:
                report.services_requested = [service_id]
                order = graph.build_order([service_id])
            elif set_id:
                rs = release_set_manifest.get(set_id)
                if rs is None:
                    raise BuildFailure(f"Release set '{set_id}' not found")
                report.services_requested = list(rs.services)
                order = graph.build_order(rs.services)
            else:
                report.services_requested = graph.all_service_ids()
                order = graph.build_order()
            report.expanded_services = list(order)
            print(f"  Build order: {' -> '.join(order)}")

            if self.config.allow_missing_platform_assets and "release" in self.config.profile.lower():
                raise BuildFailure(
                    "--allow-missing-platform-assets is a development-only "
                    "exemption and must not be used for a release profile "
                    "(CCU-BUILD-DSN-CR-004 §11)"
                )

            # 3. Prepare staging
            print("=== Preparing staging ===")
            self.staging.prepare(clean=self.config.clean)
            print(f"  Staging: {self.staging.root}")

            # 4. Prepare TARGET dependencies (recipes)
            report.target_dependencies = self._prepare_target_dependencies(
                graph, report.services_requested
            )

            # 5. Build each service
            git_commit = get_git_commit(self.project.root)
            for sid in order:
                service = service_manifest.get(sid)
                if service is None:
                    raise BuildFailure(f"Service '{sid}' not found in manifest")
                result = self._build_service(service, git_commit)
                report.service_results.append(result)
                if result.status == "failed":
                    report.status = "failed"
                    report.errors.append(f"Service '{sid}' failed: {result.error}")
                    break

            # 5.6. Release-set assembly (target + platform runtime assets)
            assembly_report = None
            claimed_paths: dict[str, str] = {}
            if report.status != "failed":
                try:
                    assembly_report, claimed_paths = self._assemble_release_set(
                        service_manifest, set_id, order, report
                    )
                except AssemblyError as exc:
                    report.status = "failed"
                    report.errors.append(f"Release-set assembly failed: {exc}")
                    report.errors.extend(getattr(exc, "errors", []) or [])

            # 5.7. Apply platform config overlay (设计 §9.3/§9.6)
            overlay_report = None
            if report.status != "failed" and not self.config.dry_run:
                print("\n=== Applying platform config overlay ===")
                try:
                    overlay_report = self._apply_config_overlay(
                        service_manifest, report.service_results,
                        claimed_paths=claimed_paths,
                    )
                    report.config_overlay_path = str(
                        (self.staging.manifests_dir / "config-overlay-report.json")
                        .relative_to(self.project.root)
                    )
                except ConfigError as exc:
                    report.status = "failed"
                    report.errors.append(f"Config overlay failed: {exc}")
                    if exc.details:
                        report.errors.extend(exc.details)

            # 6. ELF / pollution check (only for target builds; host builds
            #    skip arch check since Mach-O/ELF depends on the host OS)
            if report.status != "failed" and not self.config.dry_run and self.config.is_target:
                print("\n=== ELF / pollution check ===")
                elf_results = self._run_elf_check()
                violations = sum(len(r.violations) for r in elf_results)
                warnings = sum(len(r.warnings) for r in elf_results)
                report.elf_check_violations = violations
                report.elf_check_warnings = warnings
                if violations > 0:
                    report.status = "failed"
                    for r in elf_results:
                        for v in r.violations:
                            report.errors.append(v)
                else:
                    print(f"  {len(elf_results)} file(s) checked, "
                          f"{violations} violation(s), {warnings} warning(s)")

            # 6.5. Unit check + manifest/install-root reconciliation (REQ-001/004)
            if report.status != "failed" and not self.config.dry_run:
                print("\n=== Unit and install-root reconciliation check ===")
                report.unit_check = self._run_unit_check()
                reconcile_report = self._reconcile(
                    service_manifest, order, report.service_results,
                    assembly_report, overlay_report,
                )
                report.reconcile = reconcile_report.to_dict()
                if report.unit_check.get("status") == "fail":
                    report.status = "failed"
                    report.errors.extend(report.unit_check.get("errors", []))
                if reconcile_report.errors:
                    report.status = "failed"
                    report.errors.extend(reconcile_report.errors)

            # 7. Artifact manifest (only if all checks passed)
            if report.status != "failed" and not self.config.dry_run:
                print("\n=== Generating artifact manifest ===")
                artifact_manifest = self._generate_artifact_manifest(
                    service_manifest, lock, git_commit, report.service_results,
                    overlay_report, assembly_report,
                )
                manifest_path = self.staging.manifests_dir / "artifact-manifest.json"
                artifact_manifest.save(manifest_path)
                report.artifact_manifest_path = str(
                    manifest_path.relative_to(self.project.root)
                )
                print(f"  {len(artifact_manifest.entries)} artifact(s) -> {manifest_path}")

            if report.status != "failed":
                report.status = "success"

        except CcuBuildError as exc:
            report.status = "failed"
            report.errors.append(str(exc))
        except Exception as exc:
            report.status = "failed"
            report.errors.append(f"Unexpected error: {exc}")
            if os.environ.get("CCU_BUILD_DEBUG"):
                import traceback
                traceback.print_exc()

        report.end_time = datetime.now(timezone.utc).isoformat(timespec="seconds")
        report.duration_seconds = time.time() - pipeline_start

        report_path = self.staging.manifests_dir / "build-report.json"
        if self.staging.manifests_dir.exists() or not self.config.dry_run:
            report.save(report_path)

        return report

    def _run_elf_check(self) -> list[ElfCheckResult]:
        """Run ELF/pollution check on staging with platform-appropriate machine."""
        expected_machine = self._expected_elf_machine()
        if expected_machine is None:
            # Cannot determine expected machine (TBD architecture or host);
            # check for Mach-O and build-tree pollution only, skip arch check.
            results: list[ElfCheckResult] = []
            for path in sorted(self.staging.install_root.rglob("*")):
                if not path.is_file():
                    continue
                from .elfcheck import ElfCheckResult, classify_file
                result = ElfCheckResult(path=path)
                result.classification = classify_file(path)
                cls = result.classification
                if cls.file_type == "macho":
                    result.violations.append(
                        f"Mach-O binary detected ({cls.macho_description}): {path}"
                    )
                results.append(result)
            return results
        return check_staging(
            self.staging.install_root,
            expected_machine=expected_machine,
            expected_class=_ELFCLASS64,
        )

    def _prepare_target_dependencies(
        self, graph: DependencyGraph, service_ids: list[str]
    ) -> list[str]:
        """Prepare TARGET dependencies (recipes) before services configure.

        Real recipe execution (building locked deps from source into
        CCU_DEP_STAGING) is a future CR; for now BUILD verifies controlled
        presence and lets each project's ``find_package`` fail-closed on a
        missing package (CR-002 option (a), 设计 §3/§8.3).
        """
        target_deps = graph.target_dependency_order(service_ids)
        if not target_deps:
            return []
        if self.config.dry_run or self.config.skip_recipes:
            print(f"=== TARGET dependencies: {target_deps} (dry-run/skip) ===")
            return target_deps

        # Fail-closed: declared TARGET dependencies must already be staged in
        # CCU_DEP_STAGING before any service configure. HOST dev/test stages
        # them via the framework's scripts/stage-yaml-cpp.sh; TARGET builds use
        # a BUILD controlled recipe (future CR).
        dep_usr = self.staging.dep_staging / "usr"
        staged = dep_usr.is_dir() and any(p.is_file() for p in dep_usr.rglob("*"))
        if not staged:
            raise BuildFailure(
                f"TARGET dependencies {target_deps} declared but CCU_DEP_STAGING "
                f"is empty ({dep_usr}). Stage controlled dependencies (e.g. "
                f"yaml-cpp via the framework's scripts/stage-yaml-cpp.sh for "
                f"HOST, or a BUILD recipe for TARGET) before building."
            )
        print(f"=== TARGET dependencies: {target_deps} (staged at {dep_usr}) ===")
        return target_deps

    def _build_config_file_owners(
        self, service_results: list[ServiceResult]
    ) -> dict[str, tuple[str, str, str]]:
        """Map install-root-relative paths to (service, component, sha256).

        Only rootfs-staged component outputs are considered; these are the
        pre-overlay installers of config files (设计 §9.3 step 4).
        """
        owners: dict[str, tuple[str, str, str]] = {}
        for result in service_results:
            for comp in result.components:
                if comp.staging != "rootfs":
                    continue
                for f in comp.installed_files:
                    full = self.staging.install_root / f
                    sha = sha256_file(full) if full.is_file() else ""
                    owners[f] = (result.id, comp.name, sha)
        return owners

    def _assemble_release_set(
        self,
        service_manifest: ServiceManifest,
        set_id: str | None,
        order: list[str],
        report: BuildReport,
    ) -> tuple[AssemblyReport | None, dict[str, str]]:
        """Generate the release-set aggregation target and platform assets."""
        print("\n=== Release-set assembly ===")
        assembler = ReleaseSetAssembler(
            project=self.project,
            staging=self.staging,
            service_manifest=service_manifest,
            allow_missing_assets=self.config.allow_missing_platform_assets,
        )
        if self.config.dry_run:
            plan = assembler.plan(set_id, order, require_baseline_in_payload=False)
            report.aggregation_status = plan.aggregation_status
            report.systemd_units = list(plan.ordered_units)
            print(f"  [DRY-RUN] aggregation: {plan.aggregation_status}")
            if plan.ordered_units:
                print(f"  [DRY-RUN] units: {' '.join(plan.ordered_units)}")
            for asset in plan.assets:
                print(f"  [DRY-RUN] asset: {asset.target} ({asset.validation})")
            for err in plan.errors:
                print(f"  [DRY-RUN] error: {err}")
            if plan.errors:
                raise AssemblyError(
                    f"Release-set assembly plan failed ({len(plan.errors)} "
                    f"error(s))",
                    plan.errors,
                )
            return None, plan.claimed_paths

        if set_id is None:
            removed = assembler.clean_previous()
            for item in removed:
                print(f"  removed stale: {item['path']} ({item['reason']})")
            report.aggregation_status = "not_applicable"
            print("  no release set selected: no aggregation target")
            return None, {}

        assembly_report = assembler.assemble(set_id, order)
        report.assembly_report_path = str(
            assembler.report_path.relative_to(self.project.root)
        )
        report.aggregation_status = assembly_report.aggregation_status
        report.systemd_units = list(assembly_report.ordered_units)
        print(f"  aggregation: {assembly_report.aggregation_status}"
              + (f" ({' '.join(assembly_report.ordered_units)})"
                 if assembly_report.ordered_units else ""))
        print(f"  platform assets: {len(assembly_report.assets)} installed, "
              f"{len(assembly_report.skipped)} skipped, "
              f"{len(assembly_report.removed)} stale removed")
        claimed = {
            path: "build:release-set-assembly"
            for path in assembly_report.build_owned_paths
        }
        return assembly_report, claimed

    def _run_unit_check(self) -> dict[str, Any]:
        """Run the equivalent systemd unit check on the final install-root."""
        result = check_install_root_units(self.staging.install_root)
        print(f"  {result.engine}: {result.status} "
              f"({len(result.checked)} unit file(s))")
        for err in result.errors:
            print(f"    FAIL: {err}")
        return result.to_dict()

    def _previous_owned_paths(self) -> set[str]:
        """Install-root paths owned by the previous build (artifact manifest)."""
        path = self.staging.manifests_dir / "artifact-manifest.json"
        if not path.is_file():
            return set()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return set()
        owned: set[str] = set()
        for artifact in data.get("artifacts", []) or []:
            rel = str(artifact.get("path", ""))
            owner = str(artifact.get("owner_service", ""))
            if owner == "unknown" or not rel.startswith("rootfs/"):
                continue
            owned.add(rel[len("rootfs/"):])
        return owned

    def _previous_artifact_entries(self) -> dict[str, dict[str, Any]]:
        """Previous artifact manifest entries keyed by ``rootfs/<rel>`` path.

        Used as an ownership fallback so an incremental build never degrades
        an already-attributed release asset to an ``unknown`` owner when a
        release set shrinks (CCU-BUILD-RELEASE-REQ-004); those files are also
        reported as stale by the reconciliation step.
        """
        path = self.staging.manifests_dir / "artifact-manifest.json"
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        entries: dict[str, dict[str, Any]] = {}
        for artifact in data.get("artifacts", []) or []:
            rel = str(artifact.get("path", ""))
            if not rel.startswith("rootfs/"):
                continue
            if str(artifact.get("owner_service", "")) in ("", "unknown"):
                continue
            entries[rel] = artifact
        return entries

    def _reconcile(
        self,
        service_manifest: ServiceManifest,
        order: list[str],
        service_results: list[ServiceResult],
        assembly_report: AssemblyReport | None,
        overlay_report,
    ) -> ReconcileReport:
        """Bidirectional manifest/install-root reconciliation (REQ-001)."""
        owned: set[str] = set()
        for result in service_results:
            for comp in result.components:
                if comp.staging != "rootfs":
                    continue
                owned.update(comp.installed_files)
        if assembly_report is not None:
            owned.update(assembly_report.build_owned_paths.keys())
        if overlay_report is not None:
            for rec in overlay_report.overlaid:
                owned.add(rec.logical_path.lstrip("/"))

        staged = self.staging.scan_files()

        # Config paths whose only provider is a BUILD platform overlay source
        # that does not exist for this platform (e.g. native HOST build where
        # configs/host/rootfs is absent, CR-003 §9.8) are skipped instead of
        # reported as missing; service-default-installed paths are always
        # checked.
        platform_rootfs = self.project.platform_configs_root()
        overlay_sources: set[str] = set()
        if platform_rootfs.is_dir():
            for path in platform_rootfs.rglob("*"):
                if path.is_file():
                    overlay_sources.add(
                        str(path.relative_to(platform_rootfs)).replace(os.sep, "/")
                    )
        provided: set[str] = set()
        for service_id in order:
            svc = service_manifest.get(service_id)
            if svc is None:
                continue
            cv = svc.runtime.config_validation
            if cv is not None and cv.target_path:
                provided.add(cv.target_path)
        skip_config_paths: set[str] = set()
        for service_id in order:
            svc = service_manifest.get(service_id)
            if svc is None:
                continue
            for logical in svc.runtime.config_paths:
                if logical in provided or logical.lstrip("/") in overlay_sources:
                    continue
                skip_config_paths.add(logical)
        if skip_config_paths:
            print("  config paths with absent BUILD overlay source (skipped): "
                  + ", ".join(sorted(skip_config_paths)))

        report = reconcile_install_root(
            service_manifest=service_manifest,
            services=order,
            install_root=self.staging.install_root,
            staged_files=staged,
            owned_paths=owned,
            previous_owned_paths=self._previous_owned_paths(),
            skip_config_paths=skip_config_paths,
        )
        print(f"  {report.status}: {len(report.checked_units)} unit(s), "
              f"{len(report.checked_configs)} config path(s), "
              f"{len(report.unknown_assets)} unowned asset(s)")
        for warning in report.warnings:
            print(f"    WARN: {warning}")
        return report

    def _apply_config_overlay(
        self,
        service_manifest: ServiceManifest,
        service_results: list[ServiceResult],
        claimed_paths: dict[str, str] | None = None,
    ):
        """Apply the platform config overlay to install-root (设计 §9.3)."""
        deployment = self.project.load_config_deployment_manifest()
        file_owners = self._build_config_file_owners(service_results)
        overlay = ConfigOverlay(
            platform=self.config.platform,
            platform_rootfs=self.project.platform_configs_root(),
            install_root=self.staging.install_root,
            deployment=deployment,
            service_manifest=service_manifest,
            project_root=self.project.root,
            state_dir=self.staging.manifests_dir,
            claimed_paths=claimed_paths,
        )
        report = overlay.apply(file_owners)
        overlay.check_report(report)
        report.save(self.staging.manifests_dir / "config-overlay-report.json")
        print(f"  {len(report.overlaid)} file(s) overlaid, "
              f"{len(report.removed)} removed, {len(report.skipped)} skipped")
        return report

    def _generate_artifact_manifest(
        self,
        service_manifest: ServiceManifest,
        lock,
        git_commit: str,
        service_results: list[ServiceResult],
        overlay_report=None,
        assembly_report: AssemblyReport | None = None,
    ) -> ArtifactManifest:
        """Generate the artifact manifest from staging files."""
        platform_manifest = self.project.load_platform_manifest()
        sysroot_manifest = self.project.load_sysroot_manifest()

        manifest = ArtifactManifest(
            platform=self.config.platform,
            profile=self.config.profile,
            platform_manifest=platform_manifest,
            sysroot_manifest=sysroot_manifest,
            dependency_lock=lock,
        )

        # Consumer evidence (设计 §8.6): map each SDK-providing service to the
        # downstream services that consume it via service_dependencies.
        sdk_consumers: dict[str, list[str]] = {}
        for svc in service_manifest:
            for dep in svc.build.service_dependencies:
                sdk_consumers.setdefault(dep, []).append(svc.id)
        manifest.sdk_consumers = {k: sorted(v) for k, v in sdk_consumers.items()}

        # Build a map of logical rel path -> (service_id, target, version, component, staging)
        file_owners: dict[str, tuple[str, str, str, str, str]] = {}
        for result in service_results:
            svc = service_manifest.get(result.id)
            if svc is None:
                continue
            target_str = ",".join(svc.build.targets)
            comp_by_file: dict[str, ComponentInstallResult] = {}
            for comp in result.components:
                for f in comp.installed_files:
                    comp_by_file[f] = comp
            for path in result.installed_files:
                comp = comp_by_file.get(path)
                comp_name = comp.name if comp else "unknown"
                comp_staging = comp.staging if comp else "rootfs"
                if comp_staging == "sdk":
                    rel_key = f"sdk/{result.id}/{path}"
                else:
                    rel_key = f"rootfs/{path}"
                file_owners[rel_key] = (
                    result.id, target_str, "0.1.0", comp_name, comp_staging,
                )

        previous_entries = self._previous_artifact_entries()
        overlay_owned: dict[str, Any] = {}
        if overlay_report is not None:
            for rec in overlay_report.overlaid:
                overlay_owned[rec.logical_path.lstrip("/")] = rec
        for staged in self.staging.scan_files():
            owner_info = file_owners.get(staged.rel_path)
            build_prov = None
            if assembly_report is not None and staged.rel_path.startswith("rootfs/"):
                build_prov = assembly_report.build_owned_paths.get(
                    staged.rel_path[len("rootfs/"):]
                )
            previous = previous_entries.get(staged.rel_path)
            if build_prov is not None:
                svc_id, target, version, component, staging = (
                    "build",
                    RELEASE_SET_OWNER_TARGET,
                    "0.1.0",
                    RELEASE_SET_COMPONENT,
                    "rootfs",
                )
            elif owner_info:
                svc_id, target, version, component, staging = owner_info
            elif previous is not None:
                # Keep a previously attributed asset owned instead of `unknown`
                svc_id = str(previous.get("owner_service", "unknown"))
                target = str(previous.get("owner_target", "unknown"))
                version = str(previous.get("version", "0.1.0"))
                component = str(previous.get("install_component", "unknown"))
                staging = str(previous.get("staging", "rootfs"))
            elif staged.rel_path.startswith("rootfs/") and staged.rel_path[len("rootfs/"):] in overlay_owned:
                # BUILD platform overlay output without a service installer
                # (e.g. /etc/ccu/common.yaml) is BUILD-owned (§9.6).
                svc_id, target, version, component, staging = (
                    "build", "config-overlay", "0.1.0", "config-overlay", "rootfs",
                )
            else:
                svc_id, target, version, component, staging = (
                    "unknown", "unknown", "unknown", "unknown", "unknown",
                )
            staged.owner_service = svc_id
            manifest.add_staged_file(
                staged,
                owner_service=svc_id,
                owner_target=target,
                version=version,
                git_commit=git_commit,
                install_component=component,
                staging=staging,
                source=build_prov["source"] if build_prov else "",
                source_sha256=build_prov["source_sha256"] if build_prov else "",
                mode=build_prov["mode"] if build_prov else "",
                referenced_by=(
                    [r for r in build_prov.get("referenced_by", "").split(",") if r]
                    if build_prov else []
                ),
            )

        if assembly_report is not None and assembly_report.release_set is not None:
            manifest.release_set = {
                "id": assembly_report.release_set,
                "requested_services": list(assembly_report.requested_services),
                "expanded_services": list(assembly_report.expanded_services),
                "systemd_units": list(assembly_report.ordered_units),
                "aggregation_status": assembly_report.aggregation_status,
            }

        manifest.check_conflicts()
        if overlay_report is not None:
            manifest.attach_config_provenance(overlay_report)
        return manifest
