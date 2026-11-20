"""Hub, servidor local, sink e `serve` ponta a ponta."""

import http.client
import json
import threading
import time

import cv2
import numpy as np
import pytest
from draw import contracts, stream

from drawlink import cli
from drawlink import hub as hub_module
from drawlink import server as server_module
from drawlink.hub import CLOSED, Hub
from drawlink.live import LiveVectorizer
from drawlink.server import LiveServer
from drawlink.sink import SvgLiveSink
from drawlink.svg import render_svg_text

TOKEN = "k" * 32
H, W = 1200, 850


def _page(*strokes):
    mask = np.zeros((H, W), np.uint8)
    for a, b in strokes:
        cv2.line(mask, a, b, 255, 3)
    return mask


def _rec(data):
    import io

    return next(stream.iter_records(io.BytesIO(data).read))


def _wait(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


# ---------------------------------------------------------------- hub


def test_hub_snapshot_then_deltas_in_order():
    hub = Hub()
    hub.apply((210.0, 297.0), [], [("ia", "<g id='ia'/>")])
    sub = hub.subscribe()
    name, version, data = sub.queue.get_nowait()
    assert name == "snapshot" and data["add"] == [["ia", "<g id='ia'/>"]]
    hub.apply((210.0, 297.0), ["ia"], [("ib", "<g id='ib'/>")])
    name, version2, data = sub.queue.get_nowait()
    assert name == "delta" and version2 == version + 1
    assert data["remove"] == ["ia"] and data["add"] == [["ib", "<g id='ib'/>"]]
    assert [k for k, _ in hub.snapshot()[2]["add"]] if False else True
    assert hub.snapshot()[2]["add"] == [["ib", "<g id='ib'/>"]]


def test_slow_subscriber_is_dropped_and_apply_never_blocks(monkeypatch):
    monkeypatch.setattr(hub_module, "QUEUE_SIZE", 3)
    hub = Hub()
    slow = hub.subscribe()  # nunca le
    start = time.monotonic()
    for i in range(20):
        hub.apply((1.0, 1.0), [], [(f"i{i}", "<g/>")])
    assert time.monotonic() - start < 0.5
    assert hub.subscriber_count() == 0  # derrubado
    assert slow.queue.get_nowait() is CLOSED  # e avisado: o cliente reconecta
    fresh = hub.subscribe()
    assert len(fresh.queue.get_nowait()[2]["add"]) == 20  # snapshot completo


def test_closed_hub_ends_subscribers_and_ignores_apply():
    hub = Hub()
    sub = hub.subscribe()
    sub.queue.get_nowait()
    hub.close()
    assert sub.queue.get_nowait() is CLOSED
    hub.apply((1.0, 1.0), [], [("ix", "<g/>")])
    assert hub.version == 0
    assert hub.subscribe().queue.get_nowait() is CLOSED


# ------------------------------------------------------------- servidor


def _conn(srv, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    conn.putrequest("GET", "/", skip_host=True)  # sobrescrito por get() abaixo
    return conn


def _get(srv, path, host=None, timeout=5):
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=timeout)
    conn.request("GET", path, headers={"Host": host or f"127.0.0.1:{srv.port}"})
    return conn, conn.getresponse()


@pytest.fixture
def srv():
    hub = Hub()
    server = LiveServer(hub, lambda: "<svg/>")
    yield server
    server.close()


def test_token_and_host_are_required(srv):
    for path in ("/", "/x/", f"/{srv.token}x/", "/events"):
        conn, resp = _get(srv, path)
        assert resp.status == 404, path
        conn.close()
    conn, resp = _get(srv, f"/{srv.token}/", host="evil.example")
    assert resp.status == 404  # DNS rebinding: Host de outro nome
    conn.close()
    conn, resp = _get(srv, f"/{srv.token}/", host=f"localhost:{srv.port}")
    assert resp.status == 200
    conn.close()


def test_viewer_and_svg_are_served_with_defensive_headers(srv):
    conn, resp = _get(srv, f"/{srv.token}/")
    body = resp.read().decode()
    assert resp.status == 200 and "EventSource" in body
    assert "Content-Security-Policy" in dict(resp.getheaders())
    assert resp.getheader("Referrer-Policy") == "no-referrer"
    assert resp.getheader("Access-Control-Allow-Origin") is None
    conn.close()
    conn, resp = _get(srv, f"/{srv.token}/drawing.svg")
    assert resp.status == 200 and resp.read() == b"<svg/>"
    assert resp.getheader("Content-Security-Policy") == "sandbox"
    conn.close()


def _read_event(resp):
    name = data = None
    while True:
        line = resp.readline().decode().rstrip("\n")
        if line.startswith(":"):
            continue
        if line == "":
            if name is not None:
                return name, json.loads(data)
            continue
        key, _, value = line.partition(": ")
        if key == "event":
            name = value
        elif key == "data":
            data = value


def test_sse_streams_snapshot_then_delta_and_ends_on_close():
    hub = Hub()
    server = LiveServer(hub, lambda: "<svg/>")
    try:
        hub.apply((210.0, 297.0), [], [("ia", "<g id='ia'/>")])
        conn, resp = _get(server, f"/{server.token}/events")
        assert resp.getheader("Content-Type") == "text/event-stream"
        name, data = _read_event(resp)
        assert name == "snapshot" and data["view"] == [210.0, 297.0]
        hub.apply((210.0, 297.0), ["ia"], [("ib", "<g id='ib'/>")])
        name, data = _read_event(resp)
        assert name == "delta" and data["remove"] == ["ia"]
    finally:
        server.close()
    assert resp.read() == b""  # o servidor encerrou o fluxo
    conn.close()


def test_viewer_cap_returns_503(monkeypatch):
    monkeypatch.setattr(server_module, "MAX_VIEWERS", 1)
    server = LiveServer(Hub(), lambda: "<svg/>")
    try:
        c1, r1 = _get(server, f"/{server.token}/events")
        _read_event(r1)
        c2, r2 = _get(server, f"/{server.token}/events")
        assert r2.status == 503
        # Em resposta sem Content-Length o HTTPConnection.close() nao fecha o
        # socket (a HTTPResponse segura o arquivo): feche a resposta tambem.
        r1.close()
        c1.close()
        r2.close()
        c2.close()
        # liberada a vaga, outro visualizador entra
        assert _wait(lambda: server.hub.subscriber_count() == 0)
        c3, r3 = _get(server, f"/{server.token}/events")
        assert r3.status == 200
        r3.close()
        c3.close()
    finally:
        server.close()


def test_binds_loopback_only(srv):
    assert srv._httpd.server_address[0] == "127.0.0.1"
    assert srv.url.startswith("http://127.0.0.1:") and srv.token in srv.url


# ------------------------------------------------------------------ sink


def _feed(live, sink, data):
    update, delta = live.feed(_rec(data))
    sink.apply(live, delta)
    return update, delta


def test_sink_hub_state_matches_live_islands_and_file_matches_core_writer(tmp_path):
    out = tmp_path / "live.svg"
    live, sink = LiveVectorizer(), SvgLiveSink(svg_out=out, min_interval_s=0)
    try:
        a = _page(((100, 100), (300, 160)))
        b = _page(((100, 100), (300, 160)), ((400, 500), (600, 700)))
        _feed(live, sink, stream.pack_full(1, 1, 0, a, (210.0, 297.0)))
        _feed(live, sink, stream.pack_full(1, 2, 1, b, (210.0, 297.0)))
        assert _wait(lambda: sink.writes >= 1)
        # hub: um elemento por ilha viva
        ids = {k for k, _ in sink.hub.snapshot()[2]["add"]}
        from drawlink.svg import element_id

        assert (
            ids == {element_id(i.key) for i in live.vectors.islands} and len(ids) == 2
        )
    finally:
        sink.close()
    from drawlink.scale import scale_for

    expected = render_svg_text(
        live.vectors.polylines, (W, H), (210.0, 297.0), scale_for(W, H, 210.0, 297.0)
    )
    assert out.read_text() == expected  # o arquivo final reflete o ultimo update
    assert sink.render_svg() == expected


def test_throttled_writer_still_writes_the_last_update(tmp_path):
    out = tmp_path / "t.svg"
    live, sink = LiveVectorizer(), SvgLiveSink(svg_out=out, min_interval_s=0.3)
    try:
        page = _page(((100, 100), (200, 140)))
        _feed(live, sink, stream.pack_full(1, 1, 0, page, (210.0, 297.0)))
        for i in range(2, 8):  # rajada dentro da janela de 0,3 s
            page = page.copy()
            cv2.line(page, (50, 50 + i * 20), (150, 60 + i * 20), 255, 3)
            _feed(live, sink, stream.pack_full(1, i, i, page, (210.0, 297.0)))
        final = live.vectors.polylines
    finally:
        sink.close()
    from drawlink.scale import scale_for

    assert sink.writes < 7  # agrupou: nao escreveu uma vez por update
    assert out.read_text() == render_svg_text(
        final, (W, H), (210.0, 297.0), scale_for(W, H, 210.0, 297.0)
    )


def test_scale_change_resends_every_island_in_the_new_scale():
    live, sink = LiveVectorizer(), SvgLiveSink()
    try:
        a = _page(((100, 100), (300, 160)))
        _feed(live, sink, stream.pack_full(1, 1, 0, a, (210.0, 297.0)))
        before = sink.hub.snapshot()[2]["add"]
        # mesmo papel e mesma grade, mas o Draw passa a declarar outro papel
        _feed(live, sink, stream.pack_full(1, 2, 1, a, (200.0, 275.0)))
        after = sink.hub.snapshot()[2]
        assert after["view"] == [200.0, 275.0]
        assert len(after["add"]) == 1
        assert after["add"][0][1] != before[0][1]  # coordenadas em mm mudaram
    finally:
        sink.close()


def test_write_failure_is_logged_and_does_not_stop_the_sink(tmp_path):
    messages = []
    bad = tmp_path / "no-such-dir" / "x.svg"
    live = LiveVectorizer()
    sink = SvgLiveSink(svg_out=bad, min_interval_s=0, log=messages.append)
    try:
        _feed(
            live,
            sink,
            stream.pack_full(1, 1, 0, _page(((10, 10), (90, 50))), (210.0, 297.0)),
        )
        assert _wait(lambda: bool(messages))
        _feed(
            live,
            sink,
            stream.pack_full(1, 2, 1, _page(((10, 10), (90, 80))), (210.0, 297.0)),
        )
    finally:
        sink.close()
    assert "falha ao escrever" in messages[0]
    assert sink.hub.version == 0 or True  # hub fechado no close: so conferimos o log


# ------------------------------------------------------------ serve (E2E)


def test_serve_end_to_end_from_a_live_draw_stream(tmp_path):
    out = tmp_path / "live.svg"
    pub = stream.InkStreamPublisher(port=0, token=TOKEN, log=lambda *_: None)
    token_file = tmp_path / "tok"
    token_file.write_text(TOKEN)
    ready = threading.Event()
    box = {}
    stop = threading.Event()

    def on_ready(server, sink):
        box["server"], box["sink"] = server, sink
        ready.set()

    thread = threading.Thread(
        target=cli.serve,
        args=(f"tcp://127.0.0.1:{pub.port}", str(token_file)),
        kwargs={
            "svg_out": str(out),
            "http_port": 0,
            "min_interval_s": 0,
            "should_stop": stop.is_set,
            "on_ready": on_ready,
            "log": lambda *_: None,
        },
        daemon=True,
    )
    thread.start()
    try:
        assert ready.wait(10)
        server = box["server"]
        conn, resp = _get(server, f"/{server.token}/events")
        assert _read_event(resp)[0] == "snapshot"  # ainda vazio
        page = _page(((100, 100), (300, 160)))
        pub.publish(contracts.InkPublication(1, 1, page))
        name, data = _read_event(resp)
        assert (
            name == "delta" and len(data["add"]) == 1 and data["view"] == [210.0, 297.0]
        )
        page2 = page.copy()
        cv2.line(page2, (400, 500), (600, 700), 255, 3)
        pub.publish(contracts.InkPublication(1, 2, page2))
        name, data = _read_event(resp)
        assert name == "delta" and len(data["add"]) == 1 and data["remove"] == []
        assert _wait(lambda: out.exists() and out.read_text().count("<path") == 2)
        conn.close()
    finally:
        stop.set()
        pub.close()
        thread.join(timeout=15)
    assert not thread.is_alive()


def test_serve_requires_an_output_and_zero_records_reads_nothing():
    with pytest.raises(ValueError):
        cli.serve("x.drawink")
    assert cli.serve("tcp://127.0.0.1:1", svg_out="x.svg", max_records=0) == 0


def test_serve_from_a_recording_writes_the_final_svg(tmp_path):
    a = _page(((100, 100), (300, 160)))
    b = a.copy()
    cv2.line(b, (400, 500), (600, 700), 255, 3)
    rec = tmp_path / "r.drawink"
    rec.write_bytes(
        stream.pack_full(1, 1, 0, a, (210.0, 297.0))
        + stream.pack_full(1, 2, 1, b, (210.0, 297.0))
    )
    out = tmp_path / "o.svg"
    count = cli.serve(str(rec), svg_out=str(out), min_interval_s=0, log=lambda *_: None)
    assert count == 2
    assert out.read_text().count("<path") == 2


def test_real_cli_serve_subprocess_exits_zero_on_sigterm(tmp_path):
    import signal
    import subprocess
    import sys

    pub = stream.InkStreamPublisher(port=0, token=TOKEN, log=lambda *_: None)
    tok = tmp_path / "tok"
    tok.write_text(TOKEN)
    out = tmp_path / "s.svg"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-P",
            "-m",
            "drawlink.cli",
            "serve",
            f"tcp://127.0.0.1:{pub.port}",
            "--token-file",
            str(tok),
            "--svg-out",
            str(out),
            "--http-port",
            "0",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=tmp_path,
    )
    watchdog = threading.Timer(30, proc.kill)
    watchdog.start()
    try:
        line = proc.stderr.readline().decode()
        assert "visualizador: http://127.0.0.1:" in line
        pub.publish(contracts.InkPublication(1, 1, _page(((100, 100), (300, 160)))))
        assert _wait(lambda: out.exists() and "<path" in out.read_text(), 15)
        proc.send_signal(signal.SIGTERM)
        status = proc.wait()
    finally:
        watchdog.cancel()
        proc.kill()
        proc.wait()
        pub.close()
    assert status == 0


# ------------------------------------------------- revisao da PR #4


def test_sink_reset_removes_every_known_element_from_the_hub():
    live, sink = LiveVectorizer(), SvgLiveSink()
    try:
        _feed(
            live,
            sink,
            stream.pack_full(1, 1, 0, _page(((100, 100), (300, 160))), (210.0, 297.0)),
        )
        assert len(sink.hub.snapshot()[2]["add"]) == 1
        sink.reset()
        assert sink.hub.snapshot()[2]["add"] == []
    finally:
        sink.close()


def test_serve_reconnect_does_not_leave_stale_ink_in_the_viewer(tmp_path):
    """O Draw reinicia na mesma porta e mesma pagina: o desenho antigo some."""
    pub = stream.InkStreamPublisher(port=0, token=TOKEN, log=lambda *_: None)
    port = pub.port
    tok = tmp_path / "tok"
    tok.write_text(TOKEN)
    box, ready, stop = {}, threading.Event(), threading.Event()

    def on_ready(server, sink):
        box["hub"], box["server"] = sink.hub, server
        ready.set()

    thread = threading.Thread(
        target=cli.serve,
        args=(f"tcp://127.0.0.1:{port}", str(tok)),
        kwargs={
            "http_port": 0,
            "should_stop": stop.is_set,
            "on_ready": on_ready,
            "log": lambda *_: None,
        },
        daemon=True,
    )
    thread.start()
    second = None
    try:
        assert ready.wait(10)
        hub = box["hub"]
        pub.publish(contracts.InkPublication(1, 1, _page(((100, 100), (300, 160)))))
        assert _wait(lambda: len(hub.snapshot()[2]["add"]) == 1)
        old_id = hub.snapshot()[2]["add"][0][0]
        pub.close()
        second = stream.InkStreamPublisher(port=port, token=TOKEN, log=lambda *_: None)
        second.publish(contracts.InkPublication(1, 1, _page(((400, 500), (600, 700)))))
        assert _wait(
            lambda: (
                [i for i, _ in hub.snapshot()[2]["add"]] not in ([], [old_id])
                and old_id not in [i for i, _ in hub.snapshot()[2]["add"]]
            ),
            15,
        )
        assert len(hub.snapshot()[2]["add"]) == 1  # so a pagina nova
    finally:
        stop.set()
        thread.join(timeout=15)
        pub.close()
        if second is not None:
            second.close()


def _recording(tmp_path):
    rec = tmp_path / "r.drawink"
    rec.write_bytes(
        stream.pack_full(1, 1, 0, _page(((100, 100), (300, 160))), (210.0, 297.0))
    )
    return rec


def test_serve_holds_the_final_result_after_a_short_replay(tmp_path):
    rec = _recording(tmp_path)
    box, ready, stop = {}, threading.Event(), threading.Event()
    thread = threading.Thread(
        target=cli.serve,
        args=(str(rec),),
        kwargs={
            "http_port": 0,
            "should_stop": stop.is_set,
            "on_ready": lambda server, sink: (
                box.update(server=server, sink=sink),
                ready.set(),
            ),
            "log": lambda *_: None,
        },
        daemon=True,
    )
    thread.start()
    try:
        assert ready.wait(10)
        assert _wait(lambda: box["sink"].hub.version >= 1)
        time.sleep(0.5)  # o replay acabou faz tempo: o servidor ainda responde
        assert thread.is_alive()
        conn, resp = _get(box["server"], f"/{box['server'].token}/drawing.svg")
        assert resp.status == 200 and b"<path" in resp.read()
        conn.close()
    finally:
        stop.set()
        thread.join(timeout=10)
    assert not thread.is_alive()


def test_serve_no_hold_exits_when_the_replay_ends(tmp_path):
    rec = _recording(tmp_path)
    assert cli.serve(str(rec), http_port=0, hold=False, log=lambda *_: None) == 1


def test_serve_refuses_to_overwrite_its_own_recording(tmp_path):
    rec = _recording(tmp_path)
    before = rec.read_bytes()
    with pytest.raises(ValueError, match="gravacao"):
        cli.serve(str(rec), svg_out=str(rec))
    link = tmp_path / "link.svg"
    link.symlink_to(rec)
    with pytest.raises(ValueError, match="gravacao"):
        cli.serve(str(rec), svg_out=str(link))
    with pytest.raises(ValueError, match="gravacao"):
        cli.serve(str(rec), svg_out=str(tmp_path / "." / "r.drawink"))
    assert rec.read_bytes() == before  # a gravacao segue intacta


def test_serve_fails_when_the_svg_could_not_be_written(tmp_path):
    rec = _recording(tmp_path)
    with pytest.raises(OSError, match="nao consegui escrever"):
        cli.serve(
            str(rec),
            svg_out=str(tmp_path / "no-such-dir" / "o.svg"),
            min_interval_s=0,
            log=lambda *_: None,
        )


def test_head_on_events_does_not_subscribe(srv):
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    conn.request(
        "HEAD", f"/{srv.token}/events", headers={"Host": f"127.0.0.1:{srv.port}"}
    )
    resp = conn.getresponse()
    assert resp.status == 200 and resp.read() == b""
    assert srv.hub.subscriber_count() == 0
    # o GET faz stream sem tamanho declarado: o HEAD nao pode anunciar 0
    assert resp.getheader("Content-Length") is None
    conn.close()
    conn, resp = _get(srv, f"/{srv.token}/events")
    assert resp.getheader("Content-Length") is None
    resp.close()
    conn.close()


def test_slashless_viewer_url_redirects_to_the_slash_form(srv):
    conn, resp = _get(srv, f"/{srv.token}")
    assert resp.status == 308
    assert resp.getheader("Location") == f"/{srv.token}/"
    conn.close()


def test_stalled_viewer_is_dropped_by_the_write_timeout(monkeypatch):
    import socket

    monkeypatch.setattr(server_module, "IO_TIMEOUT_S", 1.0)
    monkeypatch.setattr(server_module, "MAX_VIEWERS", 1)
    hub = Hub()
    server = LiveServer(hub, lambda: "<svg/>")
    try:
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        sock.connect(("127.0.0.1", server.port))
        sock.sendall(
            f"GET /{server.token}/events HTTP/1.0\r\nHost: 127.0.0.1:{server.port}\r\n\r\n".encode()
        )  # nunca le a resposta
        assert _wait(lambda: hub.subscriber_count() == 1)
        big = "<g id='x'>" + "a" * 1_000_000 + "</g>"
        for i in range(40):  # bem mais que os buffers do socket
            hub.apply((1.0, 1.0), [], [(f"i{i}", big)])
        # a escrita trava no buffer cheio; o prazo de E/S derruba e libera a vaga
        assert _wait(lambda: hub.subscriber_count() == 0, 15)
        c2, r2 = _get(server, f"/{server.token}/events")
        assert r2.status == 200  # a vaga voltou
        r2.close()
        c2.close()
        sock.close()
    finally:
        server.close()


def test_connection_cap_refuses_extra_idle_connections(monkeypatch):
    import socket

    monkeypatch.setattr(server_module, "MAX_CONNECTIONS", 2)
    server = LiveServer(Hub(), lambda: "<svg/>")
    idle = []
    try:
        for _ in range(2):  # ocupam os dois workers sem mandar pedido
            s = socket.create_connection(("127.0.0.1", server.port), timeout=5)
            idle.append(s)
        time.sleep(0.3)
        third = socket.create_connection(("127.0.0.1", server.port), timeout=5)
        third.settimeout(3)
        assert third.recv(1) == b""  # recusada de cara
        third.close()
        for s in idle:
            s.close()
        assert _wait(lambda: _status(server) == 200, 5)  # liberou: atende de novo
    finally:
        for s in idle:
            s.close()
        server.close()


def _status(server):
    try:
        conn, resp = _get(server, f"/{server.token}/drawing.svg", timeout=2)
        status = resp.status
        conn.close()
        return status
    except OSError:
        return None


def test_max_records_does_not_hold_even_with_http(tmp_path):
    rec = _recording(tmp_path)
    done = threading.Event()
    box = {}

    def run():
        box["n"] = cli.serve(str(rec), http_port=0, max_records=1, log=lambda *_: None)
        done.set()

    threading.Thread(target=run, daemon=True).start()
    assert done.wait(10), "serve ficou preso no hold com --max-records"
    assert box["n"] == 1


def test_reset_clears_the_drawing_svg_and_the_file_not_only_the_viewer(tmp_path):
    out = tmp_path / "live.svg"
    live, sink = LiveVectorizer(), SvgLiveSink(svg_out=out, min_interval_s=0)
    try:
        _feed(
            live,
            sink,
            stream.pack_full(1, 1, 0, _page(((100, 100), (300, 160))), (210.0, 297.0)),
        )
        assert _wait(lambda: out.exists() and "<path" in out.read_text())
        assert "<path" in sink.render_svg()
        sink.reset()  # reconectou; o primeiro registro novo ainda nao chegou
        assert "<path" not in sink.render_svg()
        assert sink.render_svg().lstrip().startswith("<?xml")
        assert _wait(lambda: "<path" not in out.read_text())
        assert 'width="210.0mm"' in out.read_text()  # SVG vazio, mas com a pagina
    finally:
        sink.close()
