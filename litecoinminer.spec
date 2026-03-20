# litecoinminer.spec
# Build with:
#   pyinstaller --clean --noconfirm litecoinminer.spec

import os
from pathlib import Path

block_cipher = None

ROOT = Path(globals().get("SPECPATH", os.getcwd())).resolve()

def add_data(filename: str, dest: str = "."):
    p = ROOT / filename
    if p.exists():
        return [(str(p), dest)]
    print(f"[spec] warning: missing data file: {p}")
    return []

def add_binary(filename: str, dest: str = "."):
    p = ROOT / filename
    if p.exists():
        return [(str(p), dest)]
    print(f"[spec] warning: missing binary file: {p}")
    return []

# Prefer main.py if it exists; otherwise fall back to gui.py.
ENTRY = "gui.py"

datas = []
datas += add_data("litecoin_miner_config.json")
datas += add_data("litecoin_scrypt_scan.cl")

binaries = []
binaries += add_binary("LitecoinProject.dll")

# Usually do NOT bundle OpenCL.dll.
# Let the system/vendor OpenCL loader provide it unless you truly need a private one.
# binaries += add_binary("OpenCL.dll")

hiddenimports = [
    "litecoin_models",
    "litecoin_native",
    "litecoin_opencl",
    "litecoin_pool",
    "litecoin_utils",
    "litecoin_worker",
]

a = Analysis(
    [ENTRY],
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
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,   # keep True until startup works
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)