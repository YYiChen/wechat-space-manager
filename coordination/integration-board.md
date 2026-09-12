# 集成看板

更新规则：任务开始、阻塞、提交验证或释放契约锁时更新本表。`done` 必须链接到事实日志 ID；仅“代码已写”不得标记完成。

| 目标 | 负责人 | 状态 | 依赖 | 风险 | 集成门禁 | 证据 |
| --- | --- | --- | --- | --- | --- | --- |
| P0-GOV-01 | orchestrator | done | — | R1 | 文档链接、计划模板和事实日志存在 | F-20260909-001 |
| P0-GOV-02 | orchestrator | done | P0-GOV-01 | R1 | YAML 可解析；所有权不重叠 | F-20260909-002 |
| P0-GOV-03 | orchestrator | done | P0-GOV-01 | R1 | 所有契约初始锁空闲且有释放条件 | F-20260909-002 |
| P0-GOV-04 | orchestrator | done | P0-GOV-01 | R1 | 私密路径忽略测试通过；R3/R4 禁令明确 | F-20260909-003, F-20260909-005 |
| P0-GOV-05 | orchestrator | done | P0-GOV-04 | R1 | 聚合基线可追溯且无身份数据 | F-20260909-004 |
| P0-GOV-06 | orchestrator | done | P0-GOV-04 | R1 | 候选依赖有来源、revision、许可证与复用决定 | F-20260909-004 |
| P1-CONTRACT-01 | contracts | done | P0-GOV-01,03,04 | R1 | v1 Pydantic 契约、10 份 JSON Schema 和失败路径测试通过 | F-20260909-006 |
| P1-FIXTURE-01 | fixtures | done | P1-CONTRACT-01 | R1 | 合成树可重复生成；私有验收夹具仅留模板且被忽略 | F-20260909-006 |
| P2-SCAN-01 | scanner | done | P1-CONTRACT-01, P1-FIXTURE-01 | R1 | scanner 专测 4 passed；三模块专项与全仓门禁通过；合成 scanner→mapper smoke 通过 | F-20260909-019, plan/P2-SCAN-01-scanner.md |
| P2-DECODER-01 | decoder | done | P1-CONTRACT-01, P1-FIXTURE-01 | R2 | 副本解码、像素校验、缓存上限、密钥不落盘与失败路径测试齐备；全仓门禁通过 | F-20260909-017, F-20260909-018, F-20260909-019, plan/P2-DECODER-01-decoder.md |
| P2-MAPPER-01 | mapper | done | P1-CONTRACT-01, P1-FIXTURE-01 | R2 | 规范化证据映射、账号边界、冲突拒绝和置信度测试通过 | F-20260909-015 |
| P2-CLEANUP-01 | cleanup_safety | done | P1-CONTRACT-01, P1-FIXTURE-01 | R3 | 不可变计划、执行前身份复核、拒绝路径和收据构造；专项与完整集成门禁通过 | F-20260909-020, F-20260909-023, plan/P2-CLEANUP-01-cleanup-safety.md |
| P2-UI-01 | ui | done | P1-CONTRACT-01, P1-FIXTURE-01 | R1 | 筛选/汇总、预览编排与失败路径、缓存统计与过期清除、CLI JSON 输出与退出码测试齐备；全仓门禁通过 | F-20260909-022, plan/P2-UI-01-ui.md |
| P2-INTEGRATION-01 | integration | done | P2-SCAN-01, P2-DECODER-01, P2-MAPPER-01, P2-CLEANUP-01, P2-UI-01 | R1 | smoke 流 5 passed；隔离验证环境完整门禁 94 passed/1 skipped；Phase 3 单独处理依赖声明 | F-20260909-023 |
| P3-GOV-01 | orchestrator | done | P2-INTEGRATION-01 | R1 | Phase 2 状态与依赖已收口；干净环境门禁、用户归属基线提交和 worktree smoke 通过 | F-20260909-026, plan/P3-GOV-01-phase2-closeout.md |
| P3-CONTRACT-01 | contracts | done | P3-GOV-01 | R2 | 四组 v1.1 契约、22 份 schema、合成夹具与消费者失败路径通过；保留 v1.0 兼容；全门禁 106 passed/1 skipped；四锁已释放 | F-20260909-029, plan/P3-CONTRACT-01-gui-data-contracts.md |
| P3-FIXTURE-01 | fixtures | done | P3-CONTRACT-01 | R1 | 带 seed 的多维文件/记录生成器、2 个支持 + 1 个拒绝 SQLite schema、契约校验与确定性验收通过 | F-20260909-030, plan/P3-FIXTURE-01-gui-fixtures.md |
| P3-INDEX-01 | local_index | done | P3-CONTRACT-01, P3-FIXTURE-01 | SQLite v2 元数据索引、v1→v2 可回滚迁移、稳定游标分页、组合查询、聚合、隐私和清除测试通过 | F-20260909-031, plan/P3-INDEX-01-local-index.md |
| P3-DBADAPTER-01 | wechat_db_adapter | done | P3-CONTRACT-01, P3-FIXTURE-01 | R2 | 两个支持 schema、只读 URI、hash/mtime 复核、账号过滤、冲突证据和失败关闭 | F-20260909-036, plan/P3-DBADAPTER-01-database-copy-adapter.md |
| P3-FILTER-01 | filter_engine | done | P3-CONTRACT-01, P3-FIXTURE-01 | R2 | 组合筛选、稳定分页、保留/资格判定、结构化排除和三种选择快照通过；筛选专项 7 passed | F-20260909-032, plan/P3-FILTER-01-precise-filtering.md |
| P3-GUI-SHELL-01 | gui_shell | done | P3-CONTRACT-01 | R1 | PySide6 壳、分页/条件编辑、线程池 token、取消和错误恢复；Qt 专项待依赖安装后执行 | F-20260909-033, plan/P3-GUI-SHELL-01-desktop-shell.md |
| P3-EXECUTOR-01 | cleanup_executor | done | P3-CONTRACT-01, P3-FIXTURE-01 | R3 | Dry Run、受控隔离/回收存储、全量预检、身份复核、幂等恢复收据；无永久删除 | F-20260909-037, plan/P3-EXECUTOR-01-recoverable-executor.md |
| P3-WORKFLOW-01 | application_workflow | done | P3-INDEX-01, P3-DBADAPTER-01, P3-FILTER-01, P3-EXECUTOR-01 | R3 | 门面集成且 GUI 不获得文件删除能力；应用专项 6 passed；全仓 139 passed/3 skipped；Ruff 与 compileall 通过 | F-20260909-038, plan/P3-WORKFLOW-01-application-workflow.md |
| P3-GUI-FLOW-01 | gui_shell | done | P3-GUI-SHELL-01, P3-WORKFLOW-01 | R3 | 真实 application facade 接线、选择/计划/预检/Dry Run/确认/恢复和 Qt offscreen 合成流程通过 | F-20260909-040, plan/P3-GUI-FLOW-01-desktop-workflow.md |
| P3-ENTRY-01 | orchestrator | done | P3-GUI-FLOW-01 | R1 | 无参数真实 GUI 启动/退出、legacy CLI 分流、依赖 extras、缺 GUI 可操作错误均已验证 | F-20260909-041, plan/P3-ENTRY-01-entrypoint.md |
| P3-INTEGRATION-01 | integration | done | P3-ENTRY-01 | R3 | 严格公开/合成门禁所有检查通过并输出人工交接状态；不读取 private | F-20260909-042, plan/P3-INTEGRATION-01-full-gate.md |
| P3-PRIVATE-ACCEPT-01 | human_operator | blocked | P3-INTEGRATION-01 | R3 | AI 已输出 ready_for_human；本机验收仍由用户执行 | F-20260909-043, docs/project-layout-and-acceptance-manual.md |
| P3-PACKAGE-01 | packaging | planned | P3-PRIVATE-ACCEPT-01 | R2 | 源码/便携包、许可证、校验和、回滚与缓存说明 | plan/P3-PACKAGE-01-windows-package.md |
| P4-GOV-01 | orchestrator | done | P3-INTEGRATION-01 | R1 | P4 六目标与人工目标登记、所有权、波形与派工手册 | F-20260910-001, plan/P4-REAL-ROLLOUT-01.md |
| P4-REAL-MEDIAKEY-01 | decoder | done | P3-INTEGRATION-01 | R2 | cfgDword 派生通过真实 V2 探针校验；真实图片解密为可打开 JPG；源未变更、图片密钥未落盘 | F-20260910-011, plan/P4-REAL-MEDIAKEY-01-media-key-probe.md, 私有报告 P4-REAL-MEDIAKEY-01-media-key-probe.local.md |
| P4-REAL-DB-01 | wechat_db_adapter | done | P4-REAL-MEDIAKEY-01 | R2 | 真实账号发现、只读密钥状态盘点、匿名库清单与聚合摘要；账号选择与拒绝路径；专项 12 passed | F-20260910-031, plan/P4-REAL-DB-01-real-db-adapter.md |
| P4-REAL-MEDIA-01 | decoder | done | P4-REAL-MEDIAKEY-01 | R2 | 密钥派生/校验、v1/v2 识别、真实解密经适配层、像素验证、四态可用性、缓存根约束；专项 32 passed | F-20260910-061, plan/P4-REAL-MEDIA-01-real-media-adapter.md |
| P4-REAL-WORKFLOW-01 | application_workflow | done | P4-REAL-DB-01, P4-REAL-MEDIA-01 | R2 | 一次调用完成发现→盘点→只读扫描→预览→会话清单→局部清理；无删除/移动入口；专项 15 passed | F-20260910-091, plan/P4-REAL-WORKFLOW-01-real-workflow.md |
| P4-REAL-GUI-01 | gui_shell | done | P4-REAL-WORKFLOW-01 | R2 | 只读窗口（向导/筛选/预览/缓存）+ 无删除入口断言；GUI 套件 34 passed；真实数据端到端冒烟通过（35 库/27 密钥/103,852 记录/缩略图预览成功） | F-20260910-111, plan/P4-REAL-GUI-01-real-gui.md, 私有报告 P4-REAL-GUI-01-ui-smoke.local.md |
| P4-REAL-INTEGRATION-01 | integration | done | P4-REAL-GUI-01 | R2 | 公开门禁 7 项全 passed（不触碰私有输入）；真实 harness 端到端通过：35 库/27 密钥/103,847 条记录/3 张缩略图可打开 | F-20260910-131, plan/P4-REAL-INTEGRATION-01-integration-gate.md, 私有报告 P4-REAL-INTEGRATION-01-harness.local.md |
| P4-PRIVATE-ACCEPT-01 | human_operator | ready | P4-REAL-INTEGRATION-01 | R2 | 7 条私有小样本验收由用户执行；AI 已停在就绪点 | F-20260910-131, plan/P4-REAL-ROLLOUT-01.md |
| P4-REAL-PACKAGE-01 | packaging | in_progress | P4-REAL-INTEGRATION-01, P4-PRIVATE-ACCEPT-01 | R2 | 干净 Windows 环境验证、许可证完整、缓存单根、无删除入口 | F-20260910-151, plan/P4-REAL-PACKAGE-01-windows-beta-package.md |

## 统一集成门禁

1. 目标所有依赖为 `done`。
2. 变更没有越过文件所有权，或共享修改有明确审批记录。
3. 对应契约锁、schema 版本、消费者及夹具状态一致。
4. 通过静态检查和最窄相关测试；包含失败路径测试。
5. R2 以上功能已检查日志、异常和缓存不会泄露密钥、内容或绝对路径。
6. R3/R4 相关变更必须有拒绝执行测试；R4 不在自动化或 Agent 工具调用范围。
