# P3-INTEGRATION-01 当前状态记录

更新时间：2026-09-09
集成提交：`de3c31d` (`test: add phase 3 integration gate`)

## 结论

P3-INTEGRATION-01 已通过严格公开/合成门禁，可以标记为 `done`。门禁没有使用
`--allow-unresolved`，也没有读取 private 输入。

## 已通过

最近一次严格门禁中，下列检查通过：

- 静态检查、compileall
- 契约与合成夹具
- scanner、mapper、decoder
- local index、database adapter、filtering
- 100,000 条记录性能 smoke
- cleanup、executor、application workflow
- P3 集成测试（12 passed）
- desktop/legacy CLI 入口测试和 help smoke
- Qt offscreen GUI（10 passed）
- 全仓测试（157 passed，1 个 Windows symlink 能力软跳过）

## 当前失败与未解决项

没有影响公开合成门禁的失败项。清理安全专项和全仓测试各有一个 symlink 用例因当前
Windows 能力不可用而软跳过；该环境限制已由测试自身报告，未跳过任何必需的门禁检查。

## P3-PRIVATE-ACCEPT-01 判断

**当前不可进行完整的 P3-PRIVATE-ACCEPT-01，也不应读取或清理真实微信目录。**

原因：

1. P3-PRIVATE-ACCEPT-01 是 R3 人工操作，必须由用户确认备份、微信退出、目标账号根，
   再执行只读扫描、Dry Run 和极小的可恢复隔离批次；这些前置条件目前没有形成可交付
   的通过证据。

## 重新评估条件

公开前置条件已经满足。下一步只允许用户单独确认已备份、微信完全退出、目标账号根和
极小的非收藏/非数据库批次；验收过程只记录匿名聚合数量、大小、版本和通过/失败，不记录
路径、联系人、正文、密钥、截图或私有媒体。

在这些条件满足前，本项目只继续使用合成夹具和副本验证，不触碰真实微信数据。

AI 与人工的新交接协议、路径结构和操作手册见
`docs/project-layout-and-acceptance-manual.md`。AI 只输出 `blocked` 或
`ready_for_human`，不得继续执行 private 步骤。
