# 微信空间管理器（只读相册版）

把微信里的照片、视频、文件**平铺成相册**：能看、能筛、能导出、能把不要的移进系统回收站（可还原）。
**没有任何永久删除，更不会改你的聊天记录。**

> ⚠️ 使用前请先读 [免责声明](docs/disclaimer.md)：个人本地工具、按"现状"提供，
> 动手前请先备份重要聊天。

## 30 秒上手（Windows）

1. 从右侧 **Releases** 下载 `wechat-space-manager-v*-win64-portable.zip`，解压到任意文件夹。
2. 双击 `wechat-space-manager.exe`（首次启动 Windows 可能提示"未知发布者"，选"仍要运行"）。
3. 点 **「自动检测」** → 选账号 → **「连接」**（微信需登录并保持运行）。
4. 用筛选找出大文件/老照片：推荐组合 `类型=全部` + `最小=5.0 MB` + `时间=半年以前` + 排序`大小：大→小`。
5. 单击看大图；不要的就 **「移到回收站…」**（可还原）；想留的就 **「导出原图…」**。

断网也能用：全程本机处理，不上传任何数据。

## 它能做什么 / 不能做什么

| 能做 | 不能做 |
|---|---|
| 10 万级媒体平铺浏览，滚动即加载 | 永久删除（代码里就不存在这个能力） |
| 按类型 / 大小 / 时间 / 会话 / 可用性筛选，会话按占用排序 | 修改微信目录、触碰聊天数据库 |
| 图片预览（智能/缩略图/原图），视频封面 + 抽样播放 | 联网、上传、后台静默操作 |
| 导出原图；移入系统回收站（逐次确认、可还原） | 保证所有格式都能解开（会如实报错） |

详细教程见 [使用说明](docs/read-only-beta-user-guide.md)，新手先看它的"第 0 步"。

## 安全承诺

- 默认只读；密钥只在内存中使用，不落盘、不记日志、不上传。
- 真实聊天内容、密钥、数据库、媒体与绝对路径不进入本仓库（测试只用合成数据）。
- 解码产物只写应用自己的缓存目录，可一键清空；"清理" == 把文件移进系统回收站。

## 给开发者

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest tests -q
```

- 协作与安全规则：[AGENTS.md](AGENTS.md)
- 架构：[docs/architecture.md](docs/architecture.md)
- 隐私边界：[docs/data-and-license-boundaries.md](docs/data-and-license-boundaries.md)
- 打包：`python packaging/build.py`（需 PySide6 + PyInstaller，见 [打包计划](plan/P4-REAL-PACKAGE-01-windows-beta-package.md)）

## 许可证

Apache-2.0（见 [LICENSE](LICENSE)），第三方归属见 [NOTICE](NOTICE) 与
[packaging/licenses/THIRD-PARTY-NOTICES.md](packaging/licenses/THIRD-PARTY-NOTICES.md)。
本软件与腾讯/微信无任何关联。
