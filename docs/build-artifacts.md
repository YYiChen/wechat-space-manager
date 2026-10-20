# 构建产物、发布指针与保留策略

本文说明 `packaging/build.py` 现在如何登记和保留构建产物。目标是让构建历史可审计、可预览、可回滚，同时**不**把自动批量删除引入日常构建。

## 目录约定

| 路径 | 含义 | 是否可恢复 |
| --- | --- | --- |
| `dist/wechat-space-manager-<UTC 时间戳>/` | 一次性构建的不可变产物 | 可由源码重建；当前版/回滚版应保留 |
| `dist/wechat-space-manager-v<版本>-win64-portable` | 指向时间戳产物的 NTFS Junction | 删除联接不删除产物；移动 dist 后需重建 |
| `dist/LATEST.txt` | 当前发布指针，优先写相对 `dist/` 的目录名 | 单行文本，可手工回写 |
| `dist/build-registry.json` | 构建登记表（started / build_failed / verify_failed / success） | 只追加式元数据，不是产物 |
| `dist/*.zip` | 发布 ZIP，独立恢复来源 | 保留最近 3 个；旧 ZIP 只登记 |
| `build/<UTC 时间戳>/` | PyInstaller 工作缓存（含 PYZ） | 可由下一次构建重建 |
| `build/upstream-staging/` | 精简后的上游 `wechatauto` 暂存 | 构建时重建 |

## 发布顺序

1. `build.py` 在 PyInstaller 运行前写入 `started` 登记。
2. PyInstaller 失败：登记 `build_failed`，**不**修改 `LATEST.txt`。
3. 文件级安全验证失败：登记 `verify_failed`，**不**写清单、联接或指针。
4. 验证通过：写 `BUILD-MANIFEST.json`（含 `build_id`、`built_at`、`verified_at`、源码 revision），创建版本联接，最后原子替换 `LATEST.txt`。
5. 打印保留计划预览；正常构建不删除任何目录。

因此，失败或中断的构建不会成为当前版，也不会覆盖上一成功版。

PyInstaller 的 `--clean` 只清理本次构建的工作缓存，不触碰 `dist/` 中已有的时间戳发布目录、版本联接或 ZIP。构建历史只能通过显式的 `--apply-retention --yes` 收缩。

## 保留策略（默认）

| 类别 | 默认保留 | 说明 |
| --- | --- | --- |
| 成功发布（时间戳目录） | 3 个 | 当前版 + 回滚版 + 1 个近期版 |
| 失败构建 | 5 个或 7 天 | 超过任一条件才进入“可以清理” |
| 中断构建（started） | 24 小时 | 防止把仍在运行的构建当垃圾 |
| 构建工作缓存 | 7 天 | 当前/回滚版的缓存保留，便于 `--verify-only` 检查 PYZ |
| 发布 ZIP | 3 个 | ZIP 是恢复来源，只登记、不自动删除 |
| 版本联接 | 永不删除 | 只重建断开的联接；实体同名目录不覆盖 |

“可以清理”只是计划字段。真正删除还必须显式执行：

```powershell
# 只预览，不删除
python packaging/build.py --retention-plan

# 机器可读预览
python packaging/build.py --retention-plan --json

# 预览后显式应用；没有 --yes 会拒绝
python packaging/build.py --apply-retention --yes
```

应用时会再次校验：路径必须位于本仓库 `dist/` 或 `build/` 下；当前版、回滚版、版本联接、未登记目录和越界路径一律跳过。

## 可迁移性

`LATEST.txt` 现在写入相对 `dist/` 的目录名，例如：

```text
wechat-space-manager-20261010T123828Z
```

旧的绝对路径仍然可读；如果绝对路径失效，解析器依次回退到登记表成功记录和清单中的 `built_at`，**不会**把 `v0.1.5` 与时间戳目录按字典序比较。

NTFS Junction 保存绝对目标，移动整个 `dist/` 后会断开。移动后执行：

```powershell
python packaging/build.py --repair-aliases
```

该命令只重建目标已不存在的联接；仍有效的联接和同名实体目录保持不变。ZIP 与时间戳产物本身不依赖联接。

## 隔离验证

构建脚本支持临时根目录，便于在不碰真实 `dist/` 的情况下验证：

```powershell
python packaging/build.py `
  --dist-root '<temp>\dist' `
  --build-root '<temp>\build' `
  --version 0.1.6
```

`--version` 只覆盖保留测试用清单，不改变应用自身的版本常量。`--build-root` 同时决定 `upstream-staging` 的位置；构建脚本通过 `WCSM_STAGING_ROOT` 把它传给 PyInstaller spec，隔离构建不会写入仓库的 `build/upstream-staging`。

## 环境分工

| 消费者 | 环境 | 说明 |
| --- | --- | --- |
| lint / 单元测试 / 无上游导入检查 | 仓库 `.venv` | 有 ruff、pytest；**没有** pytest-qt 和完整 PySide6，不能跑 GUI 测试 |
| GUI 测试 / 全量测试 / 打包 / 真实只读适配器 | `project-assets/environments/wechat-space-manager-real-readonly-venv-isolated-20260909` | 有 PySide6、pytest-qt、PyInstaller、wechatauto、imageio-ffmpeg、cryptography；依赖见 `packaging/requirements-build-windows.txt` |
| 旧 Phase 3 / mapper / real-adapter 环境 | 待退役 | 能力被上述两个环境覆盖；真实清理由 Space_Clean 执行 |

`scripts/verify_p4.py` 目前仍固定使用 `.venv` 跑全量测试；在 `.venv` 安装 `.[test,gui]` 或由 integration owner 改用 isolated venv 之前，该入口会受到上述 GUI 测试限制。
