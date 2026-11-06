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

## CCU-BUILD-DSN-CR-004 设计映射

### 设计 §1 职责边界（Architecture Overview）

- 服务仓库拥有 baseline daemon/unit、默认配置、schema、业务 smoke；manifest 只登记既成契约。
- BUILD 拥有服务拓扑、聚合 target 模板与生成、平台 drop-in/共享 JSON、发布校验、制品与回退。
- 设备值（网络、application 注册、运行权限）只来自受控平台配置；生成器不硬编码设备值。

### 设计 §3 版本化目录与资产契约

- 模板：`packaging/systemd/ccu.target.in`（无业务 unit 名；静态 `ccu.target` 已删除）
- 平台根：`configs/<platform>/rootfs/`，包含
  - `usr/lib/systemd/system/ccu-{fota,diag,sec}.service.d/vsomeip.conf`
  - `etc/ccu/someip/vsomeip.json`（受控输入，未落地前 release 构建 fail-closed）
- Schema：`manifests/schemas/service.schema.yaml` → `runtime.platform_assets[]`
  （`source` / `target` / `required`（boolean）/ `validation` 枚举）
- 数据模型：`ccu_build/manifest.py` → `PlatformAsset`、`RuntimeConfig.platform_assets`、
  `PLATFORM_ASSET_VALIDATORS`、`RELEASE_SET_AGGREGATION_TARGET`
- 真实服务登记：`manifests/services.yaml` → `ccu-sec`（`a88bac2`）、`ccu-diag`（`2984eed`）、
  `ccu-fota`（`2fa5f98`），固定 commit；拓扑 `ccu-sec → framework`、
  `ccu-diag → framework, ccu-sec`、`ccu-fota → framework, ccu-diag`
- 发布集合：`manifests/release-sets.yaml` → `ccu-dev-fota` = framework + ccu-sec + ccu-diag + ccu-fota
- TARGET 依赖：仅登记已锁定的 `yaml-cpp`；nlohmann-json/vsomeip3/Boost/OpenSSL 由后续
  TARGET readiness CR 关闭（本 CR 不虚构 lock 条目）

### 设计 §4 聚合 target 生成

- Assembler：`ccu_build/assembly.py` → `ReleaseSetAssembler`
  - `plan()`：闭包（`DependencyGraph.build_order()`）、拓扑序 + 服务内声明序、
    确定性去重、unit 名安全校验、跨服务所有权失败、`ccu.target` 预留校验；
  - `write()`：原子写 `install-root/usr/lib/systemd/system/ccu.target`（0644）、
    渲染 `Wants==After` 且无 `Requires=`、空集合不写并记录 `aggregation_status=empty`；
  - `clean_previous()`：单服务/`--all` 构建清除上次 BUILD-owned target 与资产；
  - `_remove_stale()`：按上次 `assembly-report.json` 清理，SHA 校验保护服务/其他 owner 文件
- 报告：`out/<platform>/<profile>/manifests/assembly-report.json`
  （generated/assets/skipped/missing/conflicts/removed + `build_owned_paths`）
- 编排：`ccu_build/orchestrator.py` → `_assemble_release_set`（5.6，overlay 之前）

### 设计 §5 vsomeip 资产与校验

- 校验器：`ccu_build/assetcheck.py` → `validate_vsomeip_dropin` / `validate_vsomeip_json`
  - drop-in：单 `[Service]` 段、仅两个 `Environment=`（`PrivateTmp=`/`ExecStart=` 等一律拒绝）、
    application 名与 baseline unit 一致；
  - JSON：可解析、`applications` 类型/唯一性、routing 指向已注册 application、
    闭包所需 application 全覆盖、秘密字段/PEM 只报规则与字段路径；
  - 路径助手：`application_from_dropin_target`、`dropin_baseline_unit`；
  - 永不按扩展名猜测校验器（`run_asset_validation` 仅接受声明枚举）
- 选择/去重/授权：`ccu_build/assembly.py` → `_plan_assets`、`validate_service_assets`、
  `_authorized_rule_error`
- 开发豁免：`BuildConfig.allow_missing_platform_assets`（CLI `--allow-missing-platform-assets`），
  release profile 一律拒绝（§11）

### 设计 §6 Overlay 与部署策略集成

- 授权清单：`manifests/config-deployment.yaml` 新增
  `/usr/lib/systemd/system/*.service.d/vsomeip.conf` 与 `/etc/ccu/someip/vsomeip.json`
  （`owner=build` / `release-managed` / `replace`）
- Overlay 跳过：`ccu_build/config.py` → `ConfigOverlay(claimed_paths=...)`
  + `platform_asset_paths`（声明即跳过，未运行 assembler 时同样不生硬复制）
  - 双重写入或 owner 不一致 → `ConfigError`
- CLI `config`：`ccu_build/__main__.py` → `cmd_config` 从上次 assembly report 读取 claimed paths

### 设计 §7 制品、打包与部署

- Artifact：`ccu_build/artifact.py` → `ArtifactEntry.source/source_sha256/mode/referenced_by`、
  `ArtifactManifest.release_set`；`orchestrator._generate_artifact_manifest` 登记
  `owner_service=build` / `owner_target=release-set-assembly` /
  `install_component=ccu-release-set-runtime` / `staging=rootfs`，
  共享资产带 `referenced_by[]`，overlay 独占路径归 `build`（`config-overlay`）
- 双向核对：`ccu_build/reconcile.py` → `reconcile_install_root`
  （声明缺失 unit/config、无 owner 资产失败；上次 manifest 遗留记为 stale 警告）
- 编译期归属：`orchestrator._component_install_manifest_files`
  （读取 CMake `install_manifest_<component>.txt`，增量构建不再退化为 `unknown`）
- 打包：`ccu_build/packaging.py` → `PackageMetadata.release_set`
  + `_check_release_set_consistency`（unknown owner、target/metadata 缺失或错配 → 拒绝打包）
- 部署：`ccu_build/deploy.py`
  - `_ordered_units` 排除 `ccu.target`（仍按服务依赖序启停）
  - `_check_release_set`（包内 package.json / artifact-manifest / ccu.target Wants/After /
    实际 `.service` payload 双向核对，错配预检失败）
  - `build_deploy_plan` 枚举所有显式策略覆盖的 payload；`/etc/ccu/**` 未匹配仍 preserve；
    未授权运行资产记入 `unauthorized` 且永不 replace；provisioning 保持排除
- 最终校验：`ccu_build/unitcheck.py` → `check_install_root_units`
  （等价 systemd unit 静态检查 + 存在 `systemd-analyze` 时叠加执行）、
  `load_aggregation_target_units`；`ccu_build/verify.py` → release-set 证据、
  ownership、BUILD-owned 证据、stale、平台资产复验

### 设计 §8/§9 验证与需求追踪

| Requirement ID | 实现/测试 |
|---|---|
| RELEASE-REQ-001 | `manifests/services.yaml`、`validator.validate_release_set_assets`、`reconcile.py`、`tests/unit/test_manifest.py::test_real_service_registration`、`tests/integration/test_release_set_assembly.py::TestRealReleaseSetPlans` |
| RELEASE-REQ-002 | `assembly.py`、`packaging/systemd/ccu.target.in`、`tests/unit/test_assembly.py`（顺序/确定性/空集合/stale/单服务）、`tests/integration/test_release_set_assembly.py`（切换/clean-incremental/真实 HOST build） |
| RELEASE-REQ-003 | `assetcheck.py`、`_plan_assets`、`configs/ccu-dev/rootfs/**`、`tests/unit/test_assembly.py::TestPlatformAssets/TestVsomeipDropinValidator/TestVsomeipJsonValidator` |
| RELEASE-REQ-004 | `artifact.py`、`packaging.py`、`deploy.py`、`unitcheck.py`、`verify.py`、`tests/integration/test_release_set_assembly.py`（证据/打包拒绝/部署预检/未授权资产） |
| RELEASE-REQ-005 | 本文件、`README.md` |

### CR-004 测试覆盖

| 文件 | 覆盖范围 |
|---|---|
| `tests/unit/test_assembly.py` | 闭包/拓扑序/重复 unit/非法 unit/`ccu.target` 预留；渲染确定性、Wants==After、无 Requires、0644、空集合、单服务/`--all` 清理；资产选择/去重/引用/授权/缺失/冲突/baseline unit；stale 清理不删服务文件；overlay 跳过 claimed；drop-in/JSON 校验器 |
| `tests/unit/test_manifest.py` | 真实服务固定 revision 与拓扑、`platform_assets` 解析、release-set 成员 |
| `tests/integration/test_release_set_assembly.py` | 三集合 target 计划、release profile fail-closed、开发豁免；全流水线（assembler→overlay→artifact→package→deploy dry-run）；确定性/clean-incremental 一致；切换集合清理；打包拒绝；部署预检（target/payload/metadata 错配）；真实 HOST minimal build |
| `tests/integration/test_pipeline.py`、`test_framework_build.py` | 既有全量校验与 framework HOST 构建回归 |
