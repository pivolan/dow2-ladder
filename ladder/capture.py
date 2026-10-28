#!/usr/bin/env python3
# Логирующий заглушка-сервер для разбора протокола RLink DoW2.
# Пишет каждый запрос игры (метод, путь, query, тело) в data/capture.log и отвечает заглушкой,
# чтобы клиент прошёл дальше и показал следующие запросы. Только для стенда.
import json, os, time, threading, urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = ROOT + '/data/capture.log'
PORT = 8218
_lock = threading.Lock()

def logreq(line):
    with _lock:
        with open(LOG, 'a') as f:
            f.write(line + '\n')

class H(BaseHTTPRequestHandler):
    server_version = 'rlink-capture'
    def log_message(self, *a): pass
    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        return self.rfile.read(n) if n > 0 else b''
    def _handle(self, method):
        body = self._body()
        u = urllib.parse.urlparse(self.path)
        t = time.strftime('%H:%M:%S')
        hdr = {k: v for k, v in self.headers.items() if k.lower() in ('content-type', 'user-agent', 'authorization')}
        logreq('\n=== %s %s %s' % (t, method, u.path))
        if u.query: logreq('  query: ' + u.query)
        if hdr: logreq('  headers: ' + json.dumps(hdr))
        if body:
            try: logreq('  body: ' + body.decode('utf-8', 'replace')[:4000])
            except Exception: logreq('  body(hex): ' + body[:2000].hex())
        # заглушка: общий успешный конверт RLink
        resp = {'result': {'code': 0, 'message': 'SUCCESS'}}
        b = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def do_GET(self): self._handle('GET')
    def do_POST(self): self._handle('POST')

if __name__ == '__main__':
    ThreadingHTTPServer(('127.0.0.1', PORT), H).serve_forever()
