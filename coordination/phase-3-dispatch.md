# Phase 3 多 Agent 派工手册

本文件回答“谁先干、谁能并行、谁必须串行”。机器可读依赖见
`coordination/phase-3-waves.yaml`，总体验收见 `plan/phase-3-desktop-beta.md`。

## 现在立刻做什么

Phase 3 的契约、夹具、索引、筛选、数据库适配、执行器和 application workflow 已完成。
当前先由 GUI owner 收口 `P3-GUI-FLOW-01`，再由 orchestrator 收口 `P3-ENTRY-01`，随后
integration 重跑 `P3-INTEGRATION-01` 严格合成门禁。P3-PRIVATE-ACCEPT-01 保持
`blocked`，AI 不得提前读取或执行 private 步骤。

## 推荐分配

| Agent 名称 | 目标 | 是否可与同波并行 | 只允许写入 |
| --- | --- | --- | --- |
| 协调 Agent | P3-GOV-01、P3-ENTRY-01 | 否 | 共享治理文件、入口、依赖 |
| 契约 Agent | P3-CONTRACT-01 | 否 | `contracts/`、`domain/`、契约测试 |
| 夹具 Agent | P3-FIXTURE-01 | 单独短波 | 合成与私有模板夹具 |
| 索引 Agent | P3-INDEX-01 | 是 | `local_index/` 与其测试 |
| 筛选 Agent | P3-FILTER-01 | 是 | `filtering/` 与其测试 |
| GUI Agent | P3-GUI-SHELL-01，随后 P3-GUI-FLOW-01 | Shell 可并行；接线串行 | `gui/` 与其测试 |
| 数据库适配 Agent | P3-DBADAPTER-01 | 是 | `db_adapter/` 与其测试 |
| 执行器 Agent | P3-EXECUTOR-01 | 是，但只用合成数据 | `executor/` 与其测试 |
| 应用编排 Agent | P3-WORKFLOW-01 | 否 | `application/` 与其测试 |
| 集成 Agent | P3-INTEGRATION-01 | 否 | 集成测试与验证脚本 |
| 人工操作员 | P3-PRIVATE-ACCEPT-01 | 否 | 不写仓库私密产物 |
| 打包 Agent | P3-PACKAGE-01 | 否 | `packaging/` 与其测试 |

四个总槽位下，协调 Agent 常驻，最多同时开三个实现 Agent。推荐第一批同时派“索引、
筛选、GUI Shell”，全部交接后第二批同时派“数据库适配、执行器”。不要让两个 Agent
共用一个 checkout；每个目标从 Phase 3 基线提交创建独立分支/worktree。

## 每次派工的固定口令

把下面文字中的目标 ID 和计划文件替换后发给 Agent：

> 领取 `<TARGET-ID>`。先完整阅读 `AGENTS.md`、`coordination/targets.yaml`、
> `coordination/path-ownership.yaml`、`coordination/contract-lock.yaml`、
> `coordination/phase-3-waves.yaml` 和对应计划文件。确认依赖均为 done；只编辑分配给
> 你的目录。不得读取真实微信数据，不得修改共享文件，不得删除真实文件。若契约缺字段，
> 停止并向协调 Agent 报 `needs_decision`，不要在消费者中自造字段。完成后跑最窄测试，
> 按 AGENTS.md 交接格式提交证据，不自行把目标标为 done。

## 必须串行的边界

1. `P3-GOV-01` 在所有任务之前；没有基线 commit 就没有并行 worktree。
2. `P3-CONTRACT-01` 独占契约锁；四类契约内部也按“取得锁→迁移/测试→消费者复核→释放”
   逐个完成，不能让多个 Agent 同时编辑 `contracts/`。
3. `P3-WORKFLOW-01` 等待 index、db-adapter、filter、executor 全部完成。
4. `P3-GUI-FLOW-01` 等待 GUI Shell 和 workflow；最好继续由同一个 GUI Agent 完成。
5. 入口、完整集成、私有验收和打包按 W5→W8 严格串行。
6. 任何真实目录验证均由用户执行；Agent 不得替用户点击清理或扩大批准范围。

## AI 到人工验收的终止协议

`P3-INTEGRATION-01` 的完成条件只包含公开代码、合成夹具、临时目录、Qt offscreen 和
失败/隐私门禁，不包含任何 private 数据。严格门禁通过后，AI 运行：

```powershell
python scripts/check_private_acceptance_readiness.py
```

输出 `blocked` 时，AI 报告公开 blockers 后结束；输出 `ready_for_human` 时，AI 把
`docs/project-layout-and-acceptance-manual.md` 交给用户后结束。AI 不领取
`P3-PRIVATE-ACCEPT-01`，不探测私有目录是否存在，也不替用户运行其中任何一步。

人工目标在依赖未完成时为 `blocked`；就绪后由协调 Agent 改为 `ready`，用户开始时改为
`in_progress`，只有匿名聚合证据经过用户确认后才改为 `done`。

## 协调 Agent 的合入检查

- 先审目标路径是否越权，再审契约版本和锁状态，然后跑该目标最窄测试。
- 合入后在主工作树跑相关消费者测试；只有证据追加到事实日志才更新为 `done`。
- 若集成失败，把失败命令和最小复现退回原 owner；integration Agent 不修业务代码。
- 每次只合一个目标，保持可回滚；同一批并行完成不等于必须一次性合并。
