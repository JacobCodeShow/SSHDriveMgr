# -*- mode: python ; coding: utf-8 -*-
# SSHDriveMgr-cli — console companion. Talks to the running GUI over the
# named pipe, then runs the paramiko session in-place. Runtime chain is:
#   cli_main -> src.ssh_launcher -> {src.config, src.app_logger(logger only),
#                                   src.utils.secure_memory, src.sshfs_controller,
#                                   src.askpass_manager} + paramiko
# There is deliberately NO PyQt6 in this chain: app_logger's Qt import lives
# inside init_logger(), which only the GUI calls. PyInstaller's static
# analysis can't see that distinction and used to bundle PyQt6 anyway
# (~34 MB: Qt6Core + the software-OpenGL renderer); excluding it here is what
# keeps the CLI EXE small.
# No datas are needed: src bytecode is in the PYZ, and the CLI chain never
# reads translations/*.json or version.txt from disk.


a = Analysis(
    ['cli_main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Qt stack — not used by the console companion (saves ~34 MB)
        'PyQt6',
        # Not imported anywhere in the CLI chain. pywin32 modules are only
        # reached via the GUI's IPC DACL / UAC-repair code, which the CLI
        # never executes (saves pythoncom/pywintypes + win32 libs).
        'win32api', 'win32con', 'win32com', 'win32comext', 'win32ui',
        'pythonwin', 'Pythonwin',
        # Dev/build tooling accidentally graph-reachable via hooks
        'setuptools', '_distutils_hack', 'pip',
        'tkinter',
        '_pytest', 'pytest',
        # GUI-only dependencies, kept here defensively so a future import in
        # a shared src module doesn't silently inflate the CLI build
        'keyring', 'websockets', 'pillow', 'PIL',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='SSHDriveMgr-cli',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX off: compressed pyds regularly trip antivirus heuristics, and in a
    # onefile build UPX costs extra per-launch decompress time that outweighs
    # the size saving for a 15 MB exe.
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version='file_version_info.txt',
    icon=['assets\\app_icon.ico'],
)
