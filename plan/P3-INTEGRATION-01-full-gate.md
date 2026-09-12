# P3-INTEGRATION-01：Phase 3 完整合成门禁

状态：`done`
负责人：`integration`  
风险：`R3`  
依赖：`P3-ENTRY-01`

## 范围

在全新环境运行 schema/契约、fixture、单元、SQLite 迁移、10 万条性能 smoke、Qt offscreen、
端到端流程、R3 拒绝路径、日志隐私、Ruff、compileall 和入口 smoke。只新增集成测试与
验证脚本，不修改业务实现。

## 验收

输出机器可读的 checks/failures/timings；所有依赖目标已 done、锁已释放、路径所有权无
冲突、仓库不含私密样本。失败退回原 owner，集成 Agent 不越权修复。回滚只移除本目标
新增门禁文件，不降低已存在的安全测试。

## AI 验收边界

本目标只使用公开代码、合成夹具和临时目录。`python scripts/verify_phase3.py` 返回 0，
且所有检查均为 `passed` 时，本目标即可标记为 `done`；不得把读取真实微信目录、私有
数据库、密钥或媒体作为完成条件。

本目标完成后，AI 只运行 `python scripts/check_private_acceptance_readiness.py` 生成
`blocked` 或 `ready_for_human`。两种结果都是一次完整的 AI 就绪评估；AI 随后停止，
不得代替 `human_operator` 执行 P3-PRIVATE-ACCEPT-01。

项目路径和完整交接流程见 `docs/project-layout-and-acceptance-manual.md`。

## 当前验证状态

- 集成门禁与 9 个集成测试已经实现。
- `P3-GUI-FLOW-01`、`P3-ENTRY-01` 已有提交和合成测试证据并更新为 `done`。
- 严格 `python scripts/verify_phase3.py` 返回 0，治理、静态检查、compileall、契约/夹具、
  scanner/mapper/decoder、索引/数据库/筛选、性能、清理/执行器/工作流、集成、入口、Qt
  offscreen 和全仓检查均为 passed。
- 清理安全测试和全仓测试各有 1 个 Windows symlink 能力软跳过；这不是失败，也未通过放宽
  门禁掩盖任何错误。
- 随后的 readiness 检查只输出 `ready_for_human`；P3-PRIVATE-ACCEPT-01 仍由用户接管，
  AI 不执行任何私有验收。
