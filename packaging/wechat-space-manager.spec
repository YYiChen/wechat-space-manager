# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the read-only Windows Beta of 微信空间管理器.

Design constraints (see plan/P4-REAL-ROLLOUT-01.md section 3 and 9):

* the frozen app is **read-only with respect to the WeChat source** — nothing in
  this spec may bundle a cleanup/executor entry point;
* the upstream adapter ``wechatauto`` is Apache-2.0 and must travel with its
  LICENSE and a modification notice.  It is staged *trimmed* by
  ``packaging/stage_upstream.py`` (only ``db``/``media`` plus a minimal
  ``__init__``) so the packaged app cannot reach upstream's message-sending,
  moments or UIA surfaces;
* PySide6 is trimmed to the widgets we actually import (QtWidgets/QtCore/QtGui)
  to keep the artifact portable and small.

Build (one-dir, portable):

    python packaging/build.py

The output lands in ``dist/wechat-space-manager/`` (git-ignored).
"""

from __future__ import annotations

from pathlib import Path

# --- repo layout -------------------------------------------------------------

SPEC_DIR = Path(SPECPATH).resolve()  # noqa: F821 - injected by PyInstaller
REPO_ROOT = SPEC_DIR.parent
SRC_DIR = REPO_ROOT / "src"
LICENSE_DIR = SPEC_DIR / "licenses"
APP_ICON = SPEC_DIR / "icons" / "app.ico"
# ``stage_upstream.py`` writes the trimmed upstream package here.
STAGING_ROOT = REPO_ROOT / "build" / "upstream-staging"

# Entry script: a dedicated frozen bootstrap so we never depend on __main__'s
# argument-parsing quirks inside a bundled executable.
ENTRY_SCRIPT = SPEC_DIR / "frozen_entry.py"

# --- data files --------------------------------------------------------------

datas = [
    # Upstream license + our notice travel with the binary (Apache-2.0 obligation).
    (str(LICENSE_DIR / "wechatauto-replica-LICENSE.txt"), "licenses"),
    (str(LICENSE_DIR / "THIRD-PARTY-NOTICES.md"), "licenses"),
    # Window icon, resolved at runtime from the bundle (see
    # ``real_window._app_icon_file``); the exe icon is set below.
    (str(SPEC_DIR / "icons" / "app.png"), "icons"),
]

# --- module search path ------------------------------------------------------

# ``pathex`` puts the *staging* root ahead of site-packages so PyInstaller
# analyses the trimmed upstream package rather than failing to resolve the
# editable install.  The project ``src/`` follows.
pathex = [str(SRC_DIR)]
if STAGING_ROOT.is_dir():
    pathex.insert(0, str(STAGING_ROOT))

# --- hidden imports ----------------------------------------------------------

hiddenimports = [
    # Imported lazily inside real_media/keys.py, so PyInstaller cannot see them
    # statically.  Resolved against the staged trimmed package.
    "wechatauto",
    "wechatauto.db",
    "wechatauto.media",
    # pydantic v2 uses compiled helpers resolved at runtime.
    "pydantic",
    "pydantic_core",
]

# --- excludes ----------------------------------------------------------------

# Never ship an execution path.  The executor and cleanup packages are excluded
# so a packaging mistake cannot silently make deletion reachable in the Beta.
excludes = [
    "wechat_cleaner.executor",
    "wechat_cleaner.cleanup",
    "wechat_cleaner.ui",
    # Upstream read-write surfaces: never bundled into a read-only Beta.
    "wechatauto.wx",
    "wechatauto.guia",
    "wechatauto.moment",
    "wechatauto.sender",
    "wechatauto.uia_driver",
    "wechatauto.ui",
    "wechatauto.uia",
    "wechatauto.msgs",
    "wechatauto.utils",
    "wechatauto.wx",
    # Test/dev tooling.
    "tkinter",
    "unittest",
    "pytest",
    "lib2to3",
    # Heavy Qt modules the desktop window never imports.
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtQuick",
    "PySide6.QtQml",
    "PySide6.Qt3DCore",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtMultimedia",
    "matplotlib",
    "numpy",
    "pandas",
]

# --- analysis ----------------------------------------------------------------

block_cipher = None

a = Analysis(  # noqa: F821 - injected by PyInstaller at build time
    [str(ENTRY_SCRIPT)],
    pathex=pathex,
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="wechat-space-manager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # desktop Beta: no console window
    icon=str(APP_ICON) if APP_ICON.is_file() else None,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# ``packaging/build.py`` exports the build id so each run writes a fresh
# directory and never has to remove a previous artifact tree.
import os  # noqa: E402

COLLECT_NAME = os.environ.get("WCSM_BUILD_ID") or "wechat-space-manager"

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=COLLECT_NAME,
)
