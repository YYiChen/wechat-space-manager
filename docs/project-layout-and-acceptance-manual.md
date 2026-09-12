# 项目路径、结构与验收手册

更新时间：2026-09-09

本手册是当前 Phase 3 的接手入口，重点解决两个问题：新 AI 如何在没有聊天上下文时找到
正确文件，以及 AI 如何在不读取私有微信数据的前提下完成自己的验收并正确交给用户。

## 1. 路径边界

| 类型 | 当前路径或规则 | 用途 | 是否可提交 |
| --- | --- | --- | --- |
| 稳定仓库根 | 短路径目录（如 `C:\Projects\wechat-space-manager`） | 源码、测试、计划和治理 | 是 |
| Python 验证环境 | 项目专用 Python 3.12 venv | 安装开发、GUI 和测试依赖 | 否 |
| 合成夹具 | `tests/fixtures_synthetic/` | AI 和 CI 的完整自动验收 | 是 |
| 私有夹具模板 | `tests/fixtures_private.local.example/` | 告诉用户本机配置格式 | 是 |
| 私有本机配置 | `tests/fixtures_private.local/` | 用户填写真实路径和本机聚合结果 | 否，已忽略 |
| 微信源数据 | 仓库外、用户明确指定的目录 | 最终人工只读与小批量验收 | 永不提交 |
| 解码缓存/恢复材料 | 用户明确指定的仓库外目录 | 可清除缓存、隔离文件和收据 | 永不提交 |

不要把仓库复制回带有对话或 AI 产品名称的深层目录。源码位置保持短且稳定；真实微信
目录、数据库副本、密钥、解码结果、截图和收据始终与仓库分离。

## 2. 仓库结构

```text
<仓库根>
├─ AGENTS.md                       协作、安全和删除硬规则
├─ README.md                       用户入口与当前状态
├─ pyproject.toml                  Python、GUI、测试和打包依赖
├─ contracts/                      版本化 JSON Schema
├─ coordination/                   目标、所有权、波次、锁、看板和事实日志
├─ docs/                           架构、数据边界和本手册
├─ plan/                           每个目标的范围、验收和回滚
├─ scripts/
│  ├─ verify_phase3.py             AI 可执行的完整合成门禁
│  └─ check_private_acceptance_readiness.py
│                                  不读私有数据的人工验收就绪检查
├─ src/wechat_cleaner/
│  ├─ scanner/                     只读目录发现
│  ├─ decoder/                     副本解码与受限缓存
│  ├─ mapper/                      文件到联系人/消息证据映射
│  ├─ local_index/                 本机元数据索引
│  ├─ db_adapter/                  数据库副本只读适配
│  ├─ filtering/                   精确筛选、分页和选择快照
│  ├─ cleanup/                     不可变计划与安全预检
│  ├─ executor/                    Dry Run、隔离和恢复
│  ├─ application/                 唯一业务编排门面
│  ├─ gui/                         PySide6 桌面界面
│  └─ ui/                          Phase 2 legacy CLI 兼容层
└─ tests/                          与各模块一一对应的合成测试
```

## 3. 真实运行链路

```text
只读账号目录 ──> scanner ──┐
数据库副本 ──> db_adapter ──┼─> mapper ─> local_index ─> filtering
                            │                         │
                            └─────────────────────────┘
                                                      ↓
                                             application facade
                                              ↙              ↘
                                         PySide6 GUI     cleanup/executor
                                                           ↓
                                                Dry Run/隔离/收据/恢复
```

GUI 只能调用 application facade。永久删除不属于 Phase 3；数据库原件不得写入；Agent
不得替用户执行真实目录的隔离、回收或清理。

## 4. 当前项目位置

| 目标 | 当前状态 | 说明 |
| --- | --- | --- |
| P3-WORKFLOW-01 及之前的核心模块 | `done` | 合成测试已完成 |
| P3-GUI-FLOW-01 | `done` | 真实 application facade 接线和 Qt offscreen 合成流程已通过 |
| P3-ENTRY-01 | `done` | 无参数真实 GUI、legacy CLI、依赖安装和入口 smoke 已通过 |
| P3-INTEGRATION-01 | `done` | 严格公开/合成门禁已通过 |
| P3-PRIVATE-ACCEPT-01 | `blocked` | readiness 已为 ready_for_human，等待用户执行人工验收 |
| P3-PACKAGE-01 | `planned` | 等待人工私有验收 |

当前 P3-INTEGRATION-01 的详细失败记录见
`tests/integration/P3-INTEGRATION-01-status.md`。

## 5. 新验收流程

### 阶段 A：模块实现与合成测试（AI）

每个模块 Agent 只修改其所有路径，使用 `tests/fixtures_synthetic/` 验证。真实微信目录和
`tests/fixtures_private.local/` 不属于 AI 输入。

### 阶段 B：完整合成门禁（AI）

集成 Agent 执行：

```powershell
python scripts/verify_phase3.py
```

退出码为 0、所有依赖目标为 `done`、所有检查为 `passed` 时，
P3-INTEGRATION-01 可以标记为 `done`。这一结论只代表合成数据与公开边界通过，
**不要求也不允许读取 private 目录**。

### 阶段 C：人工验收就绪判定（AI）

集成完成后，任何 AI 或调度器只运行：

```powershell
python scripts/check_private_acceptance_readiness.py
```

该命令默认始终正常结束，并输出两种机器可读状态之一：

- `blocked`：公开或合成前置条件未完成。AI 报告 blockers 后停止。
- `ready_for_human`：所有公开前置条件完成。AI 把本手册交给用户后停止。

如果调度系统需要用退出码强制要求就绪，可以使用：

```powershell
python scripts/check_private_acceptance_readiness.py --require-ready
```

`--require-ready` 在未就绪时返回 2。无论哪种模式，检查器都不会读取私有配置目录或
真实微信目录。对 AI 而言，成功输出 `blocked` 或 `ready_for_human` 都是一次完成的
就绪评估；AI 不应继续尝试“完成”人类目标。

### 阶段 D：本机私有验收（仅用户）

仅当状态为 `ready_for_human` 时，用户才按以下顺序操作：

1. 确认已有独立备份，并完全退出微信及相关后台进程。
2. 确认目标账号根和私有 `Rec` 夹具，不把路径粘贴到提交、日志或 AI 输出。
3. 先运行只读扫描，只核对匿名聚合数量、大小和少量人工映射。
4. 运行 Dry Run，逐项检查排除原因、计划摘要和预计空间。
5. 首次只选择极小批次，并且必须排除数据库、收藏、未知归属和低置信度文件。
6. 仅使用可恢复隔离模式；验证收据后立即执行一次恢复测试。
7. 用户检查恢复结果，再决定是否扩大范围。Phase 3 不启用永久删除。

出现跨账号、映射歧义、文件身份变化、微信仍在运行、路径越界、恢复失败或计划摘要
变化时立即停止。AI 不得猜测继续，也不得替用户点击确认。

### 阶段 E：发布包（AI）

只有用户完成阶段 D，并提供不含私密信息的“通过/失败 + 匿名聚合”结论后，
P3-PRIVATE-ACCEPT-01 才能标记为 `done`，随后启动 P3-PACKAGE-01。

## 6. 状态与责任规则

| 状态 | 谁可以产生 | 含义 | 下一步 |
| --- | --- | --- | --- |
| `verification` | 模块/集成 Agent | 代码存在但门禁尚未全部通过 | 修复公开测试或退回 owner |
| `blocked` | 调度器/协调 Agent | 前置目标未完成 | 不读取 private，等待解阻 |
| `ready_for_human` | 就绪检查器输出 | AI 工作已结束，人工步骤可开始 | 用户接管 |
| `done` | 负责人 + 协调 Agent | 有可追溯证据且验收完成 | 启动下游目标 |

`ready_for_human` 是检查器输出，不写入 `targets.yaml` 的状态枚举。目标表中
P3-PRIVATE-ACCEPT-01 在等待时保持 `blocked`，用户真正开始后改为 `in_progress`，
人工证据核对后才改为 `done`。

## 7. 证据记录

- AI 合成证据：可以写入 Git，但不得含绝对私有路径、账号、联系人、正文或密钥。
- 私有原始证据：只保存在已忽略的本机目录，不进入 Git、聊天、截图或 Issue。
- 人工完成后：只允许向事实日志追加软件版本、匿名数量/大小、通过/失败和停止原因。
- 状态变更：必须同时更新目标表、计划、集成看板和事实日志；不能只改一个 `status`。

## 8. 新 AI 的固定接手顺序

1. 阅读 `AGENTS.md`。
2. 阅读本手册。
3. 阅读 `coordination/targets.yaml` 和 `coordination/integration-board.md`。
4. 阅读目标计划与 `coordination/path-ownership.yaml`。
5. 若目标是 P3-INTEGRATION-01，只运行合成门禁，不寻找 private 数据。
6. 若目标是 P3-PRIVATE-ACCEPT-01，只运行就绪检查并输出人工交接；不得代替用户执行。
