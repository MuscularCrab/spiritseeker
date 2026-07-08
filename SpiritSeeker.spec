# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for SpiritSeeker.

Handles both standard python.org installs and conda/Miniforge installs
(whose tcl/tk DLLs live in Library/bin where PyInstaller's tkinter hook
doesn't look).
"""
import os
import sys

from PyInstaller.utils.hooks import collect_all

datas = [("assets", "assets")]
binaries = []
hiddenimports = []

# imageio-ffmpeg ships the ffmpeg binary as package data
d, b, h = collect_all("imageio_ffmpeg")
datas += d
binaries += b
hiddenimports += h

# conda/Miniforge layout: runtime DLLs (tcl/tk, expat, ssl, ffi, ...) live in
# Library/bin, which PyInstaller's dependency scan misses when building from
# a venv created off a conda base. Bundle them explicitly.
import glob

conda_bin = os.path.join(sys.base_prefix, "Library", "bin")
conda_lib = os.path.join(sys.base_prefix, "Library", "lib")
if os.path.exists(os.path.join(conda_bin, "tcl86t.dll")):
    for pattern in ("lib*.dll", "ffi*.dll", "sqlite3.dll", "zlib*.dll",
                    "tcl*.dll", "tk*.dll"):
        for dll in glob.glob(os.path.join(conda_bin, pattern)):
            binaries.append((dll, "."))
    # Folder names must match PyInstaller's pyi_rth__tkinter runtime hook
    datas += [
        (os.path.join(conda_lib, "tcl8.6"), "_tcl_data"),
        (os.path.join(conda_lib, "tk8.6"), "_tk_data"),
    ]

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="SpiritSeeker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon="assets\\icon.ico",
)
