"""Рисунок питомца и бейдж в SVG. Один и тот же рисунок на странице, в бейдже и в /pet."""
from __future__ import annotations

from html import escape

COLORS = {"ok": "#7bd389", "sad": "#8fb0dd", "sick": "#c8d26b", "sleeping": "#b7a6ea", "dead": "#bdbdbd"}


def pet_svg(view: str, size: int = 160) -> str:
    body = COLORS[view]
    eyes = {
        "ok": '<circle cx="62" cy="74" r="7" fill="#222"/><circle cx="98" cy="74" r="7" fill="#222"/>'
              '<circle cx="64" cy="72" r="2.2" fill="#fff"/><circle cx="100" cy="72" r="2.2" fill="#fff"/>',
        "sad": '<circle cx="62" cy="78" r="6" fill="#222"/><circle cx="98" cy="78" r="6" fill="#222"/>'
               '<path d="M52 66 L70 70 M108 66 L90 70" stroke="#222" stroke-width="3" stroke-linecap="round"/>'
               '<path d="M100 88 q4 8 0 12 q-4 -4 0 -12z" fill="#4aa3ff"/>',
        "sick": '<path d="M55 76 q7 -6 14 0 M91 76 q7 -6 14 0" stroke="#222" stroke-width="4" fill="none" stroke-linecap="round"/>',
        "sleeping": '<path d="M55 76 q7 6 14 0 M91 76 q7 6 14 0" stroke="#222" stroke-width="4" fill="none" stroke-linecap="round"/>',
        "dead": '<path d="M55 68 l14 14 M69 68 l-14 14 M91 68 l14 14 M105 68 l-14 14" stroke="#222" stroke-width="4" stroke-linecap="round"/>',
    }[view]
    mouth = {
        "ok": '<path d="M66 98 q14 14 28 0" stroke="#222" stroke-width="4" fill="none" stroke-linecap="round"/>',
        "sad": '<path d="M66 106 q14 -12 28 0" stroke="#222" stroke-width="4" fill="none" stroke-linecap="round"/>',
        "sick": '<path d="M64 102 q6 -6 12 0 q6 6 12 0 q6 -6 8 -2" stroke="#222" stroke-width="3.5" fill="none" stroke-linecap="round"/>',
        "sleeping": '<ellipse cx="80" cy="102" rx="6" ry="4" fill="#222"/>',
        "dead": '<path d="M68 104 h24" stroke="#222" stroke-width="4" stroke-linecap="round"/>',
    }[view]
    extra = {
        "ok": '<ellipse cx="50" cy="90" rx="7" ry="4" fill="#ff8fa3" opacity=".6"/>'
              '<ellipse cx="110" cy="90" rx="7" ry="4" fill="#ff8fa3" opacity=".6"/>',
        "sad": "",
        "sick": '<g transform="rotate(25 118 104)"><rect x="114" y="84" width="8" height="34" rx="4" fill="#fff" stroke="#222" stroke-width="2"/>'
                '<circle cx="118" cy="120" r="6" fill="#f85149" stroke="#222" stroke-width="2"/>'
                '<rect x="116.5" y="98" width="3" height="20" fill="#f85149"/></g>',
        "sleeping": '<text x="112" y="44" font-family="Verdana,sans-serif" font-size="18" font-weight="bold" fill="#5b4bb7">Z</text>'
                    '<text x="128" y="30" font-family="Verdana,sans-serif" font-size="13" font-weight="bold" fill="#5b4bb7">z</text>',
        "dead": '<ellipse cx="80" cy="18" rx="26" ry="7" fill="none" stroke="#f2c94c" stroke-width="4"/>',
    }[view]
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 160 160" width="{size}" height="{size}" '
            f'role="img" aria-label="питомец">'
            '<ellipse cx="80" cy="148" rx="46" ry="7" fill="#000" opacity=".12"/>'
            f'<path d="M80 26 C122 26 138 58 138 92 C138 128 112 142 80 142 C48 142 22 128 22 92 C22 58 38 26 80 26Z" '
            f'fill="{body}" stroke="#222" stroke-width="4"/>'
            '<path d="M70 28 q10 -16 20 0" fill="none" stroke="#222" stroke-width="4" stroke-linecap="round"/>'
            f'{eyes}{mouth}{extra}</svg>')


def _text_width(text: str) -> int:
    return int(len(text) * 7.2) + 10


def health_color(health: int) -> str:
    if health >= 70:
        return "#2ea043"
    if health >= 40:
        return "#d29922"
    if health > 0:
        return "#e5534b"
    return "#8b949e"


def badge_svg(name: str, view: str, health: int) -> str:
    left = f"{name}"
    right = f"❤ {health}" if view != "dead" else "🪦 RIP"
    icon = 22
    lw, rw = icon + 6 + _text_width(left), _text_width(right)
    w = lw + rw
    inner = pet_svg(view, icon).replace("<svg ", f'<svg x="5" y="3" ', 1)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="28" role="img" '
            f'aria-label="Кодогочи {escape(name)}: здоровье {health}">'
            f'<title>Кодогочи {escape(name)}: здоровье {health}/100</title>'
            f'<rect width="{w}" height="28" rx="5" fill="#2b2d42"/>'
            f'<rect x="{lw}" width="{rw}" height="28" rx="5" fill="{health_color(health)}"/>'
            f'<rect x="{lw}" width="6" height="28" fill="{health_color(health)}"/>'
            f'{inner}'
            f'<g fill="#fff" font-family="Verdana,DejaVu Sans,sans-serif" font-size="12">'
            f'<text x="{icon + 9}" y="18.5">{escape(left)}</text>'
            f'<text x="{lw + 5}" y="18.5" font-weight="bold">{escape(right)}</text></g></svg>')
