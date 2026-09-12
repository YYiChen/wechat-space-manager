# P4-REAL-PACKAGE-01：Windows 只读 Beta 发布包

状态：`in_progress`（产物已可运行并通过构建后自检；**人工干净环境验收未完成**）
负责人：`packaging`
风险：`R2`
依赖：`P4-REAL-INTEGRATION-01`（done）、`P4-PRIVATE-ACCEPT-01`（ready，人工验收未完成）
关联：`plan/P4-REAL-ROLLOUT-01.md` 第四节 / W7；`plan/P4-REAL-WECHAT-READONLY-01.md`（父计划）
事实证据：`F-20260910-151`、`F-20260910-152`
私有报告：`tests/fixtures_private.local/P4-REAL-PACKAGE-01-package.local.md`
更新时间：`2026-09-10`

---

## 一、目标

产出一个**在无 Python 的干净 Windows 环境可双击运行**的只读 Beta：

- 用户只启动一个 exe，即可自动发现本机微信 4.x 数据根 → 选账号 → 连接 → 筛选 → 预览；
- 携带 Apache-2.0 LICENSE 与第三方声明、源码 revision、修改说明；
- **不含任何清理、移动、删除能力**；
- 不含任何真实微信数据、密钥、绝对路径。

## 二、范围与非目标

**范围**：PyInstaller one-dir 便携包、许可证随包、冻结入口、构建后安全校验、构建清单。

**非目标**：安装器（MSI/Inno）、代码签名、自动更新、单文件 onefile 模式。

> onefile 被刻意排除：它把全部内容解压到临时目录，既拖慢启动，也让"随包许可证"难以
> 被用户看到，还会让安全审查（检查 archive 内容）变复杂。one-dir 便于逐文件审计。

## 三、交付物

| 路径 | 说明 |
| --- | --- |
| `packaging/wechat-space-manager.spec` | PyInstaller 规格；许可证 data、hiddenimports、排除清理与上游写能力模块 |
| `packaging/frozen_entry.py` | 冻结入口；只 import GUI launcher，`WCSM_SELF_TEST=1` 时输出窗口自检报告 |
| `packaging/stage_upstream.py` | 生成上游**精简副本**（仅 `db.py`/`media.py` + 极简 `__init__`）到 `build/upstream-staging/` |
| `packaging/build.py` | 暂存 → 构建 → 校验 → 清单；输出到带构建 ID 的独立目录；`--verify-only` 单独校验 |
| `packaging/verify_artifact.py` | **拉起冻结 exe** 做启动自检与入口断言（非静态文件检查） |
| `packaging/licenses/wechatauto-replica-LICENSE.txt` | 上游 Apache-2.0 全文（原件复制，未改写） |
| `packaging/licenses/THIRD-PARTY-NOTICES.md` | 第三方组件、revision、修改说明、NOTICE 情况 |
| `tests/packaging/test_build_policy.py` | 免构建的策略测试（spec 只读性、暂存精简性、校验器行为） |
| `dist/wechat-space-manager-<构建ID>/` | 产出物（git-ignored）；`dist/LATEST.txt` 指向最新构建 |

## 四、关键设计决定

### 4.1 为什么排除 `executor` / `cleanup` 而不是"不调用"

策略层面的"UI 不提供删除按钮"是可被绕过的（用户仍可 import 包）。
打包层面**直接把模块从产物里删掉**，使删除能力在冻结产物中根本不存在 —— 这是可验证的
结构性保证，而非约定。`packaging/build.py::verify()` 会复查这一点。

### 4.2 上游许可证处理

`wechatauto-replica` 以 `hiddenimports` + `collect_data_files` 引入。其 Apache-2.0
`LICENSE` 原件复制进 `packaging/licenses/` 并作为 data 随包落到 `licenses/`。
**未修改上游任何源码**，故 `THIRD-PARTY-NOTICES.md` 记"无修改 + 无 NOTICE 转发义务"。

### 4.3 构建后隐私扫描

`verify()` 遍历 `_internal/` 下文本类文件，匹配 `wxid_...` 与 `xwechat_files`。
命中即失败。与 `scripts/verify_p4.py::check_privacy_patterns` 保持同一口径。

## 五、执行步骤

```bash
# 1. 环境：需含 PySide6 + wechatauto + PyInstaller
#    本机可用：预装 PySide6 + wechatauto + PyInstaller 的隔离 venv
python -m pip install "pyinstaller>=6.11,<7"        # 若缺（清华源 403 时用阿里云源）

# 2. 构建 + 校验 + 清单（产物写入 dist/wechat-space-manager-<构建ID>/）
python packaging/build.py

# 3. 冻结产物启动自检（拉起 exe，断言无删除入口）
python packaging/verify_artifact.py
python packaging/verify_artifact.py --json          # 机器可读

# 4. 仅校验文件（不拉起 exe）
python packaging/build.py --verify-only

# 5. 策略测试（不触发构建）
python -m pytest tests/packaging -q
```

## 六、验收

- [x] 可执行文件 `wechat-space-manager.exe` 存在（约 6.0 MB）
- [x] `licenses/wechatauto-replica-LICENSE.txt` 与 `licenses/THIRD-PARTY-NOTICES.md` 随包
- [x] `BUILD-MANIFEST.json` 含源码 revision 与上游 revision
- [x] PYZ 中 `executor` / `cleanup` 模块数为 0
- [x] PYZ 中上游写能力模块（`wx`/`guia`/`moment`/`sender`/`uia_driver`/`msgs`/`ui`/`uia`/`utils`）全为 0
- [x] PYZ 中上游只读模块（`wechatauto` / `wechatauto.db` / `wechatauto.media`）均存在
- [x] 隐私正则扫描无命中
- [x] 冻结 exe 启动成功，窗口标题「微信空间管理器（只读预览）」
- [x] UI 按钮清单无任何删除/清理源文件入口（`packaging/verify_artifact.py` PASSED）
- [ ] 干净无 Python 环境启动 → 发现账号 → 读库 → 筛选 → 预览 通过（**需用户确认**）

## 七、回滚

删除 `dist/` 与 `build/` 即可；本目标不写任何仓库外状态，不改微信源目录。
若产物异常，`packaging/` 三个文件本身不影响源码运行路径。

## 七之二、打包暴露并修复的两处真实缺陷

打包的价值就在于它能暴露源码测试发现不了的问题。本次发现两个：

### 7.2.1 上游包未真正进入产物（假成功）

首轮构建报"Build complete"且体积仅 130 MB，看起来是"瘦身成功"。但警告文件显示
`wechatauto` 未被解析：它是 **editable 安装**，PyInstaller 静态分析无法跟随 PEP 660
finder，最终只收集到 4 个资源文件（3 个 png + `py.typed`），**Python 代码一个都没进包**。

后果：若交付此产物，用户点击「连接」必然 `ModuleNotFoundError`。

修复：`packaging/stage_upstream.py` 生成仅含 `db.py`/`media.py` 与极简 `__init__.py` 的
精简副本到 `build/upstream-staging/`，由 spec 的 `pathex` 优先解析。

> 教训：**构建成功 ≠ 产物可用**。必须检查 PYZ 归档里的实际模块清单，
> 而不是只看构建退出码和产物体积。

### 7.2.2 只读路径被合成路径的清理代码污染（真崩溃）

排除 cleanup 后，冻结产物启动即崩溃：`ModuleNotFoundError: No module named
'wechat_cleaner.cleanup'`。

根因：`application/__init__.py` 与 `gui/__init__.py` 都在**顶层**导入合成路径 facade，
而 `workflow.py` / `services.py` / `window.py` 顶层硬导入 cleanup。即**仅导入只读门面
也会被迫拉起清理代码** —— 这是安全边界的实质性漏洞，不只是打包问题。

修复：两处 `__init__.py` 改为 **PEP 562 懒加载**。已验证导入 `gui` + `real_session` 后
cleanup/executor 均未加载；访问 `ApplicationFacadeAdapter` 时才拉起；既有 API 向后兼容。

> 所有权提示：这两个 `__init__.py` 分别归 `application_workflow` 与 `gui_shell`。
> 本次因打包阻塞而修改，建议由对应 owner 复核确认。

## 八、停止点（本次未做）

- 未在无 Python 的干净环境实测启动（需用户机器，属人工验收范围）；
- 未做代码签名、未做 MSI/Inno 安装器；
- 未实现 `__main__.py` 对 `--gui` 的正式挂接（orchestrator 所有权）。

## 九、已知限制

- 依赖当前 isolated 环境；环境内 `wechatauto-replica` 为 editable 安装，打包时按
  已安装位置收集（`direct_url.json` 记录本机路径，不进入产物）。
- 原图/`_h.dat` 容器仍无法解密，Beta 只呈现缩略图预览（见 `P4-REAL-MEDIA-01` 未完成项）。
