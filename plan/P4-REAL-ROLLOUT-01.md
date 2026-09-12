# P4-REAL-ROLLOUT-01：真实微信只读 Beta 推进总计划

状态：`planned`
负责人：`orchestrator`（推进协调）；各子目标见第四节
风险：`R2`（真实密钥进程内使用、解密缓存、媒体预览）
关联目标：`plan/P4-REAL-WECHAT-READONLY-01.md`（父计划）及其六个子目标
更新时间：`2026-09-10`

---

## 一、目标与完成定义（Definition of Done）

用户只启动「微信空间管理器」一个软件即可：自动发现本机微信 4.x 账号 → 直接以微信源目录为只读输入 →
在应用缓存根内完成数据库解密、消息/联系人映射和媒体解码 → 按联系人/时间/类型/大小筛选 →
对已落盘图片解密预览 → 显示并清理自有缓存。**首个 Beta 不含任何清理、移动或删除能力。**

完成定义 = 父计划的 6 条阶段门禁全部通过 + 7 条私有小样本验收全部通过 + 发布包在无 Python 的干净
Windows 环境验证通过。任一门禁未过，不得标记真实 Beta 或制作"可读取真实微信数据"的发布包。

## 二、现状与已验证的卡点诊断

### 事实基线（2026-09-09 / 09-10，来源：本机交接 `tests/fixtures_private.local/P4-REAL-WECHAT-READONLY-01-handoff.local.md`）

| 项 | 结果 |
| --- | --- |
| 源加密库 | 35 个 / ~1.38 GB；`db_init=ok` |
| 数据库密钥 | **27 个取得，8 个未取得**；1 个库 `quick_check=ok`；1 个匿名会话可读；28 个解密缓存文件 / 572 MB |
| 媒体 | **`blocked`**：复制到真实 `image.dat` 后，**非监控式内存 AES 密钥扫描返回 `ImageKeyNotFound`** |
| 源完整性 | 探测期间源 `.db` 未被写入；一次 before/after 快照报 `false` 经独立复核判定为并发期间外部变化，不可归因于探测 |
| 密钥落盘 | 无（session 内无密钥文件） |
| cleanup / executor | 未运行 |

### 卡点诊断（本轮新增，已只读验证）

1. **上游密钥优先级被跨过**：`wechatauto/media.py::detect_image_key()` 明文规定优先级为
   **cfgDword 派生（确定性离线，注释称实测 3000/3000）→ 显式注入 → 本地缓存 → 进程内存扫描**。
   实际探测走的是排**最末**的内存扫描。
2. **cfgDword 应已在手**：`wechatauto/db.py::extract_master_key_from_cfg()` 一次调用同时返回
   `(主密钥, cfgDword, wxId)`。27 个库密钥既已成功，cfgDword 即同源副产品；图片密钥可由
   `MD5(str(cfgDword) + wxid)[:16]` 确定性派生，**无需内存扫描**。
3. **路径与格式均无问题**（只读实测，未读内容）：真实 `msg/attach/<hash>/<月>/Img/*_t.dat` 与上游
   glob 约定匹配；近 5 个 Img 目录样本**全部通过 V2 魔数** `07 08 56 32 08 07`。故 `_probe_ct()`
   能取到正确探针，失败不可归因于"找不到文件"或"格式不符"。
4. **内存扫描本身是最弱路径**：微信 4.1.x 数据库/图片密钥不常驻内存（社区已确认），一次性扫描失败
   属预期行为。

> 结论：媒体门禁的下一步不是"再扫一次内存"，而是**走 cfgDword 派生**。该验证成本极低，应作为
> 第一个执行动作（W1）。

## 三、范围与非目标

**范围**：真实密钥提取与校验的适配层、真实数据库只读解密、媒体 v1/v2 解密与预览、应用内编排、
桌面只读界面、公开/私有集成门禁、Windows 打包。

**非目标**：发送、OCR、朋友圈互动、UI 自动化等上游非只读能力；任何对微信源的写操作；永久删除；
在公开 CI 中使用真实数据；把真实路径/账号/正文/密钥/媒体写入 Git。

## 四、目标登记与所有权（待落地）

当前 `coordination/targets.yaml` 与集成看板**尚无 P4 目标**。需先登记以下六项（原文供
orchestrator 直接采用；`depends_on` 按第五节波形修正）。

```yaml
  - id: P4-REAL-DB-01
    title: Real WeChat encrypted database adapter
    owner: wechat_db_adapter
    status: planned
    risk: R2
    depends_on: [P4-REAL-MEDIAKEY-01]   # 媒体门禁通过后才进入实现（父计划第 19-20 行）
    deliverables: [src/wechat_cleaner/real_db/, tests/real_db/]
    acceptance: Discover accounts, extract and validate SQLCipher 4 keys read-only, decrypt to app cache only, emit versioned evidence without message bodies, and diagnose the 8 unkeyed databases.
  - id: P4-REAL-MEDIAKEY-01
    title: Real image key resolution probe (cfgDword derivation first)
    owner: decoder
    status: planned
    risk: R2
    depends_on: [P3-INTEGRATION-01]
    deliverables: [scripts/probe_real_media_key.py, plan/P4-REAL-MEDIAKEY-01-media-key-probe.md]
    acceptance: Derive image AES/XOR keys from cfgDword on a real account, validate against a real V2 .dat probe, and decrypt one real image copy to an openable PNG/JPEG in a private session root; source files unchanged.
  - id: P4-REAL-MEDIA-01
    title: Real media decoder adapter
    owner: decoder
    status: planned
    risk: R2
    depends_on: [P4-REAL-MEDIAKEY-01]
    deliverables: [src/wechat_cleaner/real_media/, tests/real_media/]
    acceptance: v1/v2 image decryption or thumbnail-only status, SILK voice export, video/file read-only location, Pillow + magic verification, no key or absolute source path in artifacts or logs.
  - id: P4-REAL-WORKFLOW-01
    title: In-app real read-only workflow
    owner: application_workflow
    status: planned
    risk: R2
    depends_on: [P4-REAL-DB-01, P4-REAL-MEDIA-01]
    deliverables: [src/wechat_cleaner/application/, tests/application/]
    acceptance: One in-app flow performs discovery, decryption, scan, mapping, index and preview with no manual copies, JSON or script steps; per-run session manifest supports whole-session cleanup; executor move/delete entries disabled.
  - id: P4-REAL-GUI-01
    title: Real read-only desktop shell
    owner: gui_shell
    status: planned
    risk: R2
    depends_on: [P4-REAL-WORKFLOW-01]
    deliverables: [src/wechat_cleaner/gui/, tests/gui/]
    acceptance: First-run discovery wizard, account selection, contact/time/type/size filters, thumbnail and original preview with explicit failure reasons, cache settings and two-level cache clearing; no execution entry points.
  - id: P4-REAL-INTEGRATION-01
    title: Real read-only integration gate and private harness
    owner: integration
    status: planned
    risk: R2
    depends_on: [P4-REAL-GUI-01]
    deliverables: [tests/integration/, scripts/verify_p4.py, scripts/harness_real_readonly.py]
    acceptance: Public CI stays synthetic-only; local harness reports only version, pass/fail, anonymous counts, bytes and error-code distribution; private acceptance proves inputs came from the real source directory.
  - id: P4-REAL-PACKAGE-01
    title: Windows read-only Beta package
    owner: packaging
    status: planned
    risk: R2
    depends_on: [P4-REAL-INTEGRATION-01, P4-PRIVATE-ACCEPT-01]
    deliverables: [packaging/, tests/packaging/, dist/]
    acceptance: Portable/installer artifacts carry Apache-2.0 LICENSE, source revision and modification notice; verified on a Python-free clean Windows profile; cache in one app-owned root; no cleanup capability.
```

同时建议新增人工目标 `P4-PRIVATE-ACCEPT-01`（owner: `human_operator`，依赖
`P4-REAL-INTEGRATION-01`，交付 `local-only acceptance evidence`）。

### 上游复用登记（已完成，实施时需固化）

`fanyuantaier/wechatauto-replica` v1.2.1 / commit `798989c9b61066c120b59ceba636b557b776ff7f`，
Apache-2.0。实施时必须：新建独立适配层隔离上游异常/密钥缓存/临时库；固定 revision；随包携带
LICENSE 与修改说明；`docs/data-and-license-boundaries.md` 补记使用范围与维护责任人。

## 五、推进波形与关键路径

```yaml
version: 1
updated_at: 2026-09-10
max_active_agents_including_orchestrator: 4
waves:
  - id: W0                      # 治理前置：目标登记 + 分支
    mode: serial
    targets: [P4-GOV-01]
    exit_gate: 六个 P4 目标登记进 targets.yaml 与看板；每个目标从 P4 基线提交创建独立 worktree。
  - id: W1                      # 唯一阻塞点，最低成本，必须最先做
    mode: serial
    targets: [P4-REAL-MEDIAKEY-01]
    exit_gate: 真实图片密钥由 cfgDword 派生并验证通过；一张真实图片可在私有 session 中打开。
  - id: W2                      # 并行：两个不同 owner，路径不重叠
    mode: parallel
    targets: [P4-REAL-DB-01, P4-REAL-MEDIA-01]
    exit_gate: 两门禁通过（数据库含 8 个未取到密钥的诊断结论；媒体含原图/缩略图状态与完整性验证）。
  - id: W3
    mode: serial
    targets: [P4-REAL-WORKFLOW-01]
    exit_gate: 应用内一次流程完成发现→解密→索引→预览，无手工准备输入。
  - id: W4
    mode: serial
    targets: [P4-REAL-GUI-01]
    exit_gate: 首次启动向导 + 账号选择 + 四维筛选 + 预览与失败原因 + 两级缓存清理。
  - id: W5
    mode: serial
    targets: [P4-REAL-INTEGRATION-01]
    exit_gate: 公开合成门禁归零（不得打开 private）；本机 harness 只输出匿名聚合。
  - id: W6
    mode: human_serial
    targets: [P4-PRIVATE-ACCEPT-01]
    entry_gate: AI 输出 ready_for_human 并停止。
    exit_gate: 用户确认 7 条私有小样本验收全部通过（仅记录匿名聚合）。
  - id: W7
    mode: serial
    targets: [P4-REAL-PACKAGE-01]
    exit_gate: 干净 Windows 环境安装/启动/发现/读库/筛选/预览通过；许可证完整；无删除入口。
```

**关键路径**：`W0 → W1（媒体密钥）→ W2 → W3 → W4 → W5 → W6（人工）→ W7`。
`W1` 是唯一已知技术阻塞点，其余均为已知可做的工程实现。

## 六、多 Agent 同步文档协议（强制）

任何目标在**开工时**与**完成时**都必须产出可追溯文档，禁止只把结果留在对话里。

### 6.1 每个目标的四件套

| # | 产物 | 位置 | 时机 | 内容要求 |
| --- | --- | --- | --- | --- |
| 1 | 目标计划 | `plan/<TARGET-ID>-<slug>.md` | 开工前 | 按 `plan/TEMPLATE.md`：范围/依赖/步骤/验收/回滚/交接 |
| 2 | 事实条目 | `coordination/fact-log.md` | 完成时 | 只追加；含精确命令与结果；标注"不可推导的结论" |
| 3 | 看板状态行 | `coordination/integration-board.md` | 开始/完成 | `done` 必须链接事实 ID；开工改 `in_progress` |
| 4 | 结果与交接 | `plan/<TARGET-ID>-<slug>.md` 的结果段（合成）；`tests/fixtures_private.local/<TARGET-ID>-<slug>.local.md`（**任何涉及真实数据**） | 完成时 | 见 `plan/TEMPLATE-result.md`；私有报告只记匿名聚合 |

### 6.2 事实 ID 分配（根治撞号）

历史曾发生 `F-20260909-012` 撞号。**P4 起按目标分段**，段内递增，跨段不得借用：

| 目标 | ID 段 |
| --- | --- |
| 治理/登记/文档 | `F-20260910-001`–`010` |
| P4-REAL-MEDIAKEY-01 | `F-20260910-011`–`030` |
| P4-REAL-DB-01 | `F-20260910-031`–`060` |
| P4-REAL-MEDIA-01 | `F-20260910-061`–`090` |
| P4-REAL-WORKFLOW-01 | `F-20260910-091`–`110` |
| P4-REAL-GUI-01 | `F-20260910-111`–`130` |
| P4-REAL-INTEGRATION-01 | `F-20260910-131`–`150` |
| P4-REAL-PACKAGE-01 | `F-20260910-151`–`170` |

### 6.3 私有结果文档规范（涉及真实微信数据时）

- 位置固定：`tests/fixtures_private.local/<TARGET-ID>-<slug>.local.md`（已被 Git 忽略）。
- **只允许**出现：匿名计数、字节数、错误码分布、布尔判定（完整性/源未变更/预览可打开）。
- **禁止**出现：wxid、联系人、群名、正文、密钥、绝对媒体路径、图片内容、session 绝对路径外的本机路径。
- 必须包含"停止点"（本次未做什么）与"未完成项"，供下一个接手者判断边界。
- 模板见 `plan/TEMPLATE-result.md`。

### 6.4 跨 Agent 同步机制

- **进度总表**：本文件第七节，由**推进协调者单写**（避免多 agent 并发编辑同一文件）。
- 各目标 owner **不改**本文件，只在完成时向协调者提交"状态变更请求"（含事实 ID）。
- **交接包**沿用 `coordination/agents.yaml` 的 `handoff_required` 六字段：改动文件、验证命令、
  验证结果、风险等级、契约锁状态、未解决项。缺验证证据不得标 done。
- 并行隔离：一目标一 worktree；冲突路径按 `coordination/path-ownership.yaml` 判定，新增模块
  需先补所有权映射再落代码。

## 七、进度总表（协调者单写）

| 目标 | 状态 | 负责人 | 事实证据 | 私有报告 | 阻塞项 / 下一动作 |
| --- | --- | --- | --- | --- | --- |
| P4-GOV-01（登记与基线） | `done` | orchestrator | F-20260910-001 | — | — |
| P4-REAL-MEDIAKEY-01 | `done` | decoder | F-20260910-011 | P4-REAL-MEDIAKEY-01-media-key-probe.local.md | 已完成：cfgDword 派生通过真实 V2 探针校验 |
| P4-REAL-DB-01 | `done` | wechat_db_adapter | F-20260910-031 | — | 未完成项：quick_check 批量执行、envelope 正式接入 |
| P4-REAL-MEDIA-01 | `done` | decoder | F-20260910-061 | — | 未完成项：原图与 `_h.dat` 高清变体解密 |
| P4-REAL-WORKFLOW-01 | `done` | application_workflow | F-20260910-091 | — | 未完成项：scanner 真实目录分类缺口、联系人维度映射 |
| P4-REAL-INTEGRATION-01 | `done` | integration | F-20260910-131 | P4-REAL-INTEGRATION-01-harness.local.md | 真实 harness 通过；公开门禁 7 项全 passed |
| P4-REAL-GUI-01 | `done` | gui_shell | F-20260910-111 | P4-REAL-GUI-01-ui-smoke.local.md | 真实 UI 冒烟通过（35 库/27 密钥/103,852 记录/缩略图预览成功） |
| P4-PRIVATE-ACCEPT-01 | `ready` | human_operator | — | — | 等用户执行 7 条私有小样本验收 |
| P4-REAL-PACKAGE-01 | `in_progress` | packaging | F-20260910-151, F-20260910-152 | P4-REAL-PACKAGE-01-package.local.md | 产物已构建并通过冻结启动自检；等 W6 人工干净环境验收后可标记 done |

**当前状态一句话**：真实数据的「读取 → 处理 → 只读界面预览」全链路已端到端验证通过；剩余为人工验收与打包。

## 八、人工确认点与终止协议

AI 在以下四点必须停下等用户，不得代行：

1. **微信登录态**：密钥提取要求客户端登录并运行；图片密钥验证建议先在聊天中打开一张图。
2. **私有 session 批准**：每个真实探测轮次使用**新的** session 根
   `%LOCALAPPDATA%\WeChatSpaceManager\private-acceptance\<session-id>`，内含
   `db-cache/`、`media-source-copy/`、`media-preview/`、`anonymous-result.json`。
3. **私有小样本验收**（父计划 7 条）：由用户执行；AI 输出 `ready_for_human` 后停止。
4. **发布包**：干净环境验证结果由用户确认后才可对外称 Beta。

终止协议：AI 只输出匿名聚合；任何一次门禁未过即记录 `blocked` 与错误码分布后停止，**不得通过索要密钥、
读取桌面既有解密目录或扩大扫描范围来"凑"通过**。

## 九、风险与回滚

| 风险 | 影响 | 缓解 / 回滚 |
| --- | --- | --- |
| 图片密钥派生失败 | 媒体门禁持续 blocked | 依次降级：显式注入 → 本地密钥缓存 → 监控式扫描（用户看图时捕获）；仍失败则上报 `needs_decision` |
| 8 个库密钥取不到 | 部分会话不可读 | 记录库类别与错误码，UI 显示"该库不可读"；不阻塞其余库 |
| 上游版本漂移 | 适配层失效 | 固定 commit；适配层隔离；升级走独立计划与回归 |
| 真实数据误入 Git | 隐私事故 | 四件套强制私有路径；`.gitignore` + 提交前 `git status` 复核 |
| 源文件被写 | 数据损坏 | 只读打开、禁止写源、前后 hash/mtime 复核 |
| 手工步骤蔓延 | 违背"用户只启动一个软件" | W3 以"无手工输入"为唯一验收标准 |

## 十、本计划产生的待落地清单

1. `coordination/targets.yaml` 增加 6 个 P4 目标 + 1 个人工目标（第四节 YAML）。
2. `coordination/integration-board.md` 增加对应状态行（含依赖与门禁描述）。
3. `coordination/path-ownership.yaml` 增加 `src/wechat_cleaner/real_db/**`、
   `src/wechat_cleaner/real_media/**`、`scripts/probe_real_media_key.py`、
   `tests/real_db/**`、`tests/real_media/**` 的所有权行。
4. `coordination/phase-4-waves.yaml` 落为独立文件（第五节波形）。
5. `coordination/phase-4-dispatch.md` 派工手册（沿用 P3 的固定口令结构 + 本文件第八节终止协议）。
6. `docs/data-and-license-boundaries.md` 记录上游采用范围、revision、维护责任人。
7. 本文件第七节进度总表在 W0 完成后开始填写。

> 以上 1–6 属共享治理文件（orchestrator 所有权）。本计划仅提供原文；落地由协调者执行，
> 或由用户在确认后授权单一 Agent 代行。
