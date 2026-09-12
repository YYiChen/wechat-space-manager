# P2-DECODER-01：受约束的媒体解码适配层

状态：`done`
负责人：`decoder`
风险：`R2`
关联目标：`P2-DECODER-01`（依赖 `P1-CONTRACT-01`、`P1-FIXTURE-01`）
更新时间：`2026-09-09`

## 结果与非目标

- 结果：
  - 消费 v1 契约的解码管线：`DecodeRequest` → 校验 → 解码 → 像素验证/缩放 → 受限缓存 → `DecodeArtifact`。
  - 原创格式适配层（不 vendor 任何社区源码）：未加密图片直通；XOR 风格加密 `.dat`（单字节自动推导 + 显式密钥循环 XOR）；`video`/`file` 明确返回 `DECODER_UNAVAILABLE`。
  - R2 纪律：只处理"已批准的副本"（解码前复核 `FileIdentity`）；密钥仅经 `key_provider` 回调进入内存（bytearray，用后清零），不落盘、不进日志、异常或 artifact；缓存有总额/单文件限额与过期元数据；输出只写 `cache_root` 之下。
- 非目标：不解密 SQLCipher 数据库（mapper 输入侧）；不提取密钥（编排层）；不抽视频帧（v1）；不写微信原目录；不修改共享契约。

## 前置条件与输入

- 依赖：`P1-CONTRACT-01`、`P1-FIXTURE-01`（均 done，契约 v1.0 + 合成夹具可用）。
- 输入及其私密等级：合成夹具（公开）；真实副本仅允许在已忽略的 `tests/fixtures_private.local/` 手工验收（机密，本任务不自动触碰）。
- 要取得的共享契约锁：无（decoder 仅消费契约，不修改）。

## 实施步骤

- [x] 领取目标并在集成看板标记 `in_progress`。
- [x] 编写本计划。
- [x] `src/wechat_cleaner/decoder/`：`pipeline.py`（编排/校验/限额）、`formats.py`（探测+适配器注册表）、`key_material.py`（内存密钥纪律）、`cache.py`（会话缓存/限额/过期）。
- [x] `tests/decoder/`：合成加密图端到端解码与像素比对；失败路径（身份不匹配、账号不匹配、缓存超限、损坏数据、密钥缺失、路径逃逸）；密钥不落盘断言。
- [x] 本机 venv 安装 Pillow 并运行最窄验证；向 orchestrator 提交 `pyproject.toml` 增加 `pillow` 依赖的建议（本人无该文件写权限）。
- [x] 追加事实日志并回填看板证据。

## 验收

- [x] 可重复验证命令：`.\.venv\Scripts\python.exe -m ruff check src/wechat_cleaner/decoder tests/decoder`；`.\.venv\Scripts\python.exe -m pytest tests/decoder -q`。
- [x] 预期结果：ruff 无告警；pytest 全绿（decoder 专项 25 passed；全仓 49 passed）；失败路径测试齐全。
- [x] 失败/边界情形：身份不匹配 → `FILE_IDENTITY_MISMATCH`；账号不一致 → `ACCOUNT_MISMATCH`；缓存超限 → `CACHE_LIMIT_EXCEEDED`；哨兵/损坏数据 → `DECODE_FAILED`；缺密钥的加密文件 → `DECODER_UNAVAILABLE`；输出逃出缓存根 → `INVALID_PATH`；测试运行期间任何落盘产物中不得出现密钥字节。（账号一致性由契约模型校验强制，`ACCOUNT_MISMATCH` 由 `MediaRecord.contact_belongs_to_account` 保证，decoder 测试覆盖其消费路径。）

## 风险与回滚

- 风险：微信 4.x 真实 `.dat` 格式未在 v1 用真实样本校准，XOR 适配器覆盖面有限（合成格式可验证，真实格式留待私有夹具手工验收后扩展注册表）；Pillow 依赖变更需 orchestrator 批准，本机验证先行。
- 回滚：删除 `src/wechat_cleaner/decoder/` 与 `tests/decoder/` 即完全回退；不触碰共享契约与其他模块。
- 需要人工决定：① `pyproject.toml` 增加 `pillow>=11`；② 4.x 真实图像格式适配是否进入 v1（需提供本机私有样本做人工验收）。

## 交接

- 改动文件：`src/wechat_cleaner/decoder/*`、`tests/decoder/*`、`coordination/integration-board.md`（状态行）、本计划、`coordination/fact-log.md`（追加）。
- 事实日志条目：待验收后登记。
- 契约锁状态：无锁持有。
- 未解决项：见"需要人工决定"。
