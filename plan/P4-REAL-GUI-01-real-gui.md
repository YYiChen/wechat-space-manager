# P4-REAL-GUI-01：真实数据只读桌面界面

状态：`done`（窗口、门面、启动器、offscreen 测试与真实数据端到端冒烟均通过）
负责人：`gui_shell`
风险：`R2`
关联目标：`P4-REAL-GUI-01`（依赖 `P4-REAL-WORKFLOW-01` done）
更新时间：`2026-09-10`

## 结果与非目标

- 结果：新增三个 GUI 文件，构成"只读 Beta"的完整前端：
  - `gui/real_session.py`：`RealReadOnlyPort` 协议 + `RealSessionFacade`（包 `RealReadOnlySession`）
    + `FakeRealSessionFacade`（测试/演示）。向导（账号发现与选择）、记录加载、可用性、
    预览（返回成功产物或可读失败原因）、缓存状态与两级清除、会话清单。
  - `gui/real_window.py`：`RealReadOnlyWindow` —— 四区界面：① 连接向导（数据根 + 自动检测 +
    账号下拉 + 连接）② 文件系统维度筛选（类型 / 最小大小 / 可用性）③ 记录表 + 预览面板
    （缩略图/原图切换，失败显示错误码与原因）④ 应用缓存（占用、清除解码缓存、清除全部本地缓存）。
  - `gui/launcher.py`：`launch()` / `main()` 入口 + `discover_default_data_root()` 常见位置探测。
- **安全设计（硬约束）**：
  - 窗口**不存在**任何清理、计划、移动或删除入口——测试逐一检查所有按钮文本不含"删除/清理/回收站/执行计划/移动"；
  - 「清除解码缓存」只删 `<cache_root>/sessions/`；「清除全部本地缓存」只删应用缓存根本身，
    测试断言缓存根之外的文件不受影响；
  - 界面常驻只读声明："只读模式：仅浏览与预览，不会删除、移动或修改微信源文件。"
- 非目标：不接入 `P3` 的清理页与执行器（真实模式无清理能力）；不做真实数据库→联系人映射
  （记录保持 `UNMAPPED`）；不修改 `__main__.py`（归 orchestrator）。

## 原图与预览的语义（明确写入界面与代码文档）

预览是源文件的**解码产物**（`<md5>_t.dat` → 缓存 PNG，一对一）。因此：

| 操作 | 对预览缓存 | 对微信源文件 |
| --- | --- | --- |
| 清除解码缓存 | 删除 | **不触碰** |
| 清除全部本地缓存 | 删除（含密钥缓存） | **不触碰** |
| 删除原图（**当前不存在此能力**） | 未来应同步清理 | 仅可由独立执行器 + 用户逐次确认执行 |

即：**"同步"的正确方向是删原图时清理其预览**，而不是反向；只读 Beta 阶段二者都不会发生。

## 实施步骤

- [x] `real_session.py` 门面与协议、假实现。
- [x] `real_window.py` 四区界面与交互。
- [x] `launcher.py` 入口与常见数据根探测。
- [x] `tests/gui/test_real_session.py`（11 项，无 Qt）、`test_real_window.py`（8 项 offscreen）、
      `test_real_launcher.py`（4 项）。
- [x] 真实连接 + 预览冒烟（已在本机隔离环境完成，见私有报告 P4-REAL-GUI-01-ui-smoke.local.md）。
- [x] 事实登记与看板同步（F-20260910-111）。

## 验收

- [x] 可重复验证命令（Qt offscreen，使用含 PySide6 的环境）：
  ```powershell
  cd <仓库根>
  $env:QT_QPA_PLATFORM = "offscreen"
  <含PySide6的venv>\Scripts\python.exe -m pytest tests/gui -q
  ```
- [x] 预期结果：GUI 套件 33 passed（含既有 10 项）。
- [x] 真实目录探测冒烟：对真实数据根调用向导，正确列出账号（3 个）。
- [x] 真实连接冒烟：向导检出 3 账号并默认选中最大账号 → 连接 35 库/27 密钥 → 103,852 条记录 → 筛选缩略图 17,801 → 预览成功（23.1 KB）→ 缓存 2 文件/1 解码 → 清除 1 个。
- [x] 失败/边界情形：空数据根 → 状态栏提示；未选记录预览 → `NO_SELECTION`；
      视频预览 → `DECODER_UNAVAILABLE` + 原因文本；缓存不清 → 无副作用。

## 风险与回滚

- 风险：真实连接冒烟依赖"PySide6 + 上游"同环境；`__main__` 未挂接前只能由脚本入口启动。
- 回滚：删除三个 GUI 文件与其测试、回退 `gui/__init__.py` 导出；P3 窗口与清理链路不受影响。

## 交接

- 改动文件：`src/wechat_cleaner/gui/{real_session,real_window,launcher}.py`、
  `src/wechat_cleaner/gui/__init__.py`、`tests/gui/test_real_{session,window,launcher}.py`、
  本计划、`coordination/{fact-log.md,integration-board.md,targets.yaml}`。
- 事实日志条目：`F-20260910-111` 段。
- 契约锁状态：无锁持有。
- 未解决项：`__main__.py` 挂接（orchestrator）；原图/高清变体解密（已知限制）；
  联系人/时间维度筛选需真实数据库映射；打包中的 GUI 依赖声明。
