"""CLI do drawlink: `drawlink watch FONTE`."""

import argparse
import os
import signal
import sys
import threading

from draw.protocol import AuthenticationError, ProtocolError, resolve_token

from . import source
from .live import LiveVectorizer


class OutputClosed(Exception):
    """stdout fechou (ex.: `| head`). Nao e OSError de proposito: o follow
    trata OSError como queda do Draw e reconectaria para sempre."""


def _describe(update, live, delta=None):
    x, y, w, h = update.box
    text = (
        f"gen={update.generation} v={update.ink_version} "
        f"{'RESET ' if update.reset else ''}box=({x},{y},{w},{h}) "
        f"tinta={int((live.session.mask > 0).sum())}px"
    )
    if update.latency_us is not None:
        text += f" lat={update.latency_us / 1000:.1f}ms"
    if delta is not None:
        text += (
            f" ilhas={len(live.vectors.islands)}"
            f" +{len(delta.added)}/-{len(delta.removed)}"
            f" vetorizadas={live.vectors.vectorized_last}"
        )
    return text


def watch(
    spec,
    token_file=None,
    max_records=None,
    out=sys.stdout,
    log=None,
    should_stop=lambda: False,
    vectors=False,
):
    """Aplica registros de FONTE e imprime uma linha por atualizacao."""
    if max_records is not None and max_records <= 0:
        return 0
    log = log or (lambda msg: print(msg, file=sys.stderr, flush=True))
    live = LiveVectorizer()
    count = 0

    def on_record(record, new_connection):
        nonlocal count
        if new_connection:
            live.clear()
        if vectors:
            update, delta = live.feed(record)
        else:
            update, delta = live.session.feed(record), None
        try:
            print(_describe(update, live, delta), file=out, flush=True)
        except (BrokenPipeError, ValueError) as error:
            raise OutputClosed(str(error)) from error
        count += 1
        return max_records is not None and count >= max_records

    endpoint = source.parse_endpoint(spec)
    if endpoint is None:
        for record in source.from_file(spec):
            if on_record(record, False):
                break
    else:
        token = resolve_token(token_file=token_file)
        source.follow(
            *endpoint,
            token,
            on_record,
            should_stop=should_stop,
            on_event=log,
        )
    return count


def serve(
    spec,
    token_file=None,
    svg_out=None,
    http_port=None,
    stroke_mm=0.5,
    min_interval_s=0.25,
    max_records=None,
    should_stop=lambda: False,
    log=None,
    on_ready=None,
):
    """Segue FONTE e publica o SVG ao vivo (arquivo atomico e/ou HTTP local)."""
    from .server import LiveServer
    from .sink import SvgLiveSink

    if svg_out is None and http_port is None:
        raise ValueError("serve precisa de --svg-out e/ou --http-port")
    if max_records is not None and max_records <= 0:
        return 0
    log = log or (lambda msg: print(msg, file=sys.stderr, flush=True))
    live = LiveVectorizer()
    sink = SvgLiveSink(
        svg_out=svg_out, stroke_mm=stroke_mm, min_interval_s=min_interval_s, log=log
    )
    server = None
    count = 0
    try:
        if http_port is not None:
            server = LiveServer(sink.hub, sink.render_svg, port=http_port)
            log(f"visualizador: {server.url}")
        if on_ready is not None:
            on_ready(server, sink)

        def on_record(record, new_connection):
            nonlocal count
            if new_connection:
                live.clear()
            _update, delta = live.feed(record)
            sink.apply(live, delta)
            count += 1
            return max_records is not None and count >= max_records

        endpoint = source.parse_endpoint(spec)
        if endpoint is None:
            for record in source.from_file(spec):
                if on_record(record, False) or should_stop():
                    break
        else:
            token = resolve_token(token_file=token_file)
            source.follow(
                *endpoint, token, on_record, should_stop=should_stop, on_event=log
            )
    finally:
        if server is not None:
            server.close()
        sink.close()
    return count


def _detach_stdout():
    """Aponta o fd 1 para /dev/null depois de um pipe fechado.

    O CPython tenta descarregar o buffer do stdout de novo ao encerrar; com o
    pipe ainda quebrado isso imprime "Exception ignored" e muda o status para
    120 (derruba `| head` sob pipefail). Fica aqui, na fronteira do CLI, e nao
    em watch(), para nao mexer em streams passados por quem chama.
    """
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, sys.stdout.fileno())
        finally:
            os.close(devnull)
    except (OSError, ValueError, AttributeError):
        pass  # stdout trocado/sem fd: nada a redirecionar


def main(argv=None):
    parser = argparse.ArgumentParser(prog="drawlink")
    sub = parser.add_subparsers(dest="command", required=True)
    w = sub.add_parser("watch", help="acompanha o stream de tinta do Draw")
    w.add_argument("source", help="arquivo .drawink ou tcp://host[:porta]")
    w.add_argument("--token-file", default=None, help="token do stream (tcp://)")
    w.add_argument("--max-records", type=int, default=None)
    w.add_argument(
        "--vectors",
        action="store_true",
        help="vetoriza incrementalmente e mostra ilhas/delta por atualizacao",
    )
    sv = sub.add_parser("serve", help="publica o SVG ao vivo (arquivo e/ou HTTP local)")
    sv.add_argument("source", help="arquivo .drawink ou tcp://host[:porta]")
    sv.add_argument("--token-file", default=None, help="token do stream (tcp://)")
    sv.add_argument("--svg-out", default=None, help="SVG reescrito de forma atomica")
    sv.add_argument(
        "--http-port",
        type=int,
        default=None,
        help="visualizador em 127.0.0.1:PORTA (0 = porta livre); a URL leva um token",
    )
    sv.add_argument("--stroke-mm", type=float, default=0.5)
    sv.add_argument("--max-records", type=int, default=None)
    args = parser.parse_args(argv)
    if args.command == "serve":
        if args.svg_out is None and args.http_port is None:
            parser.error("serve precisa de --svg-out e/ou --http-port")
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        try:
            serve(
                args.source,
                args.token_file,
                svg_out=args.svg_out,
                http_port=args.http_port,
                stroke_mm=args.stroke_mm,
                max_records=args.max_records,
                should_stop=stop.is_set,
            )
        except KeyboardInterrupt:
            return 130
        except (AuthenticationError, ProtocolError, OSError, ValueError) as error:
            print(f"Erro: {error}", file=sys.stderr)
            return 2
        return 0
    try:
        watch(args.source, args.token_file, args.max_records, vectors=args.vectors)
    except KeyboardInterrupt:
        return 130
    except OutputClosed:
        _detach_stdout()
        return 0
    except (AuthenticationError, ProtocolError, OSError, ValueError) as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
