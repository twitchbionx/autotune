# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for autotune.
#
# Build:
#     pyinstaller autotune.spec
#
# Output:
#     dist/autotune.exe        (--onefile bundle, ~10-15 MB)
#
# Notes:
#   * --uac-admin is applied via the "uac_admin" kwarg on EXE() below, which
#     embeds a Windows manifest demanding admin elevation. Launching the
#     .exe from a non-elevated shell will trigger the UAC prompt. This is
#     what we want -- the tool requires admin and this makes that seamless.
#   * The example config file is bundled so a fresh user running the .exe
#     in isolation can extract it with `autotune.exe --help` guidance.
#   * We use --console (default) so CLI output is visible.

block_cipher = None


a = Analysis(
    ['autotune_main.py'],
    pathex=[],
    binaries=[],
    datas=[
        # Bundled config example -- extracted by the .exe on first-run help
        ('autotune/config.example.yaml', 'autotune'),
        # Compiled Pawn module (unsigned, for unrestricted PawnIO driver).
        # Exposes the full MSR read/write + OC Mailbox surface we need.
        ('autotune/autotune.amx', 'autotune'),
        # PowerShell bridge to the Intel XTU SDK (Option B). xtu_sdk.py
        # spawns this via powershell.exe, exchanges JSON over stdio.
        ('autotune/sdk_bridge.ps1', 'autotune'),
    ],
    hiddenimports=[
        # All autotune submodules; PyInstaller finds most via import graph
        # but we list them explicitly for safety.
        'autotune',
        'autotune.app',
        'autotune.setup',
        'autotune.cpu',
        'autotune.driver',
        'autotune.msr',
        'autotune.pawnio',
        'autotune.oc_mailbox',
        'autotune.backend',
        'autotune.config',
        'autotune.monitor',
        'autotune.state',
        'autotune.stress',
        'autotune.tuner',
        'autotune.watchdog',
        'autotune.xtu',
        # SDK-driven backend (Option B). pythonnet is imported lazily so
        # the .exe still loads on machines without it; only the sdk-* CLI
        # paths require pythonnet at runtime.
        'autotune.sdk_ids',
        'autotune.xtu_sdk',
        'autotune.sdk_backend',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # Trim unused stdlib-adjacent modules to shrink the binary
        'tkinter',
        'test',
        'unittest',
        'pydoc',
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
    name='autotune',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,               # UPX often triggers AV false positives; skip it.
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=True,
)
