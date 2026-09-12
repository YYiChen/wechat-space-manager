"""First-run capability and risk disclosure dialogs.

Two sequential popups gate the desktop entry point:

1. capability boundary — what the app does and never does;
2. risk notice — dev-beta instability, recycle-bin loss, no warranty.

The second dialog offers three exits: remember the acceptance, accept for this
run only, or quit.  Acceptance is versioned in ``settings.json`` under
``disclaimer_accepted_version``: editing the texts below without bumping
:const:`DISCLAIMER_VERSION` silently voids the gate, so the constant lives
next to the texts it covers.  A missing or corrupt settings file reads as
"not accepted" (fail-closed).

The frozen self-test and the test-suite bypass via ``WCSM_SKIP_DISCLAIMER=1``:
a modal dialog inside an unattended run would hang the packaging gate.
"""

from __future__ import annotations

import os

__all__ = [
    "DISCLAIMER_VERSION",
    "SKIP_ENV_VAR",
    "ensure_disclaimer_accepted",
    "is_disclaimer_accepted",
    "remember_disclaimer_accepted",
]

DISCLAIMER_VERSION = 2
_SETTINGS_KEY = "disclaimer_accepted_version"
SKIP_ENV_VAR = "WCSM_SKIP_DISCLAIMER"

APP_VERSION = "v0.1.2"

BOUNDARY_TITLE = "使用前请了解本软件能做什么"
BOUNDARY_TEXT = """微信空间管理器（只读相册版）v0.1.2

【它会做的】
• 读取你电脑上的微信数据，在本机生成相册、筛选和预览
• 经你每次确认后，把选中的照片/视频移出微信目录、送进"系统回收站"（可在回收站还原）
• 把解密后的原图导出到"你指定的文件夹"

【它不会做的】
• 不改写任何文件的内容物，不碰聊天数据库和收藏
• 没有永久删除，更不会在后台自动清理
• 不联网、不上传任何数据，断网也能用

列表里本来就没有数据库可选，你删不到聊天记录本身。"""

RISK_TITLE = "风险告知，请仔细阅读"
RISK_TEXT = """• 这是个人兴趣开发的软件，不保证每个功能都可用，遇到解不开的格式、
  不兼容的新版微信都属正常。如果你不清楚"清理数据"意味着什么，
  请现在点"退出软件"。
• 误删风险："移到回收站"仍然是删除动作，清空回收站后无法找回。
  动手前请先用微信自带功能备份重要聊天，并先拿 2-3 个文件试手。
• 微信兼容性：当前微信版本实测下，清理媒体文件后微信正常使用
  （缺失的图只显示"无法查看"占位）。但不保证清理后微信 100% 正常，
  微信后续升级可能改变这一行为。
• 责任边界：软件按"现状"提供，作者不对任何数据丢失负责。
  完整条款见 docs/disclaimer.md（及 Apache-2.0 第 7/8 条）。

继续使用即表示你已备份重要数据，理解清理的含义，并接受以上全部风险。"""

_CONTINUE = "继续"
_CONFIRM_ONCE = "确认，仅本次"
_CONFIRM_REMEMBER = "确认且今后不再弹出"
_QUIT = "退出软件"


def is_disclaimer_accepted() -> bool:
    """True when the stored acceptance matches the current text version."""
    from .settings import load_settings

    try:
        return int(load_settings().get(_SETTINGS_KEY, 0)) >= DISCLAIMER_VERSION
    except (TypeError, ValueError):
        return False


def remember_disclaimer_accepted() -> bool:
    """Persist the current text version; returns whether it was stored."""
    from .settings import load_settings, save_settings

    values = load_settings()
    values[_SETTINGS_KEY] = DISCLAIMER_VERSION
    return save_settings(values)


def ensure_disclaimer_accepted(parent=None) -> bool:
    """Run the two dialogs; True enters the app, False quits before it shows."""
    if os.environ.get(SKIP_ENV_VAR) == "1":
        return True
    if is_disclaimer_accepted():
        return True

    from PySide6.QtWidgets import QMessageBox

    boundary = QMessageBox(parent)
    boundary.setWindowTitle(BOUNDARY_TITLE)
    boundary.setText(BOUNDARY_TEXT)
    boundary.setIcon(QMessageBox.Icon.Information)
    go_on = boundary.addButton(_CONTINUE, QMessageBox.ButtonRole.AcceptRole)
    boundary.addButton(_QUIT, QMessageBox.ButtonRole.RejectRole)
    boundary.exec()
    if boundary.clickedButton() is not go_on:
        return False

    risk = QMessageBox(parent)
    risk.setWindowTitle(RISK_TITLE)
    risk.setText(RISK_TEXT)
    risk.setIcon(QMessageBox.Icon.Warning)
    remember = risk.addButton(_CONFIRM_REMEMBER, QMessageBox.ButtonRole.AcceptRole)
    once = risk.addButton(_CONFIRM_ONCE, QMessageBox.ButtonRole.ActionRole)
    risk.addButton(_QUIT, QMessageBox.ButtonRole.RejectRole)
    risk.exec()
    clicked = risk.clickedButton()
    if clicked is remember:
        remember_disclaimer_accepted()
        return True
    return clicked is once
