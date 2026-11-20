"""Vetorizacao incremental (drawlink.incremental) contra o batch do draw-core."""

import cv2
import numpy as np
from draw import stream
from draw.exporters.vectorize import vectorize_clean

from drawlink import cli
from drawlink.incremental import IncrementalVectorizer
from drawlink.live import LiveVectorizer

H, W = 1200, 850


def _raster(lines, shape=(H, W)):
    img = np.zeros(shape, np.uint8)
    for line in lines:
        pts = np.round(np.asarray(line["points"])).astype(np.int32).reshape(-1, 1, 2)
        cv2.polylines(img, [pts], bool(line["closed"]), 255, 1)
    return img


def _deviation(a, b):
    """Maior distancia (px) de um conjunto de polilinhas ao outro, nos dois sentidos."""
    ra, rb = _raster(a), _raster(b)
    out = 0.0
    for src, dst in ((ra, rb), (rb, ra)):
        if not (src > 0).any():
            continue
        if not (dst > 0).any():
            return float("inf")
        dist = cv2.distanceTransform(255 - dst, cv2.DIST_L2, 3)
        out = max(out, float(dist[src > 0].max()))
    return out


def _scribble(seed, strokes):
    rng = np.random.default_rng(seed)
    mask = np.zeros((H, W), np.uint8)
    for _ in range(strokes):
        kind = int(rng.integers(0, 3))
        px, py = (int(v) for v in (rng.integers(40, 800), rng.integers(40, 1150)))
        if kind == 0:
            cv2.line(
                mask, (px, py), (px + int(rng.integers(-150, 150)), py + 90), 255, 3
            )
        elif kind == 1:
            cv2.circle(mask, (px, py), int(rng.integers(10, 60)), 255, 3)
        else:
            cv2.rectangle(
                mask, (px, py), (px + int(rng.integers(20, 120)), py + 70), 255, 3
            )
    return mask


def _one_component(seed):
    """Uma pilha de segmentos; pode encostar na borda da pagina."""
    rng = np.random.default_rng(seed)
    mask = np.zeros((H, W), np.uint8)
    start = [int(rng.integers(0, W)), int(rng.integers(0, H))]
    pts = np.cumsum(rng.integers(-60, 60, (8, 2)), axis=0) + start
    cv2.polylines(
        mask, [pts.astype(np.int32).reshape(-1, 1, 2)], bool(seed % 2), 255, 3
    )
    count = cv2.connectedComponents((mask > 0).astype(np.uint8), connectivity=8)[0] - 1
    return mask if count == 1 else None


def test_single_component_matches_batch_geometry():
    checked = 0
    for seed in range(60):
        mask = _one_component(seed)
        if mask is None:
            continue
        batch = vectorize_clean(mask > 0)
        vec = IncrementalVectorizer()
        vec.update(mask)
        assert len(vec.polylines) == len(batch), seed
        assert _deviation(batch, vec.polylines) <= 1.5, seed
        checked += 1
    assert checked >= 30  # a geracao sintetica nao pode secar o teste


def test_multi_component_matches_batch_geometry_within_tolerance():
    # O batch acopla componentes (passadas da poda de espinhos, numeracao do
    # OpenCV): ate ~2,3 px e poucas arestas curtas de diferenca em 40 paginas.
    for seed in range(10):
        mask = _scribble(300 + seed, 25)
        batch = vectorize_clean(mask > 0)
        vec = IncrementalVectorizer()
        vec.update(mask)
        assert abs(len(vec.polylines) - len(batch)) <= 10, seed
        assert _deviation(batch, vec.polylines) <= 3.0, seed


def test_stroke_touching_page_border_matches_batch():
    mask = np.zeros((H, W), np.uint8)
    cv2.line(mask, (700, 300), (W - 1, 380), 255, 3)  # ate a ultima coluna
    cv2.line(mask, (0, 900), (120, 1000), 255, 3)  # ate a primeira
    batch = vectorize_clean(mask > 0)
    vec = IncrementalVectorizer()
    vec.update(mask)
    assert len(vec.polylines) == len(batch)
    assert _deviation(batch, vec.polylines) <= 1.5


def test_new_far_stroke_vectorizes_only_itself_and_keeps_other_islands():
    mask = _scribble(5, 20)
    vec = IncrementalVectorizer()
    vec.update(mask)
    before = {i.key: i for i in vec.islands}
    assert vec.vectorized_last >= len(before)
    mask2 = mask.copy()
    cv2.line(mask2, (5, 5), (60, 25), 255, 3)  # canto vazio, longe do resto
    delta = vec.update(mask2)
    assert vec.vectorized_last == 1
    assert len(delta.added) == 1 and delta.removed == ()
    for key, island in before.items():  # os outros nem foram tocados
        assert any(i is island for i in vec.islands), key


def test_growing_stroke_replaces_its_island():
    mask = np.zeros((H, W), np.uint8)
    cv2.line(mask, (100, 100), (200, 140), 255, 3)
    vec = IncrementalVectorizer()
    vec.update(mask)
    (old,) = vec.islands
    cv2.line(mask, (200, 140), (300, 150), 255, 3)
    delta = vec.update(mask)
    assert delta.removed == (old.key,)
    assert len(delta.added) == 1 and len(vec.islands) == 1
    assert vec.vectorized_last == 1


def test_erasing_removes_islands_and_unchanged_update_is_noop():
    mask = _scribble(9, 6)
    vec = IncrementalVectorizer()
    vec.update(mask)
    assert vec.islands
    again = vec.update(mask)
    assert not again and vec.vectorized_last == 0  # nada mudou: nada reprocessado
    delta = vec.update(np.zeros_like(mask))
    assert not vec.islands and vec.polylines == []
    assert len(delta.removed) > 0 and delta.added == ()


def test_island_result_is_translation_invariant():
    def draw(dx, dy):
        mask = np.zeros((H, W), np.uint8)
        cv2.circle(mask, (200 + dx, 300 + dy), 40, 255, 3)
        cv2.line(mask, (160 + dx, 300 + dy), (260 + dx, 350 + dy), 255, 3)
        return mask

    a, b = IncrementalVectorizer(), IncrementalVectorizer()
    a.update(draw(0, 0))
    b.update(draw(250, 400))
    la, lb = a.polylines, b.polylines
    assert len(la) == len(lb)
    for x, y in zip(la, lb, strict=True):
        assert np.allclose(x["points"] + [250, 400], y["points"])


def test_microfragment_is_cached_without_polylines():
    mask = np.zeros((H, W), np.uint8)
    mask[100:102, 100:102] = 255  # 4 px: no limite do filtro de microfragmentos
    mask[300, 300] = 255  # 1 px
    vec = IncrementalVectorizer()
    vec.update(mask)
    first = vec.vectorized_last
    assert first == 2
    vec.update(mask)
    assert vec.vectorized_last == 0  # ate o que nao vira vetor vem do cache


def test_shape_change_and_reset_drop_the_cache():
    vec = IncrementalVectorizer()
    mask = _scribble(2, 4)
    vec.update(mask)
    small = np.zeros((300, 200), np.uint8)
    cv2.line(small, (10, 10), (150, 90), 255, 3)
    vec.update(small)  # pagina de outro tamanho
    assert len(vec.islands) == 1
    vec.reset()
    assert vec.islands == []


def test_finalize_is_exactly_the_batch():
    mask = _scribble(11, 15)
    vec = IncrementalVectorizer()
    vec.update(mask)
    a, b = vec.finalize(mask), vectorize_clean(mask > 0)
    assert len(a) == len(b)
    assert all(
        np.array_equal(x["points"], y["points"]) for x, y in zip(a, b, strict=True)
    )


def test_live_vectorizer_follows_pages(tmp_path):
    first = np.zeros((40, 30), np.uint8)
    first[10:30, 14:17] = 255
    second = np.zeros((40, 30), np.uint8)
    second[5:8, 5:25] = 255
    live = LiveVectorizer()
    u1, d1 = live.feed(_rec(stream.pack_full(1, 1, 0, first, (210.0, 297.0))))
    assert u1.reset and len(d1.added) == 1 and d1.removed == ()
    u2, d2 = live.feed(_rec(stream.pack_full(2, 1, 1, second, (210.0, 297.0))))
    assert u2.reset  # pagina nova
    assert len(d2.added) == 1 and len(d2.removed) == 1  # a antiga sai
    # a mesma pagina de novo: conteudo identico nao e reprocessado
    live.feed(_rec(stream.pack_full(3, 1, 2, second, (210.0, 297.0))))
    assert live.vectors.vectorized_last == 0


def _rec(data):
    import io

    return next(stream.iter_records(io.BytesIO(data).read))


def test_cli_vectors_flag_prints_island_counts(tmp_path):
    import io

    mask = np.zeros((40, 30), np.uint8)
    mask[10:30, 14:17] = 255
    path = tmp_path / "v.drawink"
    path.write_bytes(stream.pack_full(1, 1, 0, mask, (210.0, 297.0)))
    out = io.StringIO()
    cli.watch(str(path), out=out, vectors=True)
    assert "ilhas=1" in out.getvalue() and "vetorizadas=1" in out.getvalue()
    out2 = io.StringIO()
    cli.watch(str(path), out=out2)
    assert "ilhas" not in out2.getvalue()
