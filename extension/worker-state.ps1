# Pure keeper calculations, shared by the launcher and Windows regression tests.
function Get-FeederProcessIds($Processes, [string]$ProfileDir) {
    $escaped = [regex]::Escape($ProfileDir)
    $pattern = '(?i)(?:^|\s)--user-data-dir=(?:"' + $escaped + '"|' + $escaped + '(?=\s|$))'
    @($Processes | Where-Object {
        $_.Name -eq 'chrome.exe' -and $_.CommandLine -and $_.CommandLine -match $pattern
    } | ForEach-Object { [uint32]$_.ProcessId })
}

function Get-FeederWorkerAge($Clients, [string]$WorkerId, [double]$Uptime, [double]$Now) {
    $own = @($Clients | Where-Object { $_.worker_id -eq $WorkerId })
    $ingest = ($own | Measure-Object -Property last_ingest_at -Maximum).Maximum
    $seen = ($own | Measure-Object -Property first_seen -Maximum).Maximum
    if ($ingest -gt 0) { return [Math]::Max(0, $Now - $ingest) }
    if ($seen -gt 0) { return [Math]::Max(0, $Now - $seen) }
    return $Uptime
}

function Get-FeederOwnedProcesses($Processes, [string]$ProfileDir, [string]$KeeperPath) {
    $browserIds = @(Get-FeederProcessIds $Processes $ProfileDir)
    $escaped = [regex]::Escape($KeeperPath)
    $pattern = '(?i)(?:^|\s)-File\s+(?:"' + $escaped + '"|' + $escaped + '(?=\s|$))'
    @($Processes | Where-Object {
        $_.ProcessId -in $browserIds -or
        ($_.Name -in @('powershell.exe', 'pwsh.exe') -and $_.CommandLine -and
         $_.CommandLine -notmatch '(?i)(?:^|\s)-(?:Command|EncodedCommand|c|e)(?:\s|$)' -and
         $_.CommandLine -match $pattern)
    })
}
