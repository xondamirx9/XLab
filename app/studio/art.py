"""Генеративная графика: у каждого сайта своя картинка, выросшая из «зерна» проекта.

Рисует только числа и цвета из палитры — никакого текста от агентов внутри SVG нет.
"""
from __future__ import annotations

import math
import random

from app.studio.spec import adjust_lightness, mix

SIZE = 800


def _smooth_closed(points: list[tuple[float, float]]) -> str:
    """Плавный замкнутый контур через точки (Catmull-Rom → кривые Безье)."""
    n = len(points)
    d = f"M{points[0][0]:.1f},{points[0][1]:.1f}"
    for i in range(n):
        p0, p1, p2, p3 = points[i - 1], points[i], points[(i + 1) % n], points[(i + 2) % n]
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        d += f" C{c1[0]:.1f},{c1[1]:.1f} {c2[0]:.1f},{c2[1]:.1f} {p2[0]:.1f},{p2[1]:.1f}"
    return d + "Z"


def _blob(rng: random.Random, cx: float, cy: float, r: float, points: int = 7, wobble: float = 0.28) -> str:
    pts = []
    for i in range(points):
        angle = math.tau * i / points
        rr = r * (1 + rng.uniform(-wobble, wobble))
        pts.append((cx + math.cos(angle) * rr, cy + math.sin(angle) * rr))
    return _smooth_closed(pts)


def _hue(color: str, shift: int) -> str:
    """Лёгкий сдвиг светлоты по зерну: два сайта с одной палитрой всё равно отличаются."""
    return adjust_lightness(color, ((shift % 21) - 10) / 200)


def render_art(kind: str, palette: dict[str, str], seed: int, hue: int = 0, uid: str = "a") -> str:
    rng = random.Random(seed)
    c1, c2, c3 = (_hue(palette[k], hue) for k in ("primary", "accent", "accent2"))
    bg, text = palette["bg"], palette["text"]
    defs = [
        f'<linearGradient id="g1" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{c1}"/><stop offset="1" stop-color="{c2}"/></linearGradient>',
        f'<linearGradient id="g2" x1="1" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{c3}"/><stop offset="1" stop-color="{c1}"/></linearGradient>',
        f'<radialGradient id="g3"><stop offset="0" stop-color="{c2}" stop-opacity=".9"/><stop offset="1" stop-color="{c2}" stop-opacity="0"/></radialGradient>',
        '<filter id="soft" x="-30%" y="-30%" width="160%" height="160%"><feGaussianBlur stdDeviation="18"/></filter>',
    ]
    body: list[str] = []

    if kind == "blobs":
        for i in range(6):
            r = rng.uniform(110, 230)
            cx, cy = rng.uniform(160, 640), rng.uniform(160, 640)
            fill = ("url(#g1)", "url(#g2)", c3, c1)[i % 4]
            blur = ' filter="url(#soft)"' if i < 2 else ""
            body.append(f'<path class="art-float" style="--i:{i}" d="{_blob(rng, cx, cy, r)}" fill="{fill}" opacity="{rng.uniform(.55, .95):.2f}"{blur}/>')
        for _ in range(14):
            body.append(f'<circle cx="{rng.uniform(40, 760):.0f}" cy="{rng.uniform(40, 760):.0f}" r="{rng.uniform(3, 9):.1f}" fill="{text}" opacity=".35"/>')
    elif kind == "waves":
        layers = 7
        for i in range(layers):
            y0 = 220 + i * 70
            amp, freq, phase = rng.uniform(20, 60), rng.uniform(1.2, 2.6), rng.uniform(0, math.tau)
            pts = [f"{x},{y0 + math.sin(x / SIZE * math.tau * freq + phase) * amp:.1f}" for x in range(0, SIZE + 1, 40)]
            fill = mix(c1, c2, i / (layers - 1))
            body.append(f'<path class="art-drift" style="--i:{i}" d="M0,{SIZE} L{" L".join(pts)} L{SIZE},{SIZE}Z" fill="{fill}" opacity="{0.25 + i * 0.1:.2f}"/>')
        body.insert(0, f'<circle cx="{rng.uniform(450, 650):.0f}" cy="{rng.uniform(120, 260):.0f}" r="120" fill="url(#g3)"/>')
    elif kind == "grid":
        horizon = rng.uniform(300, 380)
        for i in range(-12, 13):
            body.append(f'<line x1="{400 + i * 18:.0f}" y1="{horizon:.0f}" x2="{400 + i * 140:.0f}" y2="{SIZE}" stroke="{c1}" stroke-opacity=".35" stroke-width="1.2"/>')
        y, gap = horizon, 6.0
        while y < SIZE:
            body.append(f'<line x1="0" y1="{y:.1f}" x2="{SIZE}" y2="{y:.1f}" stroke="{c1}" stroke-opacity="{min(.6, .1 + (y - horizon) / 900):.2f}"/>')
            gap *= 1.28
            y += gap
        body.append(f'<circle cx="400" cy="{horizon - 90:.0f}" r="150" fill="url(#g1)" opacity=".9"/>')
        for k in range(5):
            body.append(f'<rect x="230" y="{horizon - 120 + k * 22:.0f}" width="340" height="{5 + k:.0f}" fill="{bg}"/>')
        for i in range(18):
            body.append(f'<circle class="art-blink" style="--i:{i}" cx="{rng.uniform(30, 770):.0f}" cy="{rng.uniform(20, horizon - 20):.0f}" r="{rng.uniform(1.2, 2.8):.1f}" fill="{c3}"/>')
    elif kind == "orbits":
        cx, cy = rng.uniform(360, 440), rng.uniform(360, 440)
        body.append(f'<circle cx="{cx:.0f}" cy="{cy:.0f}" r="70" fill="url(#g1)"/>')
        for i in range(6):
            rx, ry, rot = 120 + i * 50, 60 + i * 30 + rng.uniform(-10, 20), rng.uniform(-40, 40)
            body.append(f'<g class="art-spin" style="--i:{i}" transform="rotate({rot:.0f} {cx:.0f} {cy:.0f})">'
                        f'<ellipse cx="{cx:.0f}" cy="{cy:.0f}" rx="{rx:.0f}" ry="{ry:.0f}" fill="none" stroke="{text}" stroke-opacity=".22"/>'
                        f'<circle cx="{cx + rx:.0f}" cy="{cy:.0f}" r="{rng.uniform(6, 16):.0f}" fill="{(c1, c2, c3)[i % 3]}"/></g>')
    elif kind == "rays":
        cx, cy = rng.uniform(300, 500), rng.uniform(500, 700)
        count = rng.randint(28, 44)
        for i in range(count):
            a = math.pi + math.pi * i / (count - 1)
            length = rng.uniform(450, 900)
            body.append(f'<line x1="{cx:.0f}" y1="{cy:.0f}" x2="{cx + math.cos(a) * length:.0f}" y2="{cy + math.sin(a) * length:.0f}" '
                        f'stroke="{c1 if i % 3 else c3}" stroke-opacity="{rng.uniform(.15, .6):.2f}" stroke-width="{rng.uniform(.6, 2.2):.1f}"/>')
        body.append(f'<circle cx="{cx:.0f}" cy="{cy:.0f}" r="110" fill="url(#g3)"/><circle cx="{cx:.0f}" cy="{cy:.0f}" r="46" fill="url(#g1)"/>')
    else:  # topo
        cx, cy = rng.uniform(300, 500), rng.uniform(300, 500)
        offsets = [rng.uniform(-0.18, 0.18) for _ in range(9)]
        for i in range(16):
            r = 30 + i * 30
            pts = []
            for k in range(9):
                angle = math.tau * k / 9
                rr = r * (1 + offsets[k] * (1 + i / 10))
                pts.append((cx + math.cos(angle) * rr, cy + math.sin(angle) * rr))
            body.append(f'<path d="{_smooth_closed(pts)}" fill="none" stroke="{mix(c1, c2, i / 15)}" stroke-opacity="{0.75 - i * 0.035:.2f}" stroke-width="1.4"/>')

    svg = (f'<svg class="art art-{kind}" viewBox="0 0 {SIZE} {SIZE}" preserveAspectRatio="xMidYMid slice" '
            f'aria-hidden="true" focusable="false" xmlns="http://www.w3.org/2000/svg"><defs>{"".join(defs)}</defs>{"".join(body)}</svg>')
    # у каждой картинки на странице свои id, чтобы градиенты разных картинок не путались
    return svg.replace('id="', f'id="{uid}-').replace("url(#", f"url(#{uid}-")
