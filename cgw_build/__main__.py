"""CGW Build CLI entry point.

Usage::

    python3 -m cgw_build <command> [options]

Commands:
    validate   Validate manifests (schema, graph, cross-references)
    build      Configure, build, install, ELF-check and generate manifest
    package    Create a release package from staging
    deploy     Deploy a package to a target device
    verify     Verify staging output or a release package
    elfcheck   Run ELF/pollution checks on staging
    platforms  List available platform manifests
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .manifest import Project, load_yaml
from .schema import (
    validate_service_manifest,
    validate_release_set_manifest,
    validate_dependency_lock,
    validate_platform_manifest,
    validate_config_deployment_manifest,
)
from .validator import validate_all
from .graph import DependencyGraph
from .orchestrator import BuildOrchestrator, BuildConfig
from .packaging import Packager
from .deploy import Deployer
from .verify import Verifier
from .elfcheck import check_staging
from .artifact import get_git_commit


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--project-root", "-r",
        default=None,
        help="Path to the cgw-build project root (default: current directory)",
    )
    parser.add_argument(
        "--platform", "-p",
        default="cgw-dev",
        help="Target platform (default: cgw-dev; use 'host' for native builds)",
    )


def _get_project(args: argparse.Namespace) -> Project:
    return Project(args.project_root, platform=args.platform)


def cmd_validate(args: argparse.Namespace) -> int:
    """Validate manifests."""
    project = _get_project(args)
    print("=== Validating manifests ===")

    # Platform manifest
    from .manifest import load_yaml
    if project.platform != "host":
        pm_data = load_yaml(project.platform_manifest_path)
        try:
            validate_platform_manifest(pm_data, project.root)
            print(f"  [PASS] Platform manifest schema validation ({project.platform})")
        except Exception as exc:
            print(f"  [FAIL] Platform manifest schema: {exc}")
            return 1
    else:
        print(f"  [PASS] Platform: host (native, no platform manifest)")

    # Service manifest
    svc_raw = load_yaml(project.service_manifest_path)
    try:
        validate_service_manifest(svc_raw, project.root)
        print(f"  [PASS] Service manifest schema validation")
    except Exception as exc:
        print(f"  [FAIL] Service manifest schema: {exc}")
        return 1

    # Release-set manifest
    rs_raw = load_yaml(project.release_set_manifest_path)
    try:
        validate_release_set_manifest(rs_raw, project.root)
        print(f"  [PASS] Release-set manifest schema validation")
    except Exception as exc:
        print(f"  [FAIL] Release-set manifest schema: {exc}")
        return 1

    # Dependency lock
    lock_raw = load_yaml(project.dependency_lock_path)
    try:
        validate_dependency_lock(lock_raw, project.root)
        print(f"  [PASS] Dependency lock schema validation")
    except Exception as exc:
        print(f"  [FAIL] Dependency lock schema: {exc}")
        return 1

    # Config deployment manifest (CR-003 §9.4)
    cd_path = project.config_deployment_manifest_path
    if cd_path.is_file():
        cd_raw = load_yaml(cd_path)
        try:
            validate_config_deployment_manifest(cd_raw, project.root)
            print(f"  [PASS] Config deployment manifest schema validation")
        except Exception as exc:
            print(f"  [FAIL] Config deployment manifest schema: {exc}")
            return 1
    else:
        print(f"  [PASS] Config deployment manifest: (none)")

    # Load and cross-reference validation
    service_manifest = project.load_service_manifest()
    release_set_manifest = project.load_release_set_manifest()
    print(f"  Services: {list(service_manifest.services.keys())}")
    print(f"  Release sets: {list(release_set_manifest.release_sets.keys())}")

    try:
        validate_all(service_manifest, project.root)
        print(f"  [PASS] Cross-reference validation (deps, systemd, health/smoke)")
    except Exception as exc:
        print(f"  [FAIL] Cross-reference validation: {exc}")
        return 1

    # Topological sort
    lock = project.load_dependency_lock()
    graph = DependencyGraph(service_manifest, lock)
    try:
        order = graph.build_order()
        print(f"  [PASS] Topological sort: {' -> '.join(order)}")
    except Exception as exc:
        print(f"  [FAIL] Topological sort: {exc}")
        return 1

    # Target dependency set
    try:
        target_deps = sorted(graph.target_dependency_set())
        if target_deps:
            print(f"  [PASS] TARGET dependencies: {target_deps}")
        else:
            print(f"  [PASS] TARGET dependencies: (none)")
    except Exception as exc:
        print(f"  [FAIL] TARGET dependency resolution: {exc}")
        return 1

    # Platform manifest summary
    pm = project.load_platform_manifest()
    print(f"  Platform: {pm.platform}, version: {pm.version}, "
          f"arch: {pm.architecture}, sysroot: {pm.sysroot_id}")

    print("\nAll validations passed.")
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    """Build services."""
    project = _get_project(args)
    config = BuildConfig(
        platform=args.platform,
        profile=args.profile,
        jobs=args.jobs,
        clean=args.clean,
        dry_run=args.dry_run,
    )
    orch = BuildOrchestrator(project, config)

    set_id = args.set
    service_id = args.service

    if not set_id and not service_id and not args.all:
        print("Error: must specify --set, --service, or --all")
        return 1

    report = orch.build(set_id=set_id, service_id=service_id)

    print(f"\n=== Build Report ===")
    print(f"Status: {report.status}")
    print(f"Duration: {report.duration_seconds:.1f}s")
    for sr in report.service_results:
        print(f"  [{sr.id}] {sr.status} ({sr.duration_seconds:.1f}s)")
        for step, status in sr.steps.items():
            print(f"    {step}: {status}")
        if sr.installed_files:
            print(f"    Installed: {len(sr.installed_files)} file(s)")
    if report.errors:
        print(f"\nErrors:")
        for err in report.errors:
            print(f"  - {err}")

    return 0 if report.status == "success" else 1


def cmd_package(args: argparse.Namespace) -> int:
    """Create a release package."""
    project = _get_project(args)
    from .staging import StagingDir

    staging = StagingDir(project.root, args.platform, args.profile)
    packager = Packager(staging)
    git_commit = get_git_commit(project.root)

    try:
        pkg_path = packager.create_package(
            name=args.name,
            version=args.version,
            git_commit=git_commit,
        )
        print(f"\nPackage: {pkg_path}")
        return 0
    except Exception as exc:
        print(f"Packaging failed: {exc}")
        return 1


def cmd_deploy(args: argparse.Namespace) -> int:
    """Deploy a package."""
    project = _get_project(args)
    deployer = Deployer(
        project,
        target_host=args.host,
        target_user=args.user,
        identity=args.identity,
        port=args.port,
        use_sudo=not args.no_sudo,
        rollback_on_failure=not args.no_rollback,
        platform=args.platform,
    )

    package_path = Path(args.package)

    if args.rollback:
        report = deployer.rollback(args.rollback)
    else:
        execute = args.execute and not args.dry_run
        report = deployer.deploy(package_path, execute=execute)

    print(f"Deploy status: {report.status}  (target: {report.target_host})")
    for step in report.steps:
        print(f"  [{step.name}] {step.status}"
              + (f": {step.message}" if step.message else ""))
        if step.status == "planned":
            for cmd in step.commands:
                print(f"      $ {cmd}")
    if report.errors:
        print("Errors:")
        for err in report.errors:
            print(f"  - {err}")
    return 0 if "success" in report.status else 1


def cmd_verify(args: argparse.Namespace) -> int:
    """Verify staging or package."""
    project = _get_project(args)
    from .staging import StagingDir

    staging = StagingDir(project.root, args.platform, args.profile)
    verifier = Verifier(staging)

    if args.package:
        result = verifier.verify_package(Path(args.package))
    else:
        result = verifier.verify_staging()

    print(f"Verify status: {result.status}")
    for check in result.checks:
        status_icon = "PASS" if check["status"] == "success" else "FAIL"
        print(f"  [{status_icon}] [{check['name']}] {check['message']}")
    if result.errors:
        print("Errors:")
        for err in result.errors:
            print(f"  - {err}")
    return 0 if result.status == "success" else 1


def cmd_elfcheck(args: argparse.Namespace) -> int:
    """Run ELF/pollution checks on staging."""
    project = _get_project(args)
    from .staging import StagingDir

    staging = StagingDir(project.root, args.platform, args.profile)
    if not staging.install_root.exists():
        print(f"Staging install-root not found: {staging.install_root}")
        return 1

    results = check_staging(staging.install_root)
    total_violations = 0
    total_warnings = 0
    for r in results:
        if r.violations or r.warnings:
            print(f"\n{r.path}:")
            for v in r.violations:
                print(f"  VIOLATION: {v}")
                total_violations += 1
            for w in r.warnings:
                print(f"  WARNING: {w}")
                total_warnings += 1

    print(f"\n{len(results)} file(s) checked, "
          f"{total_violations} violation(s), {total_warnings} warning(s)")

    if total_violations > 0:
        return 1
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    """Apply platform config overlay to staging install-root (设计 §9.3)."""
    project = _get_project(args)
    from .staging import StagingDir
    from .config import ConfigOverlay
    from .errors import CgwBuildError

    staging = StagingDir(project.root, args.platform, args.profile)
    if not staging.install_root.exists():
        print(f"Staging install-root not found: {staging.install_root}")
        print("Run a build first.")
        return 1

    sm = project.load_service_manifest()
    deployment = project.load_config_deployment_manifest()
    overlay = ConfigOverlay(
        platform=args.platform,
        platform_rootfs=project.platform_configs_root(),
        install_root=staging.install_root,
        deployment=deployment,
        service_manifest=sm,
        project_root=project.root,
        state_dir=staging.manifests_dir,
    )
    report = overlay.apply()
    try:
        overlay.check_report(report)
    except CgwBuildError as exc:
        print(f"Config overlay failed: {exc}")
        for d in getattr(exc, "details", []):
            print(f"  - {d}")
        return 1
    report.save(staging.manifests_dir / "config-overlay-report.json")
    print(f"Config overlay: {len(report.overlaid)} overlaid, "
          f"{len(report.removed)} removed, {len(report.skipped)} skipped")
    for r in report.overlaid:
        print(f"  {r.logical_path}: {r.deploy_policy} "
              f"val={r.validation['status']} secret={r.secret_scan['status']}")
    return 0


def cmd_platforms(args: argparse.Namespace) -> int:
    """List available platform manifests."""
    project = _get_project(args)
    platforms = project.available_platforms()
    if not platforms:
        print("No platform manifests found in manifests/platforms/")
        return 1
    print(f"Available platforms ({len(platforms)}):")
    for p in platforms:
        pm = load_platform_manifest_safe(project.platforms_dir / f"{p}.yaml")
        arch = pm.get("architecture", "?") if pm else "?"
        has_tbd = any(v == "TBD" for v in (pm or {}).values() if isinstance(v, str))
        tbd_marker = " (has TBD)" if has_tbd else ""
        print(f"  {p}: arch={arch}{tbd_marker}")
    return 0


def load_platform_manifest_safe(path: Path):
    from .manifest import load_yaml
    try:
        return load_yaml(path)
    except Exception:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cgw_build",
        description=f"CGW Build Orchestrator v{__version__}",
    )
    _add_common_args(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    # validate
    p_validate = subparsers.add_parser("validate", help="Validate manifests")
    _add_common_args(p_validate)
    p_validate.set_defaults(func=cmd_validate)

    # build
    p_build = subparsers.add_parser("build", help="Build services")
    _add_common_args(p_build)
    p_build.add_argument("--profile", default="release", help="Build profile (debug/release)")
    p_build.add_argument("--set", default=None, help="Release set ID")
    p_build.add_argument("--service", default=None, help="Single service ID")
    p_build.add_argument("--all", action="store_true", help="Build all services")
    p_build.add_argument("--jobs", "-j", type=int, default=1, help="Parallel jobs")
    p_build.add_argument("--clean", action="store_true", help="Clean build")
    p_build.add_argument("--dry-run", action="store_true", help="Print commands without executing")
    p_build.set_defaults(func=cmd_build)

    # package
    p_package = subparsers.add_parser("package", help="Create a release package")
    _add_common_args(p_package)
    p_package.add_argument("--profile", default="release")
    p_package.add_argument("--name", default="cgw-dev")
    p_package.add_argument("--version", default="0.1.0-draft")
    p_package.set_defaults(func=cmd_package)

    # deploy
    p_deploy = subparsers.add_parser("deploy", help="Deploy a package to a device (dry-run by default)")
    _add_common_args(p_deploy)
    p_deploy.add_argument("package", help="Path to the package file")
    p_deploy.add_argument("--host", default=None, help="Target device host/IP")
    p_deploy.add_argument("--user", default="cgw", help="Target SSH user (default: cgw)")
    p_deploy.add_argument("--identity", "-i", default=None, help="SSH private key path")
    p_deploy.add_argument("--port", type=int, default=22, help="SSH port (default: 22)")
    p_deploy.add_argument("--no-sudo", action="store_true", help="Do not prefix remote commands with sudo")
    p_deploy.add_argument("--no-rollback", action="store_true",
                          help="Do not auto-rollback on failure")
    p_deploy.add_argument("--execute", action="store_true",
                          help="Perform the real deployment (default is dry-run plan only)")
    p_deploy.add_argument("--dry-run", action="store_true",
                          help="Force dry-run (print the command plan; overrides --execute)")
    p_deploy.add_argument("--rollback", default=None, metavar="BACKUP",
                          help="Roll back the device to the given on-device backup archive path")
    p_deploy.set_defaults(func=cmd_deploy)

    # verify
    p_verify = subparsers.add_parser("verify", help="Verify staging or package")
    _add_common_args(p_verify)
    p_verify.add_argument("--profile", default="release")
    p_verify.add_argument("--package", default=None, help="Package path to verify")
    p_verify.set_defaults(func=cmd_verify)

    # elfcheck
    p_elfcheck = subparsers.add_parser("elfcheck", help="Run ELF/pollution checks")
    _add_common_args(p_elfcheck)
    p_elfcheck.add_argument("--profile", default="release")
    p_elfcheck.set_defaults(func=cmd_elfcheck)

    # config
    p_config = subparsers.add_parser("config", help="Apply platform config overlay to staging")
    _add_common_args(p_config)
    p_config.add_argument("--profile", default="release")
    p_config.set_defaults(func=cmd_config)

    # platforms
    p_platforms = subparsers.add_parser("platforms", help="List available platform manifests")
    _add_common_args(p_platforms)
    p_platforms.set_defaults(func=cmd_platforms)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
