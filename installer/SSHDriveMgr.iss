; SSHDriveMgr Windows installer (Inno Setup 6)
;
; Builds a Setup.exe from the already-built dist\ executables (run
; build_dual.ps1 first). The app version comes from src\version.txt - the
; single source of truth the app itself reads at runtime. build_dual.ps1
; propagates the same value into file_version_info.txt, so the exe version
; resource matches; the check below fails the compile if it does not (which
; means dist\ holds a stale build).
;
; Compile with: ISCC.exe installer\SSHDriveMgr.iss
; (or run installer\build_installer.ps1, which does both steps)

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

#define ExeVersion GetVersionNumbersString(SourcePath + "..\dist\" + MyAppExeName)
#if ExeVersion != MyAppVersion + ".0"
  #pragma message "dist\" + MyAppExeName + " reports version '" + ExeVersion + "', src\version.txt says '" + MyAppVersion + "'"
  #error The built exe does not match src\version.txt (see message above) - rebuild it with build_dual.ps1
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
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "de"; MessagesFile: "compiler:Languages\German.isl"
Name: "es"; MessagesFile: "compiler:Languages\Spanish.isl"
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "nl"; MessagesFile: "compiler:Languages\Dutch.isl"

[CustomMessages]
en.AutoStartTask=Start %1 automatically when Windows starts
de.AutoStartTask=%1 automatisch mit Windows starten
es.AutoStartTask=Iniciar %1 autom獺ticamente al arrancar Windows
ru.AutoStartTask=?訄郈??郕訄?? %1 訄赲?郋邾訄?邽?迮?郕邽 郈?邽 郱訄迣??郱郕迮 Windows
nl.AutoStartTask=%1 automatisch starten bij het opstarten van Windows

en.AppPrefsPageCaption=Application Preferences
de.AppPrefsPageCaption=Anwendungseinstellungen
es.AppPrefsPageCaption=Preferencias de la aplicaci籀n
ru.AppPrefsPageCaption=?訄???郋邿郕邽 郈?邽郅郋迠迮郇邽?
nl.AppPrefsPageCaption=Toepassingsvoorkeuren

en.AppPrefsPageDescription=Choose the language and appearance %1 should start with. You can change these anytime later in the app's Settings.
de.AppPrefsPageDescription=W瓣hle die Sprache und das Erscheinungsbild, mit dem %1 starten soll. Diese Auswahl kann jederzeit in den Einstellungen der App ge瓣ndert werden.
es.AppPrefsPageDescription=Elige el idioma y la apariencia con la que debe iniciarse %1. Puedes cambiarlos m獺s tarde en los ajustes de la aplicaci籀n.
ru.AppPrefsPageDescription=??訇迮?邽?迮 ?郱?郕 邽 郋?郋?邾郅迮郇邽迮, ? 郕郋?郋??邾邽 迡郋郅迠郇郋 郱訄郈??郕訄???? 郈?邽郅郋迠迮郇邽迮 %1. ?郋郱迠迮 ??郋 邾郋迠郇郋 邽郱邾迮郇邽?? 赲 郇訄???郋邿郕訄? 郈?邽郅郋迠迮郇邽?.
nl.AppPrefsPageDescription=Kies de taal en het uiterlijk waarmee %1 moet starten. Dit kan later altijd worden gewijzigd in de instellingen van de app.

en.AppLanguageLabel=Application language:
de.AppLanguageLabel=Anwendungssprache:
es.AppLanguageLabel=Idioma de la aplicaci籀n:
ru.AppLanguageLabel=觓郱?郕 郈?邽郅郋迠迮郇邽?:
nl.AppLanguageLabel=Applicatietaal:

en.ThemeLabel=Appearance:
de.ThemeLabel=Erscheinungsbild:
es.ThemeLabel=Apariencia:
ru.ThemeLabel=?郇迮?郇邽邿 赲邽迡:
nl.ThemeLabel=Uiterlijk:

en.ThemeDark=Dark
de.ThemeDark=Dunkel
es.ThemeDark=Oscuro
ru.ThemeDark=苠?邾郇訄?
nl.ThemeDark=Donker

en.ThemeLight=Light
de.ThemeLight=Hell
es.ThemeLight=Claro
ru.ThemeLight=苤赲迮?郅訄?
nl.ThemeLight=Licht

en.ComponentGui=GUI application (required)
de.ComponentGui=Grafische Anwendung (erforderlich)
es.ComponentGui=Aplicaci籀n gr獺fica (obligatoria)
ru.ComponentGui=??訄?邽?迮?郕郋迮 郈?邽郅郋迠迮郇邽迮 (郋訇?郱訄?迮郅?郇郋)
nl.ComponentGui=Grafische toepassing (vereist)

en.ComponentCli=Command-line tool (SSHDriveMgr-cli.exe)
de.ComponentCli=Kommandozeilen-Tool (SSHDriveMgr-cli.exe)
es.ComponentCli=Herramienta de l穩nea de comandos (SSHDriveMgr-cli.exe)
ru.ComponentCli=?郇????邾迮郇? 郕郋邾訄郇迡郇郋邿 ???郋郕邽 (SSHDriveMgr-cli.exe)
nl.ComponentCli=Opdrachtregeltool (SSHDriveMgr-cli.exe)

en.MyFullInstallation=Full installation
de.MyFullInstallation=Vollst瓣ndige Installation
es.MyFullInstallation=Instalaci籀n completa
ru.MyFullInstallation=?郋郅郇訄? ???訄郇郋赲郕訄
nl.MyFullInstallation=Volledige installatie

en.MyCompactInstallation=Compact installation (GUI only)
de.MyCompactInstallation=Kompakte Installation (nur GUI)
es.MyCompactInstallation=Instalaci籀n compacta (solo GUI)
ru.MyCompactInstallation=?郋邾郈訄郕?郇訄? ???訄郇郋赲郕訄 (?郋郅?郕郋 GUI)
nl.MyCompactInstallation=Compacte installatie (alleen GUI)

en.MyCustomInstallation=Custom installation
de.MyCustomInstallation=Benutzerdefinierte Installation
es.MyCustomInstallation=Instalaci籀n personalizada
ru.MyCustomInstallation=??訇郋?郋?郇訄? ???訄郇郋赲郕訄
nl.MyCustomInstallation=Aangepaste installatie

[Types]
Name: "full"; Description: "{cm:MyFullInstallation}"
Name: "compact"; Description: "{cm:MyCompactInstallation}"
Name: "custom"; Description: "{cm:MyCustomInstallation}"; Flags: iscustom

[Components]
Name: "gui"; Description: "{cm:ComponentGui}"; Types: full compact custom; Flags: fixed
Name: "cli"; Description: "{cm:ComponentCli}"; Types: full custom

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "autostart"; Description: "{cm:AutoStartTask,{#MyAppName}}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion; Components: gui
Source: "..\dist\{#MyAppCliExeName}"; DestDir: "{app}"; Flags: ignoreversion; Components: cli
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\CHANGELOG.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autoprograms}\{#MyAppName} CLI"; Filename: "{app}\{#MyAppCliExeName}"; Components: cli
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#MyAppName}"; ValueData: """{app}\{#MyAppExeName}"""; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[Code]
var
  AppPrefsPage: TWizardPage;
  LangCombo: TNewComboBox;
  ThemeCombo: TNewComboBox;

procedure InitializeWizard;
var
  LangLabel, ThemeLabel: TNewStaticText;
begin
  AppPrefsPage := CreateCustomPage(wpSelectTasks, CustomMessage('AppPrefsPageCaption'),
    CustomMessage('AppPrefsPageDescription'));

  LangLabel := TNewStaticText.Create(AppPrefsPage);
  LangLabel.Parent := AppPrefsPage.Surface;
  LangLabel.Caption := CustomMessage('AppLanguageLabel');
  LangLabel.Top := ScaleY(8);
  LangLabel.AutoSize := True;

  LangCombo := TNewComboBox.Create(AppPrefsPage);
  LangCombo.Parent := AppPrefsPage.Surface;
  LangCombo.Style := csDropDownList;
  LangCombo.Top := LangLabel.Top + LangLabel.Height + ScaleY(4);
  LangCombo.Width := AppPrefsPage.SurfaceWidth;
  LangCombo.Items.Add('English');
  LangCombo.Items.Add('Deutsch');
  LangCombo.Items.Add('Espa簽ol');
  LangCombo.Items.Add('????郕邽邿');
  LangCombo.Items.Add('Nederlands');
  LangCombo.Items.Add('塈?媢堭堥?堜');
  LangCombo.ItemIndex := 0;

  ThemeLabel := TNewStaticText.Create(AppPrefsPage);
  ThemeLabel.Parent := AppPrefsPage.Surface;
  ThemeLabel.Caption := CustomMessage('ThemeLabel');
  ThemeLabel.Top := LangCombo.Top + LangCombo.Height + ScaleY(16);
  ThemeLabel.AutoSize := True;

  ThemeCombo := TNewComboBox.Create(AppPrefsPage);
  ThemeCombo.Parent := AppPrefsPage.Surface;
  ThemeCombo.Style := csDropDownList;
  ThemeCombo.Top := ThemeLabel.Top + ThemeLabel.Height + ScaleY(4);
  ThemeCombo.Width := AppPrefsPage.SurfaceWidth;
  ThemeCombo.Items.Add(CustomMessage('ThemeDark'));
  ThemeCombo.Items.Add(CustomMessage('ThemeLight'));
  ThemeCombo.ItemIndex := 0;
end;

function LangCodeFromIndex(Idx: Integer): String;
begin
  case Idx of
    1: Result := 'de';
    2: Result := 'es';
    3: Result := 'ru';
    4: Result := 'nl';
    5: Result := 'ar';
  else
    Result := 'en';
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  PrefsDir, PrefsFile, ThemeCode, StartWithWindowsJson, JsonContent: String;
begin
  if CurStep = ssPostInstall then
  begin
    PrefsDir := ExpandConstant('{userappdata}\SSHDriveMgr');
    ForceDirectories(PrefsDir);
    PrefsFile := PrefsDir + '\install_prefs.json';

    if ThemeCombo.ItemIndex = 1 then
      ThemeCode := 'light'
    else
      ThemeCode := 'dark';

    if WizardIsTaskSelected('autostart') then
      StartWithWindowsJson := 'true'
    else
      StartWithWindowsJson := 'false';

    JsonContent := '{' + #13#10 +
      '  "language": "' + LangCodeFromIndex(LangCombo.ItemIndex) + '",' + #13#10 +
      '  "theme": "' + ThemeCode + '",' + #13#10 +
      '  "start_with_windows": ' + StartWithWindowsJson + #13#10 +
      '}';

    SaveStringToFile(PrefsFile, JsonContent, False);
  end;
end;
