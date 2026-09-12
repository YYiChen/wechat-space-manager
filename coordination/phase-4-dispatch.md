# Phase 4 多 Agent 派工手册

机器可读依赖见 `coordination/phase-4-waves.yaml`，总计划与文档协议见
`plan/P4-REAL-ROLLOUT-01.md`，父计划见 `plan/P4-REAL-WECHAT-READONLY-01.md`。

## 现在立刻做什么

`W1 P4-REAL-MEDIAKEY-01` 是唯一已知技术阻塞点，也是其他所有实现的前置门禁。原因是上一轮
真实探测走的是上游密钥优先级中**最末位**的进程内存扫描（微信 4.1.x 密钥不常驻内存，失败属预期），
而上游首选路径是 **cfgDword 派生**（确定性、离线、实测 3000/3000），且 cfgDword 与数据库主密钥
由 `extract_master_key_from_cfg()` 一次调用同时返回。W1 通过后，`W2` 的 DB 与媒体适配可并行。

## 推荐分配

| Agent 名称 | 目标 | 可与同波并行 | 只允许写入 |
| --- | --- | --- | --- |
| 协调 Agent | P4-GOV-01 | 否 | 共享治理文件 |
| 媒体密钥 Agent | P4-REAL-MEDIAKEY-01 | 否（W1 串行） | `scripts/probe_real_media_key.py`、私有 session |
| 数据库适配 Agent | P4-REAL-DB-01 | 是（W2） | `src/wechat_cleaner/real_db/` 与其测试 |
| 媒体适配 Agent | P4-REAL-MEDIA-01 | 是（W2） | `src/wechat_cleaner/real_media/` 与其测试 |
| 应用编排 Agent | P4-REAL-WORKFLOW-01 | 否（W3） | `application/` 与其测试 |
| GUI Agent | P4-REAL-GUI-01 | 否（W4） | `gui/` 与其测试 |
| 集成 Agent | P4-REAL-INTEGRATION-01 | 否（W5） | 集成测试、`scripts/verify_p4.py`、`scripts/harness_real_readonly.py` |
| 人工操作员 | P4-PRIVATE-ACCEPT-01 | 否（W6） | 不写仓库私密产物 |
| 打包 Agent | P4-REAL-PACKAGE-01 | 否（W7） | `packaging/` 与其测试 |

## 每次派工的固定口令

> 领取 `<TARGET-ID>`。先完整阅读 `AGENTS.md`、`coordination/targets.yaml`、
> `coordination/path-ownership.yaml`、`coordination/contract-lock.yaml`、
> `coordination/phase-4-waves.yaml`、`plan/P4-REAL-ROLLOUT-01.md` 与对应计划文件。
> 确认依赖均为 done；只编辑分配给自己的目录。真实数据只能在新建私有 session 根内操作，
> 只读源目录、不落盘密钥、不输出 wxid/正文/绝对媒体路径。若契约缺字段，停止并报
> `needs_decision`。完成后按 `plan/TEMPLATE-result.md` 产出结果文档、追加事实日志
> （ID 段见总计划 6.2）、更新看板行，不自行把目标标为 done。

## 必须串行的边界

1. `P4-REAL-MEDIAKEY-01` 在所有实现目标之前；媒体门禁未过不得开始 DB/媒体适配实现。
2. `P4-REAL-WORKFLOW-01` 等待 DB 与媒体适配全部完成。
3. `P4-REAL-GUI-01` 等待 workflow；两级缓存清理必须只命中应用缓存根。
4. `P4-REAL-INTEGRATION-01` 公开门禁不得打开 private 输入。
5. 私有验收由用户执行；AI 输出 `ready_for_human` 后停止。
6. 打包在所有门禁与人工验收之后。

## 真实数据操作边界（R2）

- 每轮真实探测使用新的 session 根：
  `%LOCALAPPDATA%\WeChatSpaceManager\private-acceptance\<session-id>`，
  内含 `db-cache/`、`media-source-copy/`、`media-preview/`、`anonymous-result.json`。
- 源 `.db`、WAL、SHM 与媒体文件始终只读；前后复核 hash/mtime，只声明"自身未写"，
  不把并发期间的外部变化归因于探测。
- 报告只允许匿名计数、字节数、布尔判定与错误码分布；禁止 wxid、联系人、正文、密钥、
  绝对媒体路径与媒体内容。
- 清理、移动、回收站、永久删除一律不运行（首个 Beta 无删除能力）。
