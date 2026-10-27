#define Root AddBackslash(SourcePath)
#define AppVersion "1.8.10"

[Setup]
AppId={{C51D7C62-61AE-45D7-A96D-6DC63B8495E7}
AppName=FlickDock
AppVersion={#AppVersion}
AppPublisher=FlickDock
DefaultDirName={localappdata}\Programs\FlickDock
DefaultGroupName=FlickDock
DisableProgramGroupPage=yes
DisableDirPage=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
OutputDir={#Root}..\outputs
OutputBaseFilename=FlickDock-Setup-{#AppVersion}
SetupIconFile={#Root}flickdock.ico
UninstallDisplayIcon={app}\FlickDock.exe
UninstallDisplayName=FlickDock {#AppVersion}
Uninstallable=yes
CreateUninstallRegKey=yes
UsePreviousTasks=yes
WizardStyle=modern
Compression=lzma2
SolidCompression=no
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
VersionInfoVersion={#AppVersion}
VersionInfoDescription=FlickDock Windows Installer
VersionInfoProductName=FlickDock
VersionInfoProductVersion={#AppVersion}

[Languages]
Name: "chinesesimp"; MessagesFile: "{#Root}ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："

[Files]
Source: "{#Root}dist\FlickDock\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\FlickDock\FlickDock"; Filename: "{app}\FlickDock.exe"
Name: "{userprograms}\FlickDock\卸载 FlickDock"; Filename: "{uninstallexe}"
Name: "{userdesktop}\FlickDock"; Filename: "{app}\FlickDock.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\FlickDock.exe"; Description: "启动 FlickDock"; Flags: nowait postinstall skipifsilent; Check: not IsOnlineUpdate
Filename: "{app}\FlickDock.exe"; Flags: nowait; Check: IsOnlineUpdate

[Code]
function OpenProcess(Access: LongWord; Inherit: Boolean; ProcessId: LongWord): THandle;
  external 'OpenProcess@kernel32.dll stdcall';
function WaitForSingleObject(Handle: THandle; Milliseconds: LongWord): LongWord;
  external 'WaitForSingleObject@kernel32.dll stdcall';
function CloseHandle(Handle: THandle): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';

function IsOnlineUpdate: Boolean;
begin
  Result := ExpandConstant('{param:FDOnlineUpdate|0}') = '1';
end;

function InitializeSetup: Boolean;
var
  ParentId: Integer;
  ParentHandle: THandle;
  WaitResult: LongWord;
begin
  Result := True;
  if not IsOnlineUpdate then Exit;
  ParentId := StrToIntDef(ExpandConstant('{param:FDParentPid|0}'), 0);
  if ParentId <= 0 then
  begin
    Result := False;
    Exit;
  end;
  { The app first releases media, background jobs and its single-instance lock.
    Wait for that process before replacing files in its existing install folder. }
  ParentHandle := OpenProcess($00100000, False, ParentId);
  if ParentHandle = 0 then Exit;
  WaitResult := WaitForSingleObject(ParentHandle, 30000);
  CloseHandle(ParentHandle);
  Result := WaitResult = 0;
  if not Result then
    MsgBox('FlickDock 尚未退出，请关闭后重新更新。', mbError, MB_OK);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  RunCommand, InstalledExe: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    InstalledExe := ExpandConstant('{app}\FlickDock.exe');
    if RegQueryStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run',
      'FlickDock', RunCommand) then
    begin
      if (CompareText(RunCommand, '"' + InstalledExe + '"') = 0) or
         (CompareText(RunCommand, InstalledExe) = 0) then
        RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'FlickDock');
    end;
  end;
end;
