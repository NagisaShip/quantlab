[Setup]
AppId=QuantLab.LocalResearch.Desktop
AppName=QuantLab 本地研究
AppVersion=0.1.0
DefaultDirName={localappdata}\Programs\QuantLab
DefaultGroupName=QuantLab
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputBaseFilename=QuantLab-Local-Setup-x64
OutputDir={#OutputPath}
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\QuantLab.exe
DisableProgramGroupPage=yes
SetupLogging=yes
CloseApplications=no
LicenseFile={#SourcePath}\app\LICENSE

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; Flags: unchecked

[Files]
Source: "{#SourcePath}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\QuantLab"; Filename: "{app}\QuantLab.exe"
Name: "{autodesktop}\QuantLab"; Filename: "{app}\QuantLab.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\QuantLab.exe"; Description: "打开 QuantLab，选择项目文件夹"; Flags: nowait postinstall skipifsilent
