# 分支与 worktree 规则

多个 AI 不得在同一个工作目录同时写入。每个目标使用独立分支和 worktree：

```powershell
git worktree add ..\wt-P2-SCAN-01 -b agent/P2-SCAN-01 main
```

上面的命令要求 `main` 已有至少一个提交。本仓库当前可能仍是未提交的初始化
工作树；首次并行开发前，先由项目所有者配置 Git 身份并提交治理基线，或为每个
Agent 建立独立目录副本后再开始。不要为了绕过这个前置条件擅自伪造提交身份。

规则如下：

1. 分支名必须包含目标 ID；一个目标只允许一个写入 Agent。
2. Agent 只能修改 `coordination/path-ownership.yaml` 中自己拥有的路径。
3. 共享契约修改前先锁定契约；协调文件由 orchestrator 单写，Agent 只提交事实和建议。
4. Agent 在自己的 worktree 跑最窄测试并提交交接包；不得直接合并其他 Agent 的分支。
5. integration Agent 在干净集成 worktree 合并候选提交，跑全套测试、泄漏检查和空白检查。
6. 验证失败时保留分支和日志，按目标回滚；禁止用 `reset --hard` 覆盖别人的工作。
7. 本项目当前没有 remote；任何远程推送、发布或上传私密数据都不属于默认流程。

如果工具不支持 worktree，至少使用独立目录副本；禁止多个 Agent 共用同一 checkout。
