; ============================================================================
; SGE — Sistema de Gestão de Estoque
; ============================================================================

#define MyAppName        "SGE"
#define MyAppFullName    "SGE — SERIES"
#define MyAppVersion     "1.0.5"
#define MyAppPublisher   "Folestech, lda"
#define MyAppURL         "http://localhost"
#define MyAppExeName     "SGE.exe"
#define MyInstallerName  "SGE_Setup_v" + MyAppVersion

[Setup]
AppId={{A1B2C3D4-E5F6-4789-ABCD-1234567890AB}
AppName={#MyAppFullName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppFullName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}

DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppFullName}
DisableProgramGroupPage=yes
AllowNoIcons=yes

OutputDir=instalador
OutputBaseFilename={#MyInstallerName}
Compression=lzma2/max
SolidCompression=yes
LZMANumBlockThreads=4

PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=6.1sp1
WizardStyle=modern

CloseApplications=yes
RestartApplications=no

#if FileExists("static\img\favicon.ico")
SetupIconFile=static\img\favicon.ico
#elif FileExists("static\img\favicon.ico")
SetupIconFile=static\img\favicon.ico
#endif

UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppFullName}
ShowLanguageDialog=auto

[Languages]
Name: "portuguese"; MessagesFile: "compiler:Languages\Portuguese.isl"
Name: "english";    MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Dirs]
Name: "{app}";             Permissions: users-modify
Name: "{app}\dados";       Permissions: users-modify
Name: "{app}\backups";     Permissions: users-modify
Name: "{app}\_codigo";     Permissions: users-modify
Name: "{app}\_codigo\templates"; Permissions: users-modify
Name: "{app}\_codigo\static";    Permissions: users-modify
Name: "{app}\templates";   Permissions: users-modify
Name: "{app}\static";      Permissions: users-modify

[Files]
; 1. Ficheiros do PyInstaller
Source: "dist\SGE\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs; \
    Excludes: "dados\*,backups\*,_codigo\*"

; 2. Código-fonte para o launcher (pasta _codigo)
Source: "app.py";           DestDir: "{app}\_codigo"; Flags: ignoreversion
Source: "services.py";      DestDir: "{app}\_codigo"; Flags: ignoreversion
Source: "database.py";      DestDir: "{app}\_codigo"; Flags: ignoreversion
Source: "reports.py";       DestDir: "{app}\_codigo"; Flags: ignoreversion
Source: "diagnostico.py";   DestDir: "{app}\_codigo"; Flags: ignoreversion
Source: "atualizador.py";   DestDir: "{app}\_codigo"; Flags: ignoreversion
Source: "comparador_estoque.py";   DestDir: "{app}\_codigo"; Flags: ignoreversion
Source: "requirements.txt"; DestDir: "{app}\_codigo"; Flags: ignoreversion
Source: "templates\*"; DestDir: "{app}\_codigo\templates"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "static\*";    DestDir: "{app}\_codigo\static";    Flags: ignoreversion recursesubdirs createallsubdirs

; 3. Cópia na raiz (para o launcher procurar diretamente)
Source: "app.py";           DestDir: "{app}"; Flags: ignoreversion
Source: "services.py";      DestDir: "{app}"; Flags: ignoreversion
Source: "database.py";      DestDir: "{app}"; Flags: ignoreversion
Source: "reports.py";       DestDir: "{app}"; Flags: ignoreversion
Source: "diagnostico.py";   DestDir: "{app}"; Flags: ignoreversion
Source: "atualizador.py";   DestDir: "{app}"; Flags: ignoreversion
Source: "comparador_estoque.py";   DestDir: "{app}"; Flags: ignoreversion
Source: "requirements.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "templates\*"; DestDir: "{app}\templates"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "static\*";    DestDir: "{app}\static";    Flags: ignoreversion recursesubdirs createallsubdirs

#if FileExists("README.md")
Source: "README.md";   DestDir: "{app}"; Flags: ignoreversion isreadme
#endif
#if FileExists("LEIA-ME.txt")
Source: "LEIA-ME.txt"; DestDir: "{app}"; Flags: ignoreversion
#endif

[Icons]
Name: "{group}\{#MyAppFullName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppFullName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppFullName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppFullName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[Registry]
Root: HKLM; Subkey: "Software\{#MyAppPublisher}\{#MyAppName}"; ValueType: string; ValueName: "InstallPath"; ValueData: "{app}"; Flags: uninsdeletekey
Root: HKLM; Subkey: "Software\{#MyAppPublisher}\{#MyAppName}"; ValueType: string; ValueName: "Version";     ValueData: "{#MyAppVersion}"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\{#MyAppPublisher}\{#MyAppName}"; Flags: dontcreatekey uninsdeletekey

[UninstallDelete]
Type: files;          Name: "{app}\app_errors.log"
Type: files;          Name: "{app}\diagnostico.log"
Type: files;          Name: "{app}\reset_log.txt"
Type: files;          Name: "{app}\.secret_key"
Type: filesandordirs; Name: "{app}\dados"
Type: filesandordirs; Name: "{app}\backups"
Type: filesandordirs; Name: "{app}\_codigo"
Type: files;          Name: "{app}\static\img\logo_empresa.png"
Type: files;          Name: "{app}\static\img\logo_empresa.jpg"
Type: files;          Name: "{app}\static\img\logo_empresa.jpeg"
Type: files;          Name: "{app}\static\img\logo_empresa.gif"
Type: files;          Name: "{app}\static\img\logo_empresa.bmp"
Type: files;          Name: "{app}\static\img\logo_empresa.webp"
Type: filesandordirs; Name: "{app}"

[Code]

// Auxiliar: mata SGE.exe se estiver a correr
procedure MatarProcessoSGE();
var
  ResultCode, Tentativas: Integer;
begin
  for Tentativas := 1 to 3 do
  begin
    Exec('taskkill.exe', '/F /IM {#MyAppExeName} /T',
         '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    if (ResultCode = 128) or (ResultCode = 1) then
      Exit;
    Sleep(1200);
  end;
end;

// Auxiliar: oferece backup antes de desinstalar
procedure OferecerBackupAntesDeDesinstalar();
var
  PastaDados, PastaBackup, NomeBackup: String;
  ResultCode: Integer;
begin
  PastaDados := ExpandConstant('{app}\dados');
  if not FileExists(PastaDados + '\estoque.db') then
    Exit;

  if MsgBox(
       'Deseja guardar uma cópia dos dados (estoque.db) antes de remover o programa?'
       + #13#10 + #13#10 +
       'Escolha "Não" para remover TUDO definitivamente.',
       mbConfirmation, MB_YESNO) <> IDYES then
    Exit;

  PastaBackup := '';
  if not BrowseForFolder(
       'Escolha a pasta onde guardar o backup',
       PastaBackup,
       False) then
  begin
    MsgBox('Backup cancelado.', mbInformation, MB_OK);
    Exit;
  end;

  NomeBackup := PastaBackup + '\SGE_backup_' +
    GetDateTimeString('yyyymmdd_hhnnss', '_', ':') + '.db';

  Exec('cmd.exe',
       '/C copy /Y "' + PastaDados + '\estoque.db" "' + NomeBackup + '"',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  if ResultCode = 0 then
    MsgBox('Backup guardado em:' + #13#10 + NomeBackup, mbInformation, MB_OK)
  else
    MsgBox('Falha ao guardar o backup.', mbError, MB_OK);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    if not DirExists(ExpandConstant('{app}\dados')) then
      CreateDir(ExpandConstant('{app}\dados'));
    if not DirExists(ExpandConstant('{app}\backups')) then
      CreateDir(ExpandConstant('{app}\backups'));

    Exec('attrib.exe',
         '+H "' + ExpandConstant('{app}\_codigo') + '" /S /D',
         '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;
end;

function InitializeUninstall(): Boolean;
begin
  Result := True;
  MatarProcessoSGE();
  OferecerBackupAntesDeDesinstalar();

  if MsgBox(
       'Vai remover o SGE e TODOS os dados associados:' + #13#10 +
       '  - Base de dados (estoque.db)' + #13#10 +
       '  - Backups automáticos' + #13#10 +
       '  - Logos e logs' + #13#10 +
       '  - Configurações' + #13#10 + #13#10 +
       'Esta acção NÃO PODE SER DESFEITA. Continuar?',
       mbConfirmation, MB_YESNO) <> IDYES then
    Result := False;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    MatarProcessoSGE();
    Sleep(800);
  end;
end;