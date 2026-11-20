"""CLI do drawlink: `drawlink watch FONTE`."""

import argparse
import os
import sys

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
    args = parser.parse_args(argv)
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
