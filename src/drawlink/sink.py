"""Sink do SVG ao vivo: do (Update, VectorDelta) ao hub e ao arquivo.

- Hub (visualizador): recebe o delta em elementos por ilha. Quando a escala muda
  (grade ou papel diferentes) todo elemento antigo esta no sistema errado, entao
  o sink remove o que conhece e reenvia todas as ilhas.
- Arquivo: o SVG inteiro e reescrito de forma atomica por uma thread propria, no
  maximo a cada `min_interval_s`, e sempre ao fechar: um update atrasado pelo
  limite nunca fica sem ser escrito.
"""

import threading
import time

from .hub import Hub
from .scale import scale_for
from .svg import (
    DEFAULT_STROKE_MM,
    element_id,
    island_element,
    render_svg_text,
    write_svg_atomic,
)

EMPTY_SVG = '<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg"/>\n'


class SvgLiveSink:
    def __init__(
        self,
        hub=None,
        svg_out=None,
        stroke_mm=DEFAULT_STROKE_MM,
        min_interval_s=0.25,
        clock=time.monotonic,
        log=print,
    ):
        self.hub = hub if hub is not None else Hub()
        self._svg_out = svg_out
        self._stroke = stroke_mm
        self._min_interval = min_interval_s
        self._clock = clock
        self._log = log
        self._scale_key = None
        self._known = set()  # ids de elementos que o hub tem
        self._view = None
        self._cond = threading.Condition()
        self._snapshot = None  # (polilinhas, grade, papel, escala)
        self._dirty = False
        self._closed = False
        self.writes = 0
        self.write_error = (
            None  # ultima falha de escrita, ou None se a ultima deu certo
        )
        self._thread = None
        if svg_out is not None:
            self._thread = threading.Thread(
                target=self._writer, name="drawlink-svg", daemon=True
            )
            self._thread.start()

    def apply(self, live, delta):
        """Aplica o delta do `LiveVectorizer` (chamar depois de `live.feed`)."""
        mask = live.session.mask
        grid = (mask.shape[1], mask.shape[0])
        paper = tuple(float(v) for v in live.session.paper_mm)
        sx, sy = scale_for(*grid, *paper)
        if (grid, paper) != self._scale_key:
            self._scale_key = (grid, paper)
            removed = list(self._known)
            added = list(live.vectors.islands)
        else:
            removed = [element_id(k) for k in delta.removed]
            added = list(delta.added)
        elements = [
            (element_id(i.key), island_element(i, sx, sy, self._stroke)) for i in added
        ]
        self._known.difference_update(removed)
        self._known.update(eid for eid, _ in elements)
        self._view = paper
        self.hub.apply(paper, removed, elements)
        with self._cond:
            self._snapshot = (list(live.vectors.polylines), grid, paper, (sx, sy))
            self._dirty = True
            self._cond.notify()

    def reset(self):
        """Nova conexao com o Draw: esquece o desenho e avisa os visualizadores.

        O stream recomeca por um FULL, e o `LiveVectorizer` esquece as ilhas
        antigas; sem isto o hub manteria na tela as que nao existem mais.
        """
        removed = list(self._known)
        self._known.clear()
        self._scale_key = None
        if removed and self._view is not None:
            self.hub.apply(self._view, removed, [])
        # Viewer, drawing.svg e arquivo contam a mesma historia: o desenho antigo
        # sai de todos, mesmo que o primeiro registro novo ainda nao tenha chegado
        # (ou nunca chegue). Fica um SVG vazio, com o tamanho da pagina.
        with self._cond:
            if self._snapshot is not None:
                _lines, grid, paper, scale = self._snapshot
                self._snapshot = ([], grid, paper, scale)
                self._dirty = True
                self._cond.notify()

    def render_svg(self):
        """O SVG atual como texto (o mesmo `write_svg` do export final)."""
        with self._cond:
            snap = self._snapshot
        if snap is None:
            return EMPTY_SVG
        lines, grid, paper, scale = snap
        return render_svg_text(lines, grid, paper, scale, self._stroke)

    def _writer(self):
        last = None
        while True:
            with self._cond:
                while not self._dirty and not self._closed:
                    self._cond.wait()
                if not self._dirty:
                    return
                if last is not None and not self._closed:
                    wait = self._min_interval - (self._clock() - last)
                    if wait > 0:
                        self._cond.wait(timeout=wait)
                        continue
                snap, self._dirty = self._snapshot, False
            last = self._clock()
            try:
                lines, grid, paper, scale = snap
                write_svg_atomic(lines, self._svg_out, grid, paper, scale, self._stroke)
                self.writes += 1
                self.write_error = None
            except Exception as error:  # noqa: BLE001 - a escrita nunca derruba o stream
                self.write_error = error
                self._log(f"drawlink: falha ao escrever {self._svg_out}: {error}")

    def close(self):
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=10)
        self.hub.close()
