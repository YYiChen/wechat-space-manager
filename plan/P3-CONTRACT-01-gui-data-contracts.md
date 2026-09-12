# P3-CONTRACT-01：GUI、索引、筛选与执行契约

状态：`done`
负责人：`contracts`  
风险：`R2`  
依赖：`P3-GOV-01`

## 范围

按锁顺序定义四组兼容契约：`MediaRecordAndScanLineage`、
`FilterSpecSelectionAndSummary`、`DatabaseEvidenceEnvelope`、
`CleanupExecutionAndRecovery`。明确稳定记录键、scan lineage、文件观测时间与消息时间、
结构化证据/冲突、分页、选择快照、计划过期、可恢复位置和处理字节语义。

## 硬规则

- `observed_at` 不再被解释为消息时间；新增字段必须有 v1 兼容策略。
- 计划绑定 source scan、manifest/filter/selection digest 和 expiry。
- 中置信确认默认 false 且绑定计划；低置信、冲突、未映射和受保护项不可执行。
- 一次只激活一个契约锁；同步更新 schema、迁移说明、夹具和消费者契约测试。

## 验收与回滚

四组契约均通过 round-trip、旧版本兼容、未知字段/枚举和失败路径测试，并由所有消费者
书面确认后释放锁。回滚采用保留旧 schema、停用新版本，不覆盖已发布版本。

## 已实现与验收记录（2026-09-09）

- [x] `MediaRecordAndScanLineage`：稳定记录键、扫描 lineage、文件观测状态与消息时间，
  保持既有 `MediaRecord` v1.0 输入可读。
- [x] `FilterSpecSelectionAndSummary`：结构化筛选、排序、分页结果、汇总和不可变选择快照，
  含 digest、过期时间及选择范围约束。
- [x] `DatabaseEvidenceEnvelope`：脱敏数据库证据、来源摘要、置信度与字段级冲突，
  拒绝绝对路径和重复证据键。
- [x] `CleanupExecutionAndRecovery`：dry-run/隔离/回收站执行语义、处理与回收字节、
  不透明恢复定位器，以及计划 lineage、digest 和 expiry 绑定。
- [x] 生成并提交 22 份公开 JSON Schema；新增契约默认版本为 v1.1，既有 v1.0 schema 保留。
- [x] 合成契约夹具和消费者失败路径测试通过；未读取或写入真实微信数据库、聊天内容或私有夹具。
- [x] 最终门禁：`106 passed, 1 skipped`（Windows symlink 能力软跳过），Ruff 与 compileall 通过。
- [x] 四个契约锁均已释放，释放状态写入 `coordination/contract-lock.yaml` 与事实日志。

回滚仍按硬规则执行：保留已发布的 v1.0 schema，停用 v1.1 消费者，不覆盖历史契约或夹具。
