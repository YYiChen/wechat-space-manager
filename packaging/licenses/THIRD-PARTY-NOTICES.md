# 第三方组件与许可证声明

本发布包（微信空间管理器 · 只读 Beta）包含以下第三方组件。按各自许可要求随包分发。

---

## 1. wechatauto-replica

| 项 | 值 |
| --- | --- |
| 名称 | `wechatauto-replica` |
| 版本 | `1.2.1` |
| 固定 revision | `798989c9b61066c120b59ceba636b557b776ff7f` |
| 许可证 | Apache-2.0（全文见同目录 `wechatauto-replica-LICENSE.txt`） |
| 上游 | https://github.com/fanyuantaier/wechatauto-replica |

### 使用方式

本项目**未复制、未改写**该库的源代码。它以下游依赖形式被引入，本项目通过适配层
`src/wechat_cleaner/real_media/keys.py` 调用其两个公开入口：

- `wechatauto.db.WeChatDB` —— 只读打开 SQLCipher 4 数据库并提取主密钥与 `cfgDword`；
- `wechatauto.media.MediaDownloader` —— 解密 V2 容器图片。

### 修改说明（Apache-2.0 第 4(b) 条）

**未对上游源代码做任何修改。** 本项目仅：

- 固定依赖版本与 revision（见上表）；
- 在调用层做进程内只读适配（密钥只在内存中瞬时使用，不落盘）；
- 不使用其发送、监听、朋友圈读取、UI 自动化等非只读能力。

### NOTICE

上游发行包中不含 `NOTICE` 文件，故本项目无额外 NOTICE 转发义务。

### 上游自身依赖

`wechatauto-replica` 声明依赖：`uiautomation`、`pywin32`、`pyperclip`、`Pillow`、
`psutil`、`colorama`。其中本 Beta 实际调用链涉及 `Pillow`（图像验证）与 `psutil`
（进程探测）；发送/UI 自动化相关依赖不参与只读流程。

---

## 2. 其他运行时依赖

| 组件 | 许可证 | 用途 |
| --- | --- | --- |
| PySide6 (Qt for Python) | LGPL-3.0 / 商业双许可 | 桌面界面 |
| pydantic / pydantic-core | MIT | 领域契约校验 |
| Pillow | MIT-CMU (HPND) | 图像解码与像素验证 |
| platformdirs | MIT | 应用缓存根定位 |

> PySide6 以 LGPL-3.0 分发。本包以动态链接方式使用 Qt 库，未做静态链接或修改，
> 符合 LGPL-3.0 对重新链接的要求；Qt 库文件保持独立、可替换。

---

## 3. 数据与隐私边界

本发布包**不包含**任何真实微信数据：无数据库、无密钥、无媒体文件、无账号或联系人
标识、无绝对路径。所有真实数据仅在用户本机内存/应用自有缓存根中按需处理。

详见仓库 `docs/data-and-license-boundaries.md`。
