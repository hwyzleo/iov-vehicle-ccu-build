# /etc/ccu/someip/vsomeip.json —— CCU 平台共享 vsomeip 配置（受控基线初版）

CCU-BUILD-DSN-CR-004 §5.2 / §11：本文件是 **BUILD 拥有的平台运行时资产**，
由 `runtime.platform_assets[]` 声明、ReleaseSetAssembler 唯一安装，不允许
服务各自复制或覆盖。

## 初始基线来源（2026-11-06 快照）

| 字段 | 取值 | 依据 |
|---|---|---|
| `applications[tbox-someip]` | `0x6101` | 共享 routing manager，ID 由 TBOX-SOMEIP 运行时配置固定（`iov-vehicle-tbox-someip/config/runtime/vsomeip.json`）。本车 CCU 与 TBOX 在同一 SOME/IP 域，`routing` 必须指向该已登记应用，否则 vsomeip 自开默认域、跨服务不可见。 |
| `applications[ccu-fota]` | `0x1310` | `/etc/ccu/someip/vsomeip.json`（设备 2026-11-01 部署快照） |
| `applications[ccu-diag]` | `0x1110` | 同上（服务 ID = application ID，CCU-DIAG-DSN-CR-007） |
| `applications[ccu-sec]` | `0x1210` | 同上（CCU-SEC-DSN-CR-004） |
| `routing` | `tbox-someip` | 与 TBOX SOME/IP 同域（设备基线） |
| `service-discovery` / `payload` / `threads` / `network` | 设备基线值 | 设备 2026-11-01 部署快照，非秘密、无设备实例值 |

## 约束

- 不含 VIN、SN、ECU UID、证书、私钥、Token 等设备实例或秘密材料；
- 新增 CCU 服务时必须先在整车 SOME/IP Service Registry 取得 application id，
  再在本文件登记，不得私自取号或别名；
- 修改本文件必须同步 `manifests/config-deployment.yaml`（owner=build，
  release-managed/replace）并通过 `vsomeip-json` 校验。
