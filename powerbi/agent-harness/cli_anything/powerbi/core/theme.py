"""Power BI report themes (the JSON Desktop imports via Design > Themes > Import theme).

build_theme() produces a "modern card" theme: palette + text classes + visualStyles
that give every visual a rounded card with a soft shadow on a tinted page.
"""
from __future__ import annotations

import json
import re

HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")

PALETTES = {
    "indigo": ["#4F46E5", "#06B6D4", "#F59E0B", "#EC4899", "#10B981", "#8B5CF6", "#F97316", "#64748B"],
    "ocean": ["#0EA5E9", "#0F766E", "#22D3EE", "#1E3A8A", "#14B8A6", "#38BDF8", "#0369A1", "#94A3B8"],
    "sunset": ["#F97316", "#E11D48", "#FACC15", "#9333EA", "#FB7185", "#F59E0B", "#7C3AED", "#78716C"],
    "forest": ["#15803D", "#65A30D", "#CA8A04", "#0F766E", "#84CC16", "#A16207", "#4D7C0F", "#57534E"],
    "mono": ["#111827", "#374151", "#6B7280", "#9CA3AF", "#4B5563", "#1F2937", "#D1D5DB", "#E5E7EB"],
    # Okabe-Ito: distinguishable with common colour-vision deficiencies
    "colorblind": ["#0072B2", "#E69F00", "#009E73", "#CC79A7", "#56B4E9", "#D55E00", "#F0E442", "#000000"],
}

PRESETS = {
    "modern": dict(palette="indigo", font="Segoe UI", heading_font="Segoe UI Semibold",
                   foreground="#111827", muted="#6B7280", card="#FFFFFF", page="#F3F4F6",
                   border="#E5E7EB", radius=12, shadow=True),
    "dark": dict(palette="ocean", font="Segoe UI", heading_font="Segoe UI Semibold",
                 foreground="#F9FAFB", muted="#9CA3AF", card="#1F2937", page="#111827",
                 border="#374151", radius=12, shadow=False),
    "corporate": dict(palette="mono", font="Segoe UI", heading_font="Segoe UI Bold",
                      foreground="#1F2937", muted="#6B7280", card="#FFFFFF", page="#FFFFFF",
                      border="#D1D5DB", radius=4, shadow=False),
    "warm": dict(palette="sunset", font="Segoe UI", heading_font="Segoe UI Semibold",
                 foreground="#292524", muted="#78716C", card="#FFFFFF", page="#FAF5F0",
                 border="#E7E5E4", radius=16, shadow=True),
    "accessible": dict(palette="colorblind", font="Segoe UI", heading_font="Segoe UI Semibold",
                       foreground="#000000", muted="#4B5563", card="#FFFFFF", page="#F5F5F5",
                       border="#9CA3AF", radius=8, shadow=False),
}

# Fonts Power BI renders everywhere (service + Desktop) without embedding.
SAFE_FONTS = {"Segoe UI", "Segoe UI Light", "Segoe UI Semibold", "Segoe UI Bold", "Arial",
              "Arial Black", "Calibri", "Cambria", "Candara", "Consolas", "Constantia", "Corbel",
              "Courier New", "DIN", "Georgia", "Lucida Sans Unicode", "Tahoma", "Times New Roman",
              "Trebuchet MS", "Verdana", "Wingdings"}


def resolve_palette(p: str | list[str] | None, default="indigo") -> list[str]:
    if p is None:
        p = default
    if isinstance(p, str):
        if p.lower() in PALETTES:
            return list(PALETTES[p.lower()])
        p = [x.strip() for x in p.split(",") if x.strip()]
    bad = [c for c in p if not HEX.match(c)]
    if bad or not p:
        raise ValueError(f"Palette must be a name ({', '.join(PALETTES)}) or #RRGGBB list; bad: {bad or p}")
    return [c.upper() for c in p]


def _solid(c):
    return {"solid": {"color": c}}


def build_theme(name: str, preset: str = "modern", palette=None, font=None, heading_font=None,
                foreground=None, background=None, page_background=None, border=None,
                radius=None, shadow=None, card_style=True) -> dict:
    if preset not in PRESETS:
        raise ValueError(f"Unknown preset '{preset}'. Presets: {', '.join(PRESETS)}")
    o = dict(PRESETS[preset])
    for k, v in dict(font=font, heading_font=heading_font, foreground=foreground, card=background,
                     page=page_background, border=border, radius=radius, shadow=shadow).items():
        if v is not None:
            o[k] = v
    for k in ("foreground", "card", "page", "border"):
        if not HEX.match(o[k]):
            raise ValueError(f"{k} must be #RRGGBB, got {o[k]!r}")
    if not 0 <= int(o["radius"]) <= 40:
        raise ValueError("radius must be 0-40")
    colors = resolve_palette(palette, o["palette"])
    f, hf, fg, muted = o["font"], o["heading_font"], o["foreground"].upper(), o["muted"]
    theme = {
        "name": name,
        "dataColors": colors,
        "foreground": fg,
        "foregroundNeutralSecondary": muted,
        "background": o["card"].upper(),
        "backgroundLight": o["page"].upper(),
        "tableAccent": colors[0],
        "good": "#16A34A", "neutral": "#F59E0B", "bad": "#DC2626",
        "maximum": colors[0], "center": "#F3F4F6" if o["card"].upper() != "#1F2937" else "#374151",
        "minimum": colors[1] if len(colors) > 1 else colors[0],
        "textClasses": {
            "callout": {"fontFace": hf, "fontSize": 28, "color": fg},
            "title": {"fontFace": hf, "fontSize": 12, "color": fg},
            "header": {"fontFace": hf, "fontSize": 12, "color": fg},
            "label": {"fontFace": f, "fontSize": 10, "color": muted},
        },
    }
    if card_style:
        theme["visualStyles"] = {
            "*": {"*": {
                "background": [{"show": True, "color": _solid(o["card"]), "transparency": 0}],
                "border": [{"show": True, "color": _solid(o["border"]), "radius": int(o["radius"])}],
                "dropShadow": [{"show": bool(o["shadow"]), "color": _solid("#000000"),
                                "position": "Outer", "preset": "Custom", "shadowBlur": 12,
                                "shadowDistance": 2, "shadowSpread": 0, "angle": 90,
                                "transparency": 88}],
                "padding": [{"top": 12, "bottom": 12, "left": 16, "right": 16}],
                "title": [{"show": True, "fontFamily": hf, "fontSize": 12,
                           "fontColor": _solid(fg), "alignment": "left"}],
            }},
            "page": {"*": {
                "background": [{"color": _solid(o["page"]), "transparency": 0}],
                "outspace": [{"color": _solid(o["page"]), "transparency": 0}],
            }},
            "textbox": {"*": {
                "background": [{"show": False}],
                "border": [{"show": False}],
                "dropShadow": [{"show": False}],
            }},
        }
    return theme


def validate_theme(theme: dict) -> list[str]:
    """Problems Power BI would reject or that make a theme unusable."""
    issues = []
    if not isinstance(theme, dict):
        return ["theme must be a JSON object"]
    if not theme.get("name"):
        issues.append("missing 'name'")
    dc = theme.get("dataColors")
    if dc is not None and (not isinstance(dc, list) or not all(isinstance(c, str) and HEX.match(c) for c in dc)):
        issues.append("'dataColors' must be a list of #RRGGBB strings")
    for k in ("foreground", "background", "tableAccent", "good", "neutral", "bad",
              "maximum", "center", "minimum", "backgroundLight", "foregroundNeutralSecondary"):
        v = theme.get(k)
        if v is not None and not (isinstance(v, str) and HEX.match(v)):
            issues.append(f"'{k}' must be #RRGGBB")
    for cls, spec in (theme.get("textClasses") or {}).items():
        if not isinstance(spec, dict):
            issues.append(f"textClasses.{cls} must be an object")
        elif "fontSize" in spec and not isinstance(spec["fontSize"], (int, float)):
            issues.append(f"textClasses.{cls}.fontSize must be a number")
    vs = theme.get("visualStyles")
    if vs is not None and not isinstance(vs, dict):
        issues.append("'visualStyles' must be an object")
    return issues


def font_warnings(theme: dict) -> list[str]:
    fonts = {s.get("fontFace") for s in (theme.get("textClasses") or {}).values() if isinstance(s, dict)}
    return [f"font '{f}' may not render in the Power BI service" for f in sorted(x for x in fonts if x)
            if f not in SAFE_FONTS]


def load_theme_file(path: str) -> dict:
    with open(path, encoding="utf-8-sig") as fh:
        try:
            return json.load(fh)
        except json.JSONDecodeError as e:
            raise ValueError(f"{path}: invalid JSON ({e})") from e
