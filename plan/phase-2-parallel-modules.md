# Phase 2：可并行模块实现计划

状态：`done`  
负责人：`orchestrator`  
风险：`R1-R3`  
关联目标：`P2-SCAN-01, P2-DECODER-01, P2-MAPPER-01, P2-CLEANUP-01, P2-UI-01, P2-INTEGRATION-01`  
更新时间：`2026-09-09`

## 结果与非目标

- 结果：为五个可独立开发的模块冻结目录所有权、输入契约、合成夹具和集成顺序。
- 非目标：Phase 2 不允许 Agent 读取或修改真实微信目录，不允许写入聊天数据库，不允许执行永久删除。

## 并行分组

契约和 Phase 1 夹具已冻结后，`scanner`、`decoder`、`mapper`、`cleanup_safety`、
`ui` 可在各自目录同时开发。它们必须通过合成夹具或副本接口协作；不得互相直接
修改源代码或测试目录。`integration` 等待五个模块的最窄测试通过后再接入。

| 目标 | 只允许写入 | 输入契约 | 最小验收 |
| --- | --- | --- | --- |
| P2-SCAN-01 | `src/wechat_cleaner/scanner/`, `tests/scanner/` | `AccountRef`, `ScanManifest` | 账号根目录隔离、重解析点拒绝、确定性清单 |
| P2-DECODER-01 | `src/wechat_cleaner/decoder/`, `tests/decoder/` | `MediaRecord`, `DecodeRequest`, `DecodeArtifact` | 副本输入、缓存上限、像素验证、密钥不落盘 |
| P2-MAPPER-01 | `src/wechat_cleaner/mapper/`, `tests/mapper/` | `AccountRef`, `ContactRef`, `MediaRecord` | 脱敏数据库、副本读取、置信度与不支持版本拒绝 |
| P2-CLEANUP-01 | `src/wechat_cleaner/cleanup/`, `tests/cleanup/` | `CleanupPlan`, `CleanupReceipt` | 身份复核、路径边界、低置信度拒绝、无直接删除 |
| P2-UI-01 | `src/wechat_cleaner/ui/`, `tests/ui/` | 所有只读契约 | 预览/筛选/缓存控制不绕过安全门 |

## 接入顺序

1. 每个 Agent 领取目标、更新集成板为 `in_progress`，并在自己的计划文件写明
   输入、输出、失败路径和回滚。
2. 先跑自己的最窄测试，再由 `integration` 运行全套测试和私密泄漏检查。
3. 只有集成证据写入事实日志后，目标才能标记 `done`；共享契约锁保持空闲，
   除非确有字段变更并完成迁移说明。

## 回滚与阻塞

- 模块失败时只回滚该模块分支/目录，保留其他 Agent 的改动和契约版本。
- 发现契约缺口时暂停消费者实现，向 orchestrator 提交 `needs_decision`，不得
  在消费者内私自扩展字段。
- 任何 R3/R4 需求超出本计划，必须另立目标并由独立执行器处理。
