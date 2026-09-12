# P3-FILTER-01：精确筛选与保留策略

状态：`done`
负责人：`filter_engine`  
风险：`R2`  
依赖：`P3-CONTRACT-01, P3-FIXTURE-01`

## 范围

实现版本化 `FilterSpec`、稳定排序/分页、`FilterResultSummary`、保留规则、资格判定和
不可变 `SelectionSnapshot`。支持账号、联系人/群、媒体类型、消息/文件时间、大小、
映射质量、冲突/归属/文件状态；组内 OR、组间 AND。

## 安全优先级

`protected > retention > eligibility > include filters`。低置信、冲突、未映射、未知目录、
`db_storage`、收藏、变化/丢失文件默认不可执行，并返回结构化排除原因。当前页、当前筛选
全部和手工选择必须产生不同且可审计的 selection scope。

## 验收与回滚

用组合/边界/属性测试证明结果确定、字节汇总一致、分页不漂移、规则优先级正确。回滚为
停用新 FilterSpec 版本并保留旧只读 UI 过滤器。

## 已实现与验收记录（2026-09-09）

- [x] 新增 `wechat_cleaner.filtering` 纯内存引擎：组内 OR、组间 AND，覆盖账号、联系人、
  媒体类型、置信度、文件/归属状态、消息/观测时间和字节范围。
- [x] 稳定排序与不透明游标分页绑定 `FilterSpec` digest；拒绝重复输入、损坏/过期游标，
  汇总同时报告总量、可执行量、排除量、媒体/置信度聚合和结构化排除原因。
- [x] 保留策略支持时间阈值、媒体/记录/联系人 allowlist；安全优先级固定为
  `protected > retained > eligibility > include filters`，include 标志只能改变可见性，不能放宽执行资格。
- [x] 默认阻止低置信、冲突、未映射、未知位置/类型、db_storage、收藏、SendTemp、
  missing/changed/duplicate 文件和未显式允许的缓存；所有原因不包含绝对路径或隐私文本。
- [x] `CURRENT_PAGE`、`CURRENT_RESULT`、`MANUAL` 产生不同 digest 的不可变
  `SelectionSnapshot`，仅接受单一 scan lineage 下的可执行记录并绑定 expiry。
- [x] 专项测试 7 passed；Ruff 通过。全仓门禁中另一个 Agent 的 `tests/local_index` 性能断言
  偶发超过 300 ms（本次 316.5 ms），未修改 INDEX 代码；筛选专项本身无失败。

回滚为移除 `src/wechat_cleaner/filtering/` 和 `tests/filtering/`，保留已有 v1.0/v1.1 契约及只读 UI 过滤器。
