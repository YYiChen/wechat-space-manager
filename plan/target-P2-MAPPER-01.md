# P2-MAPPER-01：数据库到联系人媒体映射

状态：`done`  
负责人：`mapper`  
风险：`R2`  
关联目标：`P1-CONTRACT-01, P1-FIXTURE-01`  
更新时间：`2026-09-09`

## 结果与非目标

- 结果：实现一个无副作用、确定性的映射核心，将数据库适配器输出的脱敏证据与扫描候选组合为 `MediaRecord`。
- 非目标：不打开真实微信数据库、不读取密钥、不解码媒体、不修改任何微信目录、不生成清理执行动作。

## 输入与输出

- 输入：`AccountRef`、`MediaCandidate`、规范化 `MessageEvidence`。
- 输出：`MediaRecord` 与 `ContractError` 的 `MappingResult`；未能可靠归属的候选必须保留为 `UNMAPPED`，不能猜测联系人。
- 置信度：路径或内容哈希确定匹配为 `EXACT`；数据库目录前缀为 `HIGH`；名称/日期/大小联合相关为 `MEDIUM`；仅名称相关为 `LOW`；无证据为 `UNMAPPED`。

## 实施步骤

- [x] 建立 evidence/candidate 的内部不可变模型和相对路径规范化。
- [x] 实现确定性索引、冲突检测、账号边界和置信度分级。
- [x] 将结果转换为公开 `MediaRecord`/`ContractError`，不泄露绝对路径或原始聊天内容。
- [x] 添加合成映射测试和失败路径测试。

## 验收

- [x] `python -m pytest tests/mapper -q` 通过。
- [x] `python -m ruff check src/wechat_cleaner/mapper tests/mapper` 通过。
- [ ] 完整仓库测试通过；当前被其他 Agent 的 decoder 收集错误阻塞，mapper 自己的测试和编译检查已通过。
- [x] 同一输入顺序和结果顺序稳定；低/无置信度结果不被升级为可清理目标。

## 风险与回滚

- 风险：数据库版本适配器提供的路径语义可能不稳定；本核心只接受规范化证据，不假设具体 SQLite 表名。
- 回滚：删除 mapper 包和 mapper 测试即可，不改变公开 v1 契约和 Phase 1 夹具。
- 需要人工决定：后续为具体微信版本实现哪个只读数据库适配器，以及其许可证边界。

## 交接

- 改动文件：`src/wechat_cleaner/mapper/`、`tests/mapper/`。
- 事实日志条目：`F-20260909-015`。
- 契约锁状态：无公共字段修改，所有锁保持空闲。
- 未解决项：真实数据库 adapter、真实版本兼容性和 UI 接入留给后续目标。
