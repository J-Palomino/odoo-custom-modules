# PyInstaller spec — build a standalone Mint Print Agent executable.
#   pip install pyinstaller
#   pyinstaller mint_print_agent.spec
# Produces dist/mint_print_agent(.exe). Build ON the target OS (build the
# Windows .exe on Windows). The agent is pure-stdlib, so no hidden imports.
# Place mint_print_agent.conf next to the executable (the agent reads it).

block_cipher = None

a = Analysis(
    ['mint_zebra_agent.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [],
    name='mint_print_agent',
    console=True,          # keep a console so logs are visible; pythonw-style hides it
    debug=False, strip=False, upx=True,
    bootloader_ignore_signals=False,
    disable_windowed_traceback=False,
)
