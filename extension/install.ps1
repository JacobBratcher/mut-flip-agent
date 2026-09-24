# MUT Flip Feeder - one-shot installer for Windows.
# Run in PowerShell (no admin needed):
#   irm https://raw.githubusercontent.com/JacobBratcher/mut-flip-agent/main/extension/install.ps1 | iex
# Re-run any time to update the extension or change the agent URL / token.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$AgentUrl = $env:MUT_AGENT_URL
$Token = $env:MUT_FEEDER_TOKEN
if (-not $AgentUrl) { $AgentUrl = Read-Host 'Agent URL (http://<Home Assistant IP>:8099)' }
if (-not $Token) { $Token = Read-Host 'Feeder token' }
$AgentUrl = $AgentUrl.Trim().TrimEnd('/')
if ($AgentUrl -notmatch '^https?://') { $AgentUrl = "http://$AgentUrl" }
$uri = [Uri]$AgentUrl
if (-not $uri.IsDefaultPort -or $AgentUrl -match ':\d+$') { } else { $AgentUrl = "$AgentUrl`:8099"; $uri = [Uri]$AgentUrl }
$origin = $uri.GetLeftPart([UriPartial]::Authority) + '/*'

$root = Join-Path $env:LOCALAPPDATA 'MUTFlipFeeder'
$ext = Join-Path $root 'extension'
$profileDir = Join-Path $root 'profile'
$utf8 = New-Object System.Text.UTF8Encoding $false

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
    $stale = @(Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*MUTFlipFeeder*' })
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

# 6. Launcher: dedicated profile, starts minimized, keeps running when RDP disconnects.
# (Not headless: headless Chrome identifies itself as HeadlessChrome and mut.gg blocks it.)
$flags = @(
    '--start-minimized',
    "--user-data-dir=`"$profileDir`"",
    "--load-extension=`"$ext`"",
    '--no-first-run', '--no-default-browser-check',
    '--disable-background-timer-throttling',
    '--disable-renderer-backgrounding',
    '--disable-backgrounding-occluded-windows',
    'https://www.mut.gg/'
) -join ' '
$shell = New-Object -ComObject WScript.Shell
foreach ($dir in @([Environment]::GetFolderPath('Startup'), [Environment]::GetFolderPath('Desktop'))) {
    $lnk = $shell.CreateShortcut((Join-Path $dir 'MUT Flip Feeder.lnk'))
    $lnk.TargetPath = $chrome
    $lnk.Arguments = $flags
    $lnk.WorkingDirectory = Split-Path $chrome
    $lnk.Description = 'MUT Flip Feeder (mut.gg prices -> Home Assistant)'
    $lnk.WindowStyle = 7   # minimized
    $lnk.Save()
}

# 7. Don't let the PC sleep while plugged in (the feeder stops if it sleeps).
try { powercfg /change standby-timeout-ac 0 | Out-Null; powercfg /change hibernate-timeout-ac 0 | Out-Null } catch { }

Start-Process -FilePath $chrome -ArgumentList $flags -WindowStyle Minimized
Write-Host ''
Write-Host 'Done. The MUT Flip Feeder is running minimized in the taskbar and starts itself at every sign-in.' -ForegroundColor Green
Write-Host 'Leave it running. When you leave RDP, close the RDP window (disconnect), do NOT sign out.'
