# ITER-20261008 — 聚焦文件夹扫描（folder-focus scan）

- 日期：2026-10-08
- 提出人：用户（直接需求）
- 执行者：迭代维护 Agent（承 v0.1.4 布局修复之后的面向用户体验迭代，先例 F-20260911-014~022 / F-20260912-001 / F-20260914-001）
- 风险等级：R1（只读扫描，不新增任何写路径）
- 状态：in_progress

## 用户场景

用户发现账号内某一个特定文件夹体积异常大（典型如 `msg/attach/<hash>` 的某个
会话附件目录，或 `msg/file/2026-05` 的某个月份目录），但不清楚里面是什么。
希望有一个入口：输入（选择）该文件夹后，工具**只解析这个文件夹**的内容。

## 方案（自底向上）

"聚焦"= 把只读扫描的遍历起点限制在账号根下的一个子树，其余管线（记录构造、
筛选、网格、预览、导出门控）完全复用。关键不变量：**`relative_path` 仍相对
账号根**，因此媒体分类（`msg/attach/...`、`Rec/` 修正表）与会话归属
（md5(username) 反查，F-20260911-015）在聚焦模式下逐字节一致。

| 层 | 文件 | 改动 |
| --- | --- | --- |
| scanner | `src/wechat_cleaner/scanner/scanner.py` | `scan_account()` 新增可选 `subpath`；`_walk_account` 剪枝遍历（只进通往子树的路径与子树内部），其余目录**不 scandir** |
| workflow | `src/wechat_cleaner/application/real_workflow.py` | `RealReadOnlySession.records(..., subpath=None)` 转发 |
| facade | `src/wechat_cleaner/gui/real_session.py` | `RealReadOnlyPort.load_records(subpath=)` + `RealSessionFacade` + `FakeRealSessionFacade` 转发 |
| gui | `src/wechat_cleaner/gui/real_window.py` | 「聚焦文件夹…」「取消聚焦」按钮；聚焦状态摘要（含 attach 会话归属解析）；busy 管线复用 `_run_blocking` |
| packaging | `packaging/verify_artifact.py` | 白名单加「聚焦文件夹…」「取消聚焦」（只读按钮，非 mutating） |

### scanner 剪枝语义（正确性核心）

设规范化的子树前缀 `sub`（相对账号根的路径段元组）。对遍历中每个目录 `parts`：

- `parts == sub[:len(parts)]` → 在通往子树的路径上，继续下探；
- `len(parts) >= len(sub)` 且 `parts[:len(sub)] == sub` → 在子树内，正常收集；
- 其余 → 剪枝，不 `scandir`、不进入 `pending`。

效果：聚焦扫描结果 == 全量扫描结果中 `relative_path` 以该前缀开头的子集
（逐字节），manifest 的 `file_count`/`total_bytes`/`indexed_roots` 相应只反映
子树。`ScanManifest`/`ScanLineage` schema 不变，**不触碰 `contracts/` 锁**。

### subpath 校验（scanner 层，防逃逸）

- 分隔符统一（`\`/`/`），剥首尾分隔符；拒绝空段、`.`、`..`（防路径逃逸）；
- 解析后必须是账号根内**已存在**的目录（非文件）；
- reparse point 拒绝（复用 `_validated_directory` 既有检查）；
- `subpath=None` 与空串 == 全量扫描（向后兼容，既有调用零改动）。

### GUI 交互

- 「聚焦文件夹…」：`QFileDialog.getExistingDirectory`（起始目录 = 当前账号根），
  选定后校验位于账号根内，worker 线程内 `load_records(subpath=...)`；
- 聚焦期间 `filter_summary_label` 前缀显示「已聚焦 <相对路径>」，扫描完成状态栏
  报告 `N 项 / 总大小 / 用时`；聚焦目录命中 `msg\attach\<hash>` 时用已加载的
  `_session_mapping` 反查并显示归属（联系人/群聊），解析不了如实显示 hash 前缀；
- 「取消聚焦」：仅在聚焦状态下可用，回到全账号扫描；
- 聚焦目录在账号根外的选择（用户从文件管理器误选了别的盘）→ 状态栏报错，
  不发起扫描。

## 验收证据（计划）

1. `tests/scanner/`：聚焦扫描 = 全量扫描的精确子集（相对路径、分类、大小逐项
   相等）；子树外目录不被访问；非法 subpath（`..` 逃逸、不存在、指向文件、
   reparse）拒绝；`subpath=None` 行为不变。
2. `tests/application/`：`records(subpath=...)` 转发与 UNMAPPED 语义不变。
3. `tests/gui/`：fake facade 聚焦转发；窗口聚焦/取消聚焦交互、账号根外拒绝、
   会话归属摘要（offscreen）。
4. 全量 `pytest tests -q`（隔离 venv）不低于改动前基线；`ruff check`、
   `compileall` 通过。
5. 真实端到端（私有，手动）：对真实账号的一个大 attach 目录聚焦扫描，
   聚合结果（项数/字节）与全量扫描的对应子集一致——由用户验收，AI 只留脚本
   与说明。

## 回滚方法

单 commit 交付；`git revert` 该 commit 即完全回滚。无数据迁移、无缓存格式
变化、无契约版本变化；既有调用点全部使用默认参数，行为零变化。

## 跨模块所有权说明

本次按 F-20260911-014 先例做跨模块增量改动：`scanner/**`（owner scanner）、
`application/**`（owner application_workflow）、`gui/**`（owner gui_shell）、
`packaging/**`（owner packaging，当前 P4-REAL-PACKAGE-01 in_progress）。均只做
可选参数转发与白名单追加，不改既有语义；fact-log 条目中显式标注请各 owner
复核。不修改 `contracts/`、`coordination/targets.yaml`、`integration-board.md`
（本轮不注册新目标，沿用快速迭代惯例：plan 文档 + fact-log 条目承载痕迹）。

## 版本与发布

本迭代只进源码，不 bump 版本号（4 处版本号与 `DISCLAIMER_VERSION` 联动留给
发布轮统一处理）；不构建冻结包（避免与 packaging Agent 的 in_progress 工作
互相踩踏）。
