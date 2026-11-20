"""Fontes de registros: arquivo gravado, ou socket do Draw com reconexao.

Quem consome nao distingue as fontes (mesma gramatica no socket e no arquivo).
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
# O Draw fecha sem responder tanto para token errado quanto para um restart no
# meio do handshake: o cliente nao distingue. Falhas seguidas acima disto sao
# tratadas como erro de configuracao.
MAX_AUTH_FAILURES = 3


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


def follow(
    host,
    port,
    token,
    on_record,
    *,
    should_stop=lambda: False,
    sleep=time.sleep,
    on_event=lambda _msg: None,
):
    """Segue o stream do Draw, reconectando, ate on_record pedir parada.

    on_record(registro, nova_conexao) -> truthy para parar. nova_conexao e
    True no primeiro registro de cada conexao (o stream recomeca por um FULL,
    entao o consumidor zera o estado). on_record roda DENTRO do laco de
    reconexao: um ProtocolError levantado por ele (payload invalido) derruba a
    conexao e reconecta, em vez de matar o seguimento. O backoff so volta ao
    inicio depois que a conexao entrega um registro; conexoes que caem antes
    disso seguem escalando. Falhas de autenticacao seguidas
    (MAX_AUTH_FAILURES) levantam AuthenticationError.
    """
    delay = BACKOFF_START_S
    auth_failures = 0

    def backoff():
        nonlocal delay
        sleep(delay)
        delay = min(delay * 2, BACKOFF_MAX_S)

    while not should_stop():
        try:
            sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_S)
        except OSError as error:
            on_event(f"sem conexao com {host}:{port} ({error}); tentando de novo")
            backoff()
            continue
        try:
            try:
                client_handshake(sock, token)
            except AuthenticationError as error:
                auth_failures += 1
                if auth_failures >= MAX_AUTH_FAILURES:
                    raise
                on_event(f"handshake recusado ({error}); tentando de novo")
                backoff()
                continue
            except OSError as error:
                on_event(f"handshake falhou ({error}); tentando de novo")
                backoff()
                continue
            auth_failures = 0
            sock.settimeout(IDLE_TIMEOUT_S)
            first = True
            try:
                for record in _read_socket(sock, should_stop):
                    new_connection = first
                    first = False
                    if new_connection:
                        delay = BACKOFF_START_S
                    if on_record(record, new_connection):
                        return
                    if should_stop():
                        return
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
            backoff()
