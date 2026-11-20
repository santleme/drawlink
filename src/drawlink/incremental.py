"""Vetorizacao incremental da tinta confirmada, por componente conexo.

O vetorizador do draw-core (`vectorize_clean`) e offline: afina e percorre a
mascara inteira a cada chamada (segundos numa pagina cheia). A tinta confirmada
so cresce ou some em pontos isolados, e cada componente conexo (8-vizinhos) se
vetoriza sozinho: o afinamento e local, a poda de espinhos, a costura de juncoes
e o filtro de microfragmentos agem por esqueleto/componente. Entao:

1. rotula os componentes da mascara (barato: milissegundos);
2. identifica cada um por (caixa, hash dos pixels) — chave de conteudo;
3. so vetoriza os componentes cuja chave ainda nao existe; o resto vem do cache.

A caixa suja do stream nao e necessaria para a correcao (a chave de conteudo
pega qualquer mudanca), por isso `update` recebe so a mascara.
"""

import hashlib
from dataclasses import dataclass

import cv2
import numpy as np
from draw.exporters.vectorize import vectorize_clean

# Folga ao redor do componente recortado: o afinamento e a deteccao de grau
# olham a vizinhanca 3x3, e a borda do recorte nao pode parecer borda de imagem
# para o algoritmo. A folga e cortada nos limites da pagina (nunca ha pixels
# alem deles), para o lado que toca a borda real se comportar como na imagem
# inteira — o vetorizador trata a borda da imagem de forma especial.
PAD = 3


def window(x, y, w, h, shape):
    """Janela (x0, y0, x1, y1) de recorte de um componente na pagina.

    Folga PAD cortada nos limites da pagina, e origem (x0, y0) sempre PAR: a
    poda de espinhos consulta o pixel round() de centroides de meio pixel, e o
    arredondamento do Python (para o par) depende da paridade da coordenada.
    Com origem par a paridade e a mesma do batch e o pixel consultado e o mesmo.
    """
    height, width = shape
    x0, y0 = max(0, x - PAD), max(0, y - PAD)
    x0 -= x0 % 2
    y0 -= y0 % 2
    return x0, y0, min(width, x + w + PAD), min(height, y + h + PAD)


@dataclass(frozen=True, slots=True)
class Island:
    """Um componente conexo de tinta e suas polilinhas (px da pagina).

    key identifica o conteudo exato do componente: mudou um pixel, mudou a
    chave. polylines: tupla de {"points": ndarray (N, 2) [x, y], "closed": bool}.
    """

    key: tuple
    bbox: tuple[int, int, int, int]  # x, y, w, h
    polylines: tuple


@dataclass(frozen=True, slots=True)
class VectorDelta:
    """O que mudou desde o update anterior (so ilhas com polilinhas)."""

    added: tuple[Island, ...]
    removed: tuple[tuple, ...]  # chaves de ilhas que sumiram

    def __bool__(self):
        return bool(self.added or self.removed)


class IncrementalVectorizer:
    def __init__(self, epsilon=1.5):
        self._epsilon = epsilon
        self._cache = {}  # chave -> Island (inclui ilhas sem polilinhas)
        self._live = {}  # chave -> Island com polilinhas, na pagina atual
        self._shape = None
        self.vectorized_last = 0  # quantas ilhas foram vetorizadas no ultimo update

    @property
    def islands(self):
        """Ilhas com polilinhas, em ordem estavel (topo-esquerda)."""
        return sorted(self._live.values(), key=lambda i: (i.bbox[1], i.bbox[0], i.key))

    @property
    def polylines(self):
        return [line for island in self.islands for line in island.polylines]

    def finalize(self, mask):
        """Vetorizacao batch (`vectorize_clean`) da mascara inteira.

        O incremental e deterministico por componente e equivalente ao batch
        em geometria (ver tests), mas nao bit a bit: o batch acopla componentes
        (o controle de passadas da poda de espinhos e global) e a numeracao de
        componentes do OpenCV muda a orientacao de arestas curtas conforme a
        geometria da imagem. Para um export final (fim de pagina, DXF) use este
        metodo para obter exatamente o que o Draw exportaria.
        """
        return vectorize_clean(mask > 0, epsilon=self._epsilon)

    def reset(self):
        self._cache.clear()
        self._live.clear()
        self._shape = None
        self.vectorized_last = 0

    def update(self, mask, reset=False):
        """Reconcilia com a mascara atual e devolve o VectorDelta.

        Quem consome aplica `removed` e depois `added`. Mudar o tamanho da
        pagina ou passar reset=True invalida o cache (tudo e revetorizado), mas
        o delta continua relativo ao estado anterior.
        """
        if reset or (self._shape is not None and mask.shape != self._shape):
            # Pagina de outro tamanho (ou pedido explicito): as chaves em cache
            # nao valem. `_live` fica de proposito, para o delta ainda remover
            # as ilhas antigas; reset() e quem esquece tudo.
            self._cache.clear()
        self._shape = mask.shape
        ink = np.ascontiguousarray(mask > 0, dtype=np.uint8)
        count, labels, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
        live = {}
        fresh = {}
        vectorized = 0
        for label in range(1, count):
            x, y, w, h, _area = (int(v) for v in stats[label])
            region = labels[y : y + h, x : x + w] == label
            digest = hashlib.blake2b(np.packbits(region).tobytes(), digest_size=12)
            key = (x, y, w, h, digest.digest())
            island = self._cache.get(key)
            if island is None:
                island = self._build(key, labels, label, x, y, w, h)
                vectorized += 1
            fresh[key] = island
            if island.polylines:
                live[key] = island
        self.vectorized_last = vectorized
        # O cache guarda so o que existe agora: ilhas que sumiram saem dele.
        self._cache = fresh
        added = tuple(live[k] for k in live if k not in self._live)
        removed = tuple(k for k in self._live if k not in live)
        self._live = live
        return VectorDelta(added=added, removed=removed)

    def _build(self, key, labels, label, x, y, w, h):
        x0, y0, x1, y1 = window(x, y, w, h, labels.shape)
        crop = labels[y0:y1, x0:x1] == label  # so este componente
        offset = np.array([x0, y0], dtype=np.float64)
        shifted = tuple(
            {
                "points": np.asarray(line["points"], dtype=np.float64) + offset,
                "closed": bool(line["closed"]),
            }
            for line in vectorize_clean(crop, epsilon=self._epsilon)
        )
        return Island(key=key, bbox=(x, y, w, h), polylines=shifted)
