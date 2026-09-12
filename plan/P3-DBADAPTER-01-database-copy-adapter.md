# P3-DBADAPTER-01：解密数据库副本适配器

状态：`done`
负责人：`wechat_db_adapter`  
风险：`R2`  
依赖：`P3-CONTRACT-01, P3-FIXTURE-01`

## 范围

只读打开已由 decoder/外部授权流程产生的数据库副本，探测受支持 schema，读取账号、
联系人、会话、消息时间和媒体关联所需最少列，输出 `DatabaseEvidenceEnvelope`。不负责
提取密钥、不打开微信活动数据库、不写入副本、不输出消息正文。

## 验收证据

- 支持版本矩阵与列映射显式登记；未知/歧义 schema 拒绝。
- SQLite 以只读 URI 打开，副本 hash/mtime 在前后保持一致。
- 查询参数化；日志和异常不含正文、密钥、联系人名和本机绝对路径。
- 合成 fixture 覆盖缺表、缺列、损坏库、跨账号引用和冲突证据。

## 回滚

移除对应版本适配器即可；不修改 mapper、decoder 或任何数据库文件。

## 实施证据

`src/wechat_cleaner/db_adapter/` 提供版本矩阵、SQLite `mode=ro&immutable=1` 打开、账号参数化
查询、前后 hash/mtime 身份复核、结构化冲突证据和脱敏错误边界。`tests/db_adapter/` 覆盖两个
合成支持版本、未知/损坏库、只读完整性、跨账号过滤和重复媒体冲突；专项测试为 `4 passed`。
实现不提取密钥、不读取正文、不打开活动数据库，也不把副本绝对路径写进 envelope 或错误。
