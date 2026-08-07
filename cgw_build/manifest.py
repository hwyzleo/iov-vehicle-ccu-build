"""Manifest loading and data model for CGW Build.

Loads and parses platform manifest, sysroot manifest, service manifest,
release-set manifest and dependency lock from YAML files into typed
dataclass objects.

平台参数化：Project 从 manifests/platforms/<platform>.yaml 动态加载
平台清单，不硬编码任何平台常量。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import ManifestError, SchemaValidationError


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class InstallComponent:
    """A single install component declaration.

    ``name`` is the CMake install component name; ``staging`` is the
    BUILD-managed destination classification (``sdk`` or ``rootfs``).
    """

    name: str
    staging: str  # "sdk" | "rootfs"


@dataclass
class BuildConfig:
    """Build configuration for a single service.

    Canonical (plural) fields only:

      * ``targets`` - CMake build targets, executed in declared order.
      * ``install_components`` - {name, staging} component objects.
      * ``service_dependencies`` - service ids forming the build topology.
      * ``target_dependencies`` - lock dependency names triggering recipes.
    """

    preset: str
    targets: list[str] = field(default_factory=list)
    install_components: list[InstallComponent] = field(default_factory=list)
    service_dependencies: list[str] = field(default_factory=list)
    target_dependencies: list[str] = field(default_factory=list)
    cmake_cache_variables: dict[str, str] = field(default_factory=dict)
    _legacy_dependencies: list[str] = field(default_factory=list)

    @property
    def has_legacy_dependencies(self) -> bool:
        return bool(self._legacy_dependencies)


@dataclass
class RuntimeConfig:
    """Runtime configuration for a single service."""

    systemd_units: list[str] = field(default_factory=list)
    after: list[str] = field(default_factory=list)
    health_check: str | None = None
    smoke_test: str | None = None
    config_paths: list[str] = field(default_factory=list)
    persistent_paths: list[str] = field(default_factory=list)
    required_devices: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)


@dataclass
class Service:
    """A single CGW service entry."""

    id: str
    repository: str
    build: BuildConfig
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    revision: str | None = None
    source_dir: str | None = None
    description: str | None = None
    kind: str = "daemon"

    @property
    def effective_source_dir(self) -> str:
        """Source directory relative to project root."""
        return self.source_dir or self.repository

    @property
    def is_library(self) -> bool:
        return self.kind == "library"


@dataclass
class ServiceManifest:
    """Collection of all declared services."""

    services: dict[str, Service]

    def get(self, service_id: str) -> Service | None:
        return self.services.get(service_id)

    def __len__(self) -> int:
        return len(self.services)

    def __contains__(self, service_id: str) -> bool:
        return service_id in self.services

    def __iter__(self):
        return iter(self.services.values())


@dataclass
class ReleaseSet:
    """A single release set."""

    id: str
    services: list[str]
    description: str | None = None
    platform: str | None = None
    profile: str | None = None


@dataclass
class ReleaseSetManifest:
    """Collection of all declared release sets."""

    release_sets: dict[str, ReleaseSet]

    def get(self, set_id: str) -> ReleaseSet | None:
        return self.release_sets.get(set_id)

    def __len__(self) -> int:
        return len(self.release_sets)

    def __iter__(self):
        return iter(self.release_sets.values())


@dataclass
class PlatformManifest:
    """Platform baseline manifest (platforms/<platform>.yaml).

    平台参数化：所有平台属性从清单数据动态读取，不硬编码。
    """

    data: dict[str, Any]

    @property
    def platform(self) -> str:
        return self.data.get("platform", "")

    @property
    def version(self) -> str:
        return self.data.get("version", "")

    @property
    def architecture(self) -> str:
        return self.data.get("architecture", "")

    @property
    def target_triple(self) -> str:
        return self.data.get("target_triple", "")

    @property
    def rootfs_id(self) -> str:
        return self.data.get("sysroot", {}).get("id", "")

    @property
    def sysroot_path(self) -> str:
        return self.data.get("sysroot", {}).get("repository_path", "")

    @property
    def sysroot_id(self) -> str:
        return self.data.get("sysroot", {}).get("id", "")

    @property
    def cross_cc(self) -> str:
        return self.data.get("toolchain", {}).get("cross_cc", "")

    @property
    def cross_cxx(self) -> str:
        return self.data.get("toolchain", {}).get("cross_cxx", "")

    @property
    def toolchain_target_triple(self) -> str:
        return self.data.get("toolchain", {}).get("target_triple", "")

    @property
    def container_image(self) -> str:
        rbe = self.data.get("build_environment", {})
        return rbe.get("image", "")

    @property
    def container_digest(self) -> str:
        rbe = self.data.get("build_environment", {})
        return rbe.get("image_manifest_digest", "")

    @property
    def container_os(self) -> str:
        rbe = self.data.get("build_environment", {})
        return rbe.get("container_os", "")

    @property
    def runtime_user(self) -> str:
        return self.data.get("runtime", {}).get("user", "cgw")

    @property
    def runtime_group(self) -> str:
        return self.data.get("runtime", {}).get("group", "cgw")

    @property
    def persistent_state(self) -> str:
        return self.data.get("runtime", {}).get("persistent_state", "/var/lib/cgw")

    @property
    def has_tbd(self) -> bool:
        """True if any top-level value is TBD."""
        for v in self.data.values():
            if isinstance(v, str) and v == "TBD":
                return True
        return False


@dataclass
class SysrootManifest:
    """Sysroot manifest (platforms/<platform>-sysroot.yaml)."""

    data: dict[str, Any]

    @property
    def id(self) -> str:
        return self.data.get("sysroot", {}).get("id", "")

    @property
    def repository_path(self) -> str:
        return self.data.get("sysroot", {}).get("repository_path", "")

    @property
    def digest(self) -> str:
        return self.data.get("sysroot", {}).get("digest", "")

    @property
    def file_manifest(self) -> str:
        return self.data.get("sysroot", {}).get("file_manifest", "")

    @property
    def import_status(self) -> str:
        return self.data.get("sysroot", {}).get("import_status", "pending")


# ---------------------------------------------------------------------------
# Dependency lock model
# ---------------------------------------------------------------------------


@dataclass
class DependencyPatch:
    """A patch applied to a dependency source tree."""

    file: str
    sha256: str


@dataclass
class DependencyEntry:
    """A single locked TARGET dependency."""

    name: str
    version: str
    source_url: str
    source_sha256: str
    license: str
    boundary: str  # TARGET
    architecture: str
    linkage: str  # static | shared
    patches: list[DependencyPatch] = field(default_factory=list)
    cmake_options: dict[str, str] = field(default_factory=dict)

    @property
    def is_source_pinned(self) -> bool:
        """True when the source SHA-256 has been filled (not a placeholder)."""
        sha = self.source_sha256.strip()
        if not sha:
            return False
        return not sha.upper().startswith("PENDING")

    @property
    def is_static(self) -> bool:
        return self.linkage == "static"


@dataclass
class DependencyLock:
    """Collection of all locked dependencies."""

    dependencies: dict[str, DependencyEntry]
    cache: dict[str, Any] = field(default_factory=dict)

    def get(self, name: str) -> DependencyEntry | None:
        return self.dependencies.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self.dependencies

    def dependency_names(self) -> set[str]:
        return set(self.dependencies)

    def __len__(self) -> int:
        return len(self.dependencies)

    def __iter__(self):
        return iter(self.dependencies.values())


# ---------------------------------------------------------------------------
# Loading functions
# ---------------------------------------------------------------------------


def load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file and return its contents as a dict."""
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        raise ManifestError(f"Manifest file not found: {path}")
    except yaml.YAMLError as exc:
        raise ManifestError(f"YAML parse error in {path}: {exc}")
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ManifestError(f"Expected YAML mapping in {path}, got {type(data).__name__}")
    return data


def _parse_install_component(item: Any) -> InstallComponent:
    if not isinstance(item, dict):
        raise SchemaValidationError(
            "install_components entry must be an object with 'name' and 'staging'"
        )
    name = item.get("name")
    staging = item.get("staging")
    if not name or not isinstance(name, str):
        raise SchemaValidationError(
            "install_components entry requires a non-empty 'name' string"
        )
    if staging not in ("sdk", "rootfs"):
        raise SchemaValidationError(
            f"install_components '{name}' has invalid staging '{staging}'; "
            f"must be one of: sdk, rootfs"
        )
    return InstallComponent(name=name, staging=staging)


def _ensure_unique(items: list[str], label: str) -> None:
    seen: set[str] = set()
    for item in items:
        if not item:
            raise SchemaValidationError(f"{label} contains an empty element")
        if item in seen:
            raise SchemaValidationError(f"{label} contains duplicate entry: {item}")
        seen.add(item)


def _parse_build_config(data: dict[str, Any]) -> BuildConfig:
    """Parse a build config, normalising legacy single-value fields."""
    preset = data.get("preset")
    if not preset or not isinstance(preset, str):
        raise SchemaValidationError("build.preset is required and must be a non-empty string")

    has_targets = "targets" in data
    has_target = "target" in data
    if has_targets and has_target:
        raise SchemaValidationError(
            "build.target (singular) and build.targets (plural) must not be declared together"
        )
    if has_targets:
        raw_targets = data["targets"]
        if not isinstance(raw_targets, list) or not raw_targets:
            raise SchemaValidationError("build.targets must be a non-empty array")
        targets = [str(t) for t in raw_targets]
        _ensure_unique(targets, "build.targets")
    elif has_target:
        targets = [str(data["target"])]
    else:
        raise SchemaValidationError("build requires either build.target or build.targets")

    has_ics = "install_components" in data
    has_ic = "install_component" in data
    if has_ics and has_ic:
        raise SchemaValidationError(
            "build.install_component (singular) and build.install_components (plural) "
            "must not be declared together"
        )
    if has_ics:
        raw_ics = data["install_components"]
        if not isinstance(raw_ics, list):
            raise SchemaValidationError("build.install_components must be an array")
        install_components = [_parse_install_component(item) for item in raw_ics]
        _ensure_unique([c.name for c in install_components], "install_components name")
    elif has_ic:
        install_components = [
            InstallComponent(name=str(data["install_component"]), staging="rootfs")
        ]
    else:
        install_components = []

    service_deps = [str(d) for d in data.get("service_dependencies", [])]
    _ensure_unique(service_deps, "service_dependencies")
    target_deps = [str(d) for d in data.get("target_dependencies", [])]
    _ensure_unique(target_deps, "target_dependencies")

    cmake_cache_vars = {
        str(k): str(v) for k, v in data.get("cmake_cache_variables", {}).items()
    }

    legacy_deps: list[str] = []
    if "dependencies" in data:
        if service_deps or target_deps:
            raise SchemaValidationError(
                "legacy build.dependencies must not coexist with "
                "service_dependencies or target_dependencies"
            )
        legacy_deps = [str(d) for d in data["dependencies"]]
        _ensure_unique(legacy_deps, "dependencies")

    return BuildConfig(
        preset=preset,
        targets=targets,
        install_components=install_components,
        service_dependencies=service_deps,
        target_dependencies=target_deps,
        cmake_cache_variables=cmake_cache_vars,
        _legacy_dependencies=legacy_deps,
    )


def _parse_runtime_config(data: dict[str, Any]) -> RuntimeConfig:
    return RuntimeConfig(
        systemd_units=list(data.get("systemd_units", [])),
        after=list(data.get("after", [])),
        health_check=data.get("health_check"),
        smoke_test=data.get("smoke_test"),
        config_paths=list(data.get("config_paths", [])),
        persistent_paths=list(data.get("persistent_paths", [])),
        required_devices=list(data.get("required_devices", [])),
        capabilities=list(data.get("capabilities", [])),
    )


def _parse_service(service_id: str, data: dict[str, Any]) -> Service:
    repository = data.get("repository")
    source_dir = data.get("source_dir")
    if not repository and not source_dir:
        raise SchemaValidationError(
            f"Service '{service_id}' must declare either 'repository' or 'source_dir'"
        )
    return Service(
        id=service_id,
        repository=repository or source_dir,
        build=_parse_build_config(data["build"]),
        runtime=_parse_runtime_config(data.get("runtime", {})),
        revision=data.get("revision"),
        source_dir=source_dir,
        description=data.get("description"),
        kind=data.get("kind", "daemon"),
    )


def load_service_manifest(path: Path) -> ServiceManifest:
    """Load and parse a service manifest YAML file."""
    data = load_yaml(path)
    raw_services = data.get("services", {})
    services: dict[str, Service] = {}
    for sid, svc_data in raw_services.items():
        services[sid] = _parse_service(sid, svc_data)
    return ServiceManifest(services=services)


def resolve_legacy_dependencies(
    manifest: ServiceManifest, lock_names: set[str]
) -> ServiceManifest:
    """Classify legacy ``dependencies`` entries using lock context."""
    for svc in manifest:
        if not svc.build._legacy_dependencies:
            continue
        for dep in svc.build._legacy_dependencies:
            in_services = dep in manifest.services
            in_lock = dep in lock_names
            if in_services and in_lock:
                raise SchemaValidationError(
                    f"Service '{svc.id}' legacy dependency '{dep}' is ambiguous: "
                    f"matches both a service id and a lock dependency"
                )
            elif in_services:
                svc.build.service_dependencies.append(dep)
            elif in_lock:
                svc.build.target_dependencies.append(dep)
            else:
                raise SchemaValidationError(
                    f"Service '{svc.id}' legacy dependency '{dep}' could not be "
                    f"classified as a service or target dependency"
                )
        svc.build._legacy_dependencies = []
    return manifest


def load_release_set_manifest(path: Path) -> ReleaseSetManifest:
    """Load and parse a release-set manifest YAML file."""
    data = load_yaml(path)
    raw_sets = data.get("release_sets", {})
    release_sets: dict[str, ReleaseSet] = {}
    for set_id, set_data in raw_sets.items():
        release_sets[set_id] = ReleaseSet(
            id=set_id,
            services=list(set_data["services"]),
            description=set_data.get("description"),
            platform=set_data.get("platform"),
            profile=set_data.get("profile"),
        )
    return ReleaseSetManifest(release_sets=release_sets)


def load_platform_manifest(path: Path) -> PlatformManifest:
    """Load a platform manifest YAML file."""
    return PlatformManifest(data=load_yaml(path))


def load_sysroot_manifest(path: Path) -> SysrootManifest:
    """Load a sysroot manifest YAML file."""
    return SysrootManifest(data=load_yaml(path))


def _parse_dependency_entry(name: str, data: dict[str, Any]) -> DependencyEntry:
    source = data.get("source", {}) or {}
    patches_raw = data.get("patches", []) or []
    patches = [
        DependencyPatch(
            file=str(p.get("file", "")),
            sha256=str(p.get("sha256", "")),
        )
        for p in patches_raw
    ]
    cmake = data.get("cmake", {}) or {}
    options = cmake.get("options", {}) or {}
    return DependencyEntry(
        name=name,
        version=str(data.get("version", "")),
        source_url=str(source.get("url", "")),
        source_sha256=str(source.get("sha256", "")),
        license=str(data.get("license", "")),
        boundary=str(data.get("boundary", "")),
        architecture=str(data.get("architecture", "")),
        linkage=str(data.get("linkage", "")),
        patches=patches,
        cmake_options={str(k): str(v) for k, v in options.items()},
    )


def load_dependency_lock(path: Path) -> DependencyLock:
    """Load and parse a dependency lock YAML file."""
    data = load_yaml(path)
    raw_deps = data.get("dependencies")
    if raw_deps is None:
        raw_deps = {}
    if isinstance(raw_deps, list):
        raise ManifestError(
            "dependency-lock.yaml 'dependencies' must be a mapping keyed by "
            "dependency name, not a list"
        )
    deps: dict[str, DependencyEntry] = {}
    for name, entry_data in raw_deps.items():
        deps[name] = _parse_dependency_entry(name, entry_data)
    cache = data.get("cache", {}) or {}
    return DependencyLock(dependencies=deps, cache=cache)


# ---------------------------------------------------------------------------
# Project layout helper (platform-parameterized)
# ---------------------------------------------------------------------------


class Project:
    """Provides paths to project manifests and directories.

    平台参数化：platform 从构造参数传入，平台清单从
    manifests/platforms/<platform>.yaml 加载。不硬编码任何平台常量。
    """

    def __init__(self, root: Path | str | None = None, platform: str = "cgw-dev"):
        if root is None:
            root = Path.cwd()
        self.root = Path(root).resolve()
        self.platform = platform
        self._validate_root()

    def _validate_root(self) -> None:
        # "host" is a special platform for native builds; it does not
        # require a platform manifest file (no cross-compile toolchain/sysroot).
        if self.platform == "host":
            if not (self.root / "manifests" / "services.yaml").is_file():
                raise ManifestError(
                    f"Not a CGW Build project root: {self.root} "
                    f"(missing manifests/services.yaml)"
                )
            return
        platform_yaml = self.platforms_dir / f"{self.platform}.yaml"
        if not platform_yaml.is_file():
            raise ManifestError(
                f"Not a CGW Build project root or unknown platform: {self.root} "
                f"(missing manifests/platforms/{self.platform}.yaml)"
            )

    @property
    def manifests_dir(self) -> Path:
        return self.root / "manifests"

    @property
    def platforms_dir(self) -> Path:
        return self.manifests_dir / "platforms"

    @property
    def schemas_dir(self) -> Path:
        return self.manifests_dir / "schemas"

    @property
    def platform_manifest_path(self) -> Path:
        return self.platforms_dir / f"{self.platform}.yaml"

    @property
    def sysroot_manifest_name(self) -> str:
        """Determine sysroot manifest filename from platform manifest."""
        pm = self.load_platform_manifest()
        sid = pm.sysroot_id or pm.rootfs_id
        return f"{sid}.yaml"

    @property
    def sysroot_manifest_path(self) -> Path:
        return self.platforms_dir / self.sysroot_manifest_name

    @property
    def service_manifest_path(self) -> Path:
        return self.manifests_dir / "services.yaml"

    @property
    def release_set_manifest_path(self) -> Path:
        return self.manifests_dir / "release-sets.yaml"

    @property
    def dependency_lock_path(self) -> Path:
        return self.manifests_dir / "dependency-lock.yaml"

    @property
    def dependencies_dir(self) -> Path:
        return self.root / "dependencies"

    @property
    def recipes_dir(self) -> Path:
        return self.dependencies_dir / "recipes"

    @property
    def cmake_dir(self) -> Path:
        return self.root / "cmake"

    @property
    def toolchains_dir(self) -> Path:
        return self.cmake_dir / "toolchains"

    @property
    def toolchain_file_path(self) -> Path:
        return self.toolchains_dir / "cgw-target.cmake"

    @property
    def presets_path(self) -> Path:
        return self.root / "presets" / "CMakePresets.json"

    @property
    def sysroot_path(self) -> Path:
        """Physical sysroot path derived from platform manifest."""
        if self.platform == "host":
            return self.root  # native build; no separate sysroot
        pm = self.load_platform_manifest()
        repo_path = pm.sysroot_path
        if repo_path:
            return self.root / repo_path
        return self.root / "sysroots" / self.platform

    @property
    def out_dir(self) -> Path:
        return self.root / "out"

    def load_platform_manifest(self) -> PlatformManifest:
        if self.platform == "host":
            # Return a minimal platform manifest for native host builds.
            import platform as _platform
            return PlatformManifest(data={
                "platform": "host",
                "version": "native",
                "architecture": _platform.machine(),
                "sysroot": {"id": "host"},
                "toolchain": {},
                "runtime": {"user": "cgw", "group": "cgw",
                            "persistent_state": "/var/lib/cgw"},
            })
        return load_platform_manifest(self.platform_manifest_path)

    def load_sysroot_manifest(self) -> SysrootManifest:
        if self.platform == "host":
            return SysrootManifest(data={
                "sysroot": {"id": "host", "import_status": "native"}
            })
        path = self.sysroot_manifest_path
        if not path.is_file():
            raise ManifestError(f"Sysroot manifest not found: {path}")
        return load_sysroot_manifest(path)

    def load_dependency_lock(self) -> DependencyLock:
        """Load the dependency lock, returning an empty lock if absent."""
        path = self.dependency_lock_path
        if not path.is_file():
            return DependencyLock(dependencies={})
        return load_dependency_lock(path)

    def load_service_manifest(self) -> ServiceManifest:
        """Load the service manifest and resolve legacy dependencies."""
        manifest = load_service_manifest(self.service_manifest_path)
        lock = self.load_dependency_lock()
        resolve_legacy_dependencies(manifest, lock.dependency_names())
        return manifest

    def load_release_set_manifest(self) -> ReleaseSetManifest:
        return load_release_set_manifest(self.release_set_manifest_path)

    def staging_dir(self, profile: str = "release") -> Path:
        return self.out_dir / self.platform / profile

    def available_platforms(self) -> list[str]:
        """List all platform manifests available in manifests/platforms/."""
        if not self.platforms_dir.is_dir():
            return ["host"]
        platforms = sorted(
            p.stem
            for p in self.platforms_dir.glob("*.yaml")
            if not p.stem.endswith("-sysroot")
        )
        return ["host"] + platforms
