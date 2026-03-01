# -*- mode: python ; coding: utf-8 -*-
import os
import site

block_cipher = None

# Find vosk package location
vosk_path = None
for sp in site.getsitepackages():
    candidate = os.path.join(sp, "vosk")
    if os.path.isdir(candidate):
        vosk_path = candidate
        break

# Collect vosk binaries and data files
vosk_datas = []
vosk_binaries = []
if vosk_path:
    for f in os.listdir(vosk_path):
        full = os.path.join(vosk_path, f)
        if f.endswith(('.dll', '.so', '.pyd')):
            vosk_binaries.append((full, "vosk"))
        elif not f.endswith('.py') and not f.endswith('.pyc'):
            if os.path.isfile(full):
                vosk_datas.append((full, "vosk"))

a = Analysis(
    ["app.py"],
    pathex=["."],
    binaries=vosk_binaries,
    datas=vosk_datas + [
        ("version.txt", "."),   # Bundle version.txt inside exe
    ],
    hiddenimports=[
        "vosk",
        "pyttsx3",
        "pyttsx3.drivers",
        "pyttsx3.drivers.sapi5",
        "sounddevice",
        "cffi",
        "_cffi_backend",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name="QuickCast",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
