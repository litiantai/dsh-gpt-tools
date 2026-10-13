"""真实浏览器输入、跳转、滚动及访问边界验证，不调用模型。"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'dsh-gpt-supervisor/scripts')]
from autopilot.computer_use import BrowserDriver


class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name); self.events=[]
        events=self.events
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                if self.path in ('/bootstrap','/protected','/reject','/redirect'):
                    status = 401 if self.path=='/reject' or (self.path=='/protected' and 'local_session=fixture' not in self.headers.get('Cookie','')) else 302 if self.path=='/redirect' else 200
                    self.send_response(status)
                    if self.path=='/bootstrap': self.send_header('Set-Cookie','local_session=fixture; HttpOnly; SameSite=Strict; Path=/')
                    if self.path=='/redirect': self.send_header('Location','/bootstrap')
                    self.end_headers();self.wfile.write(b'{}');return
                if self.path.startswith('/event'):
                    events.append(parse_qs(urlsplit(self.path).query)); body=b'ok'
                else:
                    body=b'''<html><style>body{height:2400px}input{position:absolute;left:100px;top:100px;width:300px;height:50px}button{position:absolute;left:100px;top:200px;width:300px;height:50px}p{position:absolute;top:1600px}</style><input id="name"><button onclick="fetch('/event?name='+encodeURIComponent(document.querySelector('input').value));this.textContent='Done'">Submit</button><p>Scrolled result</p></html>'''
                self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(body)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.origin='http://127.0.0.1:'+str(self.server.server_port)
        self.driver=BrowserDriver(self.root);self.driver.open(self.origin,{})

    def tearDown(self):
        self.driver.close();self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()

    def test_user_input_submission_navigation_and_scroll(self):
        first=self.root/'first.png';self.driver.screenshot(first)
        self.driver.act({'type':'click','x':150,'y':125})
        self.driver.act({'type':'type','text':'Quarterly'})
        self.driver.act({'type':'key','key':'Tab'})
        self.driver.act({'type':'key','key':'Enter'})
        self.driver.screenshot(self.root/'submitted.png')
        self.assertEqual(self.events,[{'name':['Quarterly']}])
        self.assertNotEqual(first.read_bytes(),(self.root/'submitted.png').read_bytes())
        self.driver.act({'type':'navigate','url':'/second'})
        self.assertEqual(self.driver.screenshot(self.root/'second.png')['url'],self.origin+'/second')
        self.driver.act({'type':'scroll','dy':1700})
        self.driver.screenshot(self.root/'scroll.png')
        self.assertNotEqual((self.root/'second.png').read_bytes(),(self.root/'scroll.png').read_bytes())

    def test_driver_rejects_external_navigation_code_and_invalid_coordinates(self):
        for action in ({'type':'navigate','url':'https://example.com'}, {'type':'evaluate','code':'document.body.remove()'},
                       {'type':'click','x':-1,'y':1}, {'type':'key','key':'Meta+Q'}):
            with self.subTest(action=action), self.assertRaises(RuntimeError): self.driver.act(action)

    def test_local_session_bootstrap_needs_no_saved_login_and_shares_browser_cookie(self):
        self.driver.close();self.driver=BrowserDriver(self.root)
        result=self.driver.open(self.origin,{'session_bootstrap':{'path':'/bootstrap','probe_path':'/protected'}})
        self.assertTrue(result['session_ready'])
        self.assertEqual(result['probe_status'],200)
        checked=self.driver.send({'command':'inspect_api','path':'/protected'})
        self.assertEqual(checked['status'],200)

    def test_session_bootstrap_does_not_accept_failed_probe_redirect_or_external_path(self):
        for config in ({'path':'/bootstrap','probe_path':'/reject'}, {'path':'/redirect','probe_path':'/protected'},
                       {'path':'//example.com/bootstrap','probe_path':'/protected'},
                       {'path':'/boot\nstrap','probe_path':'/protected'}):
            self.driver.close();self.driver=BrowserDriver(self.root)
            with self.subTest(config=config),self.assertRaises(RuntimeError):
                self.driver.open(self.origin,{'session_bootstrap':config})

    def test_real_dashboard_initializes_local_session_without_user_login(self):
        from test_dashboard import Fixture
        from dashboard_server import Dashboard, handler_for
        fixture=Fixture();fixture.setUp();self.addCleanup(fixture.tearDown)
        app=Dashboard(fixture.state)
        server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(app,0,ROOT/'dashboard/dist'))
        # handler 的同源端口与临时实例实际端口一致。
        server.RequestHandlerClass=handler_for(app,server.server_port,ROOT/'dashboard/dist')
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            self.driver.close();self.driver=BrowserDriver(self.root)
            result=self.driver.open('http://127.0.0.1:'+str(server.server_port),{'session_bootstrap':{'path':'/api/bootstrap','probe_path':'/api/products'}})
            self.assertTrue(result['session_ready'])
            self.assertEqual(self.driver.send({'command':'inspect_api','path':'/api/products'})['status'],200)
        finally:
            self.driver.close();server.shutdown();server.server_close();thread.join()

if __name__=='__main__': unittest.main()
