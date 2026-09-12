# P2-SCAN-01：只读微信文件系统扫描器

状态：`verification`  
负责人：`scanner`  
风险：`R1`  
关联目标：`P1-CONTRACT-01, P1-FIXTURE-01`  
更新时间：`2026-09-09`

## 结果与非目标

- 结果：从 `xwechat_files` 容器或单个账号根目录发现账号，生成确定性的只读文件候选和 `ScanManifest`。
- 非目标：不读取数据库内容、不解码、不建立联系人映射、不写微信目录、不生成清理计划、不删除文件。

## 输入与输出

- 输入：用户明确选择的绝对目录；开发和测试只使用 `tests/fixtures_synthetic/` 或副本。
- 输出：`ScanResult`（公开 `AccountRef`、`ScanManifest`、候选文件列表和扫描错误）。候选文件只保存账号根目录下的相对路径、大小、修改时间和分类。
- 受保护内容：`db_storage/`、`business/favorite/`、`SendTemp/` 仍可被索引但永远标记为 protected；重解析点不遍历。

## 实施步骤

- [x] 定义账号发现、绝对路径校验和重解析点检测。
- [x] 定义确定性文件分类、缓存标记、候选文件身份和清单哈希。
- [x] 实现只读扫描器及包级导出。
- [x] 用合成双账号夹具覆盖保护目录、`Rec` 原图/缩略图和重解析点。
- [x] 当前 decoder/scanner/mapper 的合成接口 smoke 已通过；cleanup/UI 全链路仍属于 P2-INTEGRATION-01。

## 验收

- [x] 可重复验证命令：`python -m ruff check src/wechat_cleaner/scanner tests/scanner`、`python -m pytest tests/scanner -q`。
- [x] 完整验证：`python -m ruff check src tests contracts`、`python -m pytest -q`、`python -m compileall -q src tests contracts`。
- [x] 预期结果：账号发现稳定；同一副本重复扫描的文件序列和 `manifest_sha256` 相同；不遍历重解析点；扫描前后目录内容不变。
- [x] 失败/边界情形：根目录不存在、根目录为文件或重解析点、无账号子目录、权限/统计错误均得到明确错误，不把绝对路径写入日志。

## 风险与回滚

- 风险：不同微信版本的目录布局可能出现未知文件；未知内容只作为 `other` 候选，不得推断为可删除。
- 回滚：删除 scanner 自有目录和测试即可；不涉及真实数据和共享契约版本。
- 需要人工决定：后续是否为特定微信版本增加布局插件；任何新布局先添加合成夹具。

## 交接

- 改动文件：`src/wechat_cleaner/scanner/**`、`tests/scanner/**`。
- 事实日志条目：`F-20260909-012`、`F-20260909-014`、`F-20260909-019`。
- 契约锁状态：无共享契约修改；消费 v1 `AccountRef`、`ScanManifest`。
- 未解决项：真实数据库适配、真实微信格式校准、cleanup/UI 全链路和将 Pillow 写入 `pyproject.toml` 仍待后续集成。
