#define Root AddBackslash(SourcePath)
#define AppVersion "1.8.42"

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
Compression=lzma2/ultra64
SolidCompression=yes
; A tray window can hide on WM_CLOSE while its process still owns the bundle.
; Keep Restart Manager's confirmation, then finish closing only this app's EXEs.
CloseApplications=force
CloseApplicationsFilter=FlickDock.exe,node.exe,syncthing.exe
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
Name: "firewall"; Description: "允许 FlickDock 联网（需要管理员确认）"; GroupDescription: "网络："

[InstallDelete]
; ELDC's model was compiled into its old private native module. Remove only
; that obsolete app-owned module and attribution when upgrading in place.
Type: files; Name: "{app}\_internal\eldc\_eldc*.pyd"
Type: dirifempty; Name: "{app}\_internal\eldc"
Type: files; Name: "{app}\_internal\licenses\ELDC-LICENSE.txt"

[Files]
Source: "{#Root}dist\FlickDock\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\FlickDock\FlickDock"; Filename: "{app}\FlickDock.exe"
Name: "{userprograms}\FlickDock\卸载 FlickDock"; Filename: "{uninstallexe}"
Name: "{userdesktop}\FlickDock"; Filename: "{app}\FlickDock.exe"; Tasks: desktopicon

[Run]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ""{app}\_internal\windows-network.ps1"" -InstallDirectory ""{app}"" -Quiet"; Verb: "runas"; Flags: shellexec waituntilterminated runhidden skipifsilent; Tasks: firewall
Filename: "{app}\FlickDock.exe"; Description: "启动 FlickDock"; Flags: nowait postinstall skipifsilent; Check: not IsOnlineUpdate
Filename: "{app}\FlickDock.exe"; Parameters: "--updated"; Flags: nowait; Check: IsOnlineUpdate

[UninstallRun]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ""{app}\_internal\windows-network.ps1"" -InstallDirectory ""{app}"" -Action Remove -Quiet"; Verb: "runas"; Flags: shellexec waituntilterminated runhidden skipifdoesntexist; RunOnceId: "FlickDockFirewallCleanup"

[Code]
function OpenProcess(Access: LongWord; Inherit: Boolean; ProcessId: LongWord): THandle;
  external 'OpenProcess@kernel32.dll stdcall';
function WaitForSingleObject(Handle: THandle; Milliseconds: LongWord): LongWord;
  external 'WaitForSingleObject@kernel32.dll stdcall';
function CloseHandle(Handle: THandle): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';
function EnumProcesses(var ProcessIds: LongWord; Bytes: LongWord; var Needed: LongWord): Boolean;
  external 'EnumProcesses@psapi.dll stdcall';
function QueryFullProcessImageName(Handle: THandle; Flags: LongWord; Path: String; var Size: LongWord): Boolean;
  external 'QueryFullProcessImageNameW@kernel32.dll stdcall';
function TerminateProcess(Handle: THandle; ExitCode: LongWord): Boolean;
  external 'TerminateProcess@kernel32.dll stdcall';

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Ids: array[0..4095] of LongWord;
  Needed, Count, I, Size: LongWord;
  Process: THandle;
  ImagePath, Root: String;
begin
  Result := '';
  Root := AddBackslash(ExpandConstant('{app}'));
  if not EnumProcesses(Ids[0], SizeOf(Ids), Needed) then Exit;
  Count := Needed div SizeOf(Ids[0]);
  if Count = 0 then Exit;
  for I := 0 to Count - 1 do
  begin
    { Query first, and terminate only the exact executable in the selected
      installation. Other FlickDock installations and generic Node processes
      must never be affected. Restart Manager remains the fallback. }
    Process := OpenProcess($00101001, False, Ids[I]);
    if Process <> 0 then
    begin
      Size := 32768; ImagePath := StringOfChar(#0, Size);
      if QueryFullProcessImageName(Process, 0, ImagePath, Size) then
      begin
        SetLength(ImagePath, Size);
        if (CompareText(ImagePath, Root + 'FlickDock.exe') = 0) or
           (CompareText(ImagePath, Root + '_internal\p2p\runtime\node.exe') = 0) or
           (CompareText(ImagePath, Root + '_internal\syncthing\syncthing.exe') = 0) then
        begin
          Log('Closing verified process in selected FlickDock installation.');
          if TerminateProcess(Process, 0) then WaitForSingleObject(Process, 2000);
        end;
      end;
      CloseHandle(Process);
    end;
  end;
end;

function IsOnlineUpdate: Boolean;
begin
  Result := ExpandConstant('{param:FDOnlineUpdate|0}') = '1';
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := IsOnlineUpdate and (PageID = wpFinished);
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
  WaitResult := WaitForSingleObject(ParentHandle, 4000);
  CloseHandle(ParentHandle);
  if WaitResult = $00000102 then
  begin
    { Older clients can finish cleanup without leaving their Qt event loop.
      Let the existing Restart Manager stage close the bundle's file owners. }
    Log('FlickDock update parent did not exit; continuing to Restart Manager.');
    Result := True;
  end
  else
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
