# P2-UI-01：只读审查与缓存控制界面

状态：`done`
负责人：`ui`
风险：`R1`
关联目标：`P2-UI-01`（依赖 `P1-CONTRACT-01`、`P1-FIXTURE-01`；上游 `P2-SCAN-01`、`P2-MAPPER-01`、`P2-DECODER-01` 已 done）
更新时间：`2026-09-09`

## 结果与非目标

- 结果：
  - 分层只读 UI：`filters.py`（MediaRecord 纯函数筛选/排序/汇总）、`preview.py`（构造 `DecodeRequest` 并消费 decoder，预览产物定位与打开）、`cache_control.py`（缓存用量/会话统计、过期清除透传、限额报告）、`cli.py`（argparse 薄壳：`browse` / `preview` / `cache` 三个子命令，支持 `--json` 机器可读输出）。
  - 输入为上游契约产物：MediaRecord 列表 JSON（mapper 序列化）等；UI 不自行扫描、不自行映射。
  - 安全边界（R1）：不删除任何文件；唯一的"写"是 decoder 自身缓存（经 decoder 公开 API 过期清除）；不构造、不修改 CleanupPlan；输出不包含微信绝对路径。
- 非目标：不做 GUI 框架（v1 为 CLI，零新增依赖）；不接入 `__main__.py`（该文件归 orchestrator）；不做 CleanupPlan 展示（cleanup_safety 未完成，留待集成阶段）；不改共享契约。

## 前置条件与输入

- 依赖：`P1-CONTRACT-01`、`P1-FIXTURE-01`（done）；消费 `P2-DECODER-01` 公开 API（decode/缓存）。
- 输入及其私密等级：上游契约 JSON 与合成夹具（公开）；真实映射数据仅在本机手工使用（本机私有，不进测试/日志）。
- 要取得的共享契约锁：无（仅消费）。

## 实施步骤

- [x] 领取目标并在集成看板标记 `in_progress`。
- [x] 编写本计划。
- [x] `src/wechat_cleaner/ui/`：filters / preview / cache_control / cli 四模块。
- [x] `tests/ui/`：筛选、汇总、预览编排（含失败路径转 UI 错误）、缓存统计与过期清除、CLI JSON 输出；fixture 注入模式（不跨目录裸 import conftest）。
- [x] 全仓门禁验证（ruff / pytest / compileall）。
- [x] 追加事实日志并回填看板证据。

## 验收

- [x] 可重复验证命令：`.\.venv\Scripts\python.exe -m pytest tests/ui -q`；`-m ruff check src/wechat_cleaner/ui tests/ui`。
- [x] 预期结果：ui 专项全绿；全仓 ruff/pytest/compileall 通过。
- [x] 失败/边界情形：坏 JSON / 契约校验失败 → 可读错误非零退出码；未知 media_id → 明确错误；decode 失败 → `PreviewOutcome.error` 携带契约错误码而非异常上抛；`open_artifact` 拒绝缓存根之外的路径；CLI 无删除任何文件的路径。

## 风险与回滚

- 风险：CLI 输出若不慎包含 `copy_root`/`cache_root` 等本机路径会造成路径暴露（测试断言 JSON 摘要仅含相对信息）；UI 与未来 cleanup_safety 的衔接点（CleanupPlan 展示）未定，留待集成。
- 回滚：删除 `src/wechat_cleaner/ui/` 与 `tests/ui/` 即完全回退；不触碰共享契约与上游模块。
- 需要人工决定：① `__main__.py` 是否挂接 `wechat_cleaner.ui.cli:main` 子命令（该文件归 orchestrator）；② 未来 GUI 壳（Tkinter/Web）与 CleanupPlan 只读展示的形态。

## 交接

- 改动文件：`src/wechat_cleaner/ui/*`、`tests/ui/*`、`coordination/integration-board.md`（状态行）、本计划、`coordination/fact-log.md`（追加）。
- 事实日志条目：验收后登记。
- 契约锁状态：无锁持有。
- 未解决项：见"需要人工决定"。
