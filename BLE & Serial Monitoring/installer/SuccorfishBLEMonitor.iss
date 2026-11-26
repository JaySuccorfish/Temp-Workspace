; Inno Setup 6 script for Succorfish BLE & Serial Monitor
; Builds a standard Windows Setup installer (per-user by default, no admin UAC required,
; Start Menu & optional Desktop shortcuts, clean in-place upgrades, Add/Remove Programs entry).
;
; Compile with:
;   ISCC.exe /DMyAppVersion=2.4.0 /DSourceDir="..\dist\ValtrackDebugMonitor" SuccorfishBLEMonitor.iss

#ifndef MyAppVersion
  #define MyAppVersion "2.4.0"
#endif

#ifndef SourceDir
  #define SourceDir "..\dist\ValtrackDebugMonitor"
#endif

#ifndef OutputDir
  #define OutputDir "..\dist"
#endif

#define MyAppName "Succorfish BLE & Serial Monitor"
#define MyAppPublisher "Succorfish"
#define MyAppURL "https://github.com/shrinathpol/Temp-Workspace"
#define MyAppExeName "ValtrackDebugMonitor.exe"

[Setup]
AppId={{8E5F2A91-6C4D-4B2E-9F1A-3B7D8C0E4A21}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} v{#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/releases
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={autopf}\Succorfish BLE Monitor
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir={#OutputDir}
OutputBaseFilename=SuccorfishBLEMonitor-Setup-v{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=force
RestartApplications=yes
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
