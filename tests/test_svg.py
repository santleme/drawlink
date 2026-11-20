"""Escala travada e SVG ao vivo contra o write_svg do draw-core."""

import os
import re

import numpy as np
import pytest
from draw import config
from draw.exporters.export_vectors import write_svg

from drawlink.incremental import Island
from drawlink.scale import scale_for
from drawlink.svg import (
    element_id,
    island_element,
    polyline_element,
    render_svg_text,
    write_svg_atomic,
)


def _lines():
    return [
        {
            "points": np.array([[10.0, 20.0], [30.5, 40.25], [60.0, 20.0]]),
            "closed": False,
        },
        {
            "points": np.array([[100.0, 100.0], [140.0, 100.0], [120.0, 140.0]]),
            "closed": True,
        },
        {"points": np.array([[200.0, 300.0]]), "closed": False},
    ]


def test_scale_is_uniform_for_every_known_format_and_orientation():
    for name in config.FORMATS:
        for orientation in config.ORIENTATIONS:
            grid = config.paper_grid(name, orientation)
            paper = config.paper_mm(name, orientation)
            sx, sy = scale_for(*grid, *paper)
            assert sx == sy == config.mm_per_px(name), (name, orientation)


def test_unknown_page_falls_back_to_per_axis_ratio():
    sx, sy = scale_for(300, 400, 100.0, 200.0)
    assert (sx, sy) == (100.0 / 300, 200.0 / 400)
    # grade de A4 com papel que nao e A4: nao pode usar a escala do A4
    sx, sy = scale_for(850, 1200, 200.0, 300.0)
    assert sx != config.mm_per_px("a4")


def test_a4_scale_is_not_the_naive_ratio():
    sx, sy = scale_for(850, 1200, 210.0, 297.0)
    assert sx == sy == pytest.approx(0.2475)
    assert 210.0 / 850 != sx  # a razao ingenua em x erraria ~0,2%


def test_elements_match_the_draw_core_writer_line_by_line(tmp_path):
    sx, sy = scale_for(850, 1200, 210.0, 297.0)
    reference = tmp_path / "ref.svg"
    write_svg(
        _lines(), reference, 850, 1200, 210.0, 297.0, 0.5, scale_mm_per_px=(sx, sy)
    )
    ref_lines = reference.read_text(encoding="utf-8").splitlines()
    mine = [polyline_element(line, sx, sy) for line in _lines()]
    for element in mine:
        assert element in ref_lines


def test_atomic_file_is_identical_to_the_core_writer_and_private(tmp_path):
    sx, sy = scale_for(850, 1200, 210.0, 297.0)
    reference = tmp_path / "ref.svg"
    write_svg(
        _lines(), reference, 850, 1200, 210.0, 297.0, 0.5, scale_mm_per_px=(sx, sy)
    )
    out = tmp_path / "live.svg"
    write_svg_atomic(_lines(), out, (850, 1200), (210.0, 297.0), (sx, sy))
    assert out.read_text() == reference.read_text()
    assert os.stat(out).st_mode & 0o777 == 0o600
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


def test_failed_write_keeps_the_previous_file_and_leaves_no_temp(tmp_path):
    out = tmp_path / "live.svg"
    out.write_text("anterior")
    bad = [{"points": object(), "closed": False}]
    with pytest.raises(Exception):  # noqa: B017 - qualquer falha de escrita
        write_svg_atomic(bad, out, (850, 1200), (210.0, 297.0), (0.25, 0.25))
    assert out.read_text() == "anterior"
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


def test_island_element_wraps_polylines_with_a_stable_safe_id():
    island = Island(
        key=(1200, 850, 1, 2, 3, 4, b"\x00\x01"),
        bbox=(1, 2, 3, 4),
        polylines=tuple(_lines()),
    )
    element = island_element(island, 0.25, 0.25)
    assert re.fullmatch(r'<g id="i[0-9a-f]{16}">.*</g>', element)
    assert element_id(island.key) == element_id(island.key)
    assert element.count("<path") == 2 and element.count("<circle") == 1


def test_render_text_equals_the_core_writer_output(tmp_path):
    sx, sy = scale_for(850, 1200, 210.0, 297.0)
    reference = tmp_path / "ref.svg"
    write_svg(
        _lines(), reference, 850, 1200, 210.0, 297.0, 0.5, scale_mm_per_px=(sx, sy)
    )
    text = render_svg_text(_lines(), (850, 1200), (210.0, 297.0), (sx, sy))
    assert text == reference.read_text()
