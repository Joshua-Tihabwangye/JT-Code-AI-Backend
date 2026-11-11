"""Dashboard templates: a layout of visuals whose fields are *roles* (date, amount, category …).

A template lists its roles and a blueprint; ``guess`` proposes a column for every role from the
dataset profile (kinds and name hints), the user confirms them in a one-step wizard, and
``build`` composes the spec on the auto-dashboard grid (slicers, KPI row, main chart + side
chart, three visuals at the bottom — a 1280×720 page that fits 1366×768 without scrolling).
Blueprint entries whose optional roles are not mapped are skipped, so a template still works on a
dataset that lacks, say, a target column. No AI is involved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from apps.visualize import recommend


@dataclass(frozen=True)
class Role:
    key: str
    label: str
    kind: str  # "datetime", "numeric" or "category"
    required: bool = True
    hints: tuple[str, ...] = ()


@dataclass(frozen=True)
class Template:
    id: str
    name: str
    description: str
    color: str
    roles: tuple[Role, ...]
    # Each KPI: (role, aggregation) or ("calc:<name>", expression using {role} placeholders, format).
    kpis: tuple[tuple[str, ...], ...]
    main: dict[str, Any]
    side: dict[str, Any]
    bottom: tuple[dict[str, Any], ...]
    slicers: tuple[str, ...] = field(default_factory=tuple)


DATE = Role("date", "Date", "datetime", hints=("date", "day", "month", "time", "created", "period"))


def _r(key: str, label: str, kind: str, *hints: str, required: bool = True) -> Role:
    return Role(key, label, kind, required, hints)


TEMPLATES: tuple[Template, ...] = (
    Template(
        "sales",
        "Sales overview",
        "Revenue, profit and trends by product and region.",
        "#4f46e5",
        (
            DATE,
            _r("amount", "Revenue", "numeric", "revenue", "sales", "amount", "total"),
            _r("profit", "Profit", "numeric", "profit", "margin", "earning", required=False),
            _r("quantity", "Units", "numeric", "unit", "qty", "quantity", "volume", required=False),
            _r("target", "Target", "numeric", "target", "goal", "budget", "quota", required=False),
            _r("product", "Product", "category", "product", "item", "sku", "category"),
            _r(
                "region",
                "Region",
                "category",
                "region",
                "country",
                "city",
                "territory",
                "market",
                required=False,
            ),
            _r("channel", "Channel", "category", "channel", "source", "segment", required=False),
        ),
        kpis=(
            ("amount", "sum"),
            ("profit", "sum"),
            ("calc:Margin %", "DIVIDE(SUM([{profit}]), SUM([{amount}]))", "percent"),
            ("quantity", "sum"),
            ("calc:Vs target %", "DIVIDE(SUM([{amount}]), SUM([{target}])) - 1", "percent"),
        ),
        main={"type": "combo", "category": "date", "values": ["amount"], "secondary": ["profit"]},
        side={"type": "donut", "category": "channel", "values": ["amount"]},
        bottom=(
            {"type": "bar", "category": "product", "values": ["amount"], "top": 10},
            {"type": "matrix", "rows": "product", "columns": "region", "values": ["amount"]},
            {"type": "column", "category": "region", "values": ["amount"]},
        ),
        slicers=("date", "region", "product", "channel"),
    ),
    Template(
        "crm",
        "CRM pipeline",
        "Deals by stage and owner, won value and conversion.",
        "#0ea5e9",
        (
            DATE,
            _r("amount", "Deal value", "numeric", "value", "amount", "deal", "revenue"),
            _r("stage", "Stage", "category", "stage", "status", "phase", "pipeline"),
            _r(
                "owner",
                "Owner",
                "category",
                "owner",
                "rep",
                "salesperson",
                "agent",
                "account",
                required=False,
            ),
            _r("source", "Lead source", "category", "source", "channel", "campaign", required=False),
        ),
        kpis=(("amount", "sum"), ("amount", "count"), ("amount", "avg")),
        main={"type": "line", "category": "date", "values": ["amount"]},
        side={"type": "funnel", "category": "stage", "values": ["amount"]},
        bottom=(
            {"type": "bar", "category": "owner", "values": ["amount"], "top": 10},
            {"type": "donut", "category": "source", "values": ["amount"]},
            {"type": "matrix", "rows": "owner", "columns": "stage", "values": ["amount"]},
        ),
        slicers=("date", "stage", "owner"),
    ),
    Template(
        "finance",
        "Finance P&L",
        "Revenue, costs and margin over time and by account.",
        "#10b981",
        (
            DATE,
            _r("amount", "Revenue", "numeric", "revenue", "income", "sales", "amount"),
            _r("cost", "Costs", "numeric", "cost", "expense", "spend", "cogs"),
            _r("account", "Account / category", "category", "account", "category", "department", "type"),
            _r("budget", "Budget", "numeric", "budget", "target", "plan", required=False),
        ),
        kpis=(
            ("amount", "sum"),
            ("cost", "sum"),
            ("calc:Net", "SUM([{amount}]) - SUM([{cost}])", "number"),
            ("calc:Margin %", "DIVIDE(SUM([{amount}]) - SUM([{cost}]), SUM([{amount}]))", "percent"),
            ("calc:Vs budget %", "DIVIDE(SUM([{amount}]), SUM([{budget}])) - 1", "percent"),
        ),
        main={"type": "combo", "category": "date", "values": ["amount"], "secondary": ["cost"]},
        side={"type": "donut", "category": "account", "values": ["cost"]},
        bottom=(
            {"type": "waterfall", "category": "date", "grain": "quarter", "values": ["amount"]},
            {"type": "bar", "category": "account", "values": ["cost"], "top": 10},
            {"type": "table", "category": "account", "values": ["amount", "cost"]},
        ),
        slicers=("date", "account"),
    ),
    Template(
        "hr",
        "HR analytics",
        "Headcount, salary and satisfaction by department.",
        "#ec4899",
        (
            DATE,
            _r("employee", "Employee ID", "category", "employee", "id", "name", "person"),
            _r("department", "Department", "category", "department", "team", "division", "unit"),
            _r("salary", "Salary", "numeric", "salary", "pay", "compensation", "wage", required=False),
            _r(
                "rating",
                "Satisfaction / rating",
                "numeric",
                "satisfaction",
                "rating",
                "score",
                "engagement",
                required=False,
            ),
            _r("status", "Status", "category", "status", "attrition", "active", "left", required=False),
            _r("gender", "Gender", "category", "gender", "sex", required=False),
        ),
        kpis=(("employee", "distinct"), ("salary", "avg"), ("rating", "avg")),
        main={"type": "line", "category": "date", "values": ["employee"], "agg": "distinct"},
        side={"type": "donut", "category": "status", "values": ["employee"], "agg": "distinct"},
        bottom=(
            {"type": "bar", "category": "department", "values": ["employee"], "agg": "distinct"},
            {"type": "column", "category": "department", "values": ["salary"], "agg": "avg"},
            {
                "type": "matrix",
                "rows": "department",
                "columns": "gender",
                "values": ["employee"],
                "agg": "distinct",
            },
        ),
        slicers=("date", "department", "status"),
    ),
    Template(
        "web",
        "Web analytics",
        "Sessions, users and conversions by source and page.",
        "#f59e0b",
        (
            DATE,
            _r("sessions", "Sessions / visits", "numeric", "session", "visit", "pageview", "view"),
            _r("users", "Users", "numeric", "user", "visitor", required=False),
            _r(
                "conversions",
                "Conversions",
                "numeric",
                "conversion",
                "signup",
                "goal",
                "order",
                required=False,
            ),
            _r("source", "Traffic source", "category", "source", "channel", "medium", "referrer"),
            _r("page", "Page", "category", "page", "url", "path", "landing", required=False),
        ),
        kpis=(
            ("sessions", "sum"),
            ("users", "sum"),
            ("conversions", "sum"),
            ("calc:Conversion %", "DIVIDE(SUM([{conversions}]), SUM([{sessions}]))", "percent"),
        ),
        main={"type": "area", "category": "date", "values": ["sessions"]},
        side={"type": "donut", "category": "source", "values": ["sessions"]},
        bottom=(
            {"type": "bar", "category": "page", "values": ["sessions"], "top": 10},
            {"type": "column", "category": "source", "values": ["conversions"]},
            {"type": "table", "category": "source", "values": ["sessions", "conversions"]},
        ),
        slicers=("date", "source"),
    ),
    Template(
        "mobile",
        "Mobile app analytics",
        "Active users, installs and crashes by platform and version.",
        "#8b5cf6",
        (
            DATE,
            _r("active", "Active users", "numeric", "active", "dau", "mau", "user"),
            _r("installs", "Installs", "numeric", "install", "download", required=False),
            _r("crashes", "Crashes", "numeric", "crash", "error", required=False),
            _r("platform", "Platform", "category", "platform", "os", "device"),
            _r("version", "App version", "category", "version", "release", "build", required=False),
        ),
        kpis=(("active", "sum"), ("installs", "sum"), ("crashes", "sum")),
        main={"type": "line", "category": "date", "values": ["active"], "secondary": ["installs"]},
        side={"type": "donut", "category": "platform", "values": ["active"]},
        bottom=(
            {"type": "bar", "category": "version", "values": ["crashes"], "top": 10},
            {"type": "column", "category": "platform", "values": ["installs"]},
            {"type": "matrix", "rows": "version", "columns": "platform", "values": ["active"]},
        ),
        slicers=("date", "platform", "version"),
    ),
    Template(
        "marketing",
        "Marketing campaigns",
        "Spend, clicks, conversions and cost per conversion by campaign.",
        "#ef4444",
        (
            DATE,
            _r("spend", "Spend", "numeric", "spend", "cost", "budget"),
            _r("clicks", "Clicks", "numeric", "click", "visit", required=False),
            _r("conversions", "Conversions", "numeric", "conversion", "lead", "signup", "order"),
            _r("campaign", "Campaign", "category", "campaign", "ad", "creative"),
            _r("channel", "Channel", "category", "channel", "network", "platform", "medium", required=False),
        ),
        kpis=(
            ("spend", "sum"),
            ("clicks", "sum"),
            ("conversions", "sum"),
            ("calc:Cost per conversion", "DIVIDE(SUM([{spend}]), SUM([{conversions}]))", "number"),
        ),
        main={"type": "combo", "category": "date", "values": ["spend"], "secondary": ["conversions"]},
        side={"type": "donut", "category": "channel", "values": ["spend"]},
        bottom=(
            {"type": "bar", "category": "campaign", "values": ["conversions"], "top": 10},
            {"type": "scatter", "details": "campaign", "values": ["spend", "conversions"]},
            {"type": "table", "category": "campaign", "values": ["spend", "conversions"]},
        ),
        slicers=("date", "campaign", "channel"),
    ),
    Template(
        "inventory",
        "Inventory and operations",
        "Stock on hand, movement and reorder risk by item and site.",
        "#14b8a6",
        (
            DATE,
            _r("stock", "Stock / quantity", "numeric", "stock", "quantity", "qty", "on_hand", "inventory"),
            _r("item", "Item", "category", "item", "product", "sku", "part"),
            _r(
                "site",
                "Warehouse / site",
                "category",
                "warehouse",
                "site",
                "location",
                "store",
                "plant",
                required=False,
            ),
            _r("value", "Stock value", "numeric", "value", "cost", "amount", required=False),
            _r("reorder", "Reorder level", "numeric", "reorder", "minimum", "safety", required=False),
        ),
        kpis=(("stock", "sum"), ("value", "sum"), ("item", "distinct")),
        main={"type": "line", "category": "date", "values": ["stock"]},
        side={"type": "donut", "category": "site", "values": ["stock"]},
        bottom=(
            {"type": "bar", "category": "item", "values": ["stock"], "top": 10},
            {"type": "column", "category": "site", "values": ["value"]},
            {"type": "table", "category": "item", "values": ["stock", "reorder"]},
        ),
        slicers=("date", "site", "item"),
    ),
    Template(
        "retail",
        "Retail",
        "Store sales, basket size and best sellers.",
        "#6366f1",
        (
            DATE,
            _r("amount", "Revenue", "numeric", "revenue", "sales", "amount"),
            _r("quantity", "Units", "numeric", "unit", "qty", "quantity", required=False),
            _r("store", "Store / region", "category", "store", "region", "branch", "city", "country"),
            _r("product", "Product", "category", "product", "item", "category"),
            _r("rating", "Satisfaction", "numeric", "satisfaction", "rating", "score", required=False),
        ),
        kpis=(
            ("amount", "sum"),
            ("quantity", "sum"),
            ("calc:Revenue per unit", "DIVIDE(SUM([{amount}]), SUM([{quantity}]))", "number"),
            ("rating", "avg"),
        ),
        main={"type": "column", "category": "date", "values": ["amount"]},
        side={"type": "treemap", "category": "product", "values": ["amount"]},
        bottom=(
            {"type": "bar", "category": "store", "values": ["amount"], "top": 10},
            {"type": "matrix", "rows": "product", "columns": "store", "values": ["amount"]},
            {"type": "column", "category": "product", "values": ["quantity"]},
        ),
        slicers=("date", "store", "product"),
    ),
)
BY_ID = {template.id: template for template in TEMPLATES}


class TemplateError(ValueError):
    """The mapping does not fit the template (a required role is missing or the wrong kind)."""


def _kind(entry: dict[str, Any]) -> str:
    kind = entry.get("kind", "categorical")
    return "datetime" if kind == "datetime" else "numeric" if kind == "numeric" else "category"


def guess(template: Template, profile: dict[str, Any]) -> dict[str, str]:
    """A column for every role, from kinds and name hints (each column used at most once)."""
    columns = [entry for entry in profile.get("columnProfiles", []) if entry.get("distinct", 0) > 1]
    roles = recommend.column_roles(profile)
    used: set[str] = set()
    mapping: dict[str, str] = {}
    for role in template.roles:
        candidates = [c for c in columns if _kind(c) == role.kind and c["name"] not in used]
        if role.kind == "category":
            # Identifiers (one value per row) are fine for "distinct count" roles only.
            candidates = [
                c
                for c in candidates
                if c["name"] in roles["dimension"] or "id" in role.key or role.key == "employee"
            ]

        def score(entry: dict[str, Any], hints: tuple[str, ...] = role.hints) -> int:
            name = entry["name"].lower()
            return next((i for i, hint in enumerate(hints) if hint in name), 99)

        ranked = sorted(candidates, key=score)
        best = ranked[0] if ranked else None
        if best is not None and (role.required or score(best) < 99):
            mapping[role.key] = best["name"]
            used.add(best["name"])
    return mapping


def describe(template: Template, profile: dict[str, Any] | None = None) -> dict[str, Any]:
    visuals = [{"type": "kpi"} for _ in template.kpis[:6]] + [template.main, template.side, *template.bottom]
    return {
        "id": template.id,
        "name": template.name,
        "description": template.description,
        "color": template.color,
        "roles": [
            {"key": r.key, "label": r.label, "kind": r.kind, "required": r.required} for r in template.roles
        ],
        "visualTypes": [v["type"] for v in visuals],
        **({"guess": guess(template, profile)} if profile else {}),
    }


def build(
    template: Template, dataset_id: str, mapping: dict[str, str], profile: dict[str, Any], name: str
) -> dict[str, Any]:
    kinds = {entry["name"]: _kind(entry) for entry in profile.get("columnProfiles", [])}
    clean: dict[str, str] = {}
    for role in template.roles:
        column = mapping.get(role.key)
        if not column:
            if role.required:
                raise TemplateError(f"Choose a column for “{role.label}”.")
            continue
        if column not in kinds:
            raise TemplateError(f"{column!r} is not a column of this dataset.")
        if kinds[column] != role.kind and not (role.kind == "category" and kinds[column] != "datetime"):
            raise TemplateError(f"“{role.label}” needs a {role.kind} column; {column!r} is {kinds[column]}.")
        clean[role.key] = column
    visuals: list[dict[str, Any]] = []
    calculations: list[dict[str, Any]] = []

    def add(
        vid: str, vtype: str, layout: tuple[int, int, int, int], wells: dict[str, Any], **extra: Any
    ) -> None:
        x, y, w, h = layout
        visuals.append(
            {
                "id": vid,
                "type": vtype,
                "layout": {"x": x, "y": y, "w": w, "h": h, "z": 0},
                "data": {"datasetId": dataset_id, "wells": wells, "filters": []},
                **extra,
            }
        )

    def ref(role: str, agg: str = "sum") -> dict[str, Any]:
        return {"field": clean[role], "agg": agg}

    # Row 0: slicers.
    slicers = [role for role in template.slicers if role in clean][:4]
    width = 24 // max(len(slicers), 1) if slicers else 6
    for index, key in enumerate(slicers):
        style = "date_range" if key == "date" else "dropdown"
        add(
            f"slicer-{index}",
            "slicer",
            (index * width, 0, width, 3),
            {"category": [{"field": clean[key]}]},
            format={"slicer": {"style": style}},
        )
    # Row 3: KPIs (plain measures, or calculations when every role they use is mapped).
    cards: list[dict[str, Any]] = []
    for item in template.kpis:
        if item[0].startswith("calc:"):
            label, expression, fmt = item[0][5:], item[1], item[2]
            if any("{" + role.key + "}" in expression and role.key not in clean for role in template.roles):
                continue
            calc_id = f"calc-{len(calculations) + 1}"
            calculations.append(
                {
                    "id": calc_id,
                    "name": label,
                    "expression": expression.format(**clean),
                    "format": fmt,
                    "datasetId": dataset_id,
                }
            )
            cards.append({"values": [{"field": label, "calc": calc_id}]})
        elif item[0] in clean:
            cards.append({"values": [ref(item[0], item[1])]})
    cards = cards[:6]
    # The cards fill the row: 5 cards are 5+5+5+5+4 columns wide, not 4 each with a gap.
    widths = [24 // max(len(cards), 1) + (1 if i < 24 % max(len(cards), 1) else 0) for i in range(len(cards))]
    for index, card in enumerate(cards):
        wells = {"values": card["values"]}
        if "date" in clean:
            wells["category"] = [{"field": clean["date"]}]
        add(
            f"kpi-{index}",
            "kpi",
            (sum(widths[:index]), 3, widths[index], 5),
            wells,
            format={"kpi": {"grain": "month"}},
        )

    def blueprint(spec: dict[str, Any], vid: str, layout: tuple[int, int, int, int]) -> None:
        agg = spec.get("agg", "sum")
        vtype = spec["type"]
        if vtype == "matrix":
            if not all(spec[key] in clean for key in ("rows", "columns")):
                return
            wells = {"rows": [{"field": clean[spec["rows"]]}], "columns": [{"field": clean[spec["columns"]]}]}
        elif vtype == "scatter":
            if spec["details"] not in clean or not all(v in clean for v in spec["values"]):
                return
            wells = {"details": [{"field": clean[spec["details"]]}]}
        else:
            if spec["category"] not in clean:
                return
            category: dict[str, Any] = {"field": clean[spec["category"]]}
            if spec["category"] == "date":
                category["grain"] = spec.get("grain", "month")
            wells = {"category": [category]}
        values = [ref(v, agg) for v in spec.get("values", []) if v in clean]
        if not values:
            return
        wells["values"] = values
        secondary = [ref(v, agg) for v in spec.get("secondary", []) if v in clean]
        if vtype == "combo":
            if not secondary:
                vtype = "line"
            else:
                wells["secondaryValues"] = secondary
        extra: dict[str, Any] = {}
        if spec.get("top"):
            extra["format"] = {"topN": {"n": spec["top"], "others": False}}
        if vtype == "matrix":
            extra["format"] = {
                "conditional": {"rules": [{"kind": "scale", "min": "#eef2ff", "max": "#6366f1"}]}
            }
        add(vid, vtype, layout, wells, **extra)

    blueprint(template.main, "main", (0, 8, 16, 10))
    blueprint(template.side, "side", (16, 8, 8, 10))
    for index, spec in enumerate(template.bottom):
        blueprint(spec, f"bottom-{index}", (index * 8, 18, 8, 12))
    if not visuals:
        raise TemplateError("None of this template's visuals fit the chosen columns.")
    return {
        "theme": "executive",
        "mode": "system",
        "calculations": calculations,
        "pages": [{"id": "page-1", "name": name, "visuals": visuals}],
    }
