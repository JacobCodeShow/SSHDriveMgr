@echo off
setlocal DisableDelayedExpansion
rem Run only published dist entries. No Python installation is needed.
if not "%~2"=="" goto usage
if "%~1"=="" goto onefile
if /i "%~1"=="onedir" goto onedir
goto usage

:onefile
set "APP=%~dp0dist\SSHDriveMgr.exe"
if not exist "%APP%" goto missing
start "" /D "%~dp0" "%APP%"
if errorlevel 1 exit /b 1
exit /b 0

:onedir
set "APP=%~dp0dist\SSHDriveMgr-onedir\SSHDriveMgr.exe"
set "RUNTIME=%~dp0dist\SSHDriveMgr-onedir\_internal"
if not exist "%APP%" goto missing
if not exist "%RUNTIME%\base_library.zip" goto incomplete
rem Two or more version digits after python3: Python 3.11 and newer.
dir /b /a-d "%RUNTIME%\python3*.dll" 2>nul | "%SystemRoot%\System32\findstr.exe" /r /i /x "python3[0-9][0-9][0-9]*[.]dll" >nul
if errorlevel 1 goto incomplete
start "" /D "%~dp0" "%APP%"
if errorlevel 1 exit /b 1
exit /b 0

:missing
echo ERROR: Missing published build: "%APP%" 1>&2
echo Build with build_dual.ps1 gui or build_dual.ps1 onedir, or unpack the complete release into dist. 1>&2
echo The build directory contains intermediate files, NOT runnable releases. 1>&2
exit /b 2

:incomplete
echo ERROR: Incomplete OneDir runtime beside "%APP%". 1>&2
echo Keep the entire dist\SSHDriveMgr-onedir folder, including _internal. 1>&2
echo Rebuild with build_dual.ps1 onedir or unpack the complete release. 1>&2
exit /b 3

:usage
echo Usage: Start-SSHDriveMgr.cmd [onedir] 1>&2
echo Default: dist\SSHDriveMgr.exe. Optional onedir: complete dist\SSHDriveMgr-onedir. 1>&2
exit /b 2
