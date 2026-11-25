; Agent Mesh Windows Per-User Installer (Inno Setup 6)
;
; Design invariants:
; - Per-user install only (PrivilegesRequired=lowest, NO admin / NO UAC).
; - Installs to %LOCALAPPDATA%\Programs\Agent Mesh (isolated from %USERPROFILE%\.agent-mesh).
; - Start menu shortcut + optional Desktop shortcut + optional per-user PATH entry (HKCU only).
; - Checks Microsoft Edge WebView2 Runtime registry key and offers official download link if missing.
; - Uninstaller KEEPS %USERPROFILE%\.agent-mesh user data by default unless the user explicitly opts in
;   (or passes /PURGEDATA in silent mode).
; - Unsigned build (no paid code-signing certificate required).

#ifndef AppVersion
  #define AppVersion "1.0.0.dev0"
#endif
#ifndef StageDir
  #define StageDir "..\..\dist\stage"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist"
#endif
#ifndef OutputBaseFilename
  #define OutputBaseFilename "AgentMesh-" + AppVersion + "-win-x64-setup"
#endif

[Setup]
AppId={{8F9F4D4D-7B4B-4C7A-A14E-3A0A5F8F9C21}
AppName=Agent Mesh
AppVersion={#AppVersion}
AppVerName=Agent Mesh {#AppVersion}
AppPublisher=Agent Mesh
DefaultDirName={localappdata}\Programs\Agent Mesh
DefaultGroupName=Agent Mesh
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename={#OutputBaseFilename}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\Agent Mesh.exe
UninstallDisplayName=Agent Mesh
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ChangesEnvironment=yes
CloseApplications=no
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked
Name: "addtopath"; Description: "Add mesh.exe CLI to current user PATH"; GroupDescription: "Command-line integration:"

[Files]
Source: "{#StageDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Agent Mesh"; Filename: "{app}\Agent Mesh.exe"; WorkingDir: "{app}"; IconFilename: "{app}\Agent Mesh.exe"
Name: "{group}\Uninstall Agent Mesh"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Agent Mesh"; Filename: "{app}\Agent Mesh.exe"; WorkingDir: "{app}"; IconFilename: "{app}\Agent Mesh.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Agent Mesh.exe"; Description: "Launch Agent Mesh"; Flags: nowait postinstall skipifsilent

[Code]
const
  WebView2Guid = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WebView2DownloadUrl = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703';
  UserEnvKey = 'Environment';

function HasWebView2Key(RootKey: Integer; SubKey: String): Boolean;
var
  Version: String;
begin
  Result := False;
  if RegQueryStringValue(RootKey, SubKey, 'pv', Version) then
  begin
    Version := Trim(Version);
    if (Version <> '') and (Version <> '0.0.0.0') then
      Result := True;
  end;
end;

function IsWebView2Installed(): Boolean;
begin
  Result :=
    HasWebView2Key(HKLM64, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\' + WebView2Guid) or
    HasWebView2Key(HKLM64, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\' + WebView2Guid) or
    HasWebView2Key(HKLM32, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\' + WebView2Guid) or
    HasWebView2Key(HKCU64, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\' + WebView2Guid) or
    HasWebView2Key(HKCU32, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\' + WebView2Guid);
end;

procedure AddDirToUserPath(const DirPath: String);
var
  OldPath, NewPath, NormDir, CheckPath: String;
begin
  if not RegQueryStringValue(HKCU, UserEnvKey, 'Path', OldPath) then
    OldPath := '';
  NormDir := RemoveBackslashUnlessRoot(DirPath);
  CheckPath := ';' + Uppercase(OldPath) + ';';
  if (Pos(';' + Uppercase(NormDir) + ';', CheckPath) > 0) or
     (Pos(';' + Uppercase(NormDir) + '\;', CheckPath) > 0) then
    Exit;
  if (OldPath <> '') and (OldPath[Length(OldPath)] <> ';') then
    NewPath := OldPath + ';' + NormDir
  else
    NewPath := OldPath + NormDir;
  RegWriteExpandStringValue(HKCU, UserEnvKey, 'Path', NewPath);
end;

procedure RemoveDirFromUserPath(const DirPath: String);
var
  OldPath, NormDir, UpperNorm, Segment, OutPath: String;
  P: Integer;
begin
  if not RegQueryStringValue(HKCU, UserEnvKey, 'Path', OldPath) then
    Exit;
  NormDir := RemoveBackslashUnlessRoot(DirPath);
  UpperNorm := Uppercase(NormDir);
  OutPath := '';
  while OldPath <> '' do
  begin
    P := Pos(';', OldPath);
    if P > 0 then
    begin
      Segment := Copy(OldPath, 1, P - 1);
      Delete(OldPath, 1, P);
    end
    else
    begin
      Segment := OldPath;
      OldPath := '';
    end;
    if (Trim(Segment) <> '') and (Uppercase(RemoveBackslashUnlessRoot(Trim(Segment))) <> UpperNorm) then
    begin
      if OutPath <> '' then
        OutPath := OutPath + ';';
      OutPath := OutPath + Segment;
    end;
  end;
  RegWriteExpandStringValue(HKCU, UserEnvKey, 'Path', OutPath);
end;

function HasUninstallSwitch(const SwitchName: String): Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
  begin
    if CompareText(ParamStr(I), SwitchName) = 0 then
    begin
      Result := True;
      Exit;
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ErrCode: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    if WizardIsTaskSelected('addtopath') then
      AddDirToUserPath(ExpandConstant('{app}'));

    if (not IsWebView2Installed()) and (not WizardSilent()) then
    begin
      if MsgBox(
        'Microsoft Edge WebView2 Runtime was not detected on this machine.' + #13#10 + #13#10 +
        'Agent Mesh Desktop requires WebView2 Runtime to display its UI. ' +
        'Would you like to open the official Microsoft download page now?',
        mbConfirmation, MB_YESNO) = IDYES then
      begin
        ShellExec('open', WebView2DownloadUrl, '', '', SW_SHOWNORMAL, ewNoWait, ErrCode);
      end;
    end;
  end;
end;

function EncodeUtf16Hex(const S: String): String;
var
  I: Integer;
begin
  Result := '';
  for I := 1 to Length(S) do
    Result := Result + Format('%.4x', [Ord(S[I])]);
end;

procedure StopAppProcesses(const AppDir: String);
var
  HexDir, PsCmd: String;
  ResultCode: Integer;
begin
  HexDir := EncodeUtf16Hex(RemoveBackslashUnlessRoot(AppDir));
  PsCmd :=
    '-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "' +
    '$h = ''' + HexDir + '''; ' +
    '$d = -join $(for ($i = 0; $i -lt $h.Length; $i += 4) { [char][Convert]::ToInt32($h.Substring($i, 4), 16) }); ' +
    '$targets = @((Join-Path $d ''python\pythonw.exe''), (Join-Path $d ''python\python.exe''), (Join-Path $d ''Agent Mesh.exe''), (Join-Path $d ''mesh.exe'')); ' +
    'Get-Process -ErrorAction SilentlyContinue | ' +
    'Where-Object { try { $p = $_.Path; $p -and ($targets -contains $p) } catch { $false } } | ' +
    'Stop-Process -Force -PassThru -ErrorAction SilentlyContinue | ' +
    'Wait-Process -Timeout 5 -ErrorAction SilentlyContinue"';
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), PsCmd, '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure DeletePycacheTree(const RootDir: String);
var
  FindRec: TFindRec;
  CleanRoot, ChildPath: String;
begin
  CleanRoot := RemoveBackslashUnlessRoot(RootDir);
  if not DirExists(CleanRoot) then
    Exit;
  // Never traverse RootDir itself if it is a junction/symlink (FILE_ATTRIBUTE_REPARSE_POINT = $400)
  if FindFirst(CleanRoot, FindRec) then
  begin
    FindClose(FindRec);
    if (FindRec.Attributes and $400) <> 0 then
      Exit;
  end;
  if FindFirst(AddBackslash(CleanRoot) + '*', FindRec) then
  begin
    try
      repeat
        if (FindRec.Name = '.') or (FindRec.Name = '..') then
          Continue;
        // Never traverse junctions/symlinks (FILE_ATTRIBUTE_REPARSE_POINT = $400)
        if (FindRec.Attributes and $400) <> 0 then
          Continue;
        ChildPath := AddBackslash(CleanRoot) + FindRec.Name;
        if (FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then
        begin
          if CompareText(FindRec.Name, '__pycache__') = 0 then
            DelTree(ChildPath, True, True, True)
          else
            DeletePycacheTree(ChildPath);
          RemoveDir(ChildPath);
        end;
      until not FindNext(FindRec);
    finally
      FindClose(FindRec);
    end;
  end;
  RemoveDir(CleanRoot);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  AppDir, DataDir: String;
  ShouldPurge: Boolean;
begin
  if CurUninstallStep = usUninstall then
  begin
    StopAppProcesses(ExpandConstant('{app}'));
  end
  else if CurUninstallStep = usPostUninstall then
  begin
    AppDir := ExpandConstant('{app}');
    RemoveDirFromUserPath(AppDir);

    // Clean runtime-generated __pycache__ bytecode inside {app}\python and remove {app} if now empty.
    DeletePycacheTree(AddBackslash(AppDir) + 'python');
    RemoveDir(AppDir);

    // Keep %USERPROFILE%\.agent-mesh user data by default unless the user explicitly opts in.
    DataDir := ExpandConstant('{userprofile}\.agent-mesh');
    if DirExists(DataDir) then
    begin
      ShouldPurge := False;
      if UninstallSilent() then
      begin
        ShouldPurge := HasUninstallSwitch('/PURGEDATA');
      end
      else
      begin
        ShouldPurge := (MsgBox(
          'Keep your Agent Mesh data?' + #13#10 + #13#10 +
          'Your projects, chat history, and configuration are stored in:' + #13#10 +
          DataDir + #13#10 + #13#10 +
          'Do you want to PERMANENTLY DELETE this data folder too?' + #13#10 +
          '(Choose "No" to keep your data for future installs.)',
          mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES);
      end;
      if ShouldPurge then
        DelTree(DataDir, True, True, True);
    end;
  end;
end;
