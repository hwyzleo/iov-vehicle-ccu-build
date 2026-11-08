# iov-vehicle-ccu-build

CCU 统一目标平台构建系统。为 CCU framework、DIAG、FOTA、SEC 及后续服务
提供统一的目标平台构建、依赖管理、制品组织、打包、部署、验证和回退能力。

> **状态**：CCU-BUILD v0.5 草案。由 CCU-BUILD-DSN-CR-001～005 建立。
> 目标硬件、OS/BSP、架构、toolchain、sysroot 与正式部署通道尚待实机确认；
> 未确认值写入受控 Platform Manifest，并在首个可发布构建前关闭。
> 真实服务（ccu-sec / ccu-diag / ccu-fota）已按固定 commit 登记；受控设备
> `vsomeip.json`、生产 drop-in 强制要求与 TARGET 依赖锁仍待实机输入关闭，
> release profile 缺必需平台资产时构建 fail-closed。

## 设计原则

- **平台参数化**：`<platform>` 是变量，不硬编码任何硬件常量，不沿用 TBOX Orin 基线
- **TBD 带关闭门槛**：未确认项必须记录责任方和关闭门槛
- **统一版本化**：Platform Manifest、toolchain、sysroot 与依赖锁定统一版本化
- **HOST/TARGET 隔离**：目标查找仅从受控 sysroot、TARGET dependency staging 和 SDK staging 查找
- **服务职责分离**：BUILD 不保存业务源码，不复制服务拥有的 unit/配置/健康检查

## 仓库布局

```
ccu-build/
├── manifests/
│   ├── platforms/<platform>.yaml      平台清单（TBD + 关闭门槛）
│   ├── platforms/<platform>-sysroot.yaml  sysroot 清单
│   ├── services.yaml                   服务声明
│   ├── release-sets.yaml               发布集合
│   ├── dependency-lock.yaml            依赖锁定
│   └── schemas/                        JSON Schema (Draft-07)
├── cmake/
│   ├── toolchains/ccu-target.cmake     平台参数化 toolchain
│   └── modules/                        CcuInstall/CcuDependency/CcuBuildInfo
├── presets/CMakePresets.json           host/ccu-dev preset
├── dependencies/recipes/               TARGET 依赖 recipe
├── configs/<platform>/rootfs/          平台配置 overlay + 平台运行资产
│   ├── etc/ccu/**                      平台非秘密配置（通用 overlay）
│   ├── etc/ccu/someip/vsomeip.json     共享 vsomeip JSON（BUILD 拥有，受控输入）
│   └── usr/lib/systemd/system/*.service.d/vsomeip.conf  vsomeip drop-in（BUILD 拥有）
├── ccu_build/                          Python 编排器
├── ci/                                 CI 流水线
├── packaging/systemd/ccu.target.in     release-set 聚合 target 模板（无业务 unit 名）
├── examples/minimal/                   内置最小示例（自验证）
├── tests/                              单元 + 集成测试
└── docs/                               文档
```

## 快速开始

### 验证清单

```bash
export CCU_ROOT=$(pwd)
python3 -m ccu_build validate --platform ccu-dev
```

### Dry-run 构建（不执行编译）

```bash
python3 -m ccu_build build --set ccu-dev-minimal --dry-run
```

### Host 构建（本机编译自验证）

```bash
python3 -m ccu_build build --set ccu-dev-minimal --platform host --jobs 4
python3 -m ccu_build package --platform host
python3 -m ccu_build verify --platform host --package out/host/release/packages/*.tar.gz
```

### 部署 dry-run

```bash
python3 -m ccu_build deploy --platform ccu-dev <package.tar.gz>
```

## Release-set 聚合与平台运行资产（CCU-BUILD-DSN-CR-004）

`build --set <id>` 会按 **release-set 展开后的依赖闭包** 生成
`install-root/usr/lib/systemd/system/ccu.target`：

- 输入：`manifests/services.yaml` 的服务拓扑 + 选中 release-set；
- 顺序：服务拓扑序，再按每个服务 `runtime.systemd_units` 声明序；
- 内容：`Wants=` 与 `After=` 使用同一有序 unit 集合，从不使用 `Requires=`；
- 模板：`packaging/systemd/ccu.target.in` 只含固定结构与占位符，不含业务 unit 名；
- 单服务 / `--all` 构建不生成 target，并清理上一次 release-set 的 BUILD-owned
  target 与平台资产；无 daemon unit 的集合不生成空 target，report 记录
  `aggregation_status=empty`。

平台 vsomeip 资产通过 `runtime.platform_assets[]` 声明（`source` 相对
`configs/<platform>/rootfs`，`target` 与 source 一一对应，`validation` 为
`vsomeip-dropin` / `vsomeip-json`）：

- 只有入选服务声明的资产才被交付；共享 JSON 按 target 去重并记录 `referenced_by[]`；
- required 资产缺失、同 target 不同源、未在 `config-deployment.yaml` 授权
  （`owner=build` / `release-managed` / `replace`）、baseline unit 缺失或内容校验失败时构建失败；
- 通用 config overlay 跳过 assembler 已认领路径，双重写入或 owner 不一致即失败；
- 所有 BUILD-owned 文件在 Artifact Manifest 中记录 `owner_service=build`、
  `owner_target=release-set-assembly`、`install_component=ccu-release-set-runtime`、
  source/source SHA/final SHA/mode，不再出现 `unknown` owner。

部署预检会核对包内 `ccu.target` 的 Wants/After、release-set 证据与实际
`.service` payload，错配即失败；依赖序启停、健康检查与回退仍按服务逐步执行，
`ccu.target` 不是一次性替代品。

> **未关闭输入**：受控设备 `vsomeip.json` 内容与评审责任方尚未落地，release
> profile 一旦选择包含 vsomeip 服务的集合就会 fail-closed。开发/CI 无真实后端时
> 可使用仅在非 release profile 有效的显式豁免：
>
> ```bash
> python3 -m ccu_build build --set ccu-dev-fota --profile debug \
>   --allow-missing-platform-assets --dry-run
> ```
>
> 该豁免在 release profile 会被拒绝（CCU-BUILD-DSN-CR-004 §11）。

## DIAG 部署 seed 与文件双向核对（CCU-BUILD-DSN-CR-005）

`ccu-diag-runtime` 的精确安装集是 **5 个文件**：daemon、unit、`diag.yaml`、
schema，以及只读部署 seed `usr/share/ccu-diag/ecu-inventory.seed.json`。

- **登记**：`manifests/services.yaml` 的 `ccu-diag.revision` 固定为 DIAG CR-010
  合入 commit；seed 由服务仓库标准 `install()` 输出，BUILD 不复制、不内联设备值。
- **归属**：Artifact Manifest 记 `owner_service=ccu-diag`、`owner_target=<targets>`、
  `install_component=ccu-diag-runtime`、`staging=rootfs`、`mode=0o644`、
  `source=<service>@<revision>:<path>`、`sha256`；不得记为 `unknown` 或 `build`。
- **双向核对**：`verify_staging()` 的 `stale-assets`（install-root → manifest）与
  `manifest-install-root-parity`（manifest → install-root，逐条比对存在性、SHA-256、
  mode）必须同时通过；缺失或多出 seed 均判定失败。
- **部署**：`manifests/config-deployment.yaml` 以精确路径授权 seed
  `owner=service` / `release-managed` / `replace`，与二进制、unit、配置作为同一
  release-set 备份、安装、校验与回退；`/var/lib/ccu/diag/**`（`vin.dat`、
  `ecu_inventory.json`）保持 preserve，不进入发布包。
- **禁止入包**：`config/dev/**` 开发夹具、`/var/lib/ccu/**` 持久/实例状态及
  `vin.dat`、`ecu_inventory.json` 由 `find_forbidden_payload` 拦截，`verify` 的
  `forbidden-payload` 检查失败，`Packager` 直接拒绝打包。

### CI 流水线

```bash
./ci/pipeline.sh --dry-run                    # 仅 schema + 计划
./ci/pipeline.sh --platform host              # 本机构建
./ci/pipeline.sh --set ccu-dev-minimal        # 目标构建（需 Docker）
```

### 运行测试

```bash
python3 -m pytest tests/ -v
```

## 实施阶段

1. **BUILD 平台能力与内置示例自验证**（CCU-BUILD-DSN-CR-001）
2. Framework 接入：以 CCU framework 验证 library、多 targets、SDK staging（CR-002）
3. 配置发布治理：平台 overlay、策略清单、schema/秘密校验与一致回退（CR-003）
4. 真实服务与 release-set 运行资产：固定 revision 登记、聚合 target、
   平台 vsomeip 资产、制品追溯与部署预检（CR-004）
5. DIAG 部署 seed 安装集：五文件精确集合、manifest/install-root 双向核对、
   制品归属、部署回退与禁止入包检查（CR-005，本仓库当前实现）

## 前置决策（首个可发布构建前必须关闭）

- 目标硬件/SKU、OS/BSP、架构与 ABI
- compiler、C/C++ 标准、sysroot 来源和摘要
- 构建容器/runner、TARGET 依赖供给
- systemd/权限/capability/设备节点
- 包格式、部署通道、配置/秘密边界、目标机测试资源

## 参考

- [SPEC 需求](https://app.notion.com/p/CCU-BUILD-SPEC-3b20ec7a4b9280668ffec5d2a9a1f330)
- [SPEC 设计](https://app.notion.com/p/CCU-BUILD-SPEC-3b20ec7a4b92803fac49c7d11420a75e)
- [DSN-CR-001](https://app.notion.com/p/CCU-BUILD-DSN-CR-001-f95b57404f434d51a1e808dbe6b84964)
- [DSN-CR-004](https://app.notion.com/p/CCU-BUILD-DSN-CR-004-4100ec7a4b928140827dcd1b1f642338)
- [REQ-CR-004](https://app.notion.com/p/CCU-BUILD-REQ-CR-004-4100ec7a4b9281b8a4f4e8a00c5958dc)
- [DSN-CR-005](https://app.notion.com/p/CCU-BUILD-DSN-CR-005-4120ec7a4b9281858209f308dfd933ac)
- [REQ-CR-005](https://app.notion.com/p/CCU-BUILD-REQ-CR-005-4120ec7a4b9281d4a039d86205005e2b)
