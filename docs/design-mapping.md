# CCU-BUILD-DSN-CR-001 设计映射

本文档将 CCU-BUILD-DSN-CR-001 设计变更的各章节映射到具体实现文件。

## 设计 §1 总体架构

```
SRC → ORCH → FW → SVC → STAGE → PKG → DEV → TEST
```

- 编排器：`ccu_build/orchestrator.py`
- 流水线：`ci/pipeline.sh`

## 设计 §2 仓库布局

目录结构见 `README.md`。所有目录按设计 §2 创建。

## 设计 §3 Platform Manifest

- 清单：`manifests/platforms/ccu-dev.yaml`
- Schema：`manifests/schemas/platform.schema.yaml`
- 数据模型：`ccu_build/manifest.py` → `PlatformManifest`
- TBD 项带 `owner` 和 `close_gate`（关闭门槛）
- `prerequisites_to_close` 列出首个可发布构建前必须关闭的全部 TBD

## 设计 §4 Toolchain 与查找隔离

- Toolchain：`cmake/toolchains/ccu-target.cmake`
  - `CCU_SYSROOT` / `CCU_DEP_STAGING` / `CCU_SDK_STAGING` 环境变量
  - 查找顺序：SDK staging → TARGET dependency staging → sysroot
  - `CMAKE_FIND_ROOT_PATH_MODE_PROGRAM=NEVER`, `LIBRARY/INCLUDE/PACKAGE=ONLY`
- Presets：`presets/CMakePresets.json`（host-debug/release, ccu-dev-debug/release）
- 依赖查找：`cmake/modules/CcuDependency.cmake`
- 安装辅助：`cmake/modules/CcuInstall.cmake`
- 构建信息：`cmake/modules/CcuBuildInfo.cmake`

## 设计 §5 项目与编排模型

- 服务清单：`manifests/services.yaml`
- 发布集合：`manifests/release-sets.yaml`
- Schema：`manifests/schemas/service.schema.yaml`, `release-set.schema.yaml`
- 数据模型：`ccu_build/manifest.py` → `Service`, `BuildConfig`, `ReleaseSet`
- 依赖图：`ccu_build/graph.py` → `DependencyGraph`（拓扑排序、环检测、缺失依赖）
- 交叉引用校验：`ccu_build/validator.py`
- 编排：`ccu_build/orchestrator.py` → `BuildOrchestrator`

## 设计 §6 Staging 与制品

- Staging：`ccu_build/staging.py` → `StagingDir`
  - `out/<platform>/<profile>/{build,deps,sdk,install-root,manifests,logs,packages}/`
  - SDK/rootfs 分类 DESTDIR
- Artifact Manifest：`ccu_build/artifact.py` → `ArtifactManifest`
  - per-file: project/commit, target/component, staging, SHA-256, ELF/ABI/NEEDED/RPATH
  - 路径冲突检测

## 设计 §7 部署事务

- 部署框架：`ccu_build/deploy.py` → `Deployer`
  - 预检 → 备份 → 安装 → ldconfig → daemon-reload → 依赖序启停 → 冒烟 → 回退
  - dry-run 默认；`/var/lib/ccu` 默认保留
- 打包：`ccu_build/packaging.py` → `Packager`（tar.gz + manifest + package.json + checksum）
- 验证：`ccu_build/verify.py` → `Verifier`

## 设计 §8 CI 分层

- 提交级：`ci/pipeline.sh`（schema → build → install → elfcheck → package → verify）
- 构建容器：`ci/Dockerfile`（OS/toolchain TBD，带关闭门槛）
- 容器内构建：`ci/build-in-docker.sh`

## 设计 §9 实施阶段

本 CR 实施阶段 1：仅在 ccu-build 中完成 schema、内置示例、staging、manifest 与包自验证。

- 内置示例：`examples/minimal/`（ccu-hello-lib + ccu-hello-cli）
- 自验证不依赖修改真实服务

## 设计 §10 前置决策

`manifests/platforms/ccu-dev.yaml` → `prerequisites_to_close` 列出全部前置决策。

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

## CCU-BUILD-DSN-CR-002 设计映射

### 设计 §1 Framework Metadata / §8.1

- 服务清单：`manifests/services.yaml` -> `framework`（library，`source_dir: ../iov-vehicle-ccu-framework`，五个 targets，`install_components: ccu-framework-sdk(sdk) + ccu-framework-runtime(rootfs)`，`target_dependencies: [yaml-cpp]`，`systemd_units: []`）
- 下游消费示例：`manifests/services.yaml` -> `framework-consumer`（daemon，`service_dependencies: [framework]`，`target_dependencies: [yaml-cpp]`）
- 发布集合：`manifests/release-sets.yaml` -> `ccu-dev-framework`
- 内置 consumer 工程：`examples/framework-consumer/`（`find_package(CCUFramework)` + 链接五个 `CCUFramework::ccu-framework-*`）

### 设计 §2/§8.2 Schema 决策

- Schema：`manifests/schemas/service.schema.yaml`（复数 targets/components、显式 service/target_dependencies、library 空 runtime）——CR-001 已建立，CR-002 复用并补充测试。
- 校验：`ccu_build/validator.py` -> `validate_library_kind`（library 不得有 systemd/health/smoke）

### 设计 §3/§8.3 TARGET 依赖分类

- 依赖锁定：`manifests/dependency-lock.yaml` -> `yaml-cpp`（受控 TARGET，static，MIT，PENDING 源摘要）
- Recipe stub：`dependencies/recipes/yaml-cpp.yaml`、`dependencies/recipes/README.md`
- 编排器校验：`ccu_build/orchestrator.py` -> `_prepare_target_dependencies`（fail-closed：声明 target_dependencies 但 CCU_DEP_STAGING 空则失败）

### 设计 §4/§8.4 查找与 Staging

- Staging：`ccu_build/staging.py` -> `StagingDir`（`deps/usr`、`sdk/<project>/usr`、component DESTDIR 分类）
- TARGET toolchain 查找根：`cmake/toolchains/ccu-target.cmake`（SDK/dep/sysroot，PROGRAM=NEVER、LIBRARY/INCLUDE/PACKAGE=ONLY）
- **HOST 自验证查找根注入**：`ccu_build/orchestrator.py` -> `_configure_cmd`（HOST 不加载 toolchain，编排器注入等价 `CMAKE_PREFIX_PATH`：SDK staging -> dep staging，并保持 `CMAKE_FIND_ROOT_PATH_MODE_*` = NEVER/ONLY/ONLY/ONLY，不回退宿主路径）
- 编排器环境：`ccu_build/orchestrator.py` -> `_cmake_env`（HOST 与 TARGET 均设 `CCU_DEP_STAGING`）

### 设计 §5/§8.5 下游消费契约

- 内置 consumer：`examples/framework-consumer/CMakeLists.txt`（仅 `find_package(CCUFramework CONFIG REQUIRED)` + 导出 targets；禁止源码 include / add_subdirectory / build-tree lib）
- framework package/namespace 名以 CCU-FW（CCU-FW-DSN-CR-006）为准：`CCUFramework::ccu-framework-*`

### 设计 §6/§8.6 验证

- Artifact Manifest：`ccu_build/artifact.py` -> `ArtifactManifest`（project/target/component/staging/SHA-256/ELF；新增 `sdk_consumers` consumer 证据；`dependency_lock` 摘要含 license/arch/linkage）
- 编排器填充：`ccu_build/orchestrator.py` -> `_generate_artifact_manifest`（reverse service_dependencies 计算 sdk_consumers）

### CR-002 测试覆盖

| 文件 | 覆盖范围 |
|------|----------|
| `tests/unit/test_manifest.py` | framework/consumer 元数据、yaml-cpp lock、release-set |
| `tests/unit/test_graph.py` | framework 拓扑序、target_dependency_order |
| `tests/unit/test_orchestrator.py` | HOST 查找根注入、fail-closed 依赖校验 |
| `tests/unit/test_manifest_artifact.py` | sdk_consumers consumer 证据 |
| `tests/integration/test_pipeline.py` | 全量校验含 framework 集合 |
| `tests/integration/test_framework_build.py` | 真实 HOST 构建 framework+consumer、SDK/runtime 分组件、find_package 链接、manifest 证据 |

## CCU-BUILD-DSN-CR-003 设计映射

### 设计 §9.1 配置发布模型

- 职责边界：服务拥有开发夹具/默认模板/schema/checker；BUILD 拥有平台非秘密发布值/overlay/策略/校验/打包/回退；provisioning 拥有设备身份与秘密；CCU-FW 拥有运行时六阶段加载。
- 平台配置源：`configs/<platform>/rootfs/`（`Project.platform_configs_root()`）

### 设计 §9.2 目录设计

- 服务仓库结构：`examples/*/config/{<svc>.default.yaml, schema/<svc>.schema.yaml, dev/{common.yaml,<svc>.yaml}}`
- BUILD 仓库结构：`configs/ccu-dev/rootfs/etc/ccu/{common.yaml, conf.d/<svc>.yaml}`
- CMake 精确安装：`cmake/modules/CcuInstall.cmake` -> `ccu_install_default_config(<svc> <default.yaml>)`（重命名为 `/etc/ccu/conf.d/<svc>.yaml`，禁止通配安装）

### 设计 §9.3 Platform Overlay 算法

- Overlay 引擎：`ccu_build/config.py` -> `ConfigOverlay`
  - 7 步算法：路径规范化、清理上次状态防 stale、相对路径映射、授权覆盖+前后来源记录、原子写+权限归一化、校验、报告
  - 路径安全：`normalize_source_path`（拒绝绝对/.. /symlink 逃逸/设备文件/非普通文件）
  - 原子写：`atomic_write`（临时文件+`os.replace`+权限归一化）
  - clean/incremental 一致性：状态文件 `config-overlay-state.json`，stale build-owned 文件清理
 - 报告：`OverlayReport`（overlaid/removed/skipped，保存至 `manifests/config-overlay-report.json`）

### 设计 §9.4 部署策略清单

- 清单：`manifests/config-deployment.yaml`（version 1，per-platform files）
- Schema：`manifests/schemas/config-deployment.schema.yaml`
- 数据模型：`ccu_build/manifest.py` -> `ConfigDeploymentRule`/`PlatformConfigDeployment`/`ConfigDeploymentManifest`
- 策略解析：`ccu_build/config.py` -> `DeployPolicyResolver`（精确优先 glob、同优先级冲突失败、未匹配默认 preserve、device-managed 不可 replace）

### 设计 §9.5 配置校验接口

- 服务声明：`manifests/services.yaml` -> `runtime.config_validation{schema,target_path,default_source}`
- Schema：`manifests/schemas/service.schema.yaml`（runtime.config_validation）
- 数据模型：`ccu_build/manifest.py` -> `ConfigValidation` / `RuntimeConfig.config_validation`
- 校验器：`ccu_build/config.py` -> `ConfigValidator`（服务 schema 校验、common.yaml 校验、禁止服务顶层 `common:`）
- 交叉校验：`ccu_build/validator.py` -> `validate_config_validation`

### 设计 §9.6 秘密、权限与 Manifest

- 秘密扫描：`ccu_build/secretscan.py` -> `SecretScanner`（PEM 私钥块、禁止扩展名、敏感字段名、provisioning 目录；只报告规则/文件/字段路径，不输出值）
- 权限：非秘密 YAML 默认 0644（`_DEFAULT_MODE`）
- Artifact 来源：`ccu_build/artifact.py` -> `ArtifactEntry.config_provenance` + `ArtifactManifest.attach_config_provenance`（default/platform 来源、final SHA、deploy policy、校验与秘密结果）

### 设计 §9.7 策略化部署与一致回退

- 部署计划：`ccu_build/deploy.py` -> `Deployer.build_deploy_plan`（create/replace/preserve 枚举、provisioning 排除）
- 备份排除 provisioning：`ccu_build/deploy.py` -> backup 步骤 `grep -v credentials|provisioning`
- 一致回退：`ccu_build/deploy.py` -> `_rollback`（整包 tar 恢复 framework+二进制+动态库+unit+replace 配置，非部分回退）
- 部署报告：`DeployReport.deploy_plan`

### 设计 §9.8 与 CCU-FW 六阶段关系

- BUILD 只决定 `/etc/ccu/common.yaml` 与 `/etc/ccu/conf.d/<svc>.yaml` 来源；整文件覆盖不参与 CCU-FW 运行时深合并。HOST build overlay 为 no-op（`configs/host/rootfs` 不存在）。

### 设计 §9.9 配置发布验证

- 验收项全部由测试覆盖（见下表）。

### CR-003 测试覆盖

| 文件 | 覆盖范围 |
|------|----------|
| `tests/unit/test_config.py` | overlay 7 步算法、路径安全、原子写、授权/跳过、stale 清理、增量一致、schema/秘密失败、ConfigValidator、报告 |
| `tests/unit/test_config_deployment.py` | config-deployment schema、数据模型、DeployPolicyResolver（精确/glob/冲突/默认 preserve/device-managed） |
| `tests/unit/test_secret_scan.py` | PEM/扩展名/字段名/provisioning/report-only |
| `tests/integration/test_config_release.py` | overlay 全流水线+manifest 来源、dev 不安装、common 仅 BUILD、deploy 计划+provisioning 排除+一致回退 |
| `tests/integration/test_framework_build.py` | 真实 HOST build 生成 no-op overlay 报告、新 conf.d 路径安装 |
