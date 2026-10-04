"""Windows integration tests: python -m unittest discover -s extension/tests -p 'test_*.py'."""
import concurrent.futures
import http.client
import http.server
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest


class Echo(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def do_GET(self):
        body = self.headers.get('X-Test-Token', '').encode() + self.path.encode()
        self.reply(body)

    def do_POST(self):
        self.reply(self.rfile.read(int(self.headers['Content-Length'])))

    def reply(self, body):
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@unittest.skipUnless(sys.platform == 'win32', 'Requires Windows PowerShell and .NET Framework')
class RelayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix='mut-relay-test-')
        root = Path(cls.directory.name)
        source = Path(__file__).resolve().parents[1]
        for name in ('agent-relay.ps1', 'agent-relay.cs'):
            shutil.copy(source / name, root / name)
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Echo)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        (root / 'config.json').write_text(json.dumps({
            'agentUrl': 'http://127.0.0.1:18099',
            'relayUpstreamUrl': f'http://127.0.0.1:{cls.server.server_port}',
        }))
        cls.process = subprocess.Popen([
            'powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-File', str(root / 'agent-relay.ps1'),
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if cls.process.poll() is not None:
                raise RuntimeError('Relay exited during startup; port 18099 must be free')
            try:
                with socket.create_connection(('127.0.0.1', 18099), timeout=.1):
                    break
            except OSError:
                time.sleep(.1)
        else:
            cls.process.terminate()
            raise RuntimeError('Relay did not listen')

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        cls.process.wait(timeout=10)
        cls.server.shutdown()
        cls.server.server_close()
        cls.directory.cleanup()

    def test_headers_paths_and_keepalive(self):
        conn = http.client.HTTPConnection('127.0.0.1', 18099, timeout=5)
        try:
            for path in ('/config', '/queue?n=1'):
                conn.request('GET', path, headers={'X-Test-Token': 'test-only'})
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), ('test-only' + path).encode())
        finally:
            conn.close()

    def test_large_snapshot_body(self):
        body = bytes(range(256)) * 4096
        conn = http.client.HTTPConnection('127.0.0.1', 18099, timeout=10)
        try:
            conn.request('POST', '/ingest', body)
            self.assertEqual(conn.getresponse().read(), body)
        finally:
            conn.close()

    def test_concurrent_requests(self):
        def request(i):
            conn = http.client.HTTPConnection('127.0.0.1', 18099, timeout=10)
            try:
                conn.request('GET', f'/card/{i}')
                return conn.getresponse().read()
            finally:
                conn.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            self.assertEqual(list(pool.map(request, range(32))),
                             [f'/card/{i}'.encode() for i in range(32)])

    def test_only_loopback_listens(self):
        output = subprocess.check_output([
            'powershell.exe', '-NoProfile', '-Command',
            "Get-NetTCPConnection -State Listen -LocalPort 18099 | Select-Object -ExpandProperty LocalAddress",
        ], text=True)
        self.assertEqual(output.strip(), '127.0.0.1')


if __name__ == '__main__':
    unittest.main()
