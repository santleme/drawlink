"""Sessao + vetorizacao incremental: do registro DRAL ao delta de vetores."""

from draw_dral.stream import InkRecord

from .incremental import IncrementalVectorizer, VectorDelta
from .session import Session, Update


class LiveVectorizer:
    """Aplica registros e mantem as polilinhas da pagina atual.

    Contrato do delta: aplique `removed` e depois `added`.
    - Registro normal: delta incremental (so o que mudou).
    - Pagina nova (`Update.reset`): substituicao COMPLETA. `removed` traz todas
      as ilhas da pagina antiga e `added` todas as da nova, inclusive as de
      conteudo identico (mesma chave nos dois lados). Assim vale tanto aplicar
      o delta quanto limpar a tela no reset e desenhar `added`; o conteudo
      identico nao e revetorizado, so reentregue.
    """

    def __init__(self, epsilon=1.5):
        self.session = Session()
        self.vectors = IncrementalVectorizer(epsilon)

    def feed(self, record: InkRecord) -> tuple[Update, VectorDelta]:
        update = self.session.feed(record)
        before = tuple(self.vectors._live)
        delta = self.vectors.update(self.session.mask)
        if update.reset:
            delta = VectorDelta(added=tuple(self.vectors.islands), removed=before)
        return update, delta

    def clear(self):
        """Nova conexao: o stream recomeca por um FULL."""
        self.session.clear()
        self.vectors.reset()
