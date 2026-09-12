# P2-CLEANUP-01：清理计划与安全门

状态：`done`  
负责人：`cleanup_safety`  
风险：`R3`  
关联目标：`P1-CONTRACT-01, P1-FIXTURE-01`  
更新时间：`2026-09-09`

## 结果与非目标

- 结果：根据用户已选择的 `MediaRecord` 生成不可变 `CleanupPlan`，在独立执行器动作前重新核对文件身份，并将执行结果规范化为 `CleanupReceipt`。
- 非目标：不删除文件、不移动到回收站、不启动或停止微信、不写数据库、不调用真实微信目录。

## 输入与输出

- 输入：`AccountRef`、用户选择的 `MediaRecord`、清理处置方式和明确的缓存/永久操作确认。
- 输出：`PlanBuildResult`、`PreflightReport`、`CleanupReceipt`；所有错误使用现有 `ContractErrorCode`，不携带绝对路径、聊天内容或密钥。
- 保护规则：低/无映射、数据库/收藏/`SendTemp`、未知类型、未明确纳入的缓存和越过账号根目录的目标均拒绝。

## 实施步骤

- [x] 实现基于置信度、保护路径和缓存策略的计划构造。
- [x] 实现运行前微信状态、路径包含、重解析点、大小、修改时间和可选 SHA-256 复核。
- [x] 实现只接受逐目标执行结果的收据构造；不提供删除入口。
- [x] 用临时合成文件覆盖成功、过期、越界、微信运行和收据失败路径。
- [x] 由 integration Agent 在 cleanup/UI 完成后执行全链路集成门禁（F-20260909-023）。

## 验收

- [x] 可重复验证命令：`python -m ruff check src/wechat_cleaner/cleanup tests/cleanup`、`python -m pytest tests/cleanup -q`。
- [x] 完整验证：`python -m ruff check src tests contracts`、`python -m pytest -q`、`python -m compileall -q src tests contracts`，以及 `python scripts/verify_integration.py --full`。
- [x] 预期结果：计划不可变；不安全候选被拒绝；任何身份不匹配都会阻止执行；收据目标集合与计划严格一致。
- [x] 失败/边界情形：微信运行、重解析点、路径逃逸、文件大小/修改时间/SHA 不匹配、重复目标、缺少失败错误均拒绝或标记失败。

## 风险与回滚

- 风险：R3 执行器若绕过本模块或忽略 `PreflightReport.can_execute`，可能造成误清理。
- 回滚：删除 cleanup 自有目录和测试即可；不涉及真实文件和公共契约版本。
- 需要人工决定：未来独立执行器的回收站/隔离实现、人工确认 UI 和 `pyproject.toml` 依赖策略。

## 交接

- 改动文件：`src/wechat_cleaner/cleanup/**`、`tests/cleanup/**`。
- 事实日志条目：完成验证后追加唯一的 `F-20260909-020`。
- 契约锁状态：无锁持有；仅消费 v1 `CleanupPlan`、`CleanupReceipt` 和错误码。
- 未解决项：独立执行器、真实微信版本兼容和永久删除均未实现；UI 仍由其目标负责，cleanup 不提供删除入口。
