"""CCU Build Orchestrator - BUILD v0.1-draft.

提供 manifest 解析、schema 校验、依赖图分析、构建编排、staging、
artifact manifest 生成、ELF/污染检查、打包、部署和验证能力。

平台参数化设计：<platform>、架构、OS/BSP、toolchain、sysroot 和部署通道
由 CCU 实机基线确认后固化，不沿用 TBOX Orin 常量。
"""

__version__ = "0.1.0-draft"
