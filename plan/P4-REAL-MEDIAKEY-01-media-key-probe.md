# P4-REAL-MEDIAKEY-01：真实图片密钥解析验证（cfgDword 派生优先）

状态：`planned`
负责人：`decoder`
风险：`R2`
关联目标：`P4-REAL-MEDIAKEY-01`（依赖 `P3-INTEGRATION-01` done；解锁 `P4-REAL-DB-01`、`P4-REAL-MEDIA-01`）
更新时间：`2026-09-10`

## 结果与非目标

- 结果：
  - 在**真实账号**上走通上游首选路径：由 `cfgDword` 确定性派生图片 AES/XOR 密钥
    （`MD5(str(cfgDword) + wxid)[:16]` 与 `cfgDword & 0xFF`），并用真实 V2 `.dat` 密文探针验证。
  - 将**一张**真实加密图片解密为可打开的 PNG/JPEG，输出只落在私有 session 的 `media-preview/`。
  - 产出匿名聚合结果与结果文档；源文件 hash/mtime 前后不变。
- 非目标：不实现 `real_media` 生产模块（属 `P4-REAL-MEDIA-01`）；不批量解密；不碰视频/语音；
  不运行 cleanup/executor；不修改上游源码（除非为只读隔离所需的最小补丁，且需登记）。

## 前置条件与输入

- 依赖：上游 `wechatauto-replica` v1.2.1（commit `798989c9b61066c120b59ceba636b557b776ff7f`，
  Apache-2.0）已 editable 安装于隔离 venv
  本机隔离 venv（含全部声明依赖）。
- 输入及其私密等级：真实微信源目录（本机私有，只读）；真实媒体副本（机密，仅私有 session）。
- 需要用户配合：微信客户端**保持登录并运行**；建议先在任意聊天中打开一张图片。
- 要取得的共享契约锁：无（本目标不修改共享契约；若需新增契约字段，先报 `needs_decision`）。
- 前置阅读：`plan/P4-REAL-WECHAT-READONLY-01.md`、`plan/P4-REAL-ROLLOUT-01.md`、
  `tests/fixtures_private.local/P4-REAL-WECHAT-READONLY-01-handoff.local.md`。

## 实施步骤

- [ ] 新建私有 session 根
  `%LOCALAPPDATA%\WeChatSpaceManager\private-acceptance\<session-id>`，
  内含 `db-cache/`、`media-source-copy/`、`media-preview/`，并复制 `anonymous-result.json` 模板。
- [ ] 写最小探针脚本 `scripts/probe_real_media_key.py`（只读、单文件、无网络）：
  1. 记录源目录版本与账号目录**匿名标识**（不打印 wxid）；
  2. `WeChatDB(db_dir=<源 db_storage>, account=<目标账号>, workdir=<session>/db-cache)` 初始化；
  3. **打印 `db.cfg_dword` 是否非空**（只输出布尔，不输出数值）；
  4. 选取一个真实 `msg/attach/<hash>/<月>/Img/*_t.dat` 作为密文探针，确认其 V2 魔数；
  5. 优先调用 `MediaDownloader.detect_image_key()`（内部即 cfgDword 派生）；若返回 `None`，
     再显式 `MediaDownloader(db, save_dir=..., cfg_dword=db.cfg_dword)` 复现并记录错误码；
  6. 复制**单个** `_t.dat` 到 `media-source-copy/`，记录源/副本 hash 与 mtime；
  7. 从副本解密到 `media-preview/`，用 Pillow + 文件魔数验证可打开；
  8. 前后复核源 `.db` 与源 `.dat` 的 hash/mtime；卸载/置空内存密钥；确认 session 内无密钥文件。
- [ ] 记录匿名结果（按 `plan/TEMPLATE-result.md` 版本 B），**只保留**计数、布尔与错误码。
- [ ] 追加事实条目（`F-20260910-011`–`030` 段内），更新看板与目标计划结果段。

## 验收

- [ ] 可重复验证命令：
  ```powershell
  <隔离venv>\Scripts\python.exe `
    scripts\probe_real_media_key.py --session <session-id>
  ```
- [ ] 预期结果：`cfg_dword_present=true`；图片密钥派生并验证通过；`media_openable=true`；
      `source_db_unchanged=true`、`source_media_unchanged=true`；`key_cache_files_present=false`。
- [ ] 失败/边界情形与降级次序（每级都记录错误码，不得跳级）：
  1. `cfg_dword` 为空 → 记录并检查 `WeChatDB` 初始化是否真的走到同一指针链提取；
  2. 派生密钥验证不通过 → 确认探针取位（V2 为 `head[15:31]`）；
  3. 仍失败 → 显式注入 `image_key`（若用户既有解密钥存在，仅在本机内存使用、不落盘、不写入报告）；
  4. 仍失败 → 监控式扫描（`monitor=True`，用户在微信中打开图片时捕获）；
  5. 全部失败 → 状态 `blocked`，上报错误码分布与 `needs_decision`，**不得索要密钥**。
- [ ] 隐私复核：私有报告通过 `plan/TEMPLATE-result.md` 的禁止项清单；`git status` 无新增真实数据。

## 风险与回滚

- 风险：源目录在被读期间由微信自身写入（上一轮已遇到一次快照差异）→ 复核脚本必须区分
  "自身未写"与"并发变化"，只声明前者；密钥可能瞬时落盘（上游 `image_keys.json` 持久化行为）→
  必须把 workdir 指向 session 根并**在报告中如实记录该文件是否存在**，必要时显式禁用持久化。
- 回滚：删除 session 根目录即可；不触碰主项目模块（本目标只新增一个探针脚本）。

## 交接

- 改动文件：`scripts/probe_real_media_key.py`（新增）、本计划、`coordination/fact-log.md`（追加）、
  `coordination/integration-board.md`（状态行）、`tests/fixtures_private.local/P4-REAL-MEDIAKEY-01-*.local.md`（私有）。
- 事实日志条目：`F-20260910-011`–`030` 段内。
- 契约锁状态：无锁持有。
- 未解决项：`cfg_dword` 派生若失败的具体降级级别；8 个未取到密钥的库与本目标的关联（转 `P4-REAL-DB-01`）。
