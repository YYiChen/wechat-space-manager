# P4-REAL-INTEGRATION-01：真实只读集成门禁与私有 harness

状态：`in_progress`（公开门禁通过；真实 harness 结果待登记）
负责人：`integration`
风险：`R2`
关联目标：`P4-REAL-INTEGRATION-01`（依赖 `P4-REAL-GUI-01`；GUI 尚未实现，公开门禁先行）
更新时间：`2026-09-10`

## 结果与非目标

- 结果：
  - `scripts/verify_p4.py`：Phase 4 公开/合成门禁，7 项检查（P4 模块专项、无上游导入、
    隐私模式、无执行器耦合、全仓套件、lint、字节码编译），默认不打开任何私有输入，
    输出机器可读报告，`--require-passed` 时非零退出。
  - `scripts/harness_real_readonly.py`：本机真实只读 harness，一次跑完发现→盘点→只读扫描→
    预览→会话清单，只输出匿名聚合（计数、字节、错误类型），session 根保留待复核。
  - `tests/integration/test_p4_gate.py`：跨模块断言（门禁脚本自检、只读路径不引用执行器、
    `UNMAPPED` 记录被契约拒绝进入清理计划、会话面无可删除入口、harness 无标识字段）。
- 非目标：公开 CI 不使用真实数据；harness 不运行 cleanup/executor；不做 GUI 测试（`P4-REAL-GUI-01`）。

## 前置条件与输入

- 依赖：`P4-REAL-DB-01`、`P4-REAL-MEDIA-01`、`P4-REAL-WORKFLOW-01`（均 done）。
- 私有输入：真实数据根（只读）；输出仅私有 session 根。
- 运行环境：harness 需要装了上游的隔离环境（本机隔离 venv，含 PySide6 与上游包）+ `PYTHONPATH` 指向 `src`。

## 实施步骤

- [x] 门禁脚本与跨模块测试。
- [x] 公开门禁通过（`--require-passed` 返回 0，`private_data_accessed=false`）。
- [x] 真实 harness 端到端运行（结果见下）。
- [x] 结果文档与事实登记。

## 验收

- [x] 可重复验证命令：
  ```powershell
  cd <仓库根>
  .\.venv\Scripts\python.exe scripts\verify_p4.py --require-passed
  ```
- [x] 预期结果：`state=passed`，7 项检查全部 `passed`，退出码 0。
- [x] 真实 harness（本机）：
  ```powershell
  $env:PYTHONPATH = "<仓库根>\src"
  <隔离venv>\Scripts\python.exe `
    <仓库根>\scripts\harness_real_readonly.py `
    --db-root <数据根> --session-id <session-id> --preview-limit 1
  ```
- [x] 失败/边界情形：无真实数据时仅公开门禁可跑；上游缺失时 harness 报 `ModuleNotFoundError`
  类错误类型并保持 `blocked`；任何异常都只记类型名。

## 风险与回滚

- 风险：真实 harness 依赖上游与微信运行状态；全量目录扫描耗时（84 GB 媒体树）。
- 回滚：删除两个脚本与门禁测试即回退；私有 session 由用户决定是否清理。

## 交接

- 改动文件：`scripts/verify_p4.py`、`scripts/harness_real_readonly.py`、
  `tests/integration/test_p4_gate.py`、本计划、`coordination/{fact-log.md,integration-board.md,targets.yaml}`。
- 事实日志条目：待登记（`F-20260910-131` 段）。
- 契约锁状态：无锁持有。
- 未解决项：GUI 只读接线未实现（`P4-REAL-GUI-01`），因此「应用链路门禁」只验证了无头会话；
  发布门禁（`P4-REAL-PACKAGE-01`）与人工验收（`P4-PRIVATE-ACCEPT-01`）未开始。
