# P4-REAL-MEDIA-01：真实媒体解码适配层

状态：`done`
负责人：`decoder`
风险：`R2`
关联目标：`P4-REAL-MEDIA-01`（依赖 `P4-REAL-MEDIAKEY-01` done）
更新时间：`2026-09-10`

## 结果与非目标

- 结果：新增可选后端 `src/wechat_cleaner/real_media/`，把上游 `wechatauto` 的真实媒体能力
  隔离在独立适配层内，对外只暴露本项目契约：
  - 图片密钥的内存态解析（cfgDword 派生 + 真实密文探针校验），不落盘、不进日志；
  - v1/v2 `.dat` 解密 → 像素验证（Pillow + 魔数）→ 输出只写应用缓存根；
  - 媒体可用性状态：`ORIGINAL_AVAILABLE` / `THUMBNAIL_ONLY` / `MISSING` / `UNDECODABLE`；
  - 视频/文件/语音的**只读定位**（返回相对路径与存在性，不复制、不解码）。
- 非目标：不替换现有 synthetic `decoder` 后端；不实现数据库解密（属 `P4-REAL-DB-01`）；
  不做应用编排（属 `P4-REAL-WORKFLOW-01`）；不做 GUI；不运行清理/移动/删除。

## 前置条件与输入

- 依赖：`P4-REAL-MEDIAKEY-01` done（cfgDword 派生路径已真机验证，见 `F-20260910-011`）。
- 上游：`wechatauto-replica` v1.2.1 / commit `798989c9b61066c120b59ceba636b557b776ff7f`，
  Apache-2.0；仅在适配层内 import，缺包时返回 `DECODER_UNAVAILABLE`，不影响其他模块。
- 输入及其私密等级：真实源目录只读（本机私有）；输出仅应用缓存根。
- 要取得的共享契约锁：无（复用现有 `DecodeRequest`/`DecodeArtifact` 契约，不修改）。

## 实施步骤

- [ ] `keys.py`：`RealImageKeys`（派生、校验、内存清零、repr 掩码、`from_source()` 走上游取 cfgDword）。
- [ ] `images.py`：V2/V1 容器解析与解密（经适配层调用上游实现），像素与魔数验证，缩略图缩放。
- [ ] `locate.py`：视频/文件/语音只读定位与可用性判定。
- [ ] `adapter.py`：`RealMediaBackend` 门面，消费 `MediaRecord`，输出 `DecodeArtifact` 与
      `MediaAvailability`；错误映射契约错误码；输出路径包含检查。
- [ ] `tests/real_media/`：合成 V2/V1 容器端到端、密钥不落盘与不回显、可用性矩阵、
      路径逃逸拒绝、上游缺失时的降级；真实兼容性由私有验收 harness 覆盖。
- [ ] 全仓门禁 + 结果文档（`plan/TEMPLATE-result.md` 版本 A）+ 事实条目。

## 验收

- [ ] 可重复验证命令：`.\.venv\Scripts\python.exe -m pytest tests/real_media -q`；
      `-m ruff check src/wechat_cleaner/real_media tests/real_media`。
- [ ] 预期结果：专项全绿；全仓 ruff / pytest / compileall 通过。
- [ ] 失败/边界情形：上游缺失 → `DECODER_UNAVAILABLE`；格式不可识别 → `DECODE_FAILED`；
      密钥缺失 → `DECODER_UNAVAILABLE`；输出逃出缓存根 → `INVALID_PATH`；
      缓存超限 → `CACHE_LIMIT_EXCEEDED`；任何落盘产物不得含密钥字节。
- [ ] 隐私复核：artifact / 日志 / 异常中无密钥、无绝对源路径、无正文。

## 风险与回滚

- 风险：自研解密与真实格式漂移（缓解：真实解密调用上游已验证实现，合成夹具只做回归）；
  上游依赖漂移（缓解：适配层隔离 + 固定 revision + 缺包降级）。
- 回滚：删除 `src/wechat_cleaner/real_media/` 与 `tests/real_media/`；不影响 synthetic 链路。

## 交接

- 改动文件：`src/wechat_cleaner/real_media/*`、`tests/real_media/*`、本计划、
  `coordination/{fact-log.md,integration-board.md,targets.yaml}`。
- 事实日志条目：`F-20260910-061`。
- 契约锁状态：无锁持有。
- 未解决项：SILK 语音转码是否需要额外依赖；视频/文件定位在真实目录的覆盖率。

---

## 结果（版本 A：合成 / 公开）

状态：`done`
完成时间：`2026-09-10`
事实日志条目：`F-20260910-061`

### 结论

真实媒体后端已落地为**可选后端**：密钥派生与校验、v1/v2 容器识别、真实解密调用、
像素与魔数验证、四态可用性判定、只读定位、缓存根约束与路径包含检查全部实现并有测试覆盖。
合成的 synthetic `decoder` 链路未受影响。

### 交付物

| 路径 | 类型 | 说明 |
| --- | --- | --- |
| `src/wechat_cleaner/real_media/keys.py` | 新增 | `RealImageKeys`、`derive_image_keys`、`validate_aes_key`、`open_source_session`（上游惰性导入 + 控制台捕获） |
| `src/wechat_cleaner/real_media/images.py` | 新增 | 容器识别、探针取位、经注入 downloader 的真实解密、Pillow 像素验证 |
| `src/wechat_cleaner/real_media/locate.py` | 新增 | 只读定位与四态可用性（原图/仅缩略图/缺失/不可解码） |
| `src/wechat_cleaner/real_media/adapter.py` | 新增 | `RealMediaBackend` 门面：可用性门禁、缓存写入、artifact 构造、密钥清理 |
| `src/wechat_cleaner/real_media/__init__.py` | 新增 | 公开导出 |
| `tests/real_media/*` | 新增 | 32 项：密钥、格式、定位、编排、隐私不变量 |
| `coordination/{fact-log.md,integration-board.md,targets.yaml}` | 修改 | 状态与证据登记 |

### 验证证据

```powershell
cd <仓库根>
.\.venv\Scripts\python.exe -m ruff check src tests contracts scripts   # All checks passed
.\.venv\Scripts\python.exe -m pytest tests/real_media -q               # 32 passed
.\.venv\Scripts\python.exe -m pytest tests -q                          # 178 passed, 5 skipped
.\.venv\Scripts\python.exe -m compileall -q src tests contracts scripts # OK
```

### 门禁对照

| 门禁 | 状态 | 依据 |
| --- | --- | --- |
| P4 真实媒体门禁（父计划第 3 条） | passed | `F-20260910-011`：真实图片解密为可打开 JPG，源未变更 |
| 私有小样本「预览可打开」 | passed（单样本） | 同上；批量与筛选属 GUI/集成阶段 |
| 应用链路门禁（不依赖手工解密） | 未开始 | 属 `P4-REAL-WORKFLOW-01` |

### 数据边界声明

- 本轮测试**未接触**真实微信数据（全部合成容器 + 假下载器）；
- 密钥未落盘（测试断言 artifact 与缓存文件中不含密钥字节）；
- 微信源未变更（本轮未读写真实源目录）；
- cleanup / executor 未运行。

### 未解决项与下一动作

1. 视频/语音/文件的真实定位覆盖率 → 私有 harness（`P4-REAL-INTEGRATION-01`）。
2. SILK 语音转码是否引入额外依赖 → 待 `P4-REAL-WORKFLOW-01` 决策。
3. 上游 `keys.json` 持久化策略 → `P4-REAL-DB-01` 决定。

### 回滚方法

删除 `src/wechat_cleaner/real_media/` 与 `tests/real_media/` 即完全回退；synthetic `decoder`
链路与共享契约未改动。

### 交接六字段

- 改动文件：见上表
- 验证命令：见「验证证据」
- 验证结果：32 passed（专项）、178 passed / 5 skipped（全仓）、ruff 与 compileall 通过
- 风险等级：R2
- 契约锁状态：无锁持有
- 未解决项：见上
