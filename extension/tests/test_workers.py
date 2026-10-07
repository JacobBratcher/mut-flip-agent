"""Run keeper isolation checks with real Windows PowerShell."""
import json
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == 'win32', 'Windows PowerShell required')
class WorkerTests(unittest.TestCase):
    def run_ps(self, source):
        preamble = ". '" + str(ROOT / 'worker-state.ps1').replace("'", "''") + "'; "
        result = subprocess.run(['powershell.exe', '-NoProfile', '-Command',
                                 "$ErrorActionPreference='Stop'; " + preamble + source],
                                capture_output=True, text=True, timeout=30, check=True)
        return json.loads(result.stdout)

    def test_process_selection_excludes_other_profiles_and_personal_browsers(self):
        value = self.run_ps(r'''
          $ps = @(
            [pscustomobject]@{Name='chrome.exe';ProcessId=1;CommandLine='chrome.exe --user-data-dir="C:\Test Folder\profile" --load-extension="C:\runtime"'},
            [pscustomobject]@{Name='chrome.exe';ProcessId=2;CommandLine='chrome.exe --user-data-dir="C:\Test Folder\profile-2"'},
            [pscustomobject]@{Name='chrome.exe';ProcessId=3;CommandLine='chrome.exe --user-data-dir="C:\Personal" --load-extension="C:\Test Folder\profile"'},
            [pscustomobject]@{Name='powershell.exe';ProcessId=4;CommandLine='--user-data-dir="C:\Test Folder\profile"'},
            [pscustomobject]@{Name='chrome.exe';ProcessId=5;CommandLine=$null}
          ); @(Get-FeederProcessIds $ps 'C:\Test Folder\profile') | ConvertTo-Json -Compress
        ''')
        self.assertEqual(value, 1)

    def test_healthy_worker_does_not_mask_stalled_sibling(self):
        value = self.run_ps('''
          $clients=@(
            [pscustomobject]@{worker_id='primary';last_ingest_at=1999;first_seen=1000},
            [pscustomobject]@{worker_id='secondary';last_ingest_at=1200;first_seen=1000}
          ); @{
            primary=(Get-FeederWorkerAge $clients 'primary' 1000 2000);
            secondary=(Get-FeederWorkerAge $clients 'secondary' 1000 2000);
            missing=(Get-FeederWorkerAge $clients 'missing' 1000 2000)
          } | ConvertTo-Json -Compress
        ''')
        self.assertEqual(value, {'primary': 1, 'secondary': 800, 'missing': 1000})

    def test_upgrade_does_not_stop_shared_relay_sibling_or_personal_shell(self):
        value = self.run_ps(r'''
          $ps=@(
            [pscustomobject]@{Name='powershell.exe';ProcessId=1;CommandLine='powershell.exe -NoProfile -File "C:\MUT\feeder.ps1"'},
            [pscustomobject]@{Name='powershell.exe';ProcessId=2;CommandLine='powershell.exe -File "C:\MUT\extension\agent-relay.ps1"'},
            [pscustomobject]@{Name='powershell.exe';ProcessId=3;CommandLine='powershell.exe -File "C:\Other\feeder.ps1"'},
            [pscustomobject]@{Name='powershell.exe';ProcessId=4;CommandLine='powershell.exe -Command "Get-Content C:\MUT\feeder.ps1"'},
            [pscustomobject]@{Name='chrome.exe';ProcessId=5;CommandLine='chrome.exe --user-data-dir=C:\MUT\profile'},
            [pscustomobject]@{Name='chrome.exe';ProcessId=6;CommandLine='chrome.exe --user-data-dir=C:\Other\profile'}
          ); @(Get-FeederOwnedProcesses $ps 'C:\MUT\profile' 'C:\MUT\feeder.ps1' | ForEach-Object {$_.ProcessId}) | ConvertTo-Json -Compress
        ''')
        self.assertEqual(value, [1, 5])

    def test_server_restart_and_unfinished_ingests_get_correct_age(self):
        value = self.run_ps('''
          $clients=@([pscustomobject]@{worker_id='primary';last_ingest_at=0;first_seen=1980});
          Get-FeederWorkerAge $clients 'primary' 4000 2000 | ConvertTo-Json -Compress
        ''')
        self.assertEqual(value, 20)


if __name__ == '__main__':
    unittest.main()
