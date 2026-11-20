import io
import threading
import time

import numpy as np
import pytest
from draw import contracts, stream
from draw.protocol import AuthenticationError, ProtocolError

from drawlink import cli, source
from drawlink.session import Session

TOKEN = "k" * 32


def _mask(fill=0):
    return np.full((40, 30), fill, np.uint8)


def _file(tmp_path, *records):
    path = tmp_path / "s.drawink"
    path.write_bytes(b"".join(records))
    return path


def test_session_reset_on_generation_change(tmp_path):
    a = _mask()
    b = a.copy()
    b[1:3, 1:3] = 255
    path = _file(
        tmp_path,
        stream.pack_full(1, 1, 5, a, (210.0, 297.0)),
        stream.pack_delta(1, 2, 6, b, (1, 1, 2, 2)),
        stream.pack_full(2, 1, 7, a, (210.0, 297.0)),
    )
    session = Session()
    updates = [session.feed(r) for r in source.from_file(path)]
    assert [u.reset for u in updates] == [True, False, True]
    assert updates[1].box == (1, 1, 2, 2)
    assert np.array_equal(session.mask, a)
    assert session.paper_mm == pytest.approx((210.0, 297.0))


def test_watch_file_prints_one_line_per_record(tmp_path):
    path = _file(tmp_path, stream.pack_full(1, 1, 0, _mask(255), (1.0, 1.0)))
    out = io.StringIO()
    assert cli.watch(str(path), out=out) == 1
    assert "gen=1" in out.getvalue() and "RESET" in out.getvalue()


def test_parse_endpoint():
    assert source.parse_endpoint("tcp://h:9") == ("h", 9)
    assert source.parse_endpoint("tcp://h") == ("h", stream.DEFAULT_PORT)
    assert source.parse_endpoint("arquivo.drawink") is None
    with pytest.raises(ValueError):
        source.parse_endpoint("tcp://")


def test_cli_returns_2_on_truncated_file(tmp_path, capsys):
    good = stream.pack_full(1, 1, 0, _mask(), (1.0, 1.0))
    path = _file(tmp_path, good[:-3])
    assert cli.main(["watch", str(path)]) == 2
    assert "Erro" in capsys.readouterr().err


def _live(**kw):
    return stream.InkStreamPublisher(port=0, token=TOKEN, log=lambda *_: None, **kw)


def _pub(pub, generation, version, mask):
    pub.publish(contracts.InkPublication(generation, version, mask))


def _follow_in_thread(port, on_record, **kw):
    stop = threading.Event()
    errors = []

    def run():
        try:
            source.follow(
                "127.0.0.1",
                port,
                TOKEN.encode(),
                on_record,
                should_stop=stop.is_set,
                sleep=lambda _s: time.sleep(0.05),
                **kw,
            )
        except Exception as error:  # noqa: BLE001
            errors.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return stop, thread, errors


def _wait(cond, timeout=6.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_follow_receives_and_flags_new_connection():
    pub = _live()
    try:
        a = _mask()
        a[2:4, 2:4] = 255
        _pub(pub, 1, 1, a)
        got = []
        source.follow(
            "127.0.0.1",
            pub.port,
            TOKEN.encode(),
            lambda rec, new: got.append((rec.ink_version, new)) or True,
            sleep=lambda _s: None,
        )
        assert got == [(1, True)]
    finally:
        pub.close()


def test_reconnects_after_draw_restart():
    """Draw reinicia na mesma porta: o consumidor reconecta e recebe FULL."""
    pub = _live()
    port = pub.port
    _pub(pub, 1, 1, _mask())
    seen = []
    stop, thread, errors = _follow_in_thread(
        port, lambda rec, new: seen.append((rec.generation, new))
    )
    second = None
    try:
        assert _wait(lambda: seen == [(1, True)])
        pub.close()
        time.sleep(0.3)
        second = stream.InkStreamPublisher(port=port, token=TOKEN, log=lambda *_: None)
        _pub(second, 2, 1, _mask())
        assert _wait(lambda: len(seen) >= 2)
        assert seen[-1][1] is True
    finally:
        stop.set()
        thread.join(timeout=5)
        if second is not None:
            second.close()
        pub.close()
    assert not thread.is_alive() and not errors


def test_invalid_payload_reconnects_instead_of_ending_follow():
    pub = _live()
    _pub(pub, 1, 1, _mask())
    calls = []

    def on_record(rec, new):
        calls.append(new)
        if len(calls) == 1:
            raise ProtocolError("payload ruim")
        return True

    try:
        stop, thread, errors = _follow_in_thread(pub.port, on_record)
        assert _wait(lambda: len(calls) >= 2)
        thread.join(timeout=5)
        assert calls == [True, True] and not errors
    finally:
        stop.set()
        pub.close()


def test_stop_is_checked_while_records_keep_flowing():
    pub = _live()
    try:
        _pub(pub, 1, 1, _mask())
        n = {"v": 1}
        count = []

        def feed():
            while len(count) < 3 and not done.is_set():
                n["v"] += 1
                m = _mask()
                m[0, n["v"] % 30] = 255
                _pub(pub, 1, n["v"], m)
                time.sleep(0.02)

        done = threading.Event()
        threading.Thread(target=feed, daemon=True).start()
        source.follow(
            "127.0.0.1",
            pub.port,
            TOKEN.encode(),
            lambda rec, new: count.append(1) and False,
            should_stop=lambda: len(count) >= 3,
            sleep=lambda _s: None,
        )
        done.set()
        assert len(count) == 3
    finally:
        pub.close()


def test_auth_failures_give_up_after_limit_not_first_try():
    pub = _live()
    try:
        sleeps = []
        with pytest.raises(AuthenticationError):
            source.follow(
                "127.0.0.1",
                pub.port,
                b"z" * 32,
                lambda *_: True,
                sleep=sleeps.append,
            )
        assert len(sleeps) == source.MAX_AUTH_FAILURES - 1
    finally:
        pub.close()


def test_connection_refused_backs_off_exponentially():
    stops = iter([False] * 5 + [True] * 100)
    sleeps = []
    events = []
    source.follow(
        "127.0.0.1",
        1,  # porta sem ninguem
        TOKEN.encode(),
        lambda *_: True,
        should_stop=lambda: next(stops),
        sleep=sleeps.append,
        on_event=events.append,
    )
    assert sleeps == [0.25, 0.5, 1.0, 2.0, 4.0]
    assert len(events) == 5


def test_backoff_not_reset_by_handshake_alone(monkeypatch):
    """Draw que autentica e cai antes de qualquer registro nao zera o backoff."""
    import socket as socket_module

    from draw.protocol import server_handshake

    srv = socket_module.create_server(("127.0.0.1", 0))
    port = srv.getsockname()[1]

    def serve():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            server_handshake(conn, TOKEN.encode())
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    sleeps = []
    try:
        source.follow(
            "127.0.0.1",
            port,
            TOKEN.encode(),
            lambda *_: True,
            should_stop=lambda: len(sleeps) >= 4,
            sleep=sleeps.append,
        )
    finally:
        srv.close()
    assert sleeps[:3] == [0.25, 0.5, 1.0]


def test_watch_zero_max_records_reads_nothing():
    out = io.StringIO()
    assert cli.watch("tcp://127.0.0.1:1", max_records=0, out=out) == 0
    assert out.getvalue() == ""


def test_watch_flushes_each_line():
    class Spy(io.StringIO):
        flushes = 0

        def flush(self):
            Spy.flushes += 1
            super().flush()

    pub = _live()
    try:
        _pub(pub, 1, 1, _mask(255))
        tok = pub._token.decode()
        out = Spy()
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".tok") as handle:
            handle.write(tok)
            handle.flush()
            cli.watch(
                f"tcp://127.0.0.1:{pub.port}",
                token_file=handle.name,
                max_records=1,
                out=out,
            )
        assert Spy.flushes >= 1
    finally:
        pub.close()


def test_corrupt_zlib_in_recording_returns_2(tmp_path, capsys):
    import struct

    payload = struct.pack("<ff", 1.0, 1.0) + b"lixo que nao e zlib"
    rec = (
        stream.HEADER.pack(
            stream.MAGIC, 1, stream.KIND_FULL, 0, 1, 1, 0, 4, 4, len(payload)
        )
        + payload
    )
    path = _file(tmp_path, rec)
    assert cli.main(["watch", str(path)]) == 2
    assert "Erro" in capsys.readouterr().err
