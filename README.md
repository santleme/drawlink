# drawlink

Ponte ao vivo entre o stream de tinta do [Draw](https://github.com/santleme/draw-core)
e vetor/CAD/cursor. Processo separado: se o drawlink cair, o Draw segue
digitalizando (o publicador do Draw nunca bloqueia o laço).

Contrato do stream: `bridge/STREAM.md` no draw-core (protocolo `DRAL`; o
publicador escreve v2 e o leitor ainda aceita gravações v1).

## Estado

- **M1:** leitor de stream (arquivo gravado ou socket, com reconexão e
  backoff), `Session` que aplica FULL/DELTA e sinaliza página nova.
- **M2:** vetorização incremental por componente conexo
  (`drawlink.incremental`, `drawlink.live`), `drawlink watch --vectors`.
- **DRAL v2:** `Update.captured_at_us`, `Update.anchor_corners` e
  `Update.latency_us` (= `ts_us - captured_at_us`). `watch` imprime `lat=` quando
  há dado. Gravações v1 continuam reproduzindo, sem esses campos (`None`).
- M3 SVG ao vivo, M4 DXF, M5 cursor (Windows): ainda não.

**O que `latency_us` mede e o que não mede:** só o trecho captura -> publicação
dentro do Draw (mesmo relógio monotônico). Não inclui a câmera, o transporte
nem o processamento deste lado, então **não substitui** o Step 0 de latência
vidro->tinta do draw-core, que filma um cronômetro.

## Vetorização incremental (M2)

O `vectorize_clean` do draw-core é offline (a página inteira a cada chamada).
Aqui cada componente conexo é vetorizado sozinho e identificado por uma chave de
conteúdo (tamanho da página + caixa + hash dos pixels); só o que mudou é
vetorizado, o resto vem do cache. O tamanho da página entra na chave de
propósito: o recorte é cortado nos limites dela e o afinamento trata a borda de
forma especial, então redimensionar a página substitui todas as ilhas, mesmo as
de pixels idênticos. `update()` devolve um `VectorDelta` (ilhas novas e chaves removidas).

Contrato do delta: aplique `removed` e depois `added`. Num registro normal o
delta é incremental; numa página nova (`Update.reset`) o `LiveVectorizer`
entrega a substituição completa (`removed` = tudo da página antiga, `added` =
tudo da nova, inclusive o idêntico, sem revetorizar), então tanto aplicar o
delta quanto limpar a tela no reset e desenhar `added` estão corretos. Mudar o
tamanho da página invalida o cache, mas o delta ainda remove as ilhas antigas.

Medido (página 850x1200, esta máquina, contra o draw-core `91916f4`, com o
vetorizador do #65). O batch de 300 traços era 8,9 s antes do #65:

| traços | batch | update (1 traço entra ou sai) |
|-------:|------:|-----------------------------:|
| 30     | 39-67 ms | 3,6-4,4 ms |
| 120    | 116-132 ms | 4,0-4,9 ms |
| 300    | 296-324 ms | 5,5-6,3 ms |

Faixas de 4 repetições (mediana de 30 updates cada). O preenchimento de buracos
da página inteira custa 2,2-3,2 ms, cerca de metade de cada update; o resto é
rotular, hashear e vetorizar só o que mudou. A máquina tem carga variável: uma
execução com ela ocupada mediu 7-9 ms por update, então trate os números como
ordem de grandeza.

Os números mudaram muito com o #65: o batch de 300 traços era 8,9 s com o
vetorizador anterior, e o ganho do incremental caiu de milhares de vezes para
uns 10 a 50.

**Decisão: o incremental fica.** Um batch de ~300 ms numa página cheia
(296-324 ms na tabela) estoura o orçamento por quadro e disputa CPU com o
detector do Draw; um update de 3,6-6,3 ms cabe com folga. A equivalência com o
batch é geométrica (limites abaixo), coberta pelos testes. O motor está
congelado: nem o `fill_pinholes` (2,2-3,2 ms) nem o pior caso abaixo serão
otimizados agora.

**Limite conhecido:** o custo de um update é proporcional ao componente conexo
que ele toca. Um traço que encosta no maior componente (medido: 257 polilinhas
interligadas, 819x928 px) custa ~185 ms, perto do batch dessa mesma página (~270 ms).
Com o vetorizador anterior esse caso custava ~5 s. Cortar isso exigiria
vetorizar por trechos dentro do componente (não feito).

**Buracos pequenos:** o batch preenche buracos de até 9 px antes de remover
poeira, então uma pinta isolada dentro de um buraco de 10 px o leva a 9 e ele
vira tinta. Para o incremental ver o mesmo, os buracos são preenchidos na
página inteira antes de rotular os componentes.

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

A dependência do `draw-core` está fixada no commit `91916f4` (draw-core
#64 DRAL v2 e #65 vetorizador); passa para uma tag quando o draw-core publicar uma.
