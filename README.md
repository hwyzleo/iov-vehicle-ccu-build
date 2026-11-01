# iov-vehicle-ccu-build

CCU 统一目标平台构建系统。为 CCU framework、DIAG、FOTA、SEC 及后续服务
提供统一的目标平台构建、依赖管理、制品组织、打包、部署、验证和回退能力。

> **状态**：CCU-BUILD v0.1 草案。由 CCU-BUILD-DSN-CR-001 建立。
> 目标硬件、OS/BSP、架构、toolchain、sysroot 与正式部署通道尚待实机确认；
> 未确认值写入受控 Platform Manifest，并在首个可发布构建前关闭。

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
├── configs/<platform>/rootfs/          平台配置 overlay
├── ccu_build/                          Python 编排器
├── ci/                                 CI 流水线
├── packaging/systemd/                  release-set 聚合 unit
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

1. **BUILD 平台能力与内置示例自验证**（本 CR - CCU-BUILD-DSN-CR-001）
2. Framework 接入：以 CCU framework 验证 library、多 targets、SDK staging
3. 代表性服务试点：以 CCU-DIAG 验证 daemon、systemd、配置和 SOME/IP 冒烟
4. 反馈冻结：回收通用问题并冻结 CCU-BUILD v0.1
5. 逐步推广：接入 FOTA、SEC 及后续服务

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
