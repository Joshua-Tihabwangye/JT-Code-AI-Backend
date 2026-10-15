"""Dashboard themes: data colours plus canvas, tile and text colours, fonts and KPI tile style.

A theme is plain JSON in the spirit of Power BI report themes. Every built-in theme has a light
and a dark variant. Custom themes (``models.Theme``) use the same shape and are checked by
``clean_theme``. This module imports nothing from Django so the sandboxed engine can load it.
"""

from __future__ import annotations

import re
from typing import Any

HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
FONTS = ("Inter", "Segoe UI", "Roboto", "Poppins", "system-ui")
KPI_STYLES = ("plain", "solid", "gradient", "outlined", "icon")
DENSITIES = ("compact", "comfortable")

LIGHT = {
    "canvas": "#F4F6FA",
    "tile": "#FFFFFF",
    "border": "#E5E7EB",
    "gridline": "#EEF0F4",
    "text": "#0F172A",
    "mutedText": "#64748B",
    "accent": "#4F46E5",
}
DARK = {
    "canvas": "#0B1020",
    "tile": "#121A2E",
    "border": "#1F2A44",
    "gridline": "#1C2640",
    "text": "#E5E9F2",
    "mutedText": "#8A96B0",
    "accent": "#818CF8",
}


def _theme(
    theme_id: str,
    name: str,
    colors: list[str],
    *,
    light: dict[str, str] | None = None,
    dark: dict[str, str] | None = None,
    kpi: str = "plain",
    font: str = "Inter",
    radius: int = 12,
    good: str = "#16A34A",
    bad: str = "#DC2626",
) -> dict[str, Any]:
    return {
        "id": theme_id,
        "name": name,
        "dataColors": colors,
        "good": good,
        "neutral": "#94A3B8",
        "bad": bad,
        "light": {**LIGHT, **(light or {})},
        "dark": {**DARK, **(dark or {})},
        "fontFamily": font,
        "radius": radius,
        "shadow": "soft",
        "kpiStyle": kpi,
        "density": "comfortable",
    }


BUILT_IN: dict[str, dict[str, Any]] = {
    theme["id"]: theme
    for theme in (
        _theme(
            "executive",
            "Executive",
            ["#1E3A8A", "#2563EB", "#0EA5E9", "#14B8A6", "#F59E0B", "#64748B", "#A855F7", "#E11D48"],
            light={"canvas": "#F5F7FB", "accent": "#1E3A8A"},
            dark={"canvas": "#0A1022", "tile": "#111A30", "accent": "#60A5FA"},
        ),
        _theme(
            "midnight",
            "Midnight Navy",
            ["#22D3EE", "#F97316", "#EC4899", "#A78BFA", "#34D399", "#FACC15", "#60A5FA", "#F43F5E"],
            light={"canvas": "#EEF2FF", "accent": "#4338CA"},
            dark={"canvas": "#070B1A", "tile": "#0F1630", "border": "#1B2550", "accent": "#22D3EE"},
            kpi="icon",
        ),
        _theme(
            "corporate",
            "Corporate Blue",
            ["#1E3A8A", "#2563EB", "#60A5FA", "#64748B", "#94A3B8", "#0F766E", "#F59E0B", "#DC2626"],
            light={"accent": "#2563EB"},
            radius=8,
        ),
        _theme(
            "crm",
            "CRM Vivid",
            ["#7C3AED", "#4F46E5", "#2563EB", "#0891B2", "#0D9488", "#16A34A", "#F59E0B", "#E11D48"],
            light={"canvas": "#F6F5FF", "accent": "#7C3AED"},
            dark={"accent": "#A78BFA"},
            kpi="gradient",
        ),
        _theme(
            "vivid",
            "JT Vivid",
            [
                "#6366F1",
                "#06B6D4",
                "#F59E0B",
                "#EC4899",
                "#10B981",
                "#8B5CF6",
                "#EF4444",
                "#84CC16",
                "#F97316",
                "#14B8A6",
            ],
        ),
        _theme(
            "colorblind",
            "Colour-blind safe",
            ["#0072B2", "#E69F00", "#009E73", "#CC79A7", "#56B4E9", "#D55E00", "#F0E442", "#999999"],
            good="#0072B2",
            bad="#D55E00",
        ),
        _theme(
            "contrast",
            "High contrast",
            ["#0000FF", "#D00000", "#007F00", "#7A00CC", "#B35900", "#000000", "#008B8B", "#C71585"],
            light={
                "canvas": "#FFFFFF",
                "border": "#000000",
                "gridline": "#BDBDBD",
                "mutedText": "#333333",
                "accent": "#0000FF",
            },
            dark={
                "canvas": "#000000",
                "tile": "#000000",
                "border": "#FFFFFF",
                "gridline": "#555555",
                "text": "#FFFFFF",
                "mutedText": "#DDDDDD",
                "accent": "#FFFF00",
            },
            kpi="outlined",
            radius=4,
        ),
        _theme(
            "ocean",
            "Ocean",
            ["#0B3C5D", "#1D65A6", "#328CC1", "#72B5DD", "#14B8A6", "#5EEAD4", "#0E7490", "#164E63"],
            light={"canvas": "#F0F7FB", "accent": "#1D65A6"},
            dark={"canvas": "#06141F", "tile": "#0B2030", "accent": "#5EEAD4"},
        ),
        _theme(
            "sunset",
            "Sunset",
            ["#F94144", "#F3722C", "#F8961E", "#F9C74F", "#90BE6D", "#43AA8B", "#577590", "#9D4EDD"],
            light={"canvas": "#FFF8F3", "accent": "#F3722C"},
            dark={"canvas": "#160D12", "tile": "#22131B", "accent": "#F8961E"},
            kpi="solid",
        ),
        _theme(
            "earth",
            "Earth",
            ["#8C510A", "#BF812D", "#DFC27D", "#80CDC1", "#35978F", "#01665E", "#543005", "#003C30"],
            light={"canvas": "#FAF7F0", "accent": "#8C510A"},
            dark={"canvas": "#14110A", "tile": "#1F1A10", "accent": "#DFC27D"},
        ),
        _theme(
            "pastel",
            "Pastel",
            ["#818CF8", "#2DD4BF", "#FBBF24", "#F472B6", "#4ADE80", "#A78BFA", "#F87171", "#38BDF8"],
            light={"canvas": "#FAFAFF", "accent": "#818CF8"},
            radius=16,
        ),
        _theme(
            "tableau",
            "Tableau 10",
            [
                "#4E79A7",
                "#F28E2B",
                "#E15759",
                "#76B7B2",
                "#59A14F",
                "#EDC948",
                "#B07AA1",
                "#FF9DA7",
                "#9C755F",
                "#BAB0AC",
            ],
            radius=6,
        ),
        _theme(
            "plum",
            "Plum",
            ["#A21CAF", "#DB2777", "#F59E0B", "#7C3AED", "#14B8A6", "#F472B6", "#6366F1", "#84CC16"],
            light={"canvas": "#FBF5FB", "accent": "#A21CAF"},
            dark={"canvas": "#170A19", "tile": "#241026", "border": "#3A1A3D", "accent": "#F0ABFC"},
            kpi="solid",
        ),
    )
}
DEFAULT_THEME = "executive"


def public_themes() -> list[dict[str, Any]]:
    return list(BUILT_IN.values())


def _color(value: Any, fallback: str) -> str:
    return value if isinstance(value, str) and HEX.match(value) else fallback


def clean_theme(raw: Any) -> dict[str, Any]:
    """A safe, complete theme from user JSON (unknown keys dropped, bad values replaced)."""
    raw = raw if isinstance(raw, dict) else {}
    base = BUILT_IN.get(str(raw.get("base") or raw.get("id") or ""), BUILT_IN[DEFAULT_THEME])
    colors = [c for c in raw.get("dataColors") or [] if isinstance(c, str) and HEX.match(c)][:12]
    theme = {
        "id": str(raw.get("id") or "custom")[:40],
        "name": str(raw.get("name") or "Custom theme")[:80],
        "dataColors": colors if len(colors) >= 3 else base["dataColors"],
        "good": _color(raw.get("good"), base["good"]),
        "neutral": _color(raw.get("neutral"), base["neutral"]),
        "bad": _color(raw.get("bad"), base["bad"]),
        "fontFamily": raw.get("fontFamily") if raw.get("fontFamily") in FONTS else base["fontFamily"],
        "kpiStyle": raw.get("kpiStyle") if raw.get("kpiStyle") in KPI_STYLES else base["kpiStyle"],
        "density": raw.get("density") if raw.get("density") in DENSITIES else base["density"],
        "shadow": raw.get("shadow") if raw.get("shadow") in {"none", "soft", "medium"} else base["shadow"],
    }
    try:
        theme["radius"] = max(0, min(int(raw.get("radius", base["radius"])), 24))
    except TypeError, ValueError:
        theme["radius"] = base["radius"]
    for mode in ("light", "dark"):
        given = raw.get(mode) if isinstance(raw.get(mode), dict) else {}
        theme[mode] = {key: _color(given.get(key), value) for key, value in base[mode].items()}
    return theme


def resolve(theme: Any, mode: str = "light") -> dict[str, Any]:
    """The theme to draw with: a built-in id or a custom theme dict, in light or dark."""
    data = BUILT_IN.get(theme, BUILT_IN[DEFAULT_THEME]) if isinstance(theme, str) else clean_theme(theme)
    mode = mode if mode in {"light", "dark"} else "light"
    return {**data, "mode": mode, "structural": data[mode]}
