"""Deterministic chart recommendations and automatic field mapping (no AI, no tokens).

Works only from a dataset's stored column profile, the way Tableau's “Show Me”
does: column kinds, cardinality and names decide which charts fit and how their
slots are filled.
"""

from __future__ import annotations

import re
from typing import Any

from apps.visualize import catalog

MEASURE_HINTS = (
    "revenue",
    "sales",
    "amount",
    "profit",
    "price",
    "total",
    "value",
    "cost",
    "count",
    "sessions",
    "units",
    "income",
    "spend",
)
SCORE_HINTS = ("score", "prob", "probability", "confidence", "likelihood")
PREDICTION_HINTS = ("pred", "forecast", "estimate", "fitted")
GEO_HINTS = ("country", "nation", "iso3", "iso", "state", "region_code")


def _columns(profile: dict[str, Any]) -> list[dict[str, Any]]:
    return list(profile.get("columnProfiles") or [])


def _identifier(entry: dict[str, Any], rows: int) -> bool:
    name = entry["name"].lower()
    return (rows > 20 and entry.get("distinct", 0) >= 0.95 * rows and entry.get("kind") != "numeric") or bool(
        re.search(r"(^id$|_id$|^uuid|^guid)", name)
    )


def column_roles(profile: dict[str, Any]) -> dict[str, list[str]]:
    rows = int(profile.get("rows") or 0)
    roles: dict[str, list[str]] = {"numeric": [], "dimension": [], "datetime": [], "binary": [], "all": []}
    for entry in _columns(profile):
        if _identifier(entry, rows):
            continue
        name, kind, distinct = entry["name"], entry.get("kind", "categorical"), entry.get("distinct", 0)
        roles["all"].append(name)
        if kind == "numeric":
            roles["numeric"].append(name)
        elif kind == "datetime":
            roles["datetime"].append(name)
        elif 1 < distinct <= 60:
            roles["dimension"].append(name)
        if kind in {"categorical", "boolean"} and distinct == 2:
            roles["binary"].append(name)
    roles["numeric"].sort(key=lambda name: (_measure_rank(name), roles["all"].index(name)))
    roles["dimension"].sort(key=lambda name: _cardinality(profile, name))
    return roles


def _measure_rank(name: str) -> int:
    """Business measures first (revenue before units), in MEASURE_HINTS order."""
    lowered = name.lower()
    if any(word in lowered for word in ("target", "predicted", "forecast")):
        return len(MEASURE_HINTS) + 1
    return next((index for index, hint in enumerate(MEASURE_HINTS) if hint in lowered), len(MEASURE_HINTS))


def _cardinality(profile: dict[str, Any], column: str) -> int:
    for entry in _columns(profile):
        if entry["name"] == column:
            return int(entry.get("distinct", 0))
    return 0


def kinds(profile: dict[str, Any]) -> dict[str, str]:
    return {entry["name"]: entry.get("kind", "categorical") for entry in _columns(profile)}


def _mentioned(prompt: str, columns: list[str]) -> list[str]:
    text = prompt.lower()
    found = [(text.find(column.lower()), column) for column in columns if column.lower() in text]
    return [column for _, column in sorted(found)]


def auto_fields(
    chart_id: str, profile: dict[str, Any], prompt: str = "", preferred: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Fill a chart's slots with sensible columns (mentioned columns first)."""
    chart = catalog.CHARTS_BY_ID[chart_id]
    roles = column_roles(profile)
    column_kinds = kinds(profile)
    mentioned = _mentioned(prompt, list(column_kinds)) if prompt else []
    used: set[str] = set()
    fields: dict[str, Any] = {}
    for key, value in (preferred or {}).items():
        fields[key] = value
        used.update(value if isinstance(value, list) else [value])

    def candidates(slot: catalog.Slot) -> list[str]:
        if slot.kind == "numeric":
            pool = roles["numeric"]
        elif slot.kind == "datetime":
            pool = roles["datetime"]
        elif slot.kind in {"dimension", "categorical"}:
            pool = roles["dimension"] + ([c for c in roles["datetime"]] if slot.kind == "dimension" else [])
        else:
            pool = roles["all"]
        ranked = [c for c in mentioned if c in pool] + [c for c in pool if c not in mentioned]
        return [c for c in ranked if c not in used]

    special = _ml_columns(profile)
    for slot in chart.slots:
        if slot.key in fields:
            continue
        if slot.key in special and special[slot.key] not in used:
            fields[slot.key] = special[slot.key]
            used.add(special[slot.key])
            continue
        if slot.key in {"lat", "lon"}:
            match = next(
                (
                    c
                    for c in roles["numeric"]
                    if c.lower()
                    in ({"lat", "latitude"} if slot.key == "lat" else {"lon", "lng", "longitude"})
                ),
                None,
            )
            if match:
                fields[slot.key] = match
                used.add(match)
                continue
        if slot.key == "location":
            match = next(
                (c for c in column_kinds if any(h in c.lower() for h in GEO_HINTS) and c not in used), None
            )
            if match:
                fields[slot.key] = match
                used.add(match)
                continue
        options = candidates(slot)
        if not options and slot.required and slot.kind in {"dimension", "categorical"}:
            # No text column left (e.g. a second “Group by”): low-cardinality numbers such as ratings,
            # bedrooms or years still split the data into readable groups.
            options = [
                c for c in roles["numeric"] if c not in used and 1 < _cardinality(profile, c) <= 20
            ] + [c for c in roles["datetime"] if c not in used]
        if slot.multiple:
            if slot.required or mentioned:
                chosen = [c for c in options if c in mentioned] or options[
                    : 3 if slot.key != "features" else 6
                ]
                if slot.key == "path":
                    chosen = chosen[:3] if len(chosen) >= 2 else options[:2]
                if chosen:
                    fields[slot.key] = chosen
                    used.update(chosen)
            continue
        if not slot.required and not any(c in mentioned for c in options):
            continue
        if options:
            fields[slot.key] = options[0]
            used.add(options[0])
    return fields


def _ml_columns(profile: dict[str, Any]) -> dict[str, str]:
    names = [entry["name"] for entry in _columns(profile)]
    column_kinds = kinds(profile)
    found: dict[str, str] = {}
    score = next(
        (n for n in names if any(h in n.lower() for h in SCORE_HINTS) and column_kinds.get(n) == "numeric"),
        None,
    )
    predicted = next((n for n in names if any(h in n.lower() for h in PREDICTION_HINTS)), None)
    if predicted:
        found["predicted"] = predicted
        base = re.sub(
            r"(^|_)(predicted|prediction|pred|forecast|estimated|fitted)(_|$)", "", predicted.lower()
        ).strip("_")
        actual = next((n for n in names if n != predicted and base and n.lower() == base), None)
        actual = actual or next(
            (n for n in names if n != predicted and base and base in n.lower() and n not in {score}), None
        )
        if actual:
            found["actual"] = actual
    if score:
        found["score"] = score
        if "actual" not in found:
            binary = [
                e["name"]
                for e in _columns(profile)
                if e.get("distinct") == 2 and e["name"] not in {predicted, score}
            ]
            if binary:
                found["actual"] = binary[0]
    return found


def _fits(chart_id: str, fields: dict[str, Any]) -> bool:
    chart = catalog.CHARTS_BY_ID[chart_id]
    return all(slot.key in fields for slot in chart.slots if slot.required)


def recommendations(profile: dict[str, Any], limit: int = 9) -> list[dict[str, Any]]:
    roles = column_roles(profile)
    num, dim, dates = roles["numeric"], roles["dimension"], roles["datetime"]
    ml = _ml_columns(profile)
    ideas: list[tuple[str, dict[str, Any], dict[str, Any], str]] = []
    if dates and num:
        ideas.append(
            ("line", {"x": dates[0], "y": [num[0]]}, {}, f"See how {num[0]} changes over {dates[0]}.")
        )
        if len(dim) and _cardinality(profile, dim[0]) <= 8:
            ideas.append(
                (
                    "stacked_area",
                    {"x": dates[0], "y": num[0], "color": dim[0]},
                    {},
                    f"How each {dim[0]} contributes to {num[0]} over time.",
                )
            )
        ideas.append(
            (
                "rolling_average",
                {"x": dates[0], "y": num[0]},
                {},
                "Smooth out day-to-day noise to reveal the trend.",
            )
        )
    if dim and num:
        ideas.append(
            ("bar", {"x": dim[0], "y": num[0]}, {"sort": "desc"}, f"Rank {dim[0]} by total {num[0]}.")
        )
        if _cardinality(profile, dim[0]) <= 8:
            ideas.append(("donut", {"x": dim[0], "y": num[0]}, {}, f"Share of {num[0]} by {dim[0]}."))
        ideas.append(
            ("box", {"y": num[0], "x": dim[0]}, {}, f"Compare the spread of {num[0]} across {dim[0]}.")
        )
        if len(dim) >= 2:
            ideas.append(
                ("treemap", {"path": dim[:2], "y": num[0]}, {}, f"Drill from {dim[0]} into {dim[1]}.")
            )
    if len(dim) >= 2:
        ideas.append(
            ("stacked_bar", {"x": dim[0], "color": dim[1]}, {}, f"Mix of {dim[1]} within each {dim[0]}.")
        )
    if len(num) >= 2:
        extra = {"color": dim[0]} if dim and _cardinality(profile, dim[0]) <= 10 else {}
        ideas.append(
            (
                "scatter",
                {"x": num[1], "y": num[0], **extra},
                {"trendline": "ols"} if not extra else {},
                f"Is {num[0]} related to {num[1]}?",
            )
        )
    if len(num) >= 3:
        ideas.append(
            ("correlation_heatmap", {}, {}, "Spot every strong relationship between measures at once.")
        )
    if num:
        ideas.append(("histogram", {"x": num[0]}, {}, f"Understand the distribution of {num[0]}."))
    if len(num) >= 4:
        ideas.append(
            ("pca_2d", {"color": dim[0]} if dim else {}, {}, "Map many measures onto two dimensions.")
        )
    if "actual" in ml and "score" in ml:
        ideas.insert(
            0,
            (
                "roc_curve",
                {"actual": ml["actual"], "score": ml["score"]},
                {},
                "Evaluate the model's scores with an ROC curve.",
            ),
        )
    if "actual" in ml and "predicted" in ml:
        numeric_pair = (
            profile
            and kinds(profile).get(ml["actual"]) == "numeric"
            and kinds(profile).get(ml["predicted"]) == "numeric"
        )
        evaluation = "predicted_vs_actual" if numeric_pair else "confusion_matrix"
        ideas.insert(
            0,
            (
                evaluation,
                {"actual": ml["actual"], "predicted": ml["predicted"]},
                {},
                "Check how close the predictions are to reality.",
            ),
        )
    location = next((c for c in kinds(profile) if any(h in c.lower() for h in GEO_HINTS)), None)
    if location and num:
        mode = "ISO-3" if "iso" in location.lower() else "country names"
        ideas.append(
            (
                "choropleth",
                {"location": location, "y": num[0]},
                {"location_mode": mode},
                f"Map {num[0]} by {location}.",
            )
        )
    seen: set[str] = set()
    result = []
    catalog_labels = {str(entry["name"]): catalog.nice(entry["name"]) for entry in _columns(profile)}
    for chart_id, fields, options, reason in ideas:
        if chart_id in seen or not _fits(chart_id, fields):
            continue
        seen.add(chart_id)
        chart = catalog.CHARTS_BY_ID[chart_id]
        result.append(
            {
                "chartType": chart_id,
                "name": chart.name,
                "category": chart.category,
                "fields": fields,
                "options": options,
                "reason": catalog.humanize_text(reason, catalog_labels, capital=True),
            }
        )
    # One idea per chart family first, so a long list of trend charts cannot crowd out the rest.
    first: list[dict[str, Any]] = []
    rest: list[dict[str, Any]] = []
    for item in result:
        repeat = any(seen["category"] == item["category"] for seen in first)
        (rest if repeat else first).append(item)
    result = first + rest
    return result[:limit]


STOPWORDS = {
    "a",
    "an",
    "and",
    "the",
    "of",
    "by",
    "for",
    "in",
    "on",
    "my",
    "me",
    "show",
    "see",
    "chart",
    "plot",
    "graph",
    "to",
    "with",
    "what",
    "which",
    "is",
    "are",
    "how",
    "this",
    "data",
    "i",
    "want",
}
INTENTS: dict[str, tuple[str, ...]] = {
    "comparison": (
        "compare",
        "comparison",
        "rank",
        "ranking",
        "top",
        "versus",
        "vs",
        "across",
        "biggest",
        "largest",
        "best",
        "worst",
        "per",
    ),
    "trend": (
        "trend",
        "time",
        "monthly",
        "daily",
        "weekly",
        "yearly",
        "growth",
        "over",
        "history",
        "seasonal",
    ),
    "composition": (
        "share",
        "proportion",
        "breakdown",
        "mix",
        "percentage",
        "percent",
        "part",
        "parts",
        "split",
    ),
    "distribution": ("distribution", "spread", "histogram", "outliers", "range", "variability", "skew"),
    "statistical": ("confidence", "significant", "significance", "test", "normal", "control", "quality"),
    "relationship": ("relationship", "correlation", "correlated", "related", "affect", "impact", "drives"),
    "flow": ("flow", "journey", "path", "funnel", "network"),
    "geo": ("map", "country", "countries", "geographic", "location", "where"),
    "dimensional": ("pca", "dimensions", "3d", "projection", "clusters", "embedding"),
    "ml": ("roc", "auc", "confusion", "accuracy", "precision", "recall", "model", "classifier", "residual"),
}


CANONICAL = {
    "bar",
    "line",
    "pie",
    "histogram",
    "scatter",
    "mean_ci",
    "sankey",
    "choropleth",
    "pca_2d",
    "roc_curve",
}


def chart_scores(prompt: str) -> list[tuple[str, float]]:
    """Score every catalog chart against a prompt: intent words, tags and names (no AI)."""
    lowered = prompt.lower()
    words = set(re.findall(r"[a-z0-9]+", lowered)) - STOPWORDS
    intents = {category for category, hints in INTENTS.items() if words & set(hints)}
    scored = []
    for chart in catalog.CHARTS:
        text = " ".join([chart.id.replace("_", " "), chart.name, *chart.tags]).lower()
        score = float(len(words & set(re.findall(r"[a-z0-9]+", text))))
        if chart.category in intents:
            score += 2
        name = chart.name.lower()
        if (" " in name and name in lowered) or re.search(
            rf"\b{re.escape(name)}\s+(chart|plot|graph|diagram|map)\b", lowered
        ):
            score += 4
        if chart.level == "essential":
            score += 0.25
        if chart.id in CANONICAL:
            score += 1
        scored.append((chart.id, score))
    return sorted(scored, key=lambda item: -item[1])


def match_charts(prompt: str, limit: int = 20) -> list[str]:
    """Rank catalog charts by relevance to a prompt (for the assistant's allow-list)."""
    return [chart_id for chart_id, _ in chart_scores(prompt)[:limit]]
