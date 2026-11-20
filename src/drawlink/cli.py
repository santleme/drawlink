"""CLI do drawlink: `drawlink watch FONTE`."""

import argparse
import sys

from draw.protocol import AuthenticationError, ProtocolError, resolve_token

from . import source
from .session import Session


def _describe(update, session):
    x, y, w, h = update.box
    return (
        f"gen={update.generation} v={update.ink_version} "
        f"{'RESET ' if update.reset else ''}box=({x},{y},{w},{h}) "
        f"tinta={int((session.mask > 0).sum())}px"
    )


def watch(
    spec,
    token_file=None,
    max_records=None,
    out=sys.stdout,
    log=None,
    should_stop=lambda: False,
):
    """Aplica registros de FONTE e imprime uma linha por atualizacao."""
    if max_records is not None and max_records <= 0:
        return 0
    log = log or (lambda msg: print(msg, file=sys.stderr, flush=True))
    session = Session()
    count = 0

    def on_record(record, new_connection):
        nonlocal count
        if new_connection:
            session.clear()
        update = session.feed(record)
        print(_describe(update, session), file=out, flush=True)
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


def main(argv=None):
    parser = argparse.ArgumentParser(prog="drawlink")
    sub = parser.add_subparsers(dest="command", required=True)
    w = sub.add_parser("watch", help="acompanha o stream de tinta do Draw")
    w.add_argument("source", help="arquivo .drawink ou tcp://host[:porta]")
    w.add_argument("--token-file", default=None, help="token do stream (tcp://)")
    w.add_argument("--max-records", type=int, default=None)
    args = parser.parse_args(argv)
    try:
        watch(args.source, args.token_file, args.max_records)
    except KeyboardInterrupt:
        return 130
    except (AuthenticationError, ProtocolError, OSError, ValueError) as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
