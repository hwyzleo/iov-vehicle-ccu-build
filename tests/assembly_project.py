"""Shared synthetic BUILD project fixture for assembly tests.

Creates a minimal but complete BUILD project layout (manifests, platform
config rootfs, aggregation template) with a library plus two daemons, so that
release-set assembly, overlay, artifact, packaging and deploy behaviour can be
exercised without compiling real services.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import yaml

from ccu_build.assembly import ReleaseSetAssembler
from ccu_build.manifest import Project
from ccu_build.staging import StagingDir

PROJECT_ROOT = Path(__file__).resolve().parents[1]
UNIT_DIR = "usr/lib/systemd/system"

DROPIN = """[Service]
Environment=VSOMEIP_CONFIGURATION=/etc/ccu/someip/vsomeip.json
Environment=VSOMEIP_APPLICATION_NAME={app}
"""


def write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def make_project(tmp_path: Path, *, with_json: bool = True) -> Project:
    """Create a synthetic project with ``lib-a``, ``svc-a`` and ``svc-b``."""
    root = tmp_path / "proj"
    write_yaml(root / "manifests" / "platforms" / "testplat.yaml", {
        "platform": "testplat",
        "version": "0.1.0",
        "architecture": "TBD",
        "sysroot": {"id": "testplat-sysroot", "repository_path": "sysroots/x"},
    })
    write_yaml(root / "manifests" / "platforms" / "testplat-sysroot.yaml", {
        "sysroot": {"id": "testplat-sysroot", "import_status": "pending"},
    })
    write_yaml(root / "manifests" / "services.yaml", {
        "services": {
            "lib-a": {
                "source_dir": "src/lib-a",
                "kind": "library",
                "build": {
                    "preset": "x",
                    "targets": ["lib-a"],
                    "service_dependencies": [],
                    "install_components": [
                        {"name": "lib-a-runtime", "staging": "rootfs"}
                    ],
                },
                "runtime": {},
            },
            "svc-a": {
                "source_dir": "src/svc-a",
                "kind": "daemon",
                "build": {
                    "preset": "x",
                    "targets": ["svc-a"],
                    "service_dependencies": ["lib-a"],
                    "install_components": [
                        {"name": "svc-a-runtime", "staging": "rootfs"}
                    ],
                },
                "runtime": {
                    "systemd_units": ["svc-a.service"],
                    "platform_assets": [
                        {
                            "source": f"{UNIT_DIR}/svc-a.service.d/vsomeip.conf",
                            "target": f"/{UNIT_DIR}/svc-a.service.d/vsomeip.conf",
                            "required": True,
                            "validation": "vsomeip-dropin",
                        },
                        {
                            "source": "etc/ccu/someip/vsomeip.json",
                            "target": "/etc/ccu/someip/vsomeip.json",
                            "required": True,
                            "validation": "vsomeip-json",
                        },
                    ],
                },
            },
            "svc-b": {
                "source_dir": "src/svc-b",
                "kind": "daemon",
                "build": {
                    "preset": "x",
                    "targets": ["svc-b"],
                    "service_dependencies": ["lib-a", "svc-a"],
                    "install_components": [
                        {"name": "svc-b-runtime", "staging": "rootfs"}
                    ],
                },
                "runtime": {
                    "systemd_units": ["svc-b.service"],
                    "platform_assets": [
                        {
                            "source": f"{UNIT_DIR}/svc-b.service.d/vsomeip.conf",
                            "target": f"/{UNIT_DIR}/svc-b.service.d/vsomeip.conf",
                            "required": True,
                            "validation": "vsomeip-dropin",
                        },
                        {
                            "source": "etc/ccu/someip/vsomeip.json",
                            "target": "/etc/ccu/someip/vsomeip.json",
                            "required": True,
                            "validation": "vsomeip-json",
                        },
                    ],
                },
            },
        }
    })
    write_yaml(root / "manifests" / "release-sets.yaml", {
        "release_sets": {
            "set-full": {"services": ["lib-a", "svc-a", "svc-b"]},
            "set-a": {"services": ["svc-a"]},
            "set-lib": {"services": ["lib-a"]},
        }
    })
    write_yaml(root / "manifests" / "config-deployment.yaml", {
        "version": 1,
        "platforms": {
            "testplat": {
                "files": [
                    {"path": "/etc/ccu/common.yaml", "owner": "build",
                     "category": "release-managed", "deploy_policy": "replace"},
                    {"path_glob": "/usr/lib/systemd/system/*.service.d/vsomeip.conf",
                     "owner": "build", "category": "release-managed",
                     "deploy_policy": "replace"},
                    {"path": "/etc/ccu/someip/vsomeip.json", "owner": "build",
                     "category": "release-managed", "deploy_policy": "replace"},
                    {"path_glob": "/etc/ccu/conf.d/*.yaml", "owner": "service",
                     "category": "release-managed", "deploy_policy": "replace"},
                    {"path_glob": "/etc/ccu/credentials/**", "owner": "provisioning",
                     "category": "device-managed", "deploy_policy": "preserve"},
                ]
            }
        }
    })

    rootfs = root / "configs" / "testplat" / "rootfs"
    write_text(rootfs / UNIT_DIR / "svc-a.service.d" / "vsomeip.conf",
               DROPIN.format(app="svc-a"))
    write_text(rootfs / UNIT_DIR / "svc-b.service.d" / "vsomeip.conf",
               DROPIN.format(app="svc-b"))
    write_text(rootfs / "etc/ccu/common.yaml", "log:\n  level: INFO\n")
    if with_json:
        write_text(rootfs / "etc/ccu/someip/vsomeip.json", json.dumps({
            "unicast": "127.0.0.1",
            "network": "lo",
            "applications": [{"name": "svc-a", "id": "0x1001"},
                             {"name": "svc-b", "id": "0x1002"}],
            "routing": "svc-a",
        }, indent=2) + "\n")

    template_dst = root / "packaging" / "systemd" / "ccu.target.in"
    template_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(PROJECT_ROOT / "packaging" / "systemd" / "ccu.target.in", template_dst)
    return Project(root, platform="testplat")


def make_staging(project: Project, profile: str = "release") -> StagingDir:
    """Create a staging tree seeded with the service-installed payload."""
    staging = StagingDir(project.root, project.platform, profile)
    staging.prepare()
    unit_dir = staging.install_root / UNIT_DIR
    (unit_dir / "svc-a.service.d").mkdir(parents=True, exist_ok=True)
    (unit_dir / "svc-b.service.d").mkdir(parents=True, exist_ok=True)
    write_text(unit_dir / "svc-a.service",
               "[Unit]\nDescription=A\nAfter=network.target\n"
               "[Service]\nExecStart=/usr/bin/svc-a\n")
    write_text(unit_dir / "svc-b.service",
               "[Unit]\nDescription=B\nAfter=network.target\n"
               "[Service]\nExecStart=/usr/bin/svc-b\n")
    write_text(staging.install_root / "usr/bin/svc-a", "binary-a")
    write_text(staging.install_root / "usr/bin/svc-b", "binary-b")
    write_text(staging.install_root / "etc/ccu/conf.d/svc-a.yaml", "svc_a:\n  x: 1\n")
    write_text(staging.install_root / "etc/ccu/conf.d/svc-b.yaml", "svc_b:\n  x: 1\n")
    return staging


def make_assembler(project: Project, staging: StagingDir, **kwargs) -> ReleaseSetAssembler:
    return ReleaseSetAssembler(
        project=project,
        staging=staging,
        service_manifest=project.load_service_manifest(),
        **kwargs,
    )
