"""Sessao + vetorizacao incremental: do registro DRAL ao delta de vetores."""

from draw.stream import InkRecord

from .incremental import IncrementalVectorizer, VectorDelta
from .session import Session, Update


class LiveVectorizer:
    """Aplica registros e mantem as polilinhas da pagina atual.

    Pagina nova (`Update.reset`): o delta remove as ilhas da pagina antiga e
    traz as da nova. Quem precisa fechar a pagina (ex.: exportar DXF) usa
    `Update.reset` para saber quando.
    """

    def __init__(self, epsilon=1.5):
        self.session = Session()
        self.vectors = IncrementalVectorizer(epsilon)

    def feed(self, record: InkRecord) -> tuple[Update, VectorDelta]:
        update = self.session.feed(record)
        # Sem reset na pagina nova: o update reconcilia por chave de conteudo,
        # entao o que sumiu da mascara sai e o que e identico permanece.
        return update, self.vectors.update(self.session.mask)

    def clear(self):
        """Nova conexao: o stream recomeca por um FULL."""
        self.session.clear()
        self.vectors.reset()
