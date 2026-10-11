; Inno Setup 安装脚本: iscc /DAppVersion=0.1.0 installer\h2wtool_pc.iss
; 需先执行 pyinstaller h2wtool_pc.spec 生成 dist\H2wToolPC

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{8C1F7E2A-4B6D-4E52-9A0B-5D2C7F1E3A90}
AppName=H2wTool PC
AppVersion={#AppVersion}
AppPublisher=H2wTool
DefaultDirName={autopf}\H2wToolPC
DefaultGroupName=H2wTool PC
UninstallDisplayIcon={app}\H2wToolPC.exe
OutputDir=..\dist
OutputBaseFilename=H2wToolPC-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "firewall"; Description: "Allow H2wTool PC through Windows Firewall (needed for WiFi/LAN connections)"; GroupDescription: "Network:"

[Files]
Source: "..\dist\H2wToolPC\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\docs\PROTOCOL.md"; DestDir: "{app}\docs"; Flags: ignoreversion

[Icons]
Name: "{group}\H2wTool PC"; Filename: "{app}\H2wToolPC.exe"
Name: "{group}\Device Simulator (memtest x3)"; Filename: "{app}\H2wDeviceSimulator.exe"; Parameters: "--auto memtest --count 3 --fail-rate 0.3"
Name: "{group}\Device Simulator (wait for commands)"; Filename: "{app}\H2wDeviceSimulator.exe"
Name: "{group}\Protocol (PROTOCOL.md)"; Filename: "{app}\docs\PROTOCOL.md"
Name: "{group}\{cm:UninstallProgram,H2wTool PC}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\H2wTool PC"; Filename: "{app}\H2wToolPC.exe"; Tasks: desktopicon

[Run]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""H2wTool PC"""; Flags: runhidden; Tasks: firewall
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""H2wTool PC"" dir=in action=allow program=""{app}\H2wToolPC.exe"" enable=yes"; Flags: runhidden; Tasks: firewall
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""H2wTool PC"" dir=in action=allow program=""{app}\H2wToolPC-cli.exe"" enable=yes"; Flags: runhidden; Tasks: firewall
Filename: "{app}\H2wToolPC.exe"; Description: "{cm:LaunchProgram,H2wTool PC}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""H2wTool PC"""; Flags: runhidden; RunOnceId: "DelFirewallRule"
