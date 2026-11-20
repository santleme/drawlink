"""Fontes de registros: arquivo gravado, ou socket do Draw com reconexao.

Quem consome nao distingue as fontes (mesma regra do PROTOCOL do Draw).
O Draw escuta e o drawlink conecta, entao o drawlink e quem reconecta;
o Draw reinicia com frequencia e o stream recomeca por um FULL.
"""

import socket
import time
from urllib.parse import urlparse

from draw.protocol import AuthenticationError, ProtocolError, client_handshake
from draw.stream import DEFAULT_PORT, iter_records, records_from_file

BACKOFF_START_S = 0.25
BACKOFF_MAX_S = 5.0
CONNECT_TIMEOUT_S = 5.0
# Janela de recv sem dados: o Draw so publica quando a tinta muda, entao
# silencio e normal; o timeout so deixa o laco checar o cancelamento.
IDLE_TIMEOUT_S = 1.0


def parse_endpoint(spec):
    """'tcp://host[:porta]' -> (host, porta); None se nao for tcp."""
    if not spec.startswith("tcp://"):
        return None
    parsed = urlparse(spec)
    if not parsed.hostname:
        raise ValueError(f"endpoint sem host: {spec!r}")
    return parsed.hostname, parsed.port or DEFAULT_PORT


def from_file(path):
    """Registros de um arquivo gravado (--stream-record do Draw)."""
    return records_from_file(path)


def _read_socket(sock, should_stop):
    def read(n):
        while True:
            try:
                return sock.recv(n)
            except TimeoutError:
                if should_stop():
                    return b""

    return iter_records(read)


def from_socket(
    host,
    port,
    token,
    *,
    should_stop=lambda: False,
    sleep=time.sleep,
    on_event=lambda _msg: None,
):
    """Gera (registro, nova_conexao) de forma indefinida, reconectando.

    nova_conexao e True no primeiro registro de cada conexao: o consumidor
    zera o estado, porque o stream recomeca por um FULL. Falha de conexao e
    queda do Draw voltam a tentar com backoff exponencial; stream corrompido
    (ProtocolError) tambem reconecta, porque o v1 nao ressincroniza.
    Token recusado (AuthenticationError) NAO e retentado: repetir so enche o
    log do Draw e o erro e de configuracao.
    """
    delay = BACKOFF_START_S
    while not should_stop():
        try:
            sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_S)
        except OSError as error:
            on_event(f"sem conexao com {host}:{port} ({error}); tentando de novo")
            sleep(delay)
            delay = min(delay * 2, BACKOFF_MAX_S)
            continue
        try:
            try:
                client_handshake(sock, token)
            except AuthenticationError:
                raise
            except OSError as error:
                on_event(f"handshake falhou ({error}); tentando de novo")
                sleep(delay)
                delay = min(delay * 2, BACKOFF_MAX_S)
                continue
            sock.settimeout(IDLE_TIMEOUT_S)
            delay = BACKOFF_START_S
            first = True
            try:
                for record in _read_socket(sock, should_stop):
                    yield record, first
                    first = False
            except ProtocolError as error:
                on_event(f"stream invalido ({error}); reconectando")
            except OSError as error:
                on_event(f"conexao caiu ({error}); reconectando")
            else:
                if not should_stop():
                    on_event("Draw fechou a conexao; reconectando")
        finally:
            sock.close()
        if not should_stop():
            sleep(delay)
            delay = min(delay * 2, BACKOFF_MAX_S)
