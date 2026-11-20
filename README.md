# drawlink

Ponte ao vivo entre o stream de tinta do [Draw](https://github.com/santleme/draw-core)
e vetor/CAD/cursor. Processo separado: se o drawlink cair, o Draw segue
digitalizando (o publicador do Draw nunca bloqueia o laço).

Contrato do stream: `bridge/STREAM.md` no draw-core (protocolo `DRAL` v1).

## Estado

- **M1 (esta etapa):** leitor de stream (arquivo gravado ou socket, com
  reconexão e backoff), `Session` que aplica FULL/DELTA e sinaliza página nova.
- M2 vetorização incremental, M3 SVG ao vivo, M4 DXF, M5 cursor (Windows):
  ainda não.

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
