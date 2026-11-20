"""Estado do lado consumidor: aplica registros DRAL e sinaliza geracao nova."""

import zlib
from dataclasses import dataclass

from draw.protocol import ProtocolError
from draw.stream import InkCanvas, InkRecord


@dataclass(frozen=True, slots=True)
class Update:
    """Resultado de aplicar um registro.

    box: retangulo (x, y, w, h) alterado na grade da pagina. reset: True
    quando a geracao mudou (pagina nova) — quem vetoriza descarta o que
    tinha. generation e ink_version identificam a publicacao.

    DRAL v2: captured_at_us e o carimbo de captura do quadro da ultima
    passada (None sem dado, e sempre None em gravacao v1), no mesmo relogio de
    ts_us; latency_us = ts_us - captured_at_us e a latencia captura->publicacao
    (so o trecho dentro do Draw: nao inclui camera nem este lado). anchor_corners
    e o quad da ancora (4, 2) float32 em px da camera, ou None.
    """

    box: tuple[int, int, int, int]
    reset: bool
    generation: int
    ink_version: int
    ts_us: int
    captured_at_us: int | None = None
    anchor_corners: object = None

    @property
    def latency_us(self):
        if self.captured_at_us is None:
            return None
        return self.ts_us - self.captured_at_us


class Session:
    """Mantem a mascara confirmada a partir de registros FULL/DELTA."""

    def __init__(self):
        self._canvas = InkCanvas()

    @property
    def mask(self):
        return self._canvas.mask

    @property
    def paper_mm(self):
        return self._canvas.paper_mm

    @property
    def anchor_corners(self):
        return self._canvas.anchor_corners

    @property
    def generation(self):
        return self._canvas.generation

    def feed(self, record: InkRecord) -> Update:
        previous = self._canvas.generation
        try:
            box = self._canvas.apply(record)
        except (zlib.error, ValueError) as error:
            # Defesa em profundidade: o codec ja converte, mas payload
            # invalido nunca pode escapar como excecao crua.
            raise ProtocolError(f"registro invalido: {error}") from error
        return Update(
            box=box,
            reset=previous != record.generation,
            generation=record.generation,
            ink_version=record.ink_version,
            ts_us=record.ts_us,
            captured_at_us=self._canvas.captured_at_us,
            anchor_corners=self._canvas.anchor_corners,
        )

    def clear(self):
        """Esquece tudo (nova conexao: o stream recomeca por um FULL)."""
        self._canvas = InkCanvas()
