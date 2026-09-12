# <TARGET-ID>：结果与交接

> 使用说明：本模板有两个版本。**合成/公开结果**可直接追加到目标计划
> `plan/<TARGET-ID>-<slug>.md` 尾部；**任何接触真实微信数据的结果**必须另存为
> `tests/fixtures_private.local/<TARGET-ID>-<slug>.local.md`（Git 已忽略），
> 且只允许出现匿名聚合字段。两版都不允许出现密钥、正文、账号标识与绝对媒体路径。

---

## 版本 A：合成 / 公开结果

状态：`done | blocked | partial`  
负责人：`<agent-id>`  
风险：`R0-R3`  
完成时间：`YYYY-MM-DD`  
关联计划：`plan/<TARGET-ID>-<slug>.md`  
事实日志条目：`F-YYYYMMDD-0XX`（段内递增，见 `plan/P4-REAL-ROLLOUT-01.md` 6.2）

### 结论

一句话说明做成了什么、还有什么没做成。

### 交付物（改动文件）

| 路径 | 类型 | 说明 |
| --- | --- | --- |
| `src/wechat_cleaner/<module>/...` | 新增/修改 | |

### 验证证据（必须可复现）

```powershell
# 精确命令
.\.venv\Scripts\python.exe -m ruff check <paths>
.\.venv\Scripts\python.exe -m pytest <owned-tests> -q
```

结果：`<N> passed`；静态检查通过 / 失败原因。

### 门禁对照

| 门禁 | 状态 | 依据 |
| --- | --- | --- |
| 对应父计划门禁 | passed / blocked | 证据位置 |

### 数据边界声明

- 是否接触真实微信数据：是 / 否
- 密钥是否落盘：否（必须为否）
- 微信源是否变更：否（附复核方式）
- 清理 / 执行器是否运行：否

### 未解决项与下一动作

### 回滚方法

### 交接六字段（`coordination/agents.yaml`）

- 改动文件：
- 验证命令：
- 验证结果：
- 风险等级：
- 契约锁状态：
- 未解决项：

---

## 版本 B：真实数据私有结果（`*.local.md`）

> 本文件不得提交、截图或复制到公共渠道。发现误入 Git 立即删除并上报。

### 停止点（本次明确没有做什么）

- 没有提取哪些密钥：
- 没有读取哪些内容：
- 没有创建 / 修改哪些文件：
- 没有运行的流程（cleanup / executor / 删除）：

### 匿名聚合结果

```json
{
  "status": "passed | partial | blocked",
  "source_db_count": 0,
  "source_db_bytes": 0,
  "key_count_in_memory": 0,
  "unkeyed_count": 0,
  "db_cache_file_count": 0,
  "db_cache_bytes": 0,
  "db_integrity_passed": 0,
  "session_count": 0,
  "media_candidate_count": 0,
  "media_decoded_count": 0,
  "media_openable": false,
  "key_cache_files_present": false,
  "source_db_unchanged": true,
  "error_types": []
}
```

### 格式与完整性判定（只记布尔与类型）

| 项 | 结果 |
| --- | --- |
| 媒体格式版本（V1/V2） | |
| 预览可打开（Pillow / 魔数） | |
| 源文件 hash / mtime 复核 | |

### 禁止项复核

- [ ] 无 wxid / 账号标识
- [ ] 无联系人 / 群名 / 会话标识
- [ ] 无消息正文
- [ ] 无密钥（含哈希、前缀、长度以外的任何形式）
- [ ] 无绝对媒体路径 / session 绝对路径
- [ ] 无图片或其他媒体内容

### 私有 session

- session 根（相对描述，如 `<app-local-root>\private-acceptance\<session-id>`）：
- 内含目录：`db-cache/`、`media-source-copy/`、`media-preview/`、`anonymous-result.json`
- 计划清理方式：由应用统一清理，本轮不删除

### 未完成项与下一阻塞点

### 需要用户确认的事项
