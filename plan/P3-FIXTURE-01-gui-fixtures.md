# P3-FIXTURE-01：GUI 与数据库兼容夹具

状态：`done`
负责人：`fixtures`  
风险：`R1`  
依赖：`P3-CONTRACT-01`

## 范围

扩展确定性合成数据：多账号、私聊/群聊、多年份与月份、各媒体类型、大小边界、高/中/
低/冲突/未映射、受保护路径、文件变化和重复候选。生成至少两个受支持和一个拒绝的
SQLite schema 版本，不包含真实联系人、正文、绝对路径或密钥。

## 验收证据

- 同一种子生成内容和摘要一致，跨账号 ID 不冲突。
- 契约 schema 校验通过；边界值与拒绝样本均有清单。
- 私有 `Rec` 只保留空模板/说明，并由 `.gitignore` 阻止提交。

## 回滚

仅删除本目标新增的合成夹具；不得触碰已有私有目录或用真实数据补测试。

## 已实现与验收记录（2026-09-09）

- [x] `build_phase3_fixture.py` 提供带 seed 的确定性生成器，输出 path-free
  `phase3-manifest.json`、`media-records.json` 和数据库矩阵清单。
- [x] 覆盖两个账号、direct/group 会话、2023--2026 多年份月份、全部公开媒体类型、
  0/1/1 MiB 大小边界、present/missing/changed/duplicate 文件状态。
- [x] 覆盖 exact/high/medium/low/unmapped 映射置信度、mapped/unmapped/conflict/protected
  归属状态、受保护目录和重复候选；跨账号媒体、记录键和联系人 ID 不冲突。
- [x] 生成 `synthetic-v1`、`synthetic-v2` 两个支持的 SQLite schema，以及一个明确拒绝的
  `synthetic-legacy-rejected` schema；数据库只含结构化元数据与相对路径，不含正文、密钥或绝对路径。
- [x] 四项夹具验收测试通过：同 seed 字节级一致、契约 `MediaRecord` 校验、边界/拒绝清单、
  SQLite 表/列/user_version 矩阵校验。
- [x] CLI 生成检查通过；仅写入临时目录，未读取或写入真实微信目录和私有夹具。

回滚时移除本目标的生成器和测试即可；历史 Phase 1 文件树夹具及私有模板保持不变。
