# P4-REAL-DB-01：真实微信数据库适配器

状态：`done`
负责人：`wechat_db_adapter`
风险：`R2`
关联目标：`P4-REAL-DB-01`（依赖 `P4-REAL-MEDIAKEY-01` done）
更新时间：`2026-09-10`

## 结果与非目标

- 结果：新增可选后端 `src/wechat_cleaner/real_db/`——
  - 真实账号发现（数据根下含 `db_storage` 的目录，排除 `all_users`/`Backup` 等非账号目录）；
  - 只读会话与密钥提取（复用 `real_media.keys.open_source_session`，上游惰性导入、控制台捕获）；
  - 匿名库清单：库类别（`message/message_0` 形式）、字节数、密钥状态（`keyed`/`unkeyed`）、
    总数与未取到密钥的类别列表；
  - 匿名聚合摘要（账号数、库数、字节数、密钥覆盖、`cfg_dword_present`），不含账号标识与绝对路径。
- 非目标：不实现消息/联系人映射（属 `P4-REAL-WORKFLOW-01` 与既有 mapper）；不读消息正文；
  不写微信源；不做 integrity check 的批量执行（作为未完成项移交 workflow 阶段）；
  不修改共享契约。

## 前置条件与输入

- 依赖：`P4-REAL-MEDIAKEY-01` done（`F-20260910-011`），上游会话与 cfgDword 路径已验证。
- 输入及其私密等级：真实源 `db_storage`（只读）；解密缓存在应用缓存根内的 `db-cache/`。
- 要取得的共享契约锁：无（本目标不新增契约字段）。

## 实施步骤

- [x] `discovery.py`：账号发现与匿名聚合。
- [x] `inventory.py`：库类别归一、密钥状态、清单统计。
- [x] `adapter.py`：`RealDatabaseAdapter.open/inventory/summary/close`，会话工厂可注入。
- [x] `tests/real_db/`：合成 `db_storage` 树 + 假会话，覆盖发现、非账号目录排除、
      keyed/unkeyed 矩阵、账号选择（最大库优先/显式指定/未知账号拒绝）、无 `_keys` 属性的容错、
      `close` 释放。
- [x] 全仓门禁；结果文档与事实登记。

## 验收

- [x] 可重复验证命令：
  ```powershell
  cd <仓库根>
  .\.venv\Scripts\python.exe -m ruff check src tests contracts scripts
  .\.venv\Scripts\python.exe -m pytest tests/real_db -q
  .\.venv\Scripts\python.exe -m pytest tests -q
  ```
- [x] 预期结果：real_db 12 passed；全仓 190 passed / 5 skipped；ruff 与 compileall 通过。
- [x] 失败/边界情形：数据根不存在 → `INVALID_PATH`；无账号 → `DECODER_UNAVAILABLE`；
      未知账号 → `DECODER_UNAVAILABLE`；会话缺 `_keys` → 全部标 `unkeyed`（诚实默认）；
      摘要字符串不含账号与数据根。

## 风险与回滚

- 风险：依赖上游私有 `_keys` 映射（已在适配层内隔离并注释；上游 revision 固定）；
  密钥提取成功率随进程状态波动（W1 观察到 19/35 与 27/35），本目标如实报告未取到类别。
- 回滚：删除 `src/wechat_cleaner/real_db/` 与 `tests/real_db/`；不影响 synthetic 链路与其他模块。

## 交接

- 改动文件：`src/wechat_cleaner/real_db/{__init__,discovery,inventory,adapter}.py`、
  `tests/real_db/test_real_db.py`、本计划、`coordination/{fact-log.md,integration-board.md,targets.yaml}`。
- 事实日志条目：`F-20260910-031`。
- 契约锁状态：无锁持有。
- 未解决项：
  1. 已解密副本的 `PRAGMA quick_check` 批量执行与失败分类（移交 `P4-REAL-WORKFLOW-01`）；
  2. 输出 `DatabaseEvidenceEnvelope` 的正式接入（需要 mapper 协作，当前只做库级清单）；
  3. 上游 `keys.json` 持久化策略（当前落在应用缓存根 `db-cache/` 内，随 session 清理）。

---

## 结果（版本 A：合成 / 公开）

### 结论

真实数据库适配器已落地为可选后端，实现账号发现、只读密钥状态盘点与匿名聚合摘要；
12 项合成测试覆盖关键路径与失败路径，全仓门禁通过（190 passed / 5 skipped）。

### 验证证据

```powershell
cd <仓库根>
.\.venv\Scripts\python.exe -m ruff check src tests contracts scripts   # All checks passed
.\.venv\Scripts\python.exe -m pytest tests/real_db -q                  # 12 passed
.\.venv\Scripts\python.exe -m pytest tests -q                          # 190 passed, 5 skipped
```

### 门禁对照

| 门禁 | 状态 | 依据 |
| --- | --- | --- |
| 真实数据库门禁（父计划第 2 条） | passed（上轮提供） | `F-20260909-043` 前的真实探测与 `F-20260910-011` 会话初始化；本目标补齐匿名盘点能力 |
| 应用链路门禁 | 未开始 | 属 `P4-REAL-WORKFLOW-01` |

### 数据边界声明

- 测试**未接触**真实微信数据（合成 `db_storage` 树 + 假会话）；
- 密钥未落盘（本模块不做密钥持久化；上游 `keys.json` 落在应用缓存根，随 session 清理）；
- 微信源未变更（本模块只读）；
- cleanup / executor 未运行。

### 回滚方法

删除两个目录即完全回退。
