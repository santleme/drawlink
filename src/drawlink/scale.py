"""Escala mm/px travada, derivada da grade e do papel que o Draw publica.

O stream (FULL) traz a grade da pagina (w, h px) e o papel (w_mm, h_mm). Quando
os dois batem com um formato conhecido do Draw (`draw.config`), a escala e a
UNIFORME do formato (`mm_per_px`: o lado longo tem 1200 px); fora disso, cai na
razao por eixo. Isso evita o erro silencioso de usar mm/px diferente em x e y
numa pagina de formato conhecido (a grade A4 tem 850 px de largura, nao
210/297*1200).
"""

import math

from draw import config


def scale_for(grid_w, grid_h, paper_w_mm, paper_h_mm):
    """(sx, sy) em mm/px para uma pagina de grade (grid_w, grid_h)."""
    for name in config.FORMATS:
        for orientation in config.ORIENTATIONS:
            if config.paper_grid(name, orientation) != (grid_w, grid_h):
                continue
            w_mm, h_mm = config.paper_mm(name, orientation)
            if math.isclose(w_mm, paper_w_mm, abs_tol=0.01) and math.isclose(
                h_mm, paper_h_mm, abs_tol=0.01
            ):
                unit = config.mm_per_px(name)
                return unit, unit
    return paper_w_mm / grid_w, paper_h_mm / grid_h
