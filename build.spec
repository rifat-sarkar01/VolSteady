# -*- mode: python ; coding: utf-8 -*-
"""
VolSteady — PyInstaller Build Spec
Packages into a single .exe with no console window.
Run: pyinstaller build.spec
Output: dist/VolSteady.exe (~25-35 MB)
"""

import sys
from pathlib import Path

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[str(Path('.').resolve())],
    binaries=[],
    datas=[
        ('assets', 'assets'),
    ],
    hiddenimports=[
        'pyaudiowpatch',
        'numba',
        'numba.core',
        'numba.typed',
        'numpy',
        'pycaw',
        'pycaw.pycaw',
        'comtypes',
        'comtypes.client',
        'pystray',
        'pystray._win32',
        'PIL',
        'PIL.Image',
        'PIL.ImageDraw',
        'ctypes',
        'ctypes.wintypes',
        'dsp.pipeline',
        'dsp.compressor',
        'dsp.limiter',
        'dsp.gate',
        'dsp.level_detector',
        'gui.tray',
        'gui.settings_window',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib',
        'scipy',
        'pandas',
        'tkinter.test',
        'test',
        'unittest',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='VolSteady',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,            # No console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/icon.ico',
    version_file=None,
    uac_admin=False,
)
