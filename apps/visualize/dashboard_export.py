"""Dashboard exports: Excel (data per visual plus the raw data), interactive HTML and PDF.

Every page is drawn exactly as a viewer first sees it: author filters, parameter defaults and
calculations resolved the same way as in the browser (``prepare_visual``). PDF pages are images
painted by the browser (they look exactly like the screen) and packed here with weasyprint.
"""

from __future__ import annotations

import base64
import binascii
import csv
import datetime as dt
import html
import io
import re
from typing import Any

from apps.analytics.models import Dataset
from apps.analytics.services import dataset_bytes
from apps.core.pdf import html_to_pdf
from apps.visualize import dashboards, services, visual_plan

MAX_PDF_PAGES = 20
MAX_IMAGE_BYTES = 8_000_000
DATA_URL = re.compile(r"^data:image/(png|jpeg);base64,([A-Za-z0-9+/=]+)$")


class ExportError(ValueError):
    """The export request is invalid (bad images, nothing to export)."""


def _resolve_ref(ref: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """A well item with its field parameter (default option) and calculation filled in."""
    out = dict(ref)
    if out.get("param"):
        parameter = next((p for p in spec.get("parameters") or [] if p.get("id") == out["param"]), None)
        if parameter and parameter.get("kind") == "fields" and parameter.get("options"):
            index = min(int(parameter.get("default") or 0), len(parameter["options"]) - 1)
            option = parameter["options"][index]
            out = {**out, **option, "grain": option.get("grain") or out.get("grain")}
    if out.get("calc"):
        calc = next((c for c in spec.get("calculations") or [] if c.get("id") == out["calc"]), None)
        if calc:
            out = {
                **out,
                "field": calc["name"],
                "expr": calc["expression"],
                "format": out.get("format") or calc.get("format"),
                "label": out.get("label") or calc["name"],
            }
    return {key: value for key, value in out.items() if value is not None}


def prepare_visual(visual: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    wells = {
        key: [_resolve_ref(ref, spec) for ref in refs]
        for key, refs in ((visual.get("data") or {}).get("wells") or {}).items()
    }
    return {**visual, "data": {**visual.get("data", {}), "wells": wells}}


def context_for(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "parameters": {
            p["name"]: p.get("default", 0) for p in spec.get("parameters") or [] if p.get("kind") == "whatif"
        },
        "calculations": {c["name"]: c["expression"] for c in spec.get("calculations") or []},
        "relationships": spec.get("relationships") or [],
    }


def render_pages(
    spec: dict[str, Any], datasets: dict[str, Dataset], mode: str, today: dt.date
) -> list[tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]]:
    """Every viewer page as ``(page, visuals, outputs)``."""
    pages = []
    for page in spec.get("pages") or []:
        if page.get("hidden") or page.get("type", "page") != "page":
            continue
        visuals = [prepare_visual(v, spec) for v in page.get("visuals") or [] if not v.get("hidden")]
        drawable = [v for v in visuals if v.get("type") not in visual_plan.LOCAL_TYPES]
        filters = [*(spec.get("filters") or []), *(page.get("filters") or [])]
        outputs = (
            dashboards.render(datasets, drawable, filters, spec.get("theme"), mode, today, context_for(spec))[
                "visuals"
            ]
            if drawable
            else []
        )
        pages.append((page, visuals, outputs))
    return pages


def _csv(columns: list[Any], rows: list[list[Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([str(c) for c in columns])
    writer.writerows(rows)
    return base64.b64encode(buffer.getvalue().encode("utf-8")).decode("ascii")


def workbook(name: str, pages: list[Any], datasets: dict[str, Dataset]) -> bytes:
    """Excel: a summary of every card, one chart sheet per chart (with its data), the raw data."""
    summary: list[tuple[str, str]] = [
        ("Dashboard", name),
        ("Exported", dt.datetime.now(dt.UTC).strftime("%d %b %Y %H:%M UTC")),
    ]
    charts: list[dict[str, Any]] = []
    for page, _visuals, outputs in pages:
        for output in outputs:
            if output.get("status") != "ok":
                continue
            title = f"{page.get('name', 'Page')} · {output.get('title') or output.get('id')}"
            if output.get("kpi"):
                summary.append((title, str(output["kpi"].get("display", ""))))
            for card in output.get("cards") or []:
                summary.append(
                    (f"{page.get('name', 'Page')} · {card.get('label')}", str(card.get("display", "")))
                )
            if isinstance(output.get("option"), dict) and output["option"].get("series"):
                charts.append({"title": title[:200], "option": output["option"]})
    tables = []
    for _page, _visuals, outputs in pages:
        for output in outputs:
            data = output.get("data") or {}
            if output.get("table") and data.get("columns"):
                tables.append(
                    {
                        "name": str(output.get("title") or "Table")[:28],
                        "data": _csv(data["columns"], data["rows"]),
                    }
                )
    raw = [
        {"name": dataset.name[:20], "data": base64.b64encode(dataset_bytes(dataset)).decode("ascii")}
        for dataset in list(datasets.values())[:4]
    ]
    output = services.engine(
        "viz_report_xlsx", None, summary=summary[:500], charts=charts[:40], tables=tables[:4], datasets=raw
    )
    return services.decode(output)


def interactive_html(name: str, description: str, pages: list[Any], mode: str) -> str:
    """A standalone HTML file: every chart interactive (ECharts inlined), cards as text."""
    blocks: list[dict[str, Any]] = []
    for page, _visuals, outputs in pages:
        cards = []
        for output in outputs:
            if output.get("status") != "ok":
                continue
            if output.get("kpi"):
                cards.append(f"{output['kpi'].get('label')}: {output['kpi'].get('display')}")
            cards.extend(f"{c.get('label')}: {c.get('display')}" for c in output.get("cards") or [])
        if cards:
            blocks.append({"type": "text", "text": f"{page.get('name', 'Page')} — " + " · ".join(cards)})
        for output in outputs:
            if (
                output.get("status") == "ok"
                and isinstance(output.get("option"), dict)
                and output["option"].get("series")
            ):
                blocks.append(
                    {
                        "type": "chart",
                        "title": output.get("title") or "",
                        "subtitle": page.get("name", ""),
                        "option": output["option"],
                        "note": output.get("insight") or "",
                    }
                )
    if not blocks:
        raise ExportError("This dashboard has nothing to export yet.")
    return services.report_html(title=name, description=description, theme=mode, blocks=blocks)


def pdf(name: str, images: list[Any]) -> bytes:
    """One landscape page per image (pages painted by the browser), packed with weasyprint."""
    if not isinstance(images, list) or not images:
        raise ExportError("Send the pages to put in the PDF.")
    if len(images) > MAX_PDF_PAGES:
        raise ExportError(f"A PDF holds at most {MAX_PDF_PAGES} pages.")
    pages = []
    for index, item in enumerate(images):
        source = item.get("image") if isinstance(item, dict) else None
        match = DATA_URL.match(source or "")
        if not match:
            raise ExportError(f"Page {index + 1} is not a PNG or JPEG image.")
        try:
            size = len(base64.b64decode(match.group(2), validate=True))
        except (binascii.Error, ValueError) as exc:
            raise ExportError(f"Page {index + 1} is not valid base64.") from exc
        if size > MAX_IMAGE_BYTES:
            raise ExportError(f"Page {index + 1} is larger than 8 MB.")
        title = html.escape(str(item.get("title") or f"Page {index + 1}")[:120])
        pages.append(f'<section><h1>{title}</h1><img src="{source}" alt="{title}"></section>')
    document = (
        "<!doctype html><html><head><meta charset='utf-8'><style>"
        "@page{size:A4 landscape;margin:10mm}body{margin:0;font-family:sans-serif}"
        "section{page-break-after:always;height:100%}section:last-child{page-break-after:auto}"
        "h1{font-size:11pt;margin:0 0 4mm;color:#334155}"
        "img{max-width:100%;max-height:175mm;display:block;margin:0 auto}"
        f"</style><title>{html.escape(name)}</title></head><body>{''.join(pages)}</body></html>"
    )

    return html_to_pdf(document)
