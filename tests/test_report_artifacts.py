"""复盘报告访问必须经过会话认证，拒绝篡改和越界产物。"""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'dsh-gpt-supervisor/scripts')]
import dashboard_server as dashboard
from autopilot.report_artifacts import read_artifact


class ReportArtifactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = dashboard.Dashboard(Path(self.tmp.name) / 'state')
        self.ledger = self.app.autopilot.ledger
        self.product = self.ledger.create('products', {'name': '报告项目'}, 'paused')
        folder = self.app.store.state / 'autopilot/retrospectives' / self.product['id'] / 'report-id'
        folder.mkdir(parents=True)
        self.path = folder / 'report.html'
        self.path.write_text('<!doctype html><html><body>异常与卡点</body></html>')
        (folder / 'evidence.json').write_text(json.dumps({'coverage': {'complete': False}}))
        self.report = self.ledger.create('daily_reports', {'product_id': self.product['id'], 'html_report': {
            'path': str(self.path), 'sha256': hashlib.sha256(self.path.read_bytes()).hexdigest()}}, 'blocked')

    def tearDown(self):
        self.app.engine.stop()
        self.tmp.cleanup()

    def test_escaped_path_symlink_and_changed_html_rejected(self):
        raw, mime = read_artifact(self.ledger, self.report['id'], 'report.html')
        self.assertIn('异常'.encode(), raw)
        self.assertEqual(mime, 'text/html; charset=utf-8')
        with self.assertRaises(KeyError):
            read_artifact(self.ledger, self.report['id'], '../../connector.key')
        self.path.write_text('<script>changed()</script>')
        with self.assertRaises(ValueError):
            read_artifact(self.ledger, self.report['id'], 'report.html')
        outside = Path(self.tmp.name) / 'outside.html'
        outside.write_text('secret')
        self.path.unlink()
        self.path.symlink_to(outside)
        with self.assertRaises(ValueError):
            read_artifact(self.ledger, self.report['id'], 'report.html')

    def test_http_requires_session_and_returns_passive_html(self):
        server = dashboard.ThreadingHTTPServer(('127.0.0.1', 0), dashboard.handler_for(self.app, 0, ROOT/'dashboard/dist'))
        port = server.server_address[1]
        server.RequestHandlerClass = dashboard.handler_for(self.app, port, ROOT/'dashboard/dist')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f'http://127.0.0.1:{port}'
        url = origin + f'/api/daily_reports/{self.report["id"]}/artifact/report.html'
        try:
            with self.assertRaises(HTTPError) as caught:
                urlopen(url)
            self.assertEqual(caught.exception.code, 401)
            caught.exception.close()
            with urlopen(origin+'/api/bootstrap') as response:
                cookie = response.headers['Set-Cookie'].split(';')[0]
            with urlopen(Request(url, headers={'Cookie': cookie})) as response:
                self.assertIn('异常'.encode(), response.read())
                self.assertIn("default-src 'none'", response.headers['Content-Security-Policy'])
                self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
            with urlopen(Request(url.rsplit('/',1)[0]+'/evidence.json', headers={'Cookie': cookie})) as response:
                self.assertFalse(json.load(response)['coverage']['complete'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
