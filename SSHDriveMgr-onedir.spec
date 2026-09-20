# -*- mode: python ; coding: utf-8 -*-
# SSHDriveMgr — onedir comparison build.
#
# Identical Analysis + trimming logic as SSHDriveMgr.spec (KEEP THEM IN SYNC —
# if you change hiddenimports/excludes/_KEEP_QT_DLLS/_DROP_QML_SUBTREES/
# _DROP_BINARY_NAMES in one, change the other). The only difference is the
# final packaging step:
#
#   onefile (SSHDriveMgr.spec): single exe; every launch re-extracts ~140 MB
#     to a fresh %TEMP%\_MEIxxxx folder → slow cold start, leaves temp litter
#     if the process is killed, but distributes as one file.
#
#   onedir (this file): dist\SSHDriveMgr-onedir\{SSHDriveMgr.exe,_internal\...}
#     → no extraction at launch, much faster cold start and smaller on-disk
#     footprint duplicated once; distribute the whole folder (zip it or wrap
#     it in an installer).
#
# All runtime resource lookups use sys._MEIPASS (which PyInstaller points at
# <onedir>\_internal in this mode), so no application code changes are needed.
import re


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('src/translations', 'src/translations'),
        ('src/version.txt', 'src'),
        ('assets', 'assets'),
    ],
    hiddenimports=[
        'PyQt6.sip',
        'win32api', 'win32con', 'winreg',
        'keyring', 'keyring.backends.Windows',
        'win32com.shell', 'win32com.shell.shell', 'win32com.shell.shellcon',
        'win32event', 'win32process',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        '_pytest', 'pytest',
        'setuptools', '_distutils_hack', 'pip',
        # NOTE: QtNetwork/QtPositioning/QtPrintSupport/QtQml/QtQuick/
        # QtQuickWidgets/QtOpenGL/QtOpenGLWidgets bindings must stay INCLUDED:
        # QtWebEngineWidgets' module init imports them (excluding QtPrintSupport
        # broke the frozen build: the pre-app import failed cleanly, then the
        # second import at terminal open re-ran a half-initialized pyd and died
        # with a garbled-sip RuntimeError). Their native DLLs are shipped
        # anyway as WebEngine deps.
        'PyQt6.QtMultimedia', 'PyQt6.QtMultimediaWidgets',
        'PyQt6.QtPdf', 'PyQt6.QtPdfWidgets', 'PyQt6.QtCharts',
        'PyQt6.QtDataVisualization', 'PyQt6.QtTest', 'PyQt6.QtDesigner',
        'PyQt6.QtHelp', 'PyQt6.QtBluetooth', 'PyQt6.QtNfc',
        'PyQt6.QtSensors', 'PyQt6.QtWebSockets', 'PyQt6.QtSql',
        'PyQt6.QtDBus', 'PyQt6.QtXml',
    ],
    noarchive=False,
    optimize=0,
)

_KEEP_QM_LANGS = {'en', 'de'}

def _keep_datafile(entry):
    dest = entry[0].replace('\\', '/').lower()
    if dest.endswith('.debug.pak') or dest.endswith('.debug.bin'):
        return False
    if '/qtwebengine_locales/' in dest:
        return dest.endswith('/en-us.pak') or dest.endswith('/de.pak')
    m = re.search(r'/qt(?:_help)?_([a-z]{2}(?:_[a-z]{2})?)\.qm$', dest)
    if m:
        return m.group(1) in _KEEP_QM_LANGS
    return True

a.datas = [d for d in a.datas if _keep_datafile(d)]

_KEEP_QT_DLLS = {
    'qt6core.dll', 'qt6gui.dll', 'qt6network.dll', 'qt6opengl.dll',
    'qt6positioning.dll', 'qt6printsupport.dll', 'qt6qml.dll',
    'qt6qmlmeta.dll', 'qt6qmlmodels.dll', 'qt6qmlworkerscript.dll',
    'qt6quick.dll', 'qt6quickwidgets.dll', 'qt6svg.dll',
    'qt6webchannel.dll',
    'qt6webenginecore.dll', 'qt6webenginewidgets.dll', 'qt6widgets.dll',
}

_DROP_QML_SUBTREES = (
    'qt6/qml/qtquick3d/',
    'qt6/qml/qtquick/controls',
    'qt6/qml/qtquick/nativestyle',
    'qt6/qml/qtquick/dialogs',
    'qt6/qml/qtquick/templates',
    'qt6/qml/qtquick/pdf',
    'qt6/qml/qtquick/particles',
    'qt6/qml/qtquick/shapes',
    'qt6/qml/qtquick/timeline',
    'qt6/qml/qtmultimedia/',
    'qt6/qml/qttest/',
    'qt6/qml/qttexttospeech/',
    'qt6/qml/qtpositioning/',
    'qt6/qml/qtwebengine/',
    'qt6/qml/qtbluetooth/',
    'qt6/qml/qtnfc/',
    'qt6/qml/qtwebsockets/',
)

_DROP_BINARY_NAMES = {'opengl32sw.dll', 'mfc140u.dll'}

def _keep_binary(entry):
    dest = entry[0].replace('\\', '/').lower()
    base = dest.rsplit('/', 1)[-1]
    if '/qt6/bin/' in dest and base.startswith('qt6') and base.endswith('.dll'):
        return base in _KEEP_QT_DLLS
    # orphan plugins whose native deps (Qt6Pdf / Qt6SerialPort) are deliberately
    # not shipped: PDF image reader and GPS-NMEA position source — unused here
    if '/plugins/imageformats/qpdf.dll' in dest or '/plugins/position/' in dest:
        return False
    if '/pythonwin/' in dest or base in _DROP_BINARY_NAMES:
        return False
    return True

a.binaries = [b for b in a.binaries if _keep_binary(b)]

def _keep_qml_data(entry):
    dest = entry[0].replace('\\', '/').lower()
    if '/qt6/qml/' in dest:
        return not any(piece in dest for piece in _DROP_QML_SUBTREES)
    return True

a.datas = [d for d in a.datas if _keep_qml_data(d)]

pyz = PYZ(a.pure)

# onedir EXE: exclude_binaries=True so binaries/datas go to COLLECT, not
# into the exe. a.scripts still includes the bootloader script.
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='SSHDriveMgr',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version='file_version_info.txt',
    icon=['assets\\app_icon.ico'],
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='SSHDriveMgr-onedir',
)
