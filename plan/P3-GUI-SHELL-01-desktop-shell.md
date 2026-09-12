# P3-GUI-SHELL-01：PySide6 桌面壳

状态：`done`
负责人：`gui_shell`  
风险：`R1`  
依赖：`P3-CONTRACT-01`

## 范围

以假服务构建数据源、总览、精确筛选、清理任务、设置/缓存五页。实现表格虚拟化/分页、
条件编辑器、详情与预览面板、任务进度、取消、空/错/加载状态、中文与高 DPI。所有后台
任务通过线程池和消息信号返回；任务 token 不匹配时丢弃旧结果。

## 边界

GUI 不导入 scanner/mapper/executor 私有实现，不调用任何文件删除/移动 API，不把联系人
名、路径和筛选内容写进遥测或截图。此目标只消费假 facade，不做真实工作流接线。

## 验收与回滚

Qt offscreen 测试覆盖导航、条件增删、分页/排序、选择范围、取消和错误恢复；手工 smoke
覆盖中文、DPI 与窗口缩放。回滚可整体移除 `gui/`，旧 headless `ui/` 仍可用。

## 实施证据

`src/wechat_cleaner/gui/` 提供懒加载 PySide6 入口、中文四页桌面壳、分页表格、条件编辑器、
记录详情与 facade-backed 预览请求、
线程池任务 token、取消和错误恢复；`ApplicationFacadePort` 与确定性假门面隔离 GUI 与业务模块。
专项测试位于 `tests/gui/test_gui_shell.py`。本机隔离环境已通过 Ruff、compileall 和全仓
`124 passed, 3 skipped`；两个 Qt 专项因验证环境未能装入 PySide6 而被显式跳过，待集成环境
安装 `[gui,test]` 后重新执行 offscreen 门禁。代码不包含删除、移动或执行器调用。
