# P3-WORKFLOW-01：无头应用工作流

状态：`done`
负责人：`application_workflow`  
风险：`R3`  
依赖：`P3-INDEX-01, P3-DBADAPTER-01, P3-FILTER-01, P3-EXECUTOR-01`

## 范围

提供 GUI 唯一可用的 application facade：数据源注册、扫描/数据库副本导入、映射、索引、
查询、预览请求、选择快照、计划生成、预检、Dry Run、显式确认后的可恢复执行、收据与
恢复。采用端口/服务接口组合现有模块，不复制业务规则。

## 边界与验收

GUI 不能通过 facade 获得任意路径操作；每个长操作支持 progress/cancel/correlation id，
取消不产生半成计划。合成端到端测试覆盖成功、取消、过期扫描、数据库不支持、选择漂移、
预检变化和执行部分失败。回滚移除 application 层，各模块独立测试仍可运行。

## 实施证据

`src/wechat_cleaner/application/` 提供 `ApplicationFacade`，统一编排 scanner、database-copy
adapter、mapper、local index、filter engine、cleanup planner、preflight、decoder preview
request 和 recoverable executor。扫描与数据库导入先完成可取消的内存阶段，再原子写入索引；
选择快照绑定单一扫描 lineage、过滤摘要和过期时间；计划只接受当前索引中的选中记录，并从
已注册扫描取得权威账号根。确认入口始终要求显式确认和受控恢复目录，结果返回可恢复收据，
部分失败不会被伪装成成功；重开 facade 时只能从索引恢复查询，未重新扫描前不会生成可执行计划。

`tests/application/test_workflow.py` 覆盖扫描/导入/查询/选择/计划/预检/Dry Run/隔离执行/恢复、
取消不提交索引、过期选择、预检漂移、不支持 schema、部分执行失败和索引重开。应用层专项为
6 passed；全仓门禁为 139 passed、3 skipped，Ruff 与 compileall 通过。测试仅使用合成临时目录。

## 回滚

删除 `src/wechat_cleaner/application/` 和 `tests/application/`，恢复目标表与看板为
`planned`，即可移除编排层；scanner、mapper、index、filter、cleanup 和 executor 仍可独立使用。
