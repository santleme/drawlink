import io
import threading
import time

import numpy as np
import pytest
from draw import stream
from draw.protocol import AuthenticationError

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


def test_socket_receives_and_flags_new_connection():
    pub = _live()
    try:
        a = _mask()
        a[2:4, 2:4] = 255
        pub.publish(1, 1, a)
        got = []
        for rec, new in source.from_socket(
            "127.0.0.1", pub.port, TOKEN.encode(), sleep=lambda _s: None
        ):
            got.append((rec.ink_version, new))
            break
        assert got == [(1, True)]
    finally:
        pub.close()


def test_reconnects_after_draw_restart():
    """Draw reinicia na mesma porta: o consumidor reconecta e recebe FULL."""
    pub = _live()
    port = pub.port
    pub.publish(1, 1, _mask())
    seen = []
    stop = threading.Event()
    state = {"second": None}

    def run():
        for rec, new in source.from_socket(
            "127.0.0.1",
            port,
            TOKEN.encode(),
            should_stop=stop.is_set,
            sleep=lambda _s: time.sleep(0.05),
        ):
            seen.append((rec.generation, new))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not seen and time.monotonic() < deadline:
            time.sleep(0.02)
        assert seen == [(1, True)]
        pub.close()
        time.sleep(0.3)
        state["second"] = stream.InkStreamPublisher(
            port=port, token=TOKEN, log=lambda *_: None
        )
        state["second"].publish(2, 1, _mask())
        deadline = time.monotonic() + 8
        while len(seen) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert seen[-1] == (2, True)
    finally:
        stop.set()
        thread.join(timeout=5)
        if state["second"] is not None:
            state["second"].close()
        pub.close()
    assert not thread.is_alive()


def test_wrong_token_is_not_retried():
    pub = _live()
    try:
        sleeps = []
        with pytest.raises(AuthenticationError):
            next(
                source.from_socket(
                    "127.0.0.1", pub.port, b"z" * 32, sleep=sleeps.append
                )
            )
        assert sleeps == []
    finally:
        pub.close()


def test_connection_refused_backs_off_exponentially():
    stops = iter([False] * 5 + [True] * 100)
    sleeps = []
    events = []
    gen = source.from_socket(
        "127.0.0.1",
        1,  # porta sem ninguem
        TOKEN.encode(),
        should_stop=lambda: next(stops),
        sleep=sleeps.append,
        on_event=events.append,
    )
    assert list(gen) == []
    assert sleeps == [0.25, 0.5, 1.0, 2.0, 4.0]
    assert len(events) == 5
