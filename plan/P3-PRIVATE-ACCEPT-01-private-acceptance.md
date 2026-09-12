# P3-PRIVATE-ACCEPT-01：本机私有验收

状态：`blocked`
负责人：`human_operator`  
风险：`R3`  
依赖：`P3-INTEGRATION-01`

## 执行主体

本目标是 human-only 目标。AI 只能运行
`python scripts/check_private_acceptance_readiness.py` 检查公开前置条件，并输出
`blocked` 或 `ready_for_human`；AI 不读取私有配置、真实微信目录、数据库、密钥或媒体，
也不代替用户执行扫描、Dry Run、隔离、恢复和确认。

当就绪检查输出 `ready_for_human` 后，协调 Agent 将本目标从 `blocked` 更新为 `ready`；
用户实际开始验收时更新为 `in_progress`，人工证据复核完成后才更新为 `done`。

## 顺序

1. 用户确认备份、微信已退出和目标账号根；只读扫描指定私有 `Rec` 夹具。
2. 核对聚合数量/大小和少量人工映射，不记录联系人、路径、正文或截图。
3. 运行 Dry Run，逐项核对排除原因和计划摘要。
4. 用户手工选择极小、可恢复、非收藏、非数据库批次，使用隔离模式。
5. 验证收据和恢复，再由用户决定是否扩大范围；不启用永久删除。

## 通过与停止条件

仅记录匿名聚合值、通过/失败和软件版本。出现跨账号、映射歧义、文件变化、微信进程、
恢复失败或实际路径与计划不一致时立即停止，不靠 AI 猜测继续。

完整路径、人工步骤、停止条件和证据边界见
`docs/project-layout-and-acceptance-manual.md`。
