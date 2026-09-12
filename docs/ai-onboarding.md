# AI 接手与并行开发说明

这份文件是给没有本次对话上下文的新 Agent 的最小工作包。它不能替代
`AGENTS.md`；开始前必须先读 `AGENTS.md`，然后按下面顺序读取项目状态。

## 10 分钟接手顺序

1. `README.md`：目标、非目标和当前阶段。
2. `coordination/targets.yaml`：领取一个尚未完成的目标。
3. `coordination/integration-board.md`：确认依赖、负责人和集成门禁。
4. `coordination/contract-lock.yaml`：若要改公共契约，先取得对应锁。
5. 目标对应的 `plan/*.md`：确认范围、验收和回滚。
6. 目标负责人的路径清单：只在 `coordination/agents.yaml` 指定的目录写入。
7. Phase 3 目标还要阅读 `coordination/phase-3-waves.yaml` 和
   `coordination/phase-3-dispatch.md`，确认当前波次允许启动。
8. 涉及 P3 集成或私有验收时，阅读 `docs/project-layout-and-acceptance-manual.md`；
   AI 到 `blocked` 或 `ready_for_human` 即停止，不读取 private 数据。

如果目标没有计划文件，先复制 `plan/TEMPLATE.md` 创建计划，不要直接写代码。

## Phase 3 启动限制

`P3-GOV-01`、契约、夹具、索引、筛选、数据库适配、执行器和 application workflow
已经完成。当前 `P3-GUI-FLOW-01`、`P3-ENTRY-01`、`P3-INTEGRATION-01` 处于
`verification`，`P3-PRIVATE-ACCEPT-01` 因依赖未完成而处于 `blocked`。准确状态始终以
`coordination/targets.yaml` 为准。

Phase 3 新模块使用独立路径：`local_index/`、`db_adapter/`、`filtering/`、`gui/`、
`executor/` 和 `application/`。旧 `ui/` 是 Phase 2 兼容层，不得由这些 Agent 顺手修改。
完整顺序与可复制派工口令见 `coordination/phase-3-dispatch.md`。

## 本机开发

仓库推荐放在短路径目录（例如 `C:\Projects\wechat-space-manager`）。首次准备环境：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

最窄验证到完整验证依次为：

```powershell
.\.venv\Scripts\python.exe -m ruff check <changed paths>
.\.venv\Scripts\python.exe -m pytest tests/<owned-test-dir> -q
.\.venv\Scripts\python.exe -m ruff check src tests contracts
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q src tests contracts
```

没有真实微信数据也必须能够用 `tests/fixtures_synthetic/` 完成验证。真实目录、
数据库、密钥和解码产物只能留在本机被忽略的 `tests/fixtures_private.local/`，
不得复制到测试、日志、Issue 或 Agent 消息。

## 可并行的工作单元

下列工作单元只要先冻结契约，就可以由不同 Agent 并行实现：

| 单元 | 代码目录 | 测试目录 | 风险 | 前置条件 |
| --- | --- | --- | --- | --- |
| scanner | `src/wechat_cleaner/scanner/` | `tests/scanner/` | R1 | `ScanManifest` |
| decoder | `src/wechat_cleaner/decoder/` | `tests/decoder/` | R1/R2 | `MediaRecord`, `DecodeRequest`, `DecodeArtifact` |
| mapper | `src/wechat_cleaner/mapper/` | `tests/mapper/` | R1/R2 | `AccountRef`, `ContactRef`, `MediaRecord` |
| cleanup_safety | `src/wechat_cleaner/cleanup/` | `tests/cleanup/` | R1-R3 | `CleanupPlan`, `CleanupReceipt` |
| ui | `src/wechat_cleaner/ui/` | `tests/ui/` | R0/R1 | 只读契约消费 |
| integration | `tests/integration/` | `scripts/verify_*` | R0-R3 | 所有依赖模块的验证证据 |

并行 Agent 不得同时修改同一目录；`contracts/`、`coordination/`、`plan/`、
`docs/` 和 `README.md` 由 orchestrator 管理。契约消费者可以并行写自己的适配器，
但不得通过猜测字段绕过契约锁。

## 每个 Agent 的交接包

完成或阻塞时，必须在消息和事实日志中同时提供：

- 目标 ID、改动文件和风险等级；
- 运行过的精确命令及结果；
- 契约版本与锁状态；
- 合成夹具覆盖范围；
- 未解决项、回滚方法和需要人工决定的事项。

任何涉及 R3/R4 的删除、回收站、真实目录或数据库写入都不能由 Agent、UI 或
大模型直接执行，只能交给独立执行器并逐次人工确认。
