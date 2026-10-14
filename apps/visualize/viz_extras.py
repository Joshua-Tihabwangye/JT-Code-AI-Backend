"""Conditional formatting and analytics lines for dashboard visuals (engine side, no Django).

Conditional formatting (``format.conditional.rules``), each rule for one measure (``m0``, ``m1``…):

* ``{"kind": "scale", "min": "#hex", "max": "#hex", "mid"?: "#hex", "target": "background"|"font"}``
  — a colour scale from the lowest to the highest value;
* ``{"kind": "rules", "rules": [{"op": "gt", "value": 100, "color": "#hex"}, …], "target": …}`` —
  the first matching rule wins (``gt gte lt lte eq ne between``; ``between`` uses ``value2``);
* ``{"kind": "bars", "color"?: "#hex"}`` — data bars (tables);
* ``{"kind": "icons", "thresholds"?: [low, high]}`` — ▲ ● ▼ (good / neutral / bad), by thirds of
  the range unless thresholds are given.

Analytics lines (``format.analytics.lines``): ``constant`` (``value``), ``average``, ``median``,
``min``, ``max``, ``percentile`` (``percentile``), ``trend`` (least squares) and ``forecast``
(``horizon`` periods, Holt's linear trend with a 95 % band; linear fallback).
"""

from __future__ import annotations

import math
import re
from typing import Any

HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
MAX_RULES = 12
LINE_KINDS = ("constant", "average", "median", "min", "max", "percentile", "trend", "forecast")
OPS = ("gt", "gte", "lt", "lte", "eq", "ne", "between")


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except TypeError, ValueError:
        return None
    return None if math.isnan(number) or math.isinf(number) else number


def _color(value: Any, default: str) -> str:
    return value if isinstance(value, str) and HEX.match(value) else default


def _rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def mix(low: str, high: str, share: float) -> str:
    share = max(0.0, min(1.0, share))
    a, b = _rgb(low), _rgb(high)
    return "#" + "".join(f"{round(x + (y - x) * share):02X}" for x, y in zip(a, b, strict=True))


def rules_for(fmt: dict[str, Any], measure_id: str) -> list[dict[str, Any]]:
    """The conditional-formatting rules of one measure (the first measure when none is named)."""
    raw = fmt.get("rules") if isinstance(fmt.get("rules"), list) else []
    return [
        rule
        for rule in raw[:MAX_RULES]
        if isinstance(rule, dict)
        and rule.get("kind") in {"scale", "rules", "bars", "icons"}
        and (rule.get("measure") or "m0") == measure_id
    ]


def _matches(value: float, rule: dict[str, Any]) -> bool:
    op = rule.get("op")
    target = _number(rule.get("value"))
    if op not in OPS or target is None:
        return False
    if op == "between":
        upper = _number(rule.get("value2"))
        return upper is not None and min(target, upper) <= value <= max(target, upper)
    return {
        "gt": value > target,
        "gte": value >= target,
        "lt": value < target,
        "lte": value <= target,
        "eq": value == target,
        "ne": value != target,
    }[op]


def paint(rules: list[dict[str, Any]], values: list[Any], theme: dict[str, Any]) -> list[dict[str, Any]]:
    """One style per value: ``bg``/``color`` (colour), ``icon`` + ``iconColor``, ``bar`` (colour)."""
    numbers = [_number(v) for v in values]
    finite = [n for n in numbers if n is not None]
    low, high = (min(finite), max(finite)) if finite else (0.0, 0.0)
    styles: list[dict[str, Any]] = [{} for _ in values]
    good, neutral, bad = (
        theme.get("good", "#16A34A"),
        theme.get("neutral", "#94A3B8"),
        theme.get("bad", "#DC2626"),
    )
    for rule in rules:
        key = "color" if rule.get("target") == "font" else "bg"
        kind = rule["kind"]
        for index, number in enumerate(numbers):
            if number is None:
                continue
            style = styles[index]
            share = 0.5 if high == low else (number - low) / (high - low)
            if kind == "scale":
                start, end = _color(rule.get("min"), "#FEE2E2"), _color(rule.get("max"), "#DCFCE7")
                mid = rule.get("mid")
                if isinstance(mid, str) and HEX.match(mid):
                    style[key] = mix(start, mid, share * 2) if share < 0.5 else mix(mid, end, share * 2 - 1)
                else:
                    style[key] = mix(start, end, share)
            elif kind == "rules":
                for item in (rule.get("rules") or [])[:MAX_RULES]:
                    if isinstance(item, dict) and _matches(number, item):
                        style[key] = _color(item.get("color"), good)
                        break
            elif kind == "bars":
                style["bar"] = _color(rule.get("color"), theme.get("accent") or good)
            elif kind == "icons":
                thresholds = rule.get("thresholds") if isinstance(rule.get("thresholds"), list) else []
                t_low, t_high = (_number(t) for t in (thresholds + [None, None])[:2])
                if t_low is None or t_high is None:
                    t_low, t_high = low + (high - low) / 3, low + 2 * (high - low) / 3
                if number >= t_high:
                    style["icon"], style["iconColor"] = "▲", good
                elif number <= t_low:
                    style["icon"], style["iconColor"] = "▼", bad
                else:
                    style["icon"], style["iconColor"] = "●", neutral
    for style in styles:
        # A coloured cell keeps readable text in light and dark dashboards (unless a rule sets it).
        if style.get("bg") and not style.get("color"):
            style["color"] = readable_text(style["bg"])
    return styles


def readable_text(background: str) -> str:
    """Near-black or white, whichever reads better on the background (WCAG luminance)."""

    def channel(value: int) -> float:
        v = value / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in _rgb(background))
    luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "#0F172A" if luminance > 0.179 else "#FFFFFF"


# --- Analytics lines ----------------------------------------------------------------------------


def lines(fmt: dict[str, Any]) -> list[dict[str, Any]]:
    raw = fmt.get("lines") if isinstance(fmt.get("lines"), list) else []
    return [line for line in raw[:8] if isinstance(line, dict) and line.get("kind") in LINE_KINDS]


def _percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return math.nan
    position = (len(ordered) - 1) * max(0.0, min(1.0, share))
    lower = math.floor(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def reference_value(line: dict[str, Any], values: list[Any]) -> float | None:
    """The value of a constant/average/median/min/max/percentile line over a series."""
    finite = [n for n in (_number(v) for v in values) if n is not None]
    kind = line["kind"]
    if kind == "constant":
        return _number(line.get("value"))
    if not finite:
        return None
    if kind == "average":
        return sum(finite) / len(finite)
    if kind == "median":
        return _percentile(finite, 0.5)
    if kind == "min":
        return min(finite)
    if kind == "max":
        return max(finite)
    if kind == "percentile":
        share = _number(line.get("percentile"))
        return _percentile(finite, (share if share is not None else 90) / 100)
    return None


def line_label(line: dict[str, Any]) -> str:
    default = {
        "constant": "Target",
        "average": "Average",
        "median": "Median",
        "min": "Min",
        "max": "Max",
        "percentile": f"P{int(_number(line.get('percentile')) or 90)}",
        "trend": "Trend",
        "forecast": "Forecast",
    }[line["kind"]]
    # Braces would be read as ECharts label templates.
    return re.sub(r"[{}|]", "", str(line.get("label") or default))[:40]


def trend(values: list[Any]) -> list[float | None]:
    """A least-squares straight line through the series (blanks skipped)."""
    points = [(i, n) for i, n in enumerate(_number(v) for v in values) if n is not None]
    if len(points) < 2:
        return [None for _ in values]
    count = len(points)
    mean_x = sum(p[0] for p in points) / count
    mean_y = sum(p[1] for p in points) / count
    spread = sum((p[0] - mean_x) ** 2 for p in points)
    slope = sum((p[0] - mean_x) * (p[1] - mean_y) for p in points) / spread if spread else 0.0
    return [round(mean_y + slope * (i - mean_x), 6) for i in range(len(values))]


def forecast(values: list[Any], horizon: int) -> tuple[list[float], list[float], list[float]]:
    """``horizon`` future values with a 95 % band: Holt's linear trend, or a straight line."""
    series = [n for n in (_number(v) for v in values) if n is not None]
    horizon = max(1, min(int(horizon or 3), 24))
    if len(series) < 3:
        return [], [], []
    fitted: list[float]
    try:
        if len(series) < 6:
            raise ValueError("too short for Holt")
        import numpy as np
        from statsmodels.tsa.holtwinters import ExponentialSmoothing

        model = ExponentialSmoothing(np.asarray(series, dtype=float), trend="add").fit()
        points = [float(v) for v in model.forecast(horizon)]
        fitted = [float(v) for v in model.fittedvalues]
    except Exception:  # noqa: BLE001 - any failure falls back to the straight line
        line = trend(series + [None] * horizon)
        points = [float(v) for v in line[len(series) :] if v is not None]
        fitted = [float(v) for v in line[: len(series)] if v is not None]
    residuals = [a - b for a, b in zip(series, fitted, strict=False)]
    spread = math.sqrt(sum(r * r for r in residuals) / max(len(residuals) - 1, 1)) if residuals else 0.0
    lower = [p - 1.96 * spread * math.sqrt(step + 1) for step, p in enumerate(points)]
    upper = [p + 1.96 * spread * math.sqrt(step + 1) for step, p in enumerate(points)]
    return points, lower, upper
