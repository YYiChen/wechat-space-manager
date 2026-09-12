# 架构与依赖方向

项目采用本机优先、契约驱动的数据流。模块只能沿箭头消费上游输出，不能反向
读取另一个模块的私有实现。

```text
filesystem copy (read-only)
        |
     scanner  ----> ScanManifest + file candidates
        |                         |
        +----------------------> mapper ----> MediaRecord + confidence
                                      |
                                      +----> decoder ----> DecodeArtifact/cache
                                      |
                                      +----> ui (review only)
                                      |
                                      +----> cleanup_safety ----> CleanupPlan
                                                                    |
                                           independent executor ----> CleanupReceipt
```

## Phase 3 桌面数据流

```text
scanner + database-copy adapter + mapper
                    |
             local SQLite index
                    |
          filter engine / selection snapshot
                    |
           application workflow facade
             /                     \
      PySide6 GUI              cleanup executor
                                  |
                     dry-run / quarantine / recycle
                                  |
                   receipt + recovery locator/state
```

`src/wechat_cleaner/ui/` 保留为 Phase 2 headless 兼容层。新的桌面层位于 `gui/`，只依赖
`application/` 的公开 facade。索引、数据库适配、筛选和执行器彼此不读取私有实现；
它们只通过版本化契约交互。大数据结果在 SQLite 侧过滤、聚合和分页，GUI 不全量载入。

## 边界

- `scanner` 只做目录发现、文件身份读取和受保护目录/重解析点拒绝；不解码、不删除。
- `mapper` 只读取数据库副本或合成数据库夹具，输出联系人/消息映射和置信度；不写数据库。
- `decoder` 只处理已批准的副本，密钥只在内存中短暂使用，输出受限缓存和像素验证结果。
- `cleanup_safety` 只生成不可变计划、执行前身份复核规则和收据模型；Agent 不直接删除。
- `ui` 只能消费公开契约，不能绕过安全门调用文件删除或数据库写入。
- `integration` 只负责跨模块验证、泄漏检查和事实证据，不修改业务模块实现。
- `local_index` 只保存必要元数据，不保存正文、密钥或解码像素，并可整体清除重建。
- `db_adapter` 只读已授权的数据库副本，未知 schema 失败关闭。
- `filtering` 产生可复现筛选摘要和不可变选择，不执行文件操作。
- `application` 是 GUI 与业务能力之间的唯一门面。
- `executor` 第一版只支持 Dry Run、隔离和回收站；永久删除不存在。

## 共享层

`src/wechat_cleaner/domain/` 与 `contracts/jsonschema/` 是唯一的公开数据边界。
任何字段变化都必须先取得 `coordination/contract-lock.yaml` 对应锁，并同时更新
schema、夹具、迁移说明和消费者测试。
