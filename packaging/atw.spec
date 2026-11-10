# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller one-file build for ATW V1.

Build on the target OS (never cross-compile):
  pyinstaller packaging/atw.spec --distpath dist/<platform> --workpath build/pyinstaller
"""
import os
import sys

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.dirname(os.path.abspath(SPECPATH))
SRC = os.path.join(ROOT, "src")

hiddenimports = [
    "pynvml",
    "pydantic",
    "pydantic_core",
    "httpx",
    "httpcore",
    "h11",
    "anyio",
    "yaml",
    "typer",
    "click",
    "rich",
    "tzdata",
]
hiddenimports += collect_submodules("watchdog")
hiddenimports += collect_submodules("watchdog.observers")

a = Analysis(
    [os.path.join(SRC, "atw", "__main__.py")],
    pathex=[SRC],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "jsonschema", "IPython", "matplotlib"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="atw",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
