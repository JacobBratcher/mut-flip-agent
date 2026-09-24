# MUT Flip Feeder - one-shot installer for Windows.
# Run in PowerShell (no admin needed):
#   irm https://raw.githubusercontent.com/JacobBratcher/mut-flip-agent/main/extension/install.ps1 | iex
# Re-run any time to update the extension or change the agent URL / token.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$root = Join-Path $env:LOCALAPPDATA 'MUTFlipFeeder'
$ext = Join-Path $root 'extension'
$profileDir = Join-Path $root 'profile'
$utf8 = New-Object System.Text.UTF8Encoding $false

$AgentUrl = $env:MUT_AGENT_URL
$Token = $env:MUT_FEEDER_TOKEN
# Re-running? Reuse the URL and token from the last install.
$saved = Join-Path $ext 'config.json'
if ((-not $AgentUrl -or -not $Token) -and (Test-Path $saved)) {
    try {
        $old = [IO.File]::ReadAllText($saved) | ConvertFrom-Json
        if (-not $AgentUrl) { $AgentUrl = $old.agentUrl }
        if (-not $Token) { $Token = $old.token }
        Write-Host "Using saved agent URL $AgentUrl"
    } catch { }
}
if (-not $AgentUrl) { $AgentUrl = Read-Host 'Agent URL (http://<Home Assistant IP>:8099)' }
if (-not $Token) { $Token = Read-Host 'Feeder token' }
$AgentUrl = $AgentUrl.Trim().TrimEnd('/')
if ($AgentUrl -notmatch '^https?://') { $AgentUrl = "http://$AgentUrl" }
$uri = [Uri]$AgentUrl
if (-not $uri.IsDefaultPort -or $AgentUrl -match ':\d+$') { } else { $AgentUrl = "$AgentUrl`:8099"; $uri = [Uri]$AgentUrl }
$origin = $uri.GetLeftPart([UriPartial]::Authority) + '/*'

# 1. Check the agent is reachable before doing anything else.
Write-Host "Checking agent at $AgentUrl ..."
try {
    $cfg = Invoke-RestMethod "$AgentUrl/config" -Headers @{ 'X-Feeder-Token' = $Token } -TimeoutSec 10
    Write-Host "  OK: platform $($cfg.platform), one request every $($cfg.interval_ms) ms" -ForegroundColor Green
} catch {
    Write-Host "  Can't reach the agent or the token is wrong: $($_.Exception.Message)" -ForegroundColor Yellow
    Write-Host "  Continuing; fix the URL/token and re-run the installer." -ForegroundColor Yellow
}

# 2. Chromium. Regular Chrome no longer lets a script load an unpacked extension.
$chrome = @(
    (Join-Path $env:LOCALAPPDATA 'Chromium\Application\chrome.exe'),
    (Join-Path $env:ProgramFiles 'Chromium\Application\chrome.exe')
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $chrome) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw 'winget is not available. Install Chromium from https://github.com/Hibbiki/chromium-win64/releases, then re-run this installer.'
    }
    Write-Host 'Installing Chromium (winget: Hibbiki.Chromium) ...'
    winget install -e --id Hibbiki.Chromium --silent --accept-package-agreements --accept-source-agreements | Out-Host
    $chrome = @(
        (Join-Path $env:LOCALAPPDATA 'Chromium\Application\chrome.exe'),
        (Join-Path $env:ProgramFiles 'Chromium\Application\chrome.exe')
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $chrome) { throw 'Chromium was not found after install. Install Hibbiki Chromium manually, then re-run.' }
Write-Host "  Chromium: $chrome" -ForegroundColor Green

# 3. Close a previous feeder instance (only the one using our profile).
# Killing the parent takes its renderer children with it, so by the time the loop
# reaches those they are already gone - never treat that as a failure.
try {
    $stale = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -in 'chrome.exe', 'powershell.exe' -and $_.CommandLine -like '*MUTFlipFeeder*' -and $_.ProcessId -ne $PID })
    foreach ($p in $stale) {
        try { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue } catch { }
    }
    if ($stale.Count) { Write-Host "  Closed $($stale.Count) old feeder process(es)." }
} catch { Write-Host '  (Could not check for an old feeder window; continuing.)' }
Start-Sleep -Seconds 2

# 4. Download the extension from GitHub.
Write-Host 'Downloading extension ...'
$zip = Join-Path $env:TEMP 'mut-flip-agent.zip'
$src = Join-Path $env:TEMP 'mut-flip-agent-src'
Invoke-WebRequest 'https://codeload.github.com/JacobBratcher/mut-flip-agent/zip/refs/heads/main' -OutFile $zip -UseBasicParsing
if (Test-Path $src) { Remove-Item $src -Recurse -Force -ErrorAction SilentlyContinue }
Expand-Archive $zip $src -Force
# Find the extension folder rather than assuming the zip's top-level name.
$srcExt = Get-ChildItem $src -Directory -Recurse -Filter 'extension' |
    Where-Object { Test-Path (Join-Path $_.FullName 'manifest.json') } |
    Select-Object -First 1
if (-not $srcExt) { throw 'The download did not contain the extension folder. Try again.' }
New-Item -ItemType Directory -Force $root | Out-Null
if (Test-Path $ext) { Remove-Item $ext -Recurse -Force -ErrorAction SilentlyContinue }
Copy-Item $srcExt.FullName $ext -Recurse
Remove-Item $zip, $src -Recurse -Force -ErrorAction SilentlyContinue

# 5. Pre-configure it: agent URL + token, auto-start, and permission to reach the agent.
$config = @{ agentUrl = $AgentUrl; token = $Token; autostart = $true } | ConvertTo-Json
[IO.File]::WriteAllText((Join-Path $ext 'config.json'), $config, $utf8)
$manifestPath = Join-Path $ext 'manifest.json'
$manifest = [IO.File]::ReadAllText($manifestPath) | ConvertFrom-Json
$manifest.host_permissions = @($manifest.host_permissions) + $origin
[IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 10), $utf8)

# 6. Launcher: a normal Chromium window, hidden so it's not in the taskbar.
# (Not headless: headless Chrome identifies itself as HeadlessChrome and mut.gg blocks it.)
# A small keeper script starts the feeder, hides its windows, and restarts it if it closes.
$flags = @(
    "--user-data-dir=`"$profileDir`"",
    "--load-extension=`"$ext`"",
    '--no-first-run', '--no-default-browser-check',
    '--disable-background-timer-throttling',
    '--disable-renderer-backgrounding',
    '--disable-backgrounding-occluded-windows',
    'https://www.mut.gg/'
) -join ' '

$winCs = @'
using System; using System.Collections.Generic; using System.Runtime.InteropServices; using System.Text;
public static class FeederWin {
    delegate bool EnumProc(IntPtr h, IntPtr l);
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc f, IntPtr l);
    [DllImport("user32.dll")] static extern bool IsWindow(IntPtr h);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr h, int cmd);
    [DllImport("user32.dll")] static extern int GetClassName(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll")] static extern int GetWindowTextLength(IntPtr h);
    // Hide the visible browser windows of these processes (hidden windows leave the taskbar).
    public static long[] Hide(uint[] pids) {
        List<long> hidden = new List<long>();
        EnumWindows(delegate (IntPtr h, IntPtr l) {
            uint pid;
            GetWindowThreadProcessId(h, out pid);
            if (!IsWindowVisible(h) || Array.IndexOf(pids, pid) < 0 || GetWindowTextLength(h) == 0) return true;
            StringBuilder cls = new StringBuilder(64);
            GetClassName(h, cls, 64);
            if (cls.ToString() == "Chrome_WidgetWin_1") { ShowWindow(h, 0); hidden.Add(h.ToInt64()); }
            return true;
        }, IntPtr.Zero);
        return hidden.ToArray();
    }
    public static void Show(long[] handles) {
        foreach (long v in handles) { IntPtr h = new IntPtr(v); if (IsWindow(h)) ShowWindow(h, 9); }
    }
}
'@

$keeperPs = @'
# MUT Flip Feeder keeper: runs the feeder Chromium hidden (not in the taskbar) and restarts
# it if it closes. Double-click "MUT Flip Feeder" on the desktop to show or hide it.
$ErrorActionPreference = 'SilentlyContinue'
$chrome = '__CHROME__'
$flags = '__FLAGS__'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$showFlag = Join-Path $here 'show.flag'
$hiddenList = Join-Path $here 'hidden.txt'
Add-Type -TypeDefinition ([IO.File]::ReadAllText((Join-Path $here 'win.cs')))
Remove-Item $showFlag -ErrorAction SilentlyContinue
$pids = @(); $nextScan = 0
while ($true) {
    if ((Get-Date).Ticks -ge $nextScan) {
        $pids = @(Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" |
            Where-Object { $_.CommandLine -like '*MUTFlipFeeder*' } | ForEach-Object { [uint32]$_.ProcessId })
        if (-not $pids.Count) {
            Start-Process -FilePath $chrome -ArgumentList $flags -WindowStyle Minimized
            Start-Sleep -Seconds 3
            $nextScan = 0
            continue
        }
        $nextScan = (Get-Date).AddSeconds(15).Ticks
    }
    if (-not (Test-Path $showFlag)) {
        $h = [FeederWin]::Hide([uint32[]]$pids)
        if ($h.Count) {
            $all = @(Get-Content $hiddenList -ErrorAction SilentlyContinue) + @($h | ForEach-Object { "$_" })
            $all | Select-Object -Unique | Select-Object -Last 50 | Set-Content $hiddenList
        }
    }
    Start-Sleep -Seconds 2
}
'@

$togglePs = @'
# Show the hidden MUT Flip Feeder window, or hide it again.
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$showFlag = Join-Path $here 'show.flag'
if (Test-Path $showFlag) { Remove-Item $showFlag; exit }   # the keeper hides it again within 2 s
New-Item -ItemType File $showFlag -Force | Out-Null
Add-Type -TypeDefinition ([IO.File]::ReadAllText((Join-Path $here 'win.cs')))
$handles = @(Get-Content (Join-Path $here 'hidden.txt') -ErrorAction SilentlyContinue | ForEach-Object { [int64]$_ })
[FeederWin]::Show([int64[]]$handles)
'@

function Write-HiddenRunner($vbsPath, $ps1Path) {
    # wscript starts PowerShell with no console window at all (not even a flash).
    $cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """"$ps1Path"""""
    [IO.File]::WriteAllText($vbsPath, "CreateObject(""WScript.Shell"").Run ""$cmd"", 0, False`r`n", $utf8)
}

$keeper = Join-Path $root 'feeder.ps1'
$toggle = Join-Path $root 'toggle.ps1'
[IO.File]::WriteAllText((Join-Path $root 'win.cs'), $winCs, $utf8)
$keeperText = $keeperPs.Replace('__CHROME__', $chrome.Replace("'", "''")).Replace('__FLAGS__', $flags.Replace("'", "''"))
[IO.File]::WriteAllText($keeper, $keeperText, $utf8)
[IO.File]::WriteAllText($toggle, $togglePs, $utf8)
Write-HiddenRunner (Join-Path $root 'feeder.vbs') $keeper
Write-HiddenRunner (Join-Path $root 'toggle.vbs') $toggle

$shell = New-Object -ComObject WScript.Shell
$wscript = Join-Path $env:WINDIR 'System32\wscript.exe'
$shortcuts = @(
    @{ Dir = [Environment]::GetFolderPath('Startup'); Vbs = 'feeder.vbs'; Desc = 'MUT Flip Feeder (runs hidden at sign-in)' },
    @{ Dir = [Environment]::GetFolderPath('Desktop'); Vbs = 'toggle.vbs'; Desc = 'Show or hide the MUT Flip Feeder window' }
)
foreach ($s in $shortcuts) {
    $lnk = $shell.CreateShortcut((Join-Path $s.Dir 'MUT Flip Feeder.lnk'))
    $lnk.TargetPath = $wscript
    $lnk.Arguments = "`"$(Join-Path $root $s.Vbs)`""
    $lnk.WorkingDirectory = $root
    $lnk.IconLocation = "$chrome,0"
    $lnk.Description = $s.Desc
    $lnk.Save()
}

# 7. Don't let the PC sleep while plugged in (the feeder stops if it sleeps).
try { powercfg /change standby-timeout-ac 0 | Out-Null; powercfg /change hibernate-timeout-ac 0 | Out-Null } catch { }

Start-Process -FilePath $wscript -ArgumentList "`"$(Join-Path $root 'feeder.vbs')`""
Write-Host ''
Write-Host 'Done. The MUT Flip Feeder is running hidden (not in the taskbar), restarts itself if it closes,' -ForegroundColor Green
Write-Host 'and starts at every sign-in. Double-click "MUT Flip Feeder" on the desktop to show or hide it.' -ForegroundColor Green
Write-Host 'When you leave RDP, close the RDP window (disconnect), do NOT sign out.'
