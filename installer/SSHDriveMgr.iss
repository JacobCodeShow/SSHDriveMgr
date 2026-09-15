; SSHDriveMgr Windows installer (Inno Setup 6)
; Simple English-only installer to avoid encoding issues.
; The app itself has full multi-language support inside Settings.

#define MyAppName "SSHDriveMgr"
#define MyAppExeName "SSHDriveMgr.exe"
#define MyAppCliExeName "SSHDriveMgr-cli.exe"
#define MyAppPublisher "JacobCodeShow"
#define MyAppURL "https://github.com/JacobCodeShow/SSHDriveMgr"
#define MyAppId "{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}"

#define VersionFileHandle FileOpen(SourcePath + "..\src\version.txt")
#define MyAppVersion Trim(FileRead(VersionFileHandle))
#expr FileClose(VersionFileHandle)

#if MyAppVersion == ""
  #error Could not read a version from src\version.txt
#endif

[Setup]
AppId={{#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
OutputDir=..\dist_installer
OutputBaseFilename=SSHDriveMgr-Setup-{#MyAppVersion}
SetupIconFile=..\assets\app_icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ChangesAssociations=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"
Name: "autostart"; Description: "Start SSHDriveMgr automatically when Windows starts"; GroupDescription: "Additional icons:"

[Files]
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\{#MyAppCliExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\CHANGELOG.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autoprograms}\{#MyAppName} CLI"; Filename: "{app}\{#MyAppCliExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#MyAppName}"; ValueData: """{app}\{#MyAppExeName}"""; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch SSHDriveMgr"; Flags: nowait postinstall skipifsilent
