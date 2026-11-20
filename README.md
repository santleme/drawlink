# drawlink

Ponte ao vivo entre o stream de tinta do [Draw](https://github.com/santleme/draw-core)
e vetor/CAD/cursor. Processo separado: se o drawlink cair, o Draw segue
digitalizando (o publicador do Draw nunca bloqueia o laço).

Contrato do stream: `bridge/STREAM.md` no draw-core (protocolo `DRAL` v1).

## Estado

- **M1:** leitor de stream (arquivo gravado ou socket, com reconexão e
  backoff), `Session` que aplica FULL/DELTA e sinaliza página nova.
- **M2:** vetorização incremental por componente conexo
  (`drawlink.incremental`, `drawlink.live`), `drawlink watch --vectors`.
- M3 SVG ao vivo, M4 DXF, M5 cursor (Windows): ainda não.

## Vetorização incremental (M2)

O `vectorize_clean` do draw-core é offline (a página inteira a cada chamada).
Aqui cada componente conexo é vetorizado sozinho e identificado por uma chave de
conteúdo (caixa + hash dos pixels); só o que mudou é vetorizado, o resto vem do
cache. `update()` devolve um `VectorDelta` (ilhas novas e chaves removidas).

Medido (página 850x1200, esta máquina):

| traços | batch | update com 1 traço novo |
|-------:|------:|------------------------:|
| 30     | 99 ms | 1,6 ms |
| 120    | 574 ms | 2,1 ms |
| 300    | 8,9 s | 3,4 ms |

**Limite conhecido:** o custo de um update é proporcional ao componente conexo
que ele toca. Um traço que encosta num componente que cobre a página inteira
(medido: 510 polilinhas interligadas) custa ~5 s, perto do batch. Escrita
normal e formulários esparsos ficam longe disso; uma grade contínua não. Cortar
isso exigiria vetorizar por trechos dentro do componente (não feito).

**Equivalência com o batch (`vectorize_clean`)** é geométrica, não bit a bit,
e os testes fixam os limites medidos: com um componente, mesma contagem de
polilinhas e desvio <= 1,5 px (medido 0,95 px em 115 páginas); com vários,
desvio <= 3 px (medido 2,3 px) e até ~7 arestas curtas de diferença. Causas,
ambas do batch e não do recorte: o controle de passadas da poda de espinhos é
global (um traço pode ser podado diferente por causa de outro, longe dele), e
a numeração de componentes do OpenCV muda a orientação de arestas curtas
conforme a geometria da imagem. Efeito colateral útil: no incremental um traço
não muda quando se desenha outro em outro ponto da página. Para o export
exato de fim de página (DXF), `IncrementalVectorizer.finalize(mask)` roda o
batch.

## Uso

```bash
# Draw: draw --stream-port 8766 --stream-token-file token.txt
drawlink watch tcp://127.0.0.1:8766 --token-file token.txt
drawlink watch sessao.drawink          # replay (draw --stream-record)
```

## Desenvolvimento

```bash
pip install -e ".[dev]"
ruff check . && ruff format --check . && pytest -q
```

A dependência do `draw-core` está fixada no commit `16b6174` (PR
santleme/draw-core#60); passa para uma tag quando o draw-core publicar uma.
