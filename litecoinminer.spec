# litecoinminer.spec
# Build with:
#   pyinstaller --clean --noconfirm litecoinminer.spec

import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

ROOT = Path(globals().get("SPECPATH", os.getcwd())).resolve()

def add_data(filename, dest="."):
    p = ROOT / filename
    if p.exists():
        return [(str(p), dest)]
    print(f"[spec] warning: missing data file: {p}")
    return []

def add_binary(filename, dest="."):
    p = ROOT / filename
    if p.exists():
        return [(str(p), dest)]
    print(f"[spec] warning: missing binary file: {p}")
    return []

datas = []
datas += add_data("litecoin_miner_config.json")
datas += add_data("litecoin_scrypt_scan.cl")

binaries = []
binaries += add_binary("LitecoinProject.dll")
binaries += add_binary("OpenCL.dll")

hiddenimports = [
    "litecoin_models",   # keep this if your file is really named litecoin_modeles.py
    "litecoin_native",
    "litecoin_opencl",
    "litecoin_pool",
    "litecoin_utils",
    "litecoin_worker",
]

# If the real filename is litecoin_models.py instead, replace the line above with:
# "litecoin_models",

# Helps PyInstaller catch PyQt5 pieces if your GUI imports are dynamic.
hiddenimports += collect_submodules("PyQt5")

a = Analysis(
    ["gui.py"],
    pathex=[str(ROOT)],
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
    name="LitecoinMiner",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,   # change to True if you want a console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)