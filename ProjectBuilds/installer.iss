; AccessCaster - Inno Setup installer script.
;
; Builds AccessCasterSetup.exe from the PyInstaller onedir output in ProjectBuilds\dist\AccessCaster\.
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
; install is read-only to a standard user and nothing minds. Checked, not assumed: the only read of
; a bundled path is fx_recipes.json, and every write goes through paths.data_path.
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
; number appears. VersionInfoVersion must be NUMERIC, which "2.50" is: it
; becomes 2.50.0.0, and Windows compares those fields numerically, so it correctly outranks 2.10.
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
; all-users install, {localappdata}\Programs for a per-user one. The per-user path is therefore
; BYTE-IDENTICAL to the old hardcoded value, so an upgrade over an existing install lands in the
; same folder instead of orphaning it. Keep the leaf name "AccessCaster" (no space) for that reason.
DefaultDirName={autopf}\AccessCaster
DefaultGroupName={#AppName}
; THE GROUP NAME MUST FOLLOW DefaultGroupName, NOT THE PREVIOUS INSTALL. Inno's UsePreviousGroup
; defaults to yes, so an upgrade re-uses the remembered name: measured, a rename then writes
; correctly-named shortcuts into a folder still called by the OLD name. The user never picks a
; group here anyway (DisableProgramGroupPage below), so there is nothing to remember and no choice
; of theirs being overridden.
UsePreviousGroup=no
DisableProgramGroupPage=yes
DisableDirPage=auto
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
OutputDir=Output
OutputBaseFilename=AccessCasterSetup-v{#AppVer}
SetupIconFile=..\Project\assets\icon.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startup"; Description: "Start {#AppName} automatically when &Windows starts"; GroupDescription: "Startup:"; Flags: unchecked
; Ticked by default. Adds an inbound Windows Firewall allow-rule so a phone or other device on the
; same network can open the page. Creating the rule needs admin, so it asks for approval once
; (Private networks only). Untick it to skip and just answer the Defender popup on first run instead.
Name: "firewallrule"; Description: "Allow {#AppName} through Windows &Firewall (for phone / LAN access; asks for approval once)"; GroupDescription: "Network:"

[InstallDelete]
; THE SHORTCUTS THE OLD NAME LEFT BEHIND. Earlier builds were named "Access Caster" with a space,
; and Inno does not remove a previous install's Start Menu group or desktop
; shortcut when the name changes - it simply writes the new ones beside them. Without this an
; upgraded machine lists the app TWICE in the Start Menu, both entries launching the same exe.
;
; A NO-OP ON A MACHINE THAT NEVER HAD THE OLD NAME: Inno skips a path that is not there, silently
; and with no cost, which is why this stays in the one installer everybody gets rather than being
; split into a special build or a separate migration tool. Two installers carrying the same version
; number and behaving differently would be far worse than five lines nobody's machine reads.
; The same reasoning covers the legacy Run value and firewall rule below.
Type: files; Name: "{autoprograms}\Access Caster\*"
Type: dirifempty; Name: "{autoprograms}\Access Caster"
Type: files; Name: "{autodesktop}\Access Caster.lnk"

[Files]
; The entire PyInstaller onedir output (AccessCaster.exe + _internal\ with Python and assets).
; Relative to this .iss, so it is a sibling now that build_installer.ps1 sends --distpath here.
Source: "dist\AccessCaster\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; Optional autostart, added only if the "startup" task is ticked and removed on uninstall.
;
; DELIBERATELY HKCU EVEN IN AN ALL-USERS INSTALL, and this is the one place where the scopes should
; NOT match. AccessCaster is a server on a FIXED port (8770), so a machine-wide Run entry would
; launch an instance for every user who logs in and they would fight over that port - which is not
; hypothetical on scottpc, where a helper sits at the console while Shaun works in an RDP session
; at the same time. Autostart is therefore a choice each account makes for itself.
;
; THE --startup FLAG MATTERS AND MUST MATCH Project/autostart.py. Started at sign-in the app comes up
; in the tray with the announcer and the global shortcuts live and opens NO page; started by hand it
; opens the page as always. The app can now set this same value itself (a switch under "This app's
; own settings", for anyone who missed this checkbox), so both write the IDENTICAL string, under the
; SAME value name so uninsdeletevalue still removes it. DevTools/test_autostart.py reads this file
; and asserts they have not drifted, because nothing else would notice.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; \
    ValueName: "AccessCaster"; ValueData: """{app}\{#AppExe}"" --startup"; \
    Flags: uninsdeletevalue; Tasks: startup
; THE PRE-RENAME ENTRY, removed on EVERY install and deliberately not gated on the task. The app
; was once called "Access Caster" with a space, and the Run value carries that name on any machine
; installed then. Both entries name the same exe, so a machine upgrading with autostart already
; ticked would otherwise launch the app twice at sign-in. No Tasks: clause, because it has to go
; whether or not
; the user wants autostart this time. Project/autostart.py deletes the same name for the same
; reason; see LEGACY_VALUE_NAME there.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; \
    ValueName: "Access Caster"; Flags: deletevalue

[Run]
; Launch the app when the installer finishes (the "open the server/page when finished" step).
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName} now"; \
    Flags: nowait postinstall skipifsilent
; And offer the release notes, TICKED BY DEFAULT. They are generated
; from Project\version.py at build time (see make_release_notes.py) and ride in the bundle, so this
; opens a LOCAL file in the default browser: no network, and it works from Program Files. HTML
; rather than a text file so a screen reader gets real headings to jump between, which a Notepad
; window cannot give. `shellexec` because the target is a document rather than a program; without it
; Inno tries to execute the .html itself and fails.
Filename: "{app}\ReleaseNotes.html"; Description: "See the release notes for this version"; \
    Flags: shellexec nowait postinstall skipifsilent

[Code]
{ Firewall rule. A per-user install runs without admin, but creating a firewall rule needs it, so
  we elevate ONLY the netsh step via the 'runas' verb. In an all-users install Setup is elevated
  already and 'runas' simply runs it, so this one path covers both modes. The rule allows
  inbound connections to AccessCaster.exe on Private networks, so a phone or other device on the
  same network can open the page without the Windows Defender popup appearing on first run.
  A marker records that we added it, so uninstall only prompts to remove it if it exists.

  THE MARKER USES HKA, NOT HKCU, so it follows the install scope: a netsh rule is always
  machine-wide, so if an all-users install were marked under one admin's HKCU, an uninstall by a
  different admin would leave the rule behind for ever. HKA resolves to HKLM for an all-users
  install and HKCU for a per-user one, which is exactly the scope the rule really has. }

const
  FW_RULE_NAME = 'AccessCaster';
  { The rule's name from before the app became one word. A netsh rule is keyed by NAME, so without
    deleting this one too an upgraded machine keeps an orphaned allow-rule for ever: the
    uninstaller only ever looks for the new name. }
  FW_RULE_NAME_LEGACY = 'Access Caster';
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
  So a second user's stray copy stays until that user runs an installer themselves. Measured on
  scottpc, where a copy in another profile was found via its firewall rule and could not be touched. }

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

procedure AddFirewallRule;
var
  AppExe, Params: String;
  ResultCode: Integer;
begin
  AppExe := ExpandConstant('{app}\{#AppExe}');
  { Delete any same-named rule first so a reinstall does not stack duplicates, and the pre-rename
    one with it, then add. Both deletes are in the SAME elevated command as the add, so the user
    answers one UAC prompt rather than two. }
  Params := '/C netsh advfirewall firewall delete rule name="' + FW_RULE_NAME + '" >nul 2>&1 & ' +
            'netsh advfirewall firewall delete rule name="' + FW_RULE_NAME_LEGACY + '" >nul 2>&1 & ' +
            'netsh advfirewall firewall add rule name="' + FW_RULE_NAME + '" ' +
            'dir=in action=allow program="' + AppExe + '" enable=yes profile=private';
  if ShellExec('runas', 'cmd.exe', Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    RegWriteStringValue(HKA, FW_MARK_KEY, FW_MARK_NAME, '1');
  { If the user declines the UAC prompt, ShellExec returns False: the install still succeeds and
    the Defender popup will simply appear on first run instead. }
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('firewallrule') then
    AddFirewallRule;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Marker: String;
  ResultCode: Integer;
begin
  { Only remove the rule (and trigger a UAC prompt) if we actually added one. }
  if (CurUninstallStep = usUninstall) and
     RegQueryStringValue(HKA, FW_MARK_KEY, FW_MARK_NAME, Marker) and (Marker = '1') then
  begin
    ShellExec('runas', 'cmd.exe',
      '/C netsh advfirewall firewall delete rule name="' + FW_RULE_NAME + '" >nul 2>&1 & ' +
      'netsh advfirewall firewall delete rule name="' + FW_RULE_NAME_LEGACY + '" >nul 2>&1',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    RegDeleteKeyIncludingSubkeys(HKA, FW_MARK_KEY);
  end;
end;
