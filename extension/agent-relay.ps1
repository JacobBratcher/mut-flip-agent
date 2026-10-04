# Optional local-agent relay for VPN clients that block LAN access in selected apps.
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$cfg = Get-Content (Join-Path $here 'config.json') -Raw | ConvertFrom-Json
if (-not $cfg.relayUpstreamUrl) { exit }
$upstream = [Uri]$cfg.relayUpstreamUrl
$local = [Uri]$cfg.agentUrl
if ($upstream.Scheme -ne 'http' -or $upstream.UserInfo -or $upstream.AbsolutePath -ne '/' -or
    $local.Scheme -ne 'http' -or $local.Host -ne '127.0.0.1' -or $local.Port -ne 18099 -or
    ($upstream.IsLoopback -and $upstream.Port -eq $local.Port)) {
    throw 'Invalid local-agent relay configuration'
}
# A named mutex prevents duplicate listeners across keeper restarts.
$mutex = New-Object Threading.Mutex($false, 'Local\MUTFlipFeederAgentRelay')
try {
    if (-not $mutex.WaitOne(0)) { exit }
    Add-Type -Path (Join-Path $here 'agent-relay.cs')
    [MutAgentRelay]::Run($upstream.DnsSafeHost, $upstream.Port, $local.Port)
} finally { $mutex.Dispose() }
