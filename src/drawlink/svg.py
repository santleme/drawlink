"""SVG da tinta vetorizada: elementos por ilha e arquivo atomico.

Os elementos usam exatamente o formato do `write_svg` do draw-core (mm, Y para
baixo, <path> com cantos redondos, <circle> para pontos isolados), para o
arquivo ao vivo e o export final nao divergirem. O arquivo e escrito pelo
proprio `write_svg` num temporario e trocado com `os.replace`, entao um leitor
(AutoCAD, navegador) nunca ve um arquivo pela metade.
"""

import hashlib
import os
import tempfile
from pathlib import Path

from draw.exporters.export_vectors import write_svg

DEFAULT_STROKE_MM = 0.5


def element_id(key):
    """Id do elemento SVG de uma ilha: curto, estavel e seguro para atributo."""
    return "i" + hashlib.blake2b(repr(key).encode(), digest_size=8).hexdigest()


def polyline_element(line, sx, sy, stroke_mm=DEFAULT_STROKE_MM):
    """Uma polilinha como elemento SVG (mesmo texto do write_svg)."""
    pts = line["points"]
    if len(pts) == 1:
        cx, cy = pts[0][0] * sx, pts[0][1] * sy
        return (
            f'<circle cx="{cx:.2f}" cy="{cy:.2f}" '
            f'r="{max(stroke_mm / 2, 0.1):.2f}" fill="#000"/>'
        )
    d = "M " + " L ".join(f"{x * sx:.2f} {y * sy:.2f}" for x, y in pts)
    if line["closed"]:
        d += " Z"
    return (
        f'<path d="{d}" fill="none" stroke="#000" '
        f'stroke-width="{stroke_mm:.2f}" stroke-linecap="round" '
        f'stroke-linejoin="round"/>'
    )


def island_element(island, sx, sy, stroke_mm=DEFAULT_STROKE_MM):
    """Todas as polilinhas de uma ilha, agrupadas e marcadas com o id."""
    inner = "".join(polyline_element(p, sx, sy, stroke_mm) for p in island.polylines)
    return f'<g id="{element_id(island.key)}">{inner}</g>'


def render_svg_text(polylines, grid, paper_mm, scale, stroke_mm=DEFAULT_STROKE_MM):
    """O SVG inteiro como texto, gerado pelo `write_svg` do draw-core."""
    fd, tmp = tempfile.mkstemp(suffix=".svg")
    os.close(fd)
    try:
        write_svg(
            polylines,
            tmp,
            grid[0],
            grid[1],
            paper_mm[0],
            paper_mm[1],
            stroke_mm,
            scale_mm_per_px=scale,
        )
        return Path(tmp).read_text(encoding="utf-8")
    finally:
        os.unlink(tmp)


def write_svg_atomic(
    polylines, out_path, grid, paper_mm, scale, stroke_mm=DEFAULT_STROKE_MM
):
    """Escreve o SVG inteiro (via `write_svg` do draw-core) e troca de forma atomica.

    O temporario nasce com modo 0600 (mkstemp): a tinta e privada. Se a escrita
    falhar, o arquivo anterior fica intacto e o temporario some.
    """
    out_path = Path(out_path)
    fd, tmp = tempfile.mkstemp(
        prefix=out_path.name + ".", suffix=".tmp", dir=out_path.parent
    )
    os.close(fd)
    try:
        write_svg(
            polylines,
            tmp,
            grid[0],
            grid[1],
            paper_mm[0],
            paper_mm[1],
            stroke_mm,
            scale_mm_per_px=scale,
        )
        os.replace(tmp, out_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
