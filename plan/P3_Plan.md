规划已经正式落到仓库，并转换成适合多 Agent 协作的治理结构。

核心文件：

- [Phase 3 桌面 GUI 总计划](plan\phase-3-desktop-beta.md)
- [多 Agent 派工手册](coordination\phase-3-dispatch.md)
- [机器可读执行波次](coordination\phase-3-waves.yaml)
- [目标与依赖清单](coordination\targets.yaml)
- [路径所有权](coordination\path-ownership.yaml)
- [契约锁](coordination\contract-lock.yaml)
- [集成看板](coordination\integration-board.md)

另外为每个 Phase 3 目标建立了独立计划，共 14 份，位于 [plan 目录](plan)。

执行顺序如下：

1. 串行执行 `P3-GOV-01`（已完成）
   - 收口依赖、干净环境验证。
   - 配置用户真实 Git identity。
   - 建立首个基线提交。
   - 验证 worktree。
   - 首个基线提交：`a3fef5a`；临时 worktree smoke 已通过。

2. 串行执行 `P3-CONTRACT-01`
   - 冻结扫描 lineage、筛选、选择快照、数据库证据、执行和恢复契约。
   - 契约锁必须一个一个取得和释放。

3. 串行完成 `P3-FIXTURE-01`
   - 建立多账号、多联系人、多年份、置信度、受保护记录和数据库版本夹具。

4. 第一批并行，最多三个 Agent：
   - `P3-INDEX-01`：SQLite 本机索引。
   - `P3-FILTER-01`：精确筛选和保留策略。
   - `P3-GUI-SHELL-01`：PySide6 GUI 壳和假服务。

5. 第二批并行：
   - `P3-DBADAPTER-01`：数据库副本适配器。
   - `P3-EXECUTOR-01`：Dry Run、隔离和回收站执行器。

6. 后续全部串行：
   - `P3-WORKFLOW-01`
   - `P3-GUI-FLOW-01`
   - `P3-ENTRY-01`
   - `P3-INTEGRATION-01`
   - `P3-PRIVATE-ACCEPT-01`，由你人工操作
   - `P3-PACKAGE-01`

当前核心实现波次已完成，剩余顺序为：

1. GUI owner 修复 Qt offscreen 兼容问题并完成真实 facade 接线；
2. orchestrator 收口桌面/legacy 入口；
3. integration 运行严格合成门禁；
4. AI 运行私有验收就绪检查并停止；
5. 用户完成人工私有验收；
6. packaging 构建 Windows 发布包。

当前路径和新的 AI→人工流程以
[项目路径、结构与验收手册](docs\project-layout-and-acceptance-manual.md)
为准。后续实现 Agent 仍须使用独立分支/worktree，并遵守路径所有权。

验证结果：

- 5 个治理 YAML 均可解析。
- 28 个目标的 owner 和依赖有效。
- 依赖图无环。
- 14 个 Phase 3 计划全部存在。
- 集成看板 28 行格式一致。
- 现有完整门禁通过：`99 passed, 1 skipped`。
- Ruff 和 compileall 全部通过。
- 本次没有读取真实微信数据，也没有实现或执行任何删除。
