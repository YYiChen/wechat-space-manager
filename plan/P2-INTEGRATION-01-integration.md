# P2-INTEGRATION-01：跨模块集成与发布证据

状态：`done`  
负责人：`integration`  
风险：`R1-R2`  
关联目标：`P2-SCAN-01, P2-DECODER-01, P2-MAPPER-01, P2-CLEANUP-01, P2-UI-01`  
更新时间：`2026-09-09`

## 结果与非目标

- 结果：建立 scanner → mapper → UI/cleanup 契约边界的可重复集成测试和门禁脚本，并区分已验证、待合入和上游阻塞。
- 非目标：不修改 scanner、decoder、mapper、cleanup_safety 或 ui 的业务实现；不读取真实微信目录；不执行解码或清理。

## 集成范围

- 合成目录由 scanner 读取，候选转换为 mapper 的 `MediaCandidate`。
- mapper 输出 `MediaRecord`，由 UI 纯函数筛选/汇总，并可构造 `DecodeRequest` 和 `CleanupPlan` 契约。
- decoder 的真实像素解码、cleanup_safety 的计划执行和 UI 完整测试由各自 Agent 提供；集成层只消费其公开边界。

## 实施步骤

- [x] 建立合成 scanner → mapper → UI/cleanup-contract 流程测试。
- [x] 建立 `scripts/verify_integration.py`，支持当前阶段 `--allow-pending` 与最终完整门禁。
- [x] 记录模块缺失、测试缺失或上游静态错误，不越权修改其他 Agent 文件。
- [x] cleanup/UI 测试和 decoder 修复出现后执行无 `--allow-pending` 的全仓门禁；验证时仅在仓库外隔离环境补装 Pillow，不改项目依赖声明。

## 验收

- [x] 集成专项测试覆盖账号边界、映射置信度、UI 汇总和清理契约组合。
- [x] 脚本输出机器可读的 checks/pending/failures 结果。
- [x] 完整验收：`python scripts/verify_integration.py --full` 返回 0（94 passed、1 skipped；Ruff 与 compileall 通过）。

## 风险与回滚

- 风险：多个 Agent 同时追加事实日志或修改协调看板会产生 ID/状态冲突；集成只引用唯一证据并记录冲突。
- 回滚：删除 `tests/integration/test_phase2_flow.py`、`scripts/verify_integration.py` 和本计划即可，不触碰业务模块。
- Phase 3 决定：GUI 与 legacy CLI 入口由 `P3-ENTRY-01` 统一挂接；decoder/PySide6/Pillow 的可重复依赖由 `P3-GOV-01` 先收口。

## 交接

- 改动文件：`tests/integration/test_phase2_flow.py`、`tests/integration/conftest.py`、`scripts/verify_integration.py`。
- 事实日志条目：`F-20260909-021`（smoke 基线）、`F-20260909-023`（完整门禁）。
- 契约锁状态：无公共字段修改，所有锁保持空闲。
- 未解决项：`pyproject.toml` 尚未声明 Pillow，干净环境可能在 decoder/UI 测试收集阶段失败；真实微信格式与真实数据仍不在本集成验收范围内。
