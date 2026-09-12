# P3-ENTRY-01：桌面与旧 CLI 入口

状态：`done`
负责人：`orchestrator`  
风险：`R1`  
依赖：`P3-GUI-FLOW-01`

## 范围

统一声明运行、开发、GUI 测试和打包依赖；配置桌面入口并保留显式 legacy CLI 子命令。
默认无参数启动 GUI，headless/自动化环境可明确选择 CLI；文档列出缓存/索引位置与清除
方法。不得在入口绕过 application facade 或注入任意路径执行能力。

## 已实现

- `src/wechat_cleaner/__main__.py`：无参数进入桌面 GUI；`--gui` 显式进入 GUI；`legacy`
  和 `--legacy-cli` 显式转发到现有只读 CLI，保留原有子命令和退出码。
- `src/wechat_cleaner/gui/`：默认 GUI 使用真实 `ApplicationFacadeAdapter`；Fake facade 仅
  通过 `--gui --demo` 或测试显式选择；退出时关闭 facade 工作线程和索引连接。
- `pyproject.toml`：运行时声明 Pillow；提供 `[gui]`、`[test]` 和 `[package]` extras，
  分别用于 PySide6、pytest-qt 和 PyInstaller。
- `tests/entrypoint/`：覆盖 help、不加载 GUI 的 legacy 转发、GUI 参数转发、缺失 GUI 的
  可操作错误和无参数真实 GUI offscreen 启动/退出。

## 验收与回滚

前置 `P3-GUI-FLOW-01` 已由提交 `c3810cdc6cf2bcdf9380a41edfa78a478921b080` 提供真实
facade 接线证据。全新 Python 3.12 venv 实际安装 `.[gui,test]` 成功，并解析
PySide6 6.11.2、pytest-qt 4.5.0；CLI 现有命令及退出码保持兼容，缺 GUI 模块/依赖时给出
可操作错误。入口专项 6 passed；Qt offscreen GUI 专项 10 passed；无参数 GUI 启动/退出
smoke 通过；Ruff、格式检查和 compileall 通过。回滚入口到 CLI-only，不回滚已验证模块。

本目标只验证公开代码和合成/空索引路径；没有读取或操作真实微信目录、数据库、密钥或媒体。
完整 Phase 3 合成门禁仍由 `P3-INTEGRATION-01` 负责，私有验收仍由用户负责。
