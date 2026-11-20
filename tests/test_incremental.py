"""Vetorizacao incremental (drawlink.incremental) contra o batch do draw-core."""

import cv2
import numpy as np
from draw import stream
from draw.exporters.vectorize import vectorize_clean

from drawlink import cli
from drawlink.incremental import IncrementalVectorizer, window
from drawlink.live import LiveVectorizer

H, W = 1200, 850


def _raster(lines, shape=(H, W)):
    img = np.zeros(shape, np.uint8)
    for line in lines:
        pts = np.round(np.asarray(line["points"])).astype(np.int32).reshape(-1, 1, 2)
        if len(pts) == 1:  # ponto isolado: o exporter do Draw desenha um ponto
            x, y = pts[0, 0]
            cv2.circle(img, (int(x), int(y)), 1, 255, -1)
        else:
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
    # a mesma pagina em geracao nova: substituicao completa, sem revetorizar
    old_keys = {i.key for i in live.vectors.islands}
    u3, d3 = live.feed(_rec(stream.pack_full(3, 1, 2, second, (210.0, 297.0))))
    assert u3.reset and live.vectors.vectorized_last == 0
    assert set(d3.removed) == old_keys  # remove tudo da antiga...
    assert {i.key for i in d3.added} == old_keys  # ...e reentrega o identico
    # contrato: aplicar removed e depois added reconstroi o estado certo
    state = set(old_keys)
    state -= set(d3.removed)
    state |= {i.key for i in d3.added}
    assert state == {i.key for i in live.vectors.islands}


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


def test_deviation_sees_isolated_dots():
    dot_a = [{"points": np.array([[10.0, 10.0]]), "closed": False}]
    dot_b = [{"points": np.array([[400.0, 500.0]]), "closed": False}]
    assert _deviation(dot_a, dot_a) == 0
    assert _deviation(dot_a, dot_b) > 100  # mover um ponto nao passa despercebido


def test_window_origin_is_even_and_clipped_to_the_page():
    for x in range(0, 40):
        for y in range(0, 40):
            x0, y0, x1, y1 = window(x, y, 20, 20, (H, W))
            assert x0 % 2 == 0 and y0 % 2 == 0
            assert 0 <= x0 <= max(0, x - 3) and 0 <= y0 <= max(0, y - 3)
            assert x1 <= W and y1 <= H
    assert window(W - 10, H - 10, 10, 10, (H, W))[2:] == (W, H)  # corta na borda


def test_resize_and_reset_still_report_removals():
    mask = _scribble(4, 8)
    vec = IncrementalVectorizer()
    vec.update(mask)
    old_keys = {i.key for i in vec.islands}
    assert old_keys
    small = np.zeros((300, 200), np.uint8)
    cv2.line(small, (10, 10), (150, 90), 255, 3)
    delta = vec.update(small)  # FULL de outro tamanho, mesma geracao
    assert set(delta.removed) == old_keys  # o consumidor ainda e avisado
    assert len(delta.added) == 1
    key = vec.islands[0].key
    delta2 = vec.update(small, reset=True)  # reset invalida o cache, nao o delta
    assert vec.vectorized_last == 1
    assert delta2.added == () and delta2.removed == ()  # mesmo conteudo: nada mudou
    assert vec.islands[0].key == key


def _spurred_tree(seed):
    """Arvore de tracos grossos com espinhos curtos, num canvas 600x600.

    Espessura 4-14 e espinhos entre ~t e ~2,8t: o limite de poda e
    max(7, 2,4*r), so sensivel ao pixel consultado quando r > 3 e o espinho cai
    perto do limite.
    """
    rng = np.random.default_rng(seed)
    mask = np.zeros((600, 600), np.uint8)
    t = int(rng.integers(4, 15))
    x, y = int(rng.integers(150, 450)), int(rng.integers(150, 450))
    pts = [(x, y)]
    for _ in range(int(rng.integers(3, 7))):
        a, length = rng.uniform(0, 6.28), rng.integers(25, 70)
        nx, ny = int(x + length * np.cos(a)), int(y + length * np.sin(a))
        cv2.line(mask, (x, y), (nx, ny), 255, t)
        pts.append((nx, ny))
        x, y = nx, ny
    for _ in range(int(rng.integers(2, 6))):
        px, py = pts[int(rng.integers(0, len(pts)))]
        a = rng.uniform(0, 6.28)
        length = int(rng.integers(int(t), int(2.8 * t) + 6))
        end = (int(px + length * np.cos(a)), int(py + length * np.sin(a)))
        cv2.line(mask, (px, py), end, 255, t)
    return mask


def test_half_pixel_junction_crop_keeps_the_same_spurs_as_batch():
    # Regressao: a poda consulta round() de centroides de meio pixel, e o
    # arredondamento do Python (para o par) depende da paridade da coordenada.
    # Estas sementes divergem do batch quando o recorte tem origem impar (17 de
    # 3000 arvores) e batem com origem par.
    for seed in (13, 785, 820, 847, 943, 1362):
        mask = _spurred_tree(seed)
        batch = vectorize_clean(mask > 0)
        vec = IncrementalVectorizer()
        vec.update(mask)
        ys, xs = np.nonzero(mask)
        assert (xs.min() - 3) % 2 or (ys.min() - 3) % 2  # origem natural impar
        assert len(vec.polylines) == len(batch), seed
        assert _deviation(batch, vec.polylines) <= 1.5, seed


def test_same_generation_resize_replaces_border_strokes_geometry():
    """FULL de outro tamanho na mesma geracao: o traco que deixa de encostar na
    borda e revetorizado com outra geometria e o consumidor precisa recebe-la."""
    narrow = np.zeros((40, 30), np.uint8)
    narrow[5:35, 26:30] = 255  # 4 px de largura, colada na borda direita
    wide = np.zeros((40, 40), np.uint8)
    wide[5:35, 26:30] = 255  # mesmos pixels, agora no interior
    live = LiveVectorizer()
    consumer = {}

    def apply(delta):
        for key in delta.removed:
            consumer.pop(key)
        for island in delta.added:
            consumer[island.key] = island.polylines

    _, d1 = live.feed(_rec(stream.pack_full(1, 1, 0, narrow, (210.0, 297.0))))
    apply(d1)
    before = [np.array(line["points"]) for line in live.vectors.polylines]
    _, d2 = live.feed(_rec(stream.pack_full(1, 2, 1, wide, (210.0, 297.0))))
    apply(d2)
    after = [np.array(line["points"]) for line in live.vectors.polylines]
    # premissa do cenario: a geometria de fato muda quando deixa de ser borda
    assert len(before) != len(after) or not all(
        a.shape == b.shape and np.allclose(a, b)
        for a, b in zip(before, after, strict=True)
    )
    # o consumidor, aplicando so os deltas, tem exatamente a geometria atual
    mine = sorted(
        (
            tuple(map(tuple, np.round(line["points"], 3)))
            for polys in consumer.values()
            for line in polys
        )
    )
    truth = sorted(
        tuple(map(tuple, np.round(line["points"], 3)))
        for line in live.vectors.polylines
    )
    assert mine == truth
    assert set(consumer) == {i.key for i in live.vectors.islands}


def _hole_with_speck(speck):
    mask = np.zeros((60, 60), np.uint8)
    mask[20:33, 20:33] = 255
    mask[24:27, 24:27] = 0  # buraco 3x3 (9 px)
    mask[25, 27] = 0  # reentrancia: o buraco passa a 10 px (acima do limite de 9)
    if speck:
        mask[25, 25] = 255  # pinta isolada no centro: o buraco cai para 9
    return mask


def test_speck_inside_a_hole_fills_it_like_the_batch():
    # O batch preenche buracos antes de remover poeira: a pinta leva o buraco ao
    # limite e ele vira tinta (1 polilinha aberta); sem a pinta o buraco fica
    # (1 laco fechado). Recortar so o componente perdia a pinta.
    for speck in (False, True):
        mask = _hole_with_speck(speck)
        batch = vectorize_clean(mask > 0)
        vec = IncrementalVectorizer()
        vec.update(mask)
        assert len(vec.polylines) == len(batch), speck
        assert [bool(x["closed"]) for x in vec.polylines] == [
            bool(x["closed"]) for x in batch
        ], speck
        assert _deviation(batch, vec.polylines) <= 1.5, speck


def test_speck_arriving_in_a_hole_replaces_the_cached_loop_in_the_delta():
    # Com cache aquecido: a pinta chega e o laco em cache (buraco de 10 px) vira
    # um traco preenchido. O delta tem de remover o laco e entregar o novo.
    without, with_speck = _hole_with_speck(False), _hole_with_speck(True)
    vec = IncrementalVectorizer()
    vec.update(without)
    (loop,) = vec.islands
    assert any(line["closed"] for line in loop.polylines)
    delta = vec.update(with_speck)
    assert delta.removed == (loop.key,)
    assert len(delta.added) == 1 and delta.added[0].key != loop.key
    assert not any(line["closed"] for line in delta.added[0].polylines)
    assert vec.vectorized_last == 1
    # e a pinta saindo devolve o laco (o delta volta a trocar)
    back = vec.update(without)
    assert len(back.added) == 1 and back.removed == (delta.added[0].key,)
    assert any(line["closed"] for line in back.added[0].polylines)
