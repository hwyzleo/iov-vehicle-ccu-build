# CGW-BUILD-DSN-CR-001 设计映射

本文档将 CGW-BUILD-DSN-CR-001 设计变更的各章节映射到具体实现文件。

## 设计 §1 总体架构

```
SRC → ORCH → FW → SVC → STAGE → PKG → DEV → TEST
```

- 编排器：`cgw_build/orchestrator.py`
- 流水线：`ci/pipeline.sh`

## 设计 §2 仓库布局

目录结构见 `README.md`。所有目录按设计 §2 创建。

## 设计 §3 Platform Manifest

- 清单：`manifests/platforms/cgw-dev.yaml`
- Schema：`manifests/schemas/platform.schema.yaml`
- 数据模型：`cgw_build/manifest.py` → `PlatformManifest`
- TBD 项带 `owner` 和 `close_gate`（关闭门槛）
- `prerequisites_to_close` 列出首个可发布构建前必须关闭的全部 TBD

## 设计 §4 Toolchain 与查找隔离

- Toolchain：`cmake/toolchains/cgw-target.cmake`
  - `CGW_SYSROOT` / `CGW_DEP_STAGING` / `CGW_SDK_STAGING` 环境变量
  - 查找顺序：SDK staging → TARGET dependency staging → sysroot
  - `CMAKE_FIND_ROOT_PATH_MODE_PROGRAM=NEVER`, `LIBRARY/INCLUDE/PACKAGE=ONLY`
- Presets：`presets/CMakePresets.json`（host-debug/release, cgw-dev-debug/release）
- 依赖查找：`cmake/modules/CgwDependency.cmake`
- 安装辅助：`cmake/modules/CgwInstall.cmake`
- 构建信息：`cmake/modules/CgwBuildInfo.cmake`

## 设计 §5 项目与编排模型

- 服务清单：`manifests/services.yaml`
- 发布集合：`manifests/release-sets.yaml`
- Schema：`manifests/schemas/service.schema.yaml`, `release-set.schema.yaml`
- 数据模型：`cgw_build/manifest.py` → `Service`, `BuildConfig`, `ReleaseSet`
- 依赖图：`cgw_build/graph.py` → `DependencyGraph`（拓扑排序、环检测、缺失依赖）
- 交叉引用校验：`cgw_build/validator.py`
- 编排：`cgw_build/orchestrator.py` → `BuildOrchestrator`

## 设计 §6 Staging 与制品

- Staging：`cgw_build/staging.py` → `StagingDir`
  - `out/<platform>/<profile>/{build,deps,sdk,install-root,manifests,logs,packages}/`
  - SDK/rootfs 分类 DESTDIR
- Artifact Manifest：`cgw_build/artifact.py` → `ArtifactManifest`
  - per-file: project/commit, target/component, staging, SHA-256, ELF/ABI/NEEDED/RPATH
  - 路径冲突检测

## 设计 §7 部署事务

- 部署框架：`cgw_build/deploy.py` → `Deployer`
  - 预检 → 备份 → 安装 → ldconfig → daemon-reload → 依赖序启停 → 冒烟 → 回退
  - dry-run 默认；`/var/lib/cgw` 默认保留
- 打包：`cgw_build/packaging.py` → `Packager`（tar.gz + manifest + package.json + checksum）
- 验证：`cgw_build/verify.py` → `Verifier`

## 设计 §8 CI 分层

- 提交级：`ci/pipeline.sh`（schema → build → install → elfcheck → package → verify）
- 构建容器：`ci/Dockerfile`（OS/toolchain TBD，带关闭门槛）
- 容器内构建：`ci/build-in-docker.sh`

## 设计 §9 实施阶段

本 CR 实施阶段 1：仅在 cgw-build 中完成 schema、内置示例、staging、manifest 与包自验证。

- 内置示例：`examples/minimal/`（cgw-hello-lib + cgw-hello-cli）
- 自验证不依赖修改真实服务

## 设计 §10 前置决策

`manifests/platforms/cgw-dev.yaml` → `prerequisites_to_close` 列出全部前置决策。

## 测试覆盖

### 单元测试（`tests/unit/`）

| 文件 | 覆盖范围 |
|------|----------|
| `test_schema.py` | service/release-set/lock/platform schema 校验 |
| `test_manifest.py` | 清单解析、legacy 归一化、平台参数化加载 |
| `test_graph.py` | 拓扑排序、环检测、缺失依赖、TARGET 依赖顺序 |
| `test_validator.py` | 交叉引用、kind、source_dir、systemd、health/smoke |
| `test_staging.py` | staging 目录、SHA-256、路径冲突、scan_files |
| `test_elfcheck.py` | ELF 解析、架构/污染检测、Mach-O/ar |
| `test_manifest_artifact.py` | Artifact Manifest 生成与冲突检测 |

### 集成测试（`tests/integration/`）

| 文件 | 覆盖范围 |
|------|----------|
| `test_pipeline.py` | 端到端校验、dry-run 构建、ELF 检查 |
| `test_package.py` | 打包、checksum、包结构验证 |
