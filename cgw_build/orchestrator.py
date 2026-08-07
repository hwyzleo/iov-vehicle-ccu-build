"""Build orchestration for CGW Build.

Ties together manifest loading, validation, dependency-graph ordering,
TARGET dependency recipes, CMake configure/build/install (multi-target,
multi-component with SDK/rootfs staging), ELF checking and artifact
manifest generation into a single pipeline.

平台参数化：platform 从配置传入，toolchain/sysroot 从平台清单动态
确定，不硬编码任何平台常量。

Usage::

    orch = BuildOrchestrator(project, config=BuildConfig(platform="cgw-dev"))
    report = orch.build(set_id="cgw-dev-minimal")
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
from .elfcheck import check_staging, ElfCheckResult, EM_AARCH64, EM_X86_64, _ELFCLASS64, _MACHINE_NAMES
from .errors import BuildFailure, CgwBuildError
from .graph import DependencyGraph
from .manifest import (
    Project,
    Service,
    ServiceManifest,
    ReleaseSetManifest,
    InstallComponent,
)
from .schema import validate_service_manifest, validate_release_set_manifest, validate_dependency_lock
from .staging import StagingDir
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

    platform: str = "cgw-dev"
    profile: str = "release"
    jobs: int = 1
    clean: bool = False
    dry_run: bool = False
    skip_recipes: bool = False

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
    target_dependencies: list[str] = field(default_factory=list)
    start_time: str = ""
    end_time: str = ""
    duration_seconds: float = 0.0
    status: str = "pending"
    service_results: list[ServiceResult] = field(default_factory=list)
    artifact_manifest_path: str | None = None
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
    """Orchestrates the full CGW build pipeline."""

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
            f"-DCGW_ROOT={self.project.root}",
        ]

        if self.config.is_target:
            toolchain = self.project.toolchain_file_path
            cmd.append(f"-DCMAKE_TOOLCHAIN_FILE={toolchain}")
            sysroot = self.project.sysroot_path
            cmd.append(f"-DCGW_SYSROOT={sysroot}")
        else:
            # HOST self-verification (设计 §4/§8.4, CGW-BUILD-DSN-CR-002): no
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
            "CGW_ROOT": str(self.project.root),
        }
        # CGW_DEP_STAGING is always set: the TARGET toolchain consumes it, and
        # HOST build logs record it (HOST find-root injection happens via
        # CMAKE_PREFIX_PATH in _configure_cmd, 设计 §4/§8.4).
        env["CGW_DEP_STAGING"] = str(self.staging.dep_staging)
        if self.config.is_target:
            env["CGW_SYSROOT"] = str(self.project.sysroot_path)
            env["PKG_CONFIG_SYSROOT_DIR"] = str(self.staging.dep_staging)
            # Platform manifest architecture for toolchain CMAKE_SYSTEM_PROCESSOR
            try:
                pm = self.project.load_platform_manifest()
                if pm.architecture and pm.architecture != "TBD":
                    env["CGW_TARGET_ARCH"] = pm.architecture
                if pm.cross_cc and pm.cross_cc != "TBD":
                    env["CGW_CROSS_CC"] = pm.cross_cc
                if pm.cross_cxx and pm.cross_cxx != "TBD":
                    env["CGW_CROSS_CXX"] = pm.cross_cxx
            except Exception:
                pass
        if service is not None and service.build.service_dependencies:
            first_dep = service.build.service_dependencies[0]
            sdk = self.staging.sdk_dir(first_dep)
            env["CGW_SDK_STAGING"] = str(sdk)
            sdk_dirs = [str(self.staging.sdk_dir(d)) for d in service.build.service_dependencies]
            sdk_dirs.reverse()
            env["CGW_SDK_STAGING_DIRS"] = ":".join(sdk_dirs)
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
                comp_result.installed_files = sorted(new_files)
                all_installed.extend(new_files)
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
            print(f"  Build order: {' -> '.join(order)}")

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

            # 7. Artifact manifest (only if all checks passed)
            if report.status != "failed" and not self.config.dry_run:
                print("\n=== Generating artifact manifest ===")
                artifact_manifest = self._generate_artifact_manifest(
                    service_manifest, lock, git_commit, report.service_results
                )
                manifest_path = self.staging.manifests_dir / "artifact-manifest.json"
                artifact_manifest.save(manifest_path)
                report.artifact_manifest_path = str(
                    manifest_path.relative_to(self.project.root)
                )
                print(f"  {len(artifact_manifest.entries)} artifact(s) -> {manifest_path}")

            if report.status != "failed":
                report.status = "success"

        except CgwBuildError as exc:
            report.status = "failed"
            report.errors.append(str(exc))
        except Exception as exc:
            report.status = "failed"
            report.errors.append(f"Unexpected error: {exc}")

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
        CGW_DEP_STAGING) is a future CR; for now BUILD verifies controlled
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
        # CGW_DEP_STAGING before any service configure. HOST dev/test stages
        # them via the framework's scripts/stage-yaml-cpp.sh; TARGET builds use
        # a BUILD controlled recipe (future CR).
        dep_usr = self.staging.dep_staging / "usr"
        staged = dep_usr.is_dir() and any(p.is_file() for p in dep_usr.rglob("*"))
        if not staged:
            raise BuildFailure(
                f"TARGET dependencies {target_deps} declared but CGW_DEP_STAGING "
                f"is empty ({dep_usr}). Stage controlled dependencies (e.g. "
                f"yaml-cpp via the framework's scripts/stage-yaml-cpp.sh for "
                f"HOST, or a BUILD recipe for TARGET) before building."
            )
        print(f"=== TARGET dependencies: {target_deps} (staged at {dep_usr}) ===")
        return target_deps

    def _generate_artifact_manifest(
        self,
        service_manifest: ServiceManifest,
        lock,
        git_commit: str,
        service_results: list[ServiceResult],
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

        for staged in self.staging.scan_files():
            owner_info = file_owners.get(staged.rel_path)
            if owner_info:
                svc_id, target, version, component, staging = owner_info
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
            )

        manifest.check_conflicts()
        return manifest
