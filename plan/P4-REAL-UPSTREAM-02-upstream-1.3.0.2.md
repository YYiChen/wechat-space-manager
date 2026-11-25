# P4-REAL-UPSTREAM-02：上游适配层升级到 wechatauto-replica v1.3.0.2

状态：`done`  
负责人：`packaging`（本轮由 lead 单人执行，未与其他 Agent 并发写同一路径）  
风险：`R2`  
关联目标：`P4-REAL-PACKAGE-01`, `P4-REAL-MEDIA-01`, `P4-REAL-DB-01`  
更新时间：`2026-11-26`

## 结果与非目标

- 结果：把上游 pin 从 `798989c`（v1.2.1）升到 `fb883c5`（v1.3.0.2），使连接阶段在
  微信 4.2.0.53 上重新能取到 `cfgDword`；同时让裁剪打包、许可证声明、日志与密钥
  落盘边界与新版本行为一致，并修正新版本带来的连接耗时回归。
- 非目标：不改上游源码；不新增删除/发送能力；不改变本项目的错误码字典（
  `DECODER_UNAVAILABLE` 仍是唯一对外码）。

## 前置条件与输入

- 依赖：`P4-REAL-PACKAGE-01` 的打包链（spec/staging/build_store）已可用。
- 输入及私密等级：本机真实微信进程（R2，只在内存中读取，仅记录哈希、布尔与耗时）；
  私有验收夹具不参与本目标。
- 契约锁：不需要（未改 `contracts/`）。

## 实施步骤

- [x] 核对上游 tag：`v1.3.0`（`abca0b7`, 2026-11-21）、`v1.3.0.2`（`fb883c5`, 2026-11-24）。
- [x] 在隔离 venv 中把 editable 上游切到 `v1.3.0.2`，元数据刷新为 1.3.0.2。
- [x] 机器实测（微信 4.2.0.13）：`extract_image_key_seed()` 与旧
      `extract_master_key_from_cfg()` 返回同一 `cfgDword`（sha 前 8 位一致），
      新链不覆盖旧链、无回归；两链各耗 0.24–0.25 s；`image_digest` 在该版本为空。
- [x] 固定 pin：`packaging/requirements-build-windows.txt` → `fb883c5`。
- [x] 裁剪白名单补 `logger.py`/`param.py`：`packaging/stage_upstream.py`；
      spec `hiddenimports` 同步补 `wechatauto.logger`、`wechatauto.param`、`colorama`。
- [x] 关闭上游文件日志、密钥镜像重定向到缓存根：`src/wechat_cleaner/real_media/keys.py`。
- [x] 连接耗时回归修正：显式账号时跳过上游 1.3.x 账号自愈
      （`_account_pinned_session_class`），账号发现路径（`account=None`）保留完整自愈。
- [x] 许可证与清单：`packaging/licenses/THIRD-PARTY-NOTICES.md`、`packaging/build.py`。
- [x] 版本号 0.1.5 → 0.1.6（`pyproject.toml`、`gui/disclaimer.py`）。
- [x] 打包策略与适配层测试同步：白名单、revision、hiddenimports、自愈跳过、
      密钥镜像环境变量还原。
- [x] 真实构建 + `verify_artifact` 通过，并用冻结包自检（offscreen，无控制台）验证可启动。

## 验收

- [x] 可重复验证命令：`python -m pytest tests/real_media tests/packaging -q`
- [x] 预期结果：95 passed；`wechatauto.db` 可导入；`MediaDownloader.__init__` 签名兼容。
- [x] 失败/边界情形：上游 `logger` 缺失 → 报 `DECODER_UNAVAILABLE`（不是
      `ImportError` 直崩）；`WECHATAUTO_KEYS_DIR` 还原后不残留进程级环境变量。
- [x] 无控制台运行时（`sys.stdout`/`sys.stderr` 均为 `None`）导入并真实连接成功，
      证明冻结窗口模式不会再因上游 logger/colorama 崩溃。
- [x] 连接耗时：同账号同缓存目录 12.05 s（1.2.1）→ 35.4–41.7 s（1.3.0.2 原样）
      → 10.0–10.5 s（跳过自愈后），密钥数 27 与 `cfgDword` 均不变。

## 风险与回滚

- 风险：新链读整模块 `ReadProcessMemory`（约 200 MB）在部分机器可能被 AV 拦截；
  上游 `_read()` 现在要求整块读满，截断即视为失败（更保守）。跳过账号自愈依赖
  “用户已显式选择账号 + 页 1 HMAC 校验”，若将来出现选错账号的现场，需要回退该跳过。
- 回滚：`git revert` 本次提交；上游 clone 侧 `git checkout 798989c` 并
  `pip install -e . --no-deps`，再重建便携包。
- 需要人工决定：是否把 `DECODER_UNAVAILABLE` 拆成更细的错误码（涉及 contracts，
  需契约锁），本轮不做。

## 交接

- 改动文件：见 `coordination/integration-board.md` 对应行。
- 事实日志条目：`F-20261126-001` 至 `F-20261126-004`。
- 契约锁状态：未取得、未修改 `contracts/`。
