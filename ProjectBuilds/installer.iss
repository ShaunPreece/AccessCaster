; AccessCaster - Inno Setup installer script.
;
; Builds Output\AccessCasterSetup-v<version>.exe from the PyInstaller onedir output in ProjectBuilds\dist\AccessCaster\.
; Start Menu shortcut, optional Desktop shortcut, an optional "Start AccessCaster when Windows
; starts" task (default OFF), and it launches the app when the install finishes. Uninstalling
; removes the program but LEAVES the user's data in %LOCALAPPDATA%\AccessCaster (saved profiles,
; presets, settings) untouched.
;
; IT INSTALLS EITHER FOR JUST YOU OR FOR ALL USERS, and Setup asks which at startup.
;
; NEITHER MODE IS PREFERRED FOR ACCESSIBILITY REASONS, and in particular **UAC IS NOT A PROBLEM
; WITH A SCREEN READER**. Do not introduce "avoids a UAC prompt" as an argument for anything in
; this project. The only real reason per-user remains the fallback default is that it works for a
; user with no admin rights at all; beyond that the two modes are equals and the user picks.
;
; WHICH ONE IS OFFERED FIRST IS DECIDED BY THE PREVIOUS INSTALL, not by us: Inno's
; UsePreviousPrivileges defaults to yes, so an upgrade re-uses the mode that machine was installed
; with and only a fresh machine really faces the choice. That is what stops an existing per-user
; install being orphaned beside a new all-users one.
;
; THE TWO SETTINGS HAVE TO AGREE. Offering the choice via PrivilegesRequiredOverridesAllowed while
; DefaultDirName points at {localappdata} half-wires it: choosing "all users" then registers the
; app system-wide against a folder inside ONE user's profile. {autopf} below is what keeps them
; consistent.
;
; SAFE BECAUSE THE APP NEVER WRITES BESIDE ITS EXE. paths.py splits read-only bundled assets from
; writable user data and forces the latter to %LOCALAPPDATA%\AccessCaster, so a Program Files
; install is read-only to a standard user and nothing minds. The bundled files (the help, the icon
; and the two JSON files) are only ever read, and every write goes through paths.data_path.
;
; Compile with the installed Inno Setup 6:
;   "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" installer.iss

#define AppName "AccessCaster"
#define AppExe "AccessCaster.exe"
#define AppPublisher "Shaun Preece"
; THE APP ID GUID LIVES IN ONE PLACE: HERE. Both the AppId directive below and the [Code] check that
; finds a previous install are built from this define, so they cannot drift apart - and if they did,
; the check would silently find nothing, which is the worst kind of broken.
; The braces are added at each use site, because Inno's [Setup] parser needs them doubled and the
; Pascal string does not.
#define AppIdGuid "A1C7E2F4-3B9D-4E6A-8C21-5F0D9B7E14A2"
; App version. build_installer.ps1 passes the real value from Project\version.py via
; ISCC /DAppVer=<version>. The fallback below is only used for a manual ISCC compile.
#ifndef AppVer
  #define AppVer "0.0"
#endif

[Setup]
; A stable, unique AppId so upgrades replace the previous install rather than stacking.
AppId={{{#AppIdGuid}}
AppName={#AppName}
AppVersion={#AppVer}
AppPublisher={#AppPublisher}
; THE SETUP EXE'S OWN FILE PROPERTIES. Without these the installer shows a BLANK version in
; Explorer and in the "do you want to run this" dialogs, leaving the filename as the only place the
; number appears. VersionInfoVersion must be NUMERIC: 2.63.2 becomes 2.63.2.0, and Windows
; compares those fields as numbers.
; AppVersion above stays the DISPLAY string (Programs and Features); these are the binary resource.
VersionInfoVersion={#AppVer}
VersionInfoProductVersion={#AppVer}
VersionInfoProductName={#AppName}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} Setup
; The FALLBACK default is per-user, so the app is installable with no admin rights at all; Setup
; offers "all users" at startup, and on an upgrade Inno's UsePreviousPrivileges (default yes)
; pre-selects whatever this machine used last. See the header: UAC is not a factor here.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
; {autopf} FOLLOWS THE CHOICE ABOVE, which is the whole point of using it: Program Files for an
; all-users install, {localappdata}\Programs for a per-user one. Every per-user install has gone to
; {localappdata}\Programs\AccessCaster, so an upgrade lands in the same folder instead of orphaning
; it. Keep the leaf name "AccessCaster" (no space) for that reason.
DefaultDirName={autopf}\AccessCaster
DefaultGroupName={#AppName}
; THE GROUP NAME MUST FOLLOW DefaultGroupName, NOT THE PREVIOUS INSTALL. Inno's UsePreviousGroup
; defaults to yes, so an upgrade re-uses the remembered name, so a rename writes
; correctly-named shortcuts into a folder still called by the OLD name. The user never picks a
; group here anyway (DisableProgramGroupPage below), so there is nothing to remember and no choice
; of theirs being overridden.
UsePreviousGroup=no
DisableProgramGroupPage=yes
DisableDirPage=auto
; A RUNNING COPY THAT DOES NOT CLOSE IS ENDED. Restart Manager asks the app to close and, under
; "yes", reports "unable to automatically close all applications" if it is still running, which
; leaves an update stuck on an error the user has to answer. 3.0.0's quit can stall with the update
; dialog open, so the copy being replaced is ended instead; RestartApplications still brings it
; back. By then the app has refused to start the update over desk work, so nothing is half written.
CloseApplications=force
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
OutputDir=Output
OutputBaseFilename=AccessCasterSetup-v{#AppVer}
SetupIconFile=..\Project\assets\icon.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; This version's release notes, on a page of their own once the files are in place, generated from
; Project\version.py by make_release_notes.py. AFTER rather than before: before, the page stands
; between the user and what they chose to do, and on a first install "what is new" means nothing.
; A page of Setup's own keeps the focus in Setup, where a browser opened beside the app races it
; for the front.
InfoAfterFile=release-notes.txt
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startup"; Description: "Start {#AppName} automatically when &Windows starts"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
; The entire PyInstaller onedir output (AccessCaster.exe + _internal\ with Python and assets).
; Relative to this .iss: build_installer.ps1 sends PyInstaller's --distpath to ProjectBuilds\dist.
Source: "dist\AccessCaster\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; Optional autostart, added only if the "startup" task is ticked and removed on uninstall.
;
; DELIBERATELY HKCU EVEN IN AN ALL-USERS INSTALL, and this is the one place where the scopes should
; NOT match. Only one copy of AccessCaster runs per PC (Project/instance.py), so a machine-wide Run
; entry would start it for every user who logs in and all but the first would be refused, as on a
; PC with one user at the console and another in a Remote Desktop session. Autostart is therefore
; a choice each account makes for itself.
;
; THE --startup FLAG MATTERS AND MUST MATCH Project/autostart.py. Started at sign-in the app comes up
; in the tray with the announcer and the global shortcuts live and opens NO window; started by hand
; it opens its window. The app sets this same value itself (App settings, General, "Start
; AccessCaster when you sign in to Windows"), so both write the IDENTICAL string, under the
; SAME value name so uninsdeletevalue still removes it. DevTools/test_autostart.py reads this file
; and asserts they have not drifted, because nothing else would notice.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; \
    ValueName: "AccessCaster"; ValueData: """{app}\{#AppExe}"" --startup"; \
    Flags: uninsdeletevalue; Tasks: startup

[InstallDelete]
; The release notes page earlier versions installed beside the exe.
Type: files; Name: "{app}\ReleaseNotes.html"

[Run]
; Launch the app when the installer finishes.
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName} now"; \
    Flags: nowait postinstall skipifsilent

[Code]
{ THE FIREWALL RULE AN EARLIER VERSION ADDED, which nothing needs now that the app serves no page
  to the network. A netsh rule is machine-wide and removing it needs admin, so the delete is
  elevated on its own through the 'runas' verb; in an all-users install Setup is elevated already
  and 'runas' simply runs it. A marker records that a rule was added, so only a machine that has
  one is asked.

  THE MARKER USES HKA, NOT HKCU, so it follows the install scope: HKA resolves to HKLM for an
  all-users install and HKCU for a per-user one, which is the scope the rule was recorded in. }

const
  FW_RULE_NAME = 'AccessCaster';
  FW_MARK_KEY  = 'Software\AccessCaster';
  FW_MARK_NAME = 'FirewallRuleAdded';
  { Where Inno records its own uninstall entry: the AppId with an _is1 suffix, under HKCU for a
    per-user install and HKLM for an all-users one. Built from the AppIdGuid define so it cannot
    drift from the AppId directive. }
  UNINST_SUBKEY = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{{#AppIdGuid}}_is1';


{ TWO INSTALLS OF THE SAME APP CAN COEXIST, ONE PER HIVE, AND WINDOWS SHOWS BOTH.
  Settings > Installed apps merges HKCU and HKLM, so a machine with a per-user copy AND an all-users
  copy lists "AccessCaster" TWICE, distinguishable only by version number, with two identically
  named Start Menu shortcuts to match. Neither uninstaller touches the other.

  So this is what stops a user seeing two copies of AccessCaster in the list: when installing for
  all users, offer to remove the personal copy first.

  IT CAN ONLY EVER SEE THE HIVE OF WHOEVER RUNS SETUP. If an elevated Setup is running as a
  different admin account than the one holding the per-user copy, HKCU is that admin's hive and this
  finds nothing - correctly, because it could not cleanly remove another user's install either
  (their uninstaller and their registry entries live in a profile Setup has no business rewriting).
  So a second user's stray copy stays until that user runs an installer themselves. }

function PreviousInstall(Hive: Integer; var UninstallString: String; var Version: String): Boolean;
begin
  Result := RegQueryStringValue(Hive, UNINST_SUBKEY, 'UninstallString', UninstallString)
            and (UninstallString <> '');
  if Result then
    if not RegQueryStringValue(Hive, UNINST_SUBKEY, 'DisplayVersion', Version) then
      Version := '';
end;


function DescribeVersion(Version: String): String;
begin
  if Version = '' then
    Result := 'an earlier version'
  else
    Result := 'version ' + Version;
end;


function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Uninst, Version: String;
  ResultCode: Integer;
begin
  { Never blocks the install. A tidy-up that cannot be done is a cosmetic problem; refusing to
    install over it would be a real one. }
  Result := '';

  if IsAdminInstallMode then
  begin
    if PreviousInstall(HKCU, Uninst, Version) then
      if MsgBox('You already have AccessCaster installed just for you (' +
                DescribeVersion(Version) + ').' + #13#10#13#10 +
                'Installing for all users will not replace it, so it would stay in your Installed ' +
                'apps list as a second entry with the same name.' + #13#10#13#10 +
                'Remove that personal copy now?', mbConfirmation, MB_YESNO) = IDYES then
        { /SILENT rather than /VERYSILENT so a failure still shows itself, and the result is
          ignored on purpose: see the comment on Result above. Your saved profiles, presets and
          settings are untouched either way - they live in %LOCALAPPDATA%\AccessCaster, which no
          uninstaller removes. }
        Exec(RemoveQuotes(Uninst), '/SILENT /NORESTART /SUPPRESSMSGBOXES', '',
             SW_SHOW, ewWaitUntilTerminated, ResultCode);
  end
  else
  begin
    { The mirror image, which cannot be fixed from here: a per-user install running while an
      all-users copy exists. Removing that one needs admin, and this Setup deliberately has none,
      so say so plainly rather than leaving the user to wonder why there are two. }
    if PreviousInstall(HKLM, Uninst, Version) then
      MsgBox('AccessCaster is already installed on this PC for all users (' +
             DescribeVersion(Version) + ').' + #13#10#13#10 +
             'Installing just for you will add a second copy, and both will appear in your ' +
             'Installed apps list.' + #13#10#13#10 +
             'To replace the shared copy instead, cancel and run this installer again, choosing ' +
             '"Install for all users".', mbInformation, MB_OK);
  end;
end;

function FirewallRuleAdded: Boolean;
var
  Marker: String;
begin
  Result := RegQueryStringValue(HKA, FW_MARK_KEY, FW_MARK_NAME, Marker) and (Marker = '1');
end;

{ Declining the permission prompt leaves the rule and the marker, so the next install or the
  uninstall asks again. }
procedure RemoveFirewallRule;
var
  ResultCode: Integer;
begin
  if ShellExec('runas', 'cmd.exe',
       '/C netsh advfirewall firewall delete rule name="' + FW_RULE_NAME + '" >nul 2>&1',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    RegDeleteKeyIncludingSubkeys(HKA, FW_MARK_KEY);
end;

{ NOT ON A SILENT RUN. An update from inside the app runs Setup silently, and a permission prompt
  appearing in the middle of an update nobody is watching is unexplained. An allow-rule for a
  program that no longer listens does no harm meanwhile. }
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and not WizardSilent and FirewallRuleAdded then
  begin
    MsgBox('This version of AccessCaster no longer needs the Windows Firewall rule an earlier ' +
           'version added for phone access. Windows will now ask for permission to remove it.',
           mbInformation, MB_OK);
    RemoveFirewallRule;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  { Only remove the rule, and raise a permission prompt, if one was added. }
  if (CurUninstallStep = usUninstall) and FirewallRuleAdded then
    RemoveFirewallRule;
end;
