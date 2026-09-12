# P3-GUI-FLOW-01：桌面完整工作流接线

状态：`done`
负责人：`gui_shell`  
风险：`R3`  
依赖：`P3-GUI-SHELL-01, P3-WORKFLOW-01`

## 范围

用真实 application facade 替换 GUI 假服务。完成扫描/索引进度、服务端筛选分页、按需
预览、当前页/全部结果/手选三种选择、计划摘要、排除原因、预检、Dry Run、二次确认、
隔离/回收站、收据和恢复入口。保持旧查询结果丢弃和取消语义。

## 验收

Qt offscreen 端到端测试证明 GUI 不直接导入 executor/file APIs；计划摘要变化会使确认
失效；按钮状态与后台任务状态一致；错误不会误报成功。真实 Windows smoke 留给集成和
私有验收。回滚恢复 fake facade，保留可演示 GUI 壳。

## 实施证据与边界

GUI 已通过公开的 `ApplicationFacadePort` 接入筛选、当前页/全部结果/手选、计划摘要、预检、
Dry Run、明确确认、收据和恢复入口；计划或筛选变化会清空旧选择并禁用确认。默认门面为真实
`ApplicationFacadeAdapter`，Fake facade 只通过 `--demo` 或测试显式选择。适配器把线程亲和的
SQLite 工作流固定在专用队列，窗口退出时关闭工作线程和索引连接。`tests/gui/` 在 PySide6
6.11.2、pytest-qt 4.5.0 的 offscreen 环境通过；测试只使用合成夹具和空索引，不删除或移动
任何真实文件。

前置 `P3-GUI-FLOW-01` 的提交证据为
`c3810cdc6cf2bcdf9380a41edfa78a478921b080`；专项 GUI 测试为 10 passed，Ruff、格式检查和
compileall 通过。真实 Windows 私有验收仍不属于本目标。
