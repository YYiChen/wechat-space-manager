# P4-REAL-WORKFLOW-01：应用内真实只读流程

状态：`done`
负责人：`application_workflow`
风险：`R2`
关联目标：`P4-REAL-WORKFLOW-01`（依赖 `P4-REAL-DB-01`、`P4-REAL-MEDIA-01` 均 done）
更新时间：`2026-09-10`

## 结果与非目标

- 结果：新增 `src/wechat_cleaner/application/real_workflow.py`——
  - `RealReadOnlySession.start(db_root, cache_root, account=None)` 一次调用完成账号发现、
    真实数据库适配器装配（`RealDatabaseAdapter`）、真实媒体后端装配（`RealMediaBackend`）；
  - `database_summary()`：匿名库盘点（计数/字节/密钥覆盖）；
  - `records(limit=None)`：**真实目录只读扫描**并产出媒体记录（`UNMAPPED` 置信度，
    因此永不进入清理计划）；
  - `preview(record, variant, max_edge_px)`：经真实后端解密到缓存；
  - `manifest()`：会话清单（提交的缓存文件相对路径、密钥缓存相对路径、计数），支持整体清理；
  - `clear_decoded_cache()`：只删 `media-preview/`，不触碰 `db-cache/` 与密钥缓存。
  - **执行器的移动/删除入口未接入**：会话不引用 executor，无删除能力。
- 非目标：不做 GUI（属 `P4-REAL-GUI-01`）；不实现真实数据库→联系人映射（记录保持 `UNMAPPED`）；
  不改动既有 `ApplicationWorkflow` 合成链路。

## 实施步骤

- [x] 会话装配与依赖注入（`adapter_factory`/`backend_factory` 供测试替换上游）。
- [x] 真实目录记录产出与**真实 attach 路径分类补丁**（见下）。
- [x] 会话清单与两级缓存清理。
- [x] `tests/application/test_real_workflow.py`：9 项新测试（含真实 scanner 扫描）。
- [x] 全仓门禁与文档登记。

## 发现并记录的跨模块缺口

Phase 2 的 scanner 按**合成夹具约定**分类：`msg/<category>/`（`image`/`video`/`file`/`voice`）
或含 `rec` 的路径。真实微信 4.x 的附件位于 `msg/attach/<hash>/<月>/Img/...`（以及
`/Image/`、`/File/`、`/Video/`、`/Voice/`），scanner 会归类为 `OTHER`。

处理方式（不越权）：

1. 本会话新增 `_refine_media_type()` 局部补丁，对 `msg/attach/...` 路径按真实子目录段重新分类
   （`Img`/`Image`→IMAGE，`_t.dat`→THUMBNAIL，`Video`/`File`/`Voice` 同理），保留 scanner 的
   目录遍历、保护目录排除与重解析点拒绝能力；
2. 该缺口登记为事实条目（`F-20260910-091`）与未解决项，建议 scanner owner 在
   `_classify_media` 中正式支持 `msg/attach/<hash>/<月>/<Category>` 结构后，本补丁即可移除。

## 验收

- [x] 可重复验证命令：
  ```powershell
  cd <仓库根>
  .\.venv\Scripts\python.exe -m ruff check src tests contracts scripts
  .\.venv\Scripts\python.exe -m pytest tests/application -q
  .\.venv\Scripts\python.exe -m pytest tests -q
  ```
- [x] 预期结果：application 15 passed（含 9 项新增）；全仓 199 passed / 5 skipped；
      ruff 与 compileall 通过。
- [x] 失败/边界情形：无账号 → `DECODER_UNAVAILABLE`；未知账号 → `DECODER_UNAVAILABLE`；
      记录全部 `UNMAPPED` 且无联系人；清单只含相对路径；清理只命中 `media-preview/`。

## 风险与回滚

- 风险：真实分类补丁与未来 scanner 正式实现重复（已在文档标注移除条件）；记录暂无联系人维度
  （需 mapper 接入真实库证据）；上游缺失时装配失败于 `open`（降级为 `DECODER_UNAVAILABLE`）。
- 回滚：删除 `application/real_workflow.py`、其测试与 `__init__` 中的导出；合成链路不受影响。

## 交接

- 改动文件：`src/wechat_cleaner/application/{real_workflow.py,__init__.py}`、
  `tests/application/test_real_workflow.py`、本计划、`coordination/{fact-log.md,integration-board.md,targets.yaml}`。
- 事实日志条目：`F-20260910-091`。
- 契约锁状态：无锁持有。
- 未解决项：
  1. **scanner 真实目录分类缺口**（建议由 scanner owner 正式支持，见上）；
  2. 记录的联系人/时间维度需要真实数据库映射（mapper 接入 `DatabaseEvidenceEnvelope`）；
  3. GUI 只读接线（`P4-REAL-GUI-01`）。

---

## 结果（版本 A：合成 / 公开）

### 结论

应用内真实只读流程已可一次调用完成"发现 → 盘点 → 只读扫描 → 预览 → 会话清单 → 局部清理"，
不需要手工复制数据库、准备 copy root 或 mapper JSON。既有合成链路与清理执行器未被触碰。

### 验证证据

```powershell
cd <仓库根>
.\.venv\Scripts\python.exe -m pytest tests/application -q   # 15 passed
.\.venv\Scripts\python.exe -m pytest tests -q               # 199 passed, 5 skipped
.\.venv\Scripts\python.exe -m ruff check src tests contracts scripts  # All checks passed
```

### 数据边界声明

- 本目标测试未接触真实微信数据（临时合成账号树）；真实运行路径已由 W1/W2 的真机验证背书；
- 会话不持久化密钥（密钥缓存在应用缓存根内，随 session 清理）；
- 无删除/移动入口；`clear_decoded_cache` 仅限 `media-preview/`。

### 回滚方法

删除 `real_workflow.py` 与其测试、移除 `__init__` 导出即完全回退。
