"""Hub de eventos do SVG ao vivo: estado atual + difusao para os visualizadores.

Guarda os elementos SVG por id e entrega a cada assinante uma fila limitada.
Quem apply() nunca espera por um visualizador lento: se a fila de alguem enche,
aquele assinante e derrubado (o navegador reconecta e recebe um snapshot novo).
"""

import json
import queue
import threading

QUEUE_SIZE = 256
CLOSED = object()


class Hub:
    def __init__(self):
        self._lock = threading.Lock()
        self._elements = {}  # id -> elemento SVG (ordem de insercao)
        self._view = None  # (largura_mm, altura_mm)
        self._version = 0
        self._subscribers = set()
        self._closed = False

    @property
    def version(self):
        with self._lock:
            return self._version

    def apply(self, view, removed, added):
        """Aplica 'removed' e depois 'added' e difunde o delta.

        view: (w_mm, h_mm). removed: ids. added: lista de (id, elemento).
        """
        with self._lock:
            if self._closed:
                return
            for element_id in removed:
                self._elements.pop(element_id, None)
            for element_id, element in added:
                self._elements[element_id] = element
            self._view = tuple(view)
            self._version += 1
            event = (
                "delta",
                self._version,
                {
                    "v": self._version,
                    "view": list(self._view),
                    "remove": list(removed),
                    "add": [[i, e] for i, e in added],
                },
            )
            for sub in list(self._subscribers):
                try:
                    sub.queue.put_nowait(event)
                except queue.Full:
                    self._subscribers.discard(sub)
                    sub.close_overflow()

    def snapshot_event(self):
        """Evento 'snapshot' com o estado inteiro (chamar com o lock)."""
        return (
            "snapshot",
            self._version,
            {
                "v": self._version,
                "view": None if self._view is None else list(self._view),
                "add": [[i, e] for i, e in self._elements.items()],
            },
        )

    def snapshot(self):
        with self._lock:
            return self.snapshot_event()

    def subscribe(self):
        """Assinante com o snapshot ja na fila, atomico com a inscricao."""
        sub = Subscriber()
        with self._lock:
            if self._closed:
                sub.queue.put_nowait(CLOSED)
                return sub
            sub.queue.put_nowait(self.snapshot_event())
            self._subscribers.add(sub)
        return sub

    def unsubscribe(self, sub):
        with self._lock:
            self._subscribers.discard(sub)

    def subscriber_count(self):
        with self._lock:
            return len(self._subscribers)

    def close(self):
        with self._lock:
            self._closed = True
            subs, self._subscribers = list(self._subscribers), set()
        for sub in subs:
            sub.close_overflow()


class Subscriber:
    def __init__(self):
        self.queue = queue.Queue(maxsize=QUEUE_SIZE)

    def close_overflow(self):
        """Esvazia a fila e deixa so o fim: o leitor encerra e o cliente reconecta."""
        try:
            while True:
                self.queue.get_nowait()
        except queue.Empty:
            pass
        try:
            self.queue.put_nowait(CLOSED)
        except queue.Full:  # nao deveria: acabamos de esvaziar
            pass


def encode_event(event):
    """(nome, id, dados) -> bytes de um evento SSE."""
    name, event_id, data = event
    return (
        f"event: {name}\nid: {event_id}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"
    ).encode()
