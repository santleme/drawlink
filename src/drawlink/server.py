"""Servidor HTTP local do SVG ao vivo (so 127.0.0.1, com token na URL).

Rotas, todas sob /<token>/:
  /              visualizador (HTML com um EventSource)
  /drawing.svg   o desenho atual, no formato do export do Draw
  /events        fluxo SSE: um 'snapshot' ao conectar, depois 'delta'

A tinta e privada. Defesas: so escuta em loopback; o token (aleatorio, por
execucao) vai no caminho e e comparado em tempo constante; o cabecalho Host e
conferido (contra DNS rebinding); sem CORS; Referrer-Policy: no-referrer (o
token esta na URL); teto de visualizadores simultaneos.
"""

import hmac
import queue
import secrets
import select
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .hub import CLOSED, encode_event

MAX_VIEWERS = 8
# Conexoes simultaneas (qualquer rota, autenticada ou nao): o excedente e fechado
# na hora. Com o prazo de E/S abaixo isso limita o que um cliente local sem token
# consegue ocupar (nao impede: quem controla a maquina local sempre pode negar
# servico, so fica limitado e temporario).
MAX_CONNECTIONS = 32
# Prazo de leitura/escrita por conexao: cliente que nao manda o pedido, ou nao
# drena o fluxo, e derrubado em vez de segurar o worker e a vaga para sempre.
IO_TIMEOUT_S = 10.0
KEEPALIVE_S = 15.0
# Com que frequencia o handler ocioso confere se o navegador ja fechou a conexao:
# sem isso a vaga ficaria presa ate a proxima escrita de keepalive falhar.
PEER_CHECK_S = 1.0


def _peer_closed(conn):
    try:
        readable, _, _ = select.select([conn], [], [], 0)
        return bool(readable) and conn.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return True


VIEWER_HTML = """<!doctype html>
<html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>drawlink</title>
<style>
html,body{margin:0;height:100%;background:#111;color:#bbb;font:14px system-ui}
#page{display:block;margin:0 auto;max-width:100%;max-height:100vh;background:#fff}
#st{position:fixed;left:8px;top:8px;opacity:.8}
</style></head><body>
<div id="st">conectando...</div>
<svg id="page" xmlns="http://www.w3.org/2000/svg"><g id="ink"></g></svg>
<script>
(function () {
  var svg = document.getElementById("page"), ink = document.getElementById("ink");
  var st = document.getElementById("st");
  function parse(markup) {
    var doc = new DOMParser().parseFromString(
      '<svg xmlns="http://www.w3.org/2000/svg">' + markup + "</svg>", "image/svg+xml");
    return doc.documentElement.firstChild;
  }
  function setView(v) {
    if (!v) return;
    svg.setAttribute("viewBox", "0 0 " + v[0] + " " + v[1]);
    svg.setAttribute("width", v[0] + "mm");
    svg.setAttribute("height", v[1] + "mm");
  }
  function add(list) {
    list.forEach(function (pair) {
      var old = document.getElementById(pair[0]);
      if (old) old.remove();
      var node = parse(pair[1]);
      if (node) ink.appendChild(document.importNode(node, true));
    });
  }
  var es = new EventSource("events");
  es.addEventListener("snapshot", function (e) {
    var d = JSON.parse(e.data);
    ink.textContent = "";
    setView(d.view); add(d.add);
    st.textContent = "ao vivo (v" + d.v + ")";
  });
  es.addEventListener("delta", function (e) {
    var d = JSON.parse(e.data);
    d.remove.forEach(function (id) {
      var n = document.getElementById(id); if (n) n.remove();
    });
    setView(d.view); add(d.add);
    st.textContent = "ao vivo (v" + d.v + ")";
  });
  es.onerror = function () { st.textContent = "reconectando..."; };
})();
</script></body></html>
"""


def _make_handler(hub, token, render_svg, allowed_hosts, viewers):
    class Handler(BaseHTTPRequestHandler):
        server_version = "drawlink"
        sys_version = ""
        timeout = IO_TIMEOUT_S

        def log_message(self, *_args):
            pass

        def _send(self, status, body=b"", ctype="text/plain; charset=utf-8", extra=()):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in extra:
                self.send_header(key, value)
            self.end_headers()
            if body and self.command != "HEAD":
                self.wfile.write(body)

        def _route(self):
            """Subcaminho depois do token, ou None se host/token nao conferem."""
            if self.headers.get("Host", "") not in allowed_hosts:
                return None
            parts = self.path.split("?", 1)[0].split("/")
            if len(parts) < 2 or parts[0] != "":
                return None
            offered = parts[1].encode()
            if not hmac.compare_digest(offered, token.encode()):
                return None
            if len(parts) == 2:
                return "/@slashless"  # /TOKEN sem barra: o caminho relativo quebraria
            return "/" + "/".join(parts[2:])

        def do_GET(self):  # noqa: N802 - API do BaseHTTPRequestHandler
            route = self._route()
            if route is None:
                return self._send(404, b"nao encontrado\n")
            if route == "/@slashless":
                return self._send(308, b"", extra=(("Location", f"/{token}/"),))
            if route == "/":
                csp = (
                    "default-src 'none'; script-src 'unsafe-inline'; "
                    "style-src 'unsafe-inline'; connect-src 'self'"
                )
                return self._send(
                    200,
                    VIEWER_HTML.encode(),
                    "text/html; charset=utf-8",
                    (("Content-Security-Policy", csp),),
                )
            if route == "/drawing.svg":
                return self._send(
                    200,
                    render_svg().encode(),
                    "image/svg+xml",
                    (("Content-Security-Policy", "sandbox"),),
                )
            if route == "/events":
                return self._events()
            return self._send(404, b"nao encontrado\n")

        do_HEAD = do_GET  # noqa: N815

        def _events(self):
            if self.command == "HEAD":  # so os cabecalhos: sem assinar o hub
                return self._send(200, b"", "text/event-stream")
            if not viewers.acquire(blocking=False):
                return self._send(503, b"visualizadores demais\n")
            sub = None
            try:
                sub = hub.subscribe()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.flush()
                last_write = time.monotonic()
                while True:
                    try:
                        event = sub.queue.get(timeout=PEER_CHECK_S)
                    except queue.Empty:
                        if _peer_closed(self.connection):
                            return
                        if time.monotonic() - last_write >= KEEPALIVE_S:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                            last_write = time.monotonic()
                        continue
                    last_write = time.monotonic()
                    if event is CLOSED:
                        return
                    self.wfile.write(encode_event(event))
                    self.wfile.flush()
            except (OSError, ValueError):
                return  # visualizador saiu
            finally:
                if sub is not None:
                    hub.unsubscribe(sub)
                viewers.release()

    return Handler


class _CappedServer(ThreadingHTTPServer):
    """ThreadingHTTPServer que recusa conexoes alem de `max_connections`."""

    daemon_threads = True

    def __init__(self, address, handler, max_connections):
        super().__init__(address, handler)
        self._slots = threading.BoundedSemaphore(max_connections)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class LiveServer:
    """Servidor em thread propria; `url` ja traz o token."""

    def __init__(self, hub, render_svg, port=0, token=None):
        self.hub = hub
        self.token = token or secrets.token_urlsafe(24)
        self._viewers = threading.BoundedSemaphore(MAX_VIEWERS)
        # O Host valido depende da porta, so conhecida depois do bind: o handler
        # recebe um conjunto que e preenchido logo abaixo.
        allowed = set()
        handler = _make_handler(hub, self.token, render_svg, allowed, self._viewers)
        self._httpd = _CappedServer(("127.0.0.1", port), handler, MAX_CONNECTIONS)
        self.port = self._httpd.server_address[1]
        allowed.update(
            {f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"[::1]:{self.port}"}
        )
        self.url = f"http://127.0.0.1:{self.port}/{self.token}/"
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="drawlink-http", daemon=True
        )
        self._thread.start()

    def close(self):
        self.hub.close()
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)
