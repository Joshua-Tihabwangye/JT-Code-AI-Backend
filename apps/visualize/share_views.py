"""Public, read-only shared dashboards and embeds (``/shared/<token>/``).

A link only reveals its dashboard: the client asks for visuals by id and the spec comes from the
server, filters may only use columns the dashboard already uses, the datasets are the ones the
link's dashboard owner can analyze, and nothing can be changed. Unknown, revoked and expired
tokens all answer the same 404.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.analytics.access import datasets_analyzable_by
from apps.core.throttling import _client_ip, _rate, _Throttle
from apps.visualize import dashboard_export, dashboard_spec, dashboards, themes, visual_plan
from apps.visualize.dashboard_views import SpecInvalid, share_hash
from apps.visualize.models import DashboardShareLink
from apps.visualize.serializers import SharedVisualsSerializer

TOUCH_EVERY = dt.timedelta(minutes=5)


class ShareThrottle(_Throttle):
    """Anonymous viewers: the per-IP ceiling and a budget per link."""

    scope = "visualize_share"

    def allow_request(self, request: Request, view: Any) -> bool:
        if not self._check(f"ip:{_client_ip(request)}", _rate("ip")):
            return False
        token = str(view.kwargs.get("token") or "")
        return self._check(f"{self.scope}:link:{share_hash(token)[:24]}", _rate(self.scope))


def _link(token: str) -> DashboardShareLink:
    if not 20 <= len(token) <= 80:
        raise NotFound("This link is not valid.")
    link = (
        DashboardShareLink.objects.select_related("dashboard", "dashboard__owner", "dashboard__organization")
        .filter(token_hash=share_hash(token), revoked_at__isnull=True)
        .first()
    )
    now = timezone.now()
    if link is None or (link.expires_at is not None and link.expires_at <= now):
        raise NotFound("This link is not valid.")
    if link.last_used_at is None or now - link.last_used_at > TOUCH_EVERY:
        DashboardShareLink.objects.filter(pk=link.pk).update(last_used_at=now)
    return link


DATE_LEVELS = ("year", "quarter", "month", "day")
DRILLABLE = {
    "bar",
    "column",
    "line",
    "area",
    "combo",
    "donut",
    "pie",
    "treemap",
    "funnel",
    "waterfall",
    "table",
    "matrix",
}


def drilled(visual: dict[str, Any], level: Any) -> dict[str, Any]:
    """The visual at a level of its own drill hierarchy (as the browser's ``drilled``): the next
    category field, or a finer date grain. Nothing outside the hierarchy can be asked for."""
    if level is None or visual.get("type") not in DRILLABLE:
        return visual
    key = "rows" if visual["type"] == "matrix" else "category"
    wells = visual["data"].get("wells") or {}
    refs = wells.get(key) or []
    if len(refs) > 1 and visual["type"] not in {"matrix", "table"}:
        levels = refs
    elif refs and refs[0].get("grain"):
        levels = [{**refs[0], "grain": grain} for grain in DATE_LEVELS]
    else:
        return visual
    try:
        index = max(0, min(int(level), len(levels) - 1))
    except TypeError, ValueError:
        return visual
    return {**visual, "data": {**visual["data"], "wells": {**wells, key: [levels[index]]}}}


def _clean_filters(raw: Any, allowed: set[str]) -> list[dict[str, Any]]:
    items = raw if isinstance(raw, list) else []
    if len(items) > dashboard_spec.MAX_FILTERS:
        raise dashboard_spec.SpecError("Too many filters.")
    out = [dashboard_spec.clean_filter(item, "Filters") for item in items]
    for item in out:
        nested = item["value"] if item["op"] == "or" and isinstance(item.get("value"), list) else []
        fields = {item["field"], *(n.get("field") for n in nested if isinstance(n, dict))}
        if not fields <= allowed:
            raise dashboard_spec.SpecError("Shared dashboards can only be filtered on the fields they show.")
    return out


def public_pages(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Visible pages, plus the tooltip and drill-through pages they open (usually hidden)."""
    return [p for p in spec.get("pages") or [] if not p.get("hidden") or p.get("type") != "page"]


class _Public(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list[Any] = []
    throttle_classes = [ShareThrottle]


class SharedDashboardView(_Public):
    @extend_schema(responses={200: OpenApiTypes.OBJECT})
    def get(self, request: Request, token: str) -> Response:
        link = _link(token)
        dashboard = link.dashboard
        spec = dict(dashboard.spec)
        # Viewers of a public link cannot personalize, and hidden pages stay hidden.
        spec["settings"] = {**(spec.get("settings") or {}), "allowPersonalize": False}
        spec["pages"] = public_pages(spec)
        return Response(
            {
                "id": str(dashboard.id),
                "name": dashboard.name,
                "description": dashboard.description,
                "kind": link.kind,
                "spec": spec,
                # Public viewers cannot list the built-in themes, so the theme comes resolved.
                "theme": spec.get("theme")
                if isinstance(spec.get("theme"), dict)
                else next((t for t in themes.public_themes() if t["id"] == spec.get("theme")), None),
                "updatedAt": dashboard.updated_at,
            }
        )


class SharedVisualsView(_Public):
    @extend_schema(request=SharedVisualsSerializer, responses={200: OpenApiTypes.OBJECT})
    def post(self, request: Request, token: str) -> Response:
        link = _link(token)
        dashboard = link.dashboard
        serializer = SharedVisualsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        spec = dashboard.spec
        pages = public_pages(spec)
        page_of = {v["id"]: page for page in pages for v in page.get("visuals") or []}
        by_id = {v["id"]: v for page in pages for v in page.get("visuals") or []}
        values = {str(k)[:40]: v for k, v in list((data.get("parameters") or {}).items())[:50]}
        allowed = dashboard_spec.fields_used(spec)
        asked = [{"id": i} for i in data.get("visualIds") or []] + [
            item
            for item in data.get("visuals") or []
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        try:
            filters = _clean_filters(data.get("filters"), allowed)
            visuals = []
            for item in {item["id"]: item for item in asked}.values():
                base = by_id.get(item["id"])
                if base is None or base.get("type") in visual_plan.LOCAL_TYPES:
                    continue
                visual = drilled(dashboard_export.prepare_visual(base, spec, values), item.get("level"))
                extra = _clean_filters(item.get("filters"), allowed)
                highlight = _clean_filters(item.get("highlight"), allowed)
                own = visual["data"].get("filters") or []
                visual["data"] = {**visual["data"], "filters": [*own, *extra], "highlight": highlight}
                visuals.append(visual)
        except dashboard_spec.SpecError as exc:
            raise SpecInvalid(str(exc)) from exc
        if not visuals:
            raise NotFound("Visual not found.")
        # The author's locked and hidden filters always apply (the viewer cannot remove them).
        page = page_of[visuals[0]["id"]]
        if any(page_of[v["id"]] is not page for v in visuals):
            raise SpecInvalid("Ask for the visuals of one page at a time.")
        fixed = [
            f
            for f in [*(spec.get("filters") or []), *(page.get("filters") or [])]
            if f.get("locked") or f.get("hidden")
        ]
        wanted = dashboard_spec.dataset_ids(spec) | {
            end["datasetId"] for r in spec.get("relationships") or [] for end in (r["from"], r["to"])
        }
        datasets = {
            str(d.id): d
            for d in datasets_analyzable_by(dashboard.owner, dashboard.organization).filter(id__in=wanted)
        }
        context = dashboard_export.context_for(spec, values)
        today = data.get("today") or timezone.localdate()
        rendered = dashboards.render(
            datasets, visuals, [*fixed, *filters], spec.get("theme"), data["mode"], today, context
        )
        return Response(rendered)
