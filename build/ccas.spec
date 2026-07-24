# -*- mode: python ; coding: utf-8 -*-

import os
import platform
import sys
from pathlib import Path

ROOT = Path(os.path.dirname(os.path.abspath(SPEC))).parent

PROJECT = "ccas"

OS_NAMES = {
    "win32": "windows",
    "cygwin": "windows",
    "darwin": "macos",
    "linux": "linux",
}

ARCH_NAMES = {
    "amd64": "x64",
    "x86_64": "x64",
    "x64": "x64",
    "i386": "x86",
    "i686": "x86",
    "x86": "x86",
    "aarch64": "arm64",
    "arm64": "arm64",
    "armv7l": "arm",
}


def resolve_os():
    for prefix, name in OS_NAMES.items():
        if sys.platform.startswith(prefix):
            return name
    return sys.platform


def resolve_arch():
    machine = platform.machine().lower()
    return ARCH_NAMES.get(machine, machine or "unknown")


ARTIFACT_NAME = (
    os.environ.get("CCAS_ARTIFACT_NAME")
    or f"{PROJECT}-{resolve_os()}-{resolve_arch()}"
)

LANG_DATA = [(str(path), "lang") for path in sorted((ROOT / "lang").glob("*.json"))]

HIDDEN = [
    "app.cli",
    "app.installer",
    "app.wrapper",
    "core.claudecfg",
    "core.detect",
    "core.log",
    "core.store",
    "core.version",
    "system.secure",
    "ui.i18n",
    "ui.menu",
    "ui.usage",
]

if sys.platform == "win32":
    HIDDEN.append("system.pathenv_win")
else:
    HIDDEN.append("system.pathenv_posix")

analysis = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=LANG_DATA,
    hiddenimports=HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "test",
        "distutils",
        "lib2to3",
        "pydoc_data",
        "numpy",
        "PIL",
    ],
    noarchive=False,
)

pyz = PYZ(analysis.pure, analysis.zipped_data)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.zipfiles,
    analysis.datas,
    [],
    name=ARTIFACT_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
