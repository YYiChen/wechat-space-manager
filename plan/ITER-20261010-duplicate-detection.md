# ITER-20261010 — 同名与重复文件检测（后端接口）

- 日期：2026-10-10
- 提出人：用户（直接需求，含追加的括号归一化与内容哈希要求）
- 执行者：迭代维护 Agent（承 F-20261008-001 聚焦文件夹扫描之后）
- 风险等级：R1（全部只读：内存分组 + 流式读取文件字节；不新增任何写路径，无密钥参与）
- 状态：planning → 待用户确认后实施
- 本轮边界：**不动 UI 及其交互**；只交付检测功能与后端接口（facade 方法），UI 由下一位 Agent 消费本文档定义的接口实现。

## 0. 前置状态（接手须知）

上一迭代（F-20261008-001，2026-10-08）已交付**文件夹聚焦扫描**并在真实账号端到端验证：

- 后端：`scanner.scan_account(subpath=)` 剪枝遍历 → `RealReadOnlySession.records(subpath=)` → `RealReadOnlyPort.load_records(subpath=)`（真实/假门面均已同步）。
- UI：「聚焦文件夹…」「取消聚焦」按钮已存在（`gui/real_window.py` 的 `focus_button`/`unfocus_button`，`_folder_focus` 状态）。
- 证据：`coordination/fact-log.md` F-20261008-001；计划 `plan/ITER-20261008-focus-folder.md`。
- **重复检测直接复用聚焦语义**：分组只在"当前已加载记录"上进行——聚焦目录模式下检测范围自动收窄到该目录，全账号模式覆盖全账号。无需新的范围参数。

改动尚未提交（工作区 modified，HEAD=v0.1.4 `34b83d8`），版本号未 bump，冻结包未重建。测试基线：487 passed / 4 failed（4 个为 v0.1.4 同款环境性失败，已基线对照确认）。

## 1. 需求语义定义（与用户确认的口径）

用户需求原文归纳：检测同名文件让人知道它们是同名；同名+大小一样+MD5 一样说明是同一个文件，用更严肃的颜色标注；重复文件常被系统/用户重命名为 `xxx (1).jpg` 形态，检测时需剥掉该后缀再比对；内容相同可用 MD5"或者使用其他方法"。

**三级检测信号**（供 UI 映射颜色；本轮只交付信号计算，不交付颜色）：

| 级别 | 判定 | 成本 | 建议颜色 |
| --- | --- | --- | --- |
| S1 规范化同名 | 剥掉扩展名前的 ` (N)` / `(N)` 后缀后，文件名 stem 相同 | 纯内存，零 IO | 黄色 |
| S2 同名同大小 | S1 且 `byte_size` 相同 | 纯内存（大小已在记录内），零 IO | 橙色（中间级，待确认） |
| S3 内容相同 | S2 且对文件**密文字节**的 MD5 相同 | 读文件内容，懒计算 | 红色 |

关键设计决策与理由：

1. **哈希对象 = .dat 密文字节（推荐）**，不是解密后的明文。同一密文必然对应同一明文，"是不是同一份文件"的判定成立；且无需解密、无密钥参与、纯只读 R1，速度取决于磁盘而非解密。明文哈希（跨账号/跨备份比对语义）列为后续可选扩展（R2）。
2. **MD5 而非 SHA-256**：满足用户口径；作为"同一文件"工程判定足够（碰撞需 2^64 量级构造且需配合同大小条件）。实现处留常量可切换。
3. **S3 以 S2 为前提**（符合用户"不仅同名、且大小一样"的分级语义），MD5 只对 S2 候选组懒计算——避免对 10 万文件全量读盘。
4. **括号归一化的固有歧义**：原名本身带 ` (1)` 的文件会被归入同名组——这是规范化无法避免的误归组，由 S2/S3 门兜底（大小不同即不再升级）。文档如实记录。
5. **文件名样式**：兼容 ` (N)`（Windows 资源管理器风格，带空格）与 `(N)`（无空格）；大小写折叠按平台语义；扩展名保留参与比较（`photo.jpg` 与 `photo.png` 不同名）。
6. **protected/cache 记录天然不在检测范围**：records 管线已排除 protected，缓存记录 `is_regenerable_cache=True` 建议排除出重复组（可重复生成，无清理意义），实现为参数默认排除。

## 2. 架构与接口（后端，本轮交付）

### 2.1 新模块 `src/wechat_cleaner/duplicates/`

| 文件 | 内容 | 风险 |
| --- | --- | --- |
| `names.py` | `normalize_stem(filename) -> str`（剥 ` (N)`/`(N)`，纯函数）；`duplicate_key(record) -> tuple[str, str]`（规范化文件名，大小写折叠） | R0 |
| `groups.py` | `same_name_groups(records, *, include_cache=False) -> tuple[DuplicateGroup, ...]`：纯内存分组，S1 组内若大小一致自动升 S2 | R1 |
| `content.py` | `file_md5(path, *, chunk_bytes=1MiB, progress=None, cancel=None) -> str`：流式分块 MD5（先 stat 复核 size+mtime_ns 与记录一致，漂移即拒绝，复用 FileIdentity 语义）；`verify_group(member_paths, ...) -> GroupVerification`：组内逐文件哈希，成员 MD5 全同 → `content_identical=True` | R1 |
| `model.py` | 模块内 frozen dataclass：`DuplicateGroup`（key、display_name、members: tuple[MediaRecord,...]、level: "S1"/"S2"、path→md5 可选映射）、`GroupVerification`（content_identical、md5_by_relative_path、skipped: 逐条原因） | R0 |

**不进 `contracts/`**：结果模型是展示性信息，不进 CleanupPlan 管线；用模块 dataclass 避免契约锁流程。若未来筛选/清理要消费重复组，再由 contracts Agent 升级为正式契约。

### 2.2 门面接口（`gui/real_session.py`，追加到 `RealReadOnlyPort`）

```
duplicate_groups() -> tuple[DuplicateGroup, ...]
    基于已加载记录的 S1/S2 分组；纯内存，UI 线程可直调。
verify_duplicate_group(group) -> GroupVerification
    对一组 S2 候选做 MD5 判定；读文件，必须在 worker 线程（复用 _run_blocking）。
    Fake 门面同步实现（可注入假 md5 结果供 UI 测试）。
```

绝对路径解析复用 `real_media.locate.resolve_source_path`（`source_path` 已有先例：仅 UI/worker 内存使用，不落日志/收据）。

### 2.3 UI（本轮不做，留给下一位 Agent 的消费说明）

- 颜色映射：S1 黄 / S2 橙 / S3 红——委托 `MediaCellDelegate` 角标或格子背景色，数据源 = 窗口持有的 `duplicate index`（record.media_id → level）。
- 建议入口：「重复检测」面板或筛选下拉（列出组、点击组高亮成员）；MD5 验证按钮对选中组触发（worker 线程 + 进度条，组内文件数即分母）。
- 与聚焦的关系：面板标题显示当前范围（全账号 / 已聚焦 <路径>）。

## 3. 实施阶段

| 阶段 | 内容 | 验收 |
| --- | --- | --- |
| D1 | `names.py` + `groups.py`（纯函数与纯内存分组） | 归一化单测（` (1)`/`(1)`/多层括号/中文名/无扩展名/大小写）；分组单测（S1/S2 升级、跨目录、缓存排除、空输入） |
| D2 | `content.py`（流式 MD5 + 身份复核 + 失败降级） | 合成文件单测（同内容不同名/不同内容同名/文件漂移拒绝/缺失跳过并记原因）；大文件分块进度断言 |
| D3 | facade 端口/真实/假三实现 + `tests/gui` 转发测试 | fake 可注入 md5；真实门面走 worker 语义；只读保证测试不新增写路径 |
| D4 | 治理痕迹 + 真实只读探针（聚合数字）+ 本文档收尾 | `pytest`/`ruff`/`compileall` 全绿；探针删除；fact-log 追加 |

## 4. 治理与痕迹

- `coordination/path-ownership.yaml`（owner: orchestrator）需登记 `src/wechat_cleaner/duplicates/**`、`tests/duplicates/**`——本迭代以迭代维护角色登记，fact-log 中标注请 orchestrator 复核（先例：F-20260910-091 real_db 模块登记）。
- 完成后在 `coordination/fact-log.md` 追加 `F-20261010-001`（append-only）：实现、证据、边界、给 UI Agent 的接口消费说明。
- 本文档即接手文档；实现后补充「已完成状态」一节与真实探针聚合数字。
- 不动：`contracts/`、`cleanup/`、`targets.yaml`、`integration-board.md`、版本号、冻结包。

## 5. 回滚

按阶段分别 commit（D1-D4 各一枚），`git revert` 对应提交即可整体或部分回滚；无数据迁移、无缓存格式变化、无契约变化。

## 6. 已完成状态（2026-10-10 实现完成，证据见 fact-log F-20261010-001）

- [x] D1 纯函数与分组（`names.py` / `groups.py`；测试 `tests/duplicates/test_names_groups.py`）
- [x] D2 内容哈希（`content.py`；测试 `tests/duplicates/test_content.py`）
- [x] D3 门面接口（端口/真实/假三实现；测试 `tests/gui/test_real_session.py` 追加 3 项）
- [x] D4 治理痕迹与真实探针（`path-ownership.yaml` 登记 owner=maintainer 待复核；探针已删）

最终验收：全仓 515 passed / 4 failed（与 F-20261008-001 基线同款环境性失败，0 回归）；
ruff / compileall 全绿。真实账号 `msg/file` 区探针：3,741 条记录 → 521 个同名组、
329 个组达到 S2、1,383 条记录在重复组中（括号归一化两种风格均验证命中）。

**留给下一位 UI Agent**：无需改后端。消费 `RealReadOnlyPort.duplicate_groups()`
（UI 线程直调）拿 `DuplicateIndex`——`level_of(media_id)` 返回 `"S1"`/`"S2"`/`None`
映射黄/橙/无标记；对候选组调 `verify_duplicate_group(group)`（必须经
`_run_blocking` worker），`GroupVerification.content_identical=True` 时该组全体
成员标红。Fake 门面 `verify_md5_overrides` 可注入摘要驱动颜色测试。
