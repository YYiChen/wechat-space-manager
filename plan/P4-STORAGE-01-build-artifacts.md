# P4-STORAGE-01：构建产物登记、保留与发布指针

- 目标：解决时间戳构建/发布目录无界累积、版本号目录重复、`LATEST.txt` 在验证前更新且不可迁移的问题。
- Owner：packaging（集成与协调：orchestrator）
- 风险：R1（只改构建/发布工具；不触碰微信数据、不删除现有产物）
- 依赖：无；与 `P4-REAL-PACKAGE-01` 共用 `packaging/`。
- 范围外：不执行现有 `dist/`、`build/` 的批量清理或搬迁；实际空间整理由 `C:\Projects\Space_Clean` 执行。

## 1. 范围与约束

1. 每次构建仍写入新的时间戳目录，避免 PyInstaller 覆盖不可变产物。
2. 新增 `dist/build-registry.json`，记录 `started` / `build_failed` / `verify_failed` / `success`，失败构建不再只能靠目录猜测。
3. 版本号目录只作为 NTFS Junction 指向时间戳产物；已有实体目录不覆盖、不删除。
4. `LATEST.txt` 先验证后原子更新；写入相对 `dist/` 的目录名，旧绝对路径继续可读。
5. 保留策略只提供预览；`--apply-retention --yes` 才删除，且保护当前版、回滚版、版本联接和未登记目录。
6. 默认保留：成功版 3 个（当前 + 回滚 + 1 个近期）、失败构建 5 个或 7 天、中断构建 24 小时、工作缓存 7 天、ZIP 3 个（ZIP 仅登记，不自动删除）。
7. 移动 `dist/` 后 `LATEST.txt` 仍可解析；版本联接因 NTFS 绝对目标会断开，用 `--repair-aliases` 重建，不覆盖仍有效的联接或实体目录。

## 2. 改动

- `packaging/build_store.py`：登记表、指针解析、版本联接、保留计划、显式应用、联接修复。
- `packaging/build.py`：先登记构建，验证通过后写清单/联接/`LATEST`；新增
  `--dist-root`、`--build-root`、`--build-id`、`--version`、`--retention-plan`、
  `--apply-retention`、`--repair-aliases`、`--json`。
- `packaging/stage_upstream.py`：新增 `--staging-root`，允许把裁剪后的上游包写到隔离构建根。
- `packaging/wechat-space-manager.spec`：读取 `WCSM_STAGING_ROOT`，不再无条件使用仓库 `build/upstream-staging`。
- `packaging/verify_artifact.py`：改用同一解析器，不再按目录名字典序猜最新版。
- `tests/packaging/test_build_store.py`：多构建、失败、混合命名、移动、联接修复、保留与越界保护。
- `docs/build-artifacts.md`：操作说明与策略表。

## 3. 验收证据

在仓库内使用 isolated venv：

```powershell
$env:PYTHONPATH='<repo>\src'
<isolated>\Scripts\python.exe -m pytest tests/packaging -q
<isolated>\Scripts\python.exe packaging/build.py --retention-plan --json
```

隔离构建（不写仓库 `dist/`）：

```powershell
$env:PYTHONPATH='<repo>\src'
<isolated>\Scripts\python.exe packaging/build.py `
  --dist-root '<temp>\dist' --build-root '<temp>\build' --version 0.1.6
```

已验证：

- 连续两次真实构建：v0.1.5 → v0.1.6；`LATEST.txt` 为相对目录名；两个版本联接均建立。
- 隔离构建 `20261020T084812Z` 在临时根完成：`[stage] staging root` 指向临时 `build/upstream-staging`，仓库同名目录 8 个文件的 SHA256 前后一致；产物 239 文件/208.2 MiB，`--verify-only` 与冻结自检通过（19 按钮，`clipped_buttons: []`）。
- 两次中断构建被登记为 `build_failed`，`LATEST.txt` 保持上一成功版；未生成版本联接。
- 将整个 dist/build 移到含空格的新根后，`--verify-only` 与冻结自检仍通过；`--repair-aliases` 重建断开的版本联接。
- 将失败构建/工作缓存时间调旧后执行 `--apply-retention --yes`：删除 6 项/498.0 MB，当前版、回滚版和两个版本联接保留，随后 `--verify-only` 通过。
- 近 7 天内的失败构建默认只是“仅登记”，不自动删除。
- 所有删除仅发生在临时目录；仓库真实 `dist/`、`build/` 未删除/搬迁。

## 4. 回滚

- 代码回滚：`git revert` 本目标提交，或将 `packaging/build.py`、`packaging/build_store.py`、
  `packaging/verify_artifact.py`、`tests/packaging/test_build_store.py` 恢复到上一版本。
- 数据回滚：`LATEST.txt` 是单行文本，可手工写回上一成功版的绝对或相对路径；
  旧版 `build.py --verify-only` 仍可读取绝对路径。
- 产物回滚：版本号 Junction 不复制内容，删除联接不会删除时间戳产物；ZIP 是独立恢复来源。
- 不删除 `build-registry.json`，它只是追加式登记；旧版本代码会忽略该文件。

## 5. 未解决项

- `scripts/verify_p4.py` 仍硬编码 `.venv`，而 `.venv` 缺少 pytest-qt/full PySide6，跑 GUI 测试会崩溃；脚本归属 integration，需由该 owner 改为测试用 isolated venv（或在 `.venv` 安装 `.[test,gui]`）。本目标不改该文件。
- 真实 `dist/` 的旧历史没有 `build-registry.json`；首次新构建会创建登记表，旧成功版通过清单识别，旧工作目录按“孤儿缓存”分类。
- ZIP 在策略中只登记不自动删除，因其是恢复来源；是否退役由 Space_Clean 逐文件核验后决定。
