; MAIL MONSTER PRO — Inno Setup 6 스크립트 (Task 5-1)
; 사전: PyInstaller로 dist\MAIL_MONSTER_PRO.exe 생성 후 컴파일
; 컴파일: ISCC.exe MAIL_MONSTER_PRO.iss (또는 Inno Setup Compiler에서 열기)

#define MyAppName "MAIL MONSTER PRO"
#define MyAppVersion "2.6.5"
#define MyAppPublisher "MAIL MONSTER"
#define MyAppExeName "MAIL_MONSTER_PRO.exe"

[Setup]
AppId={{8F6E2B0A-1D4C-4E5F-9A8B-3C2D1E0F5A6B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64
OutputDir=..\dist\installer
OutputBaseFilename=MAIL_MONSTER_PRO_Setup_{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\pro.ico
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
