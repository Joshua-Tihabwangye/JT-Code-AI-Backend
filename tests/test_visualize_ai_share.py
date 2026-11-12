"""Dashboards phase 6: templates, AI (page, ask, format, suggest), explain a change, the summary
visual and public share links."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.billing.models import CreditWallet
from apps.identity.models import Role
from apps.visualize import dashboard_export, dashboard_spec, samples, visual_plan
from apps.visualize.models import DashboardShareLink
from tests.test_visualize_api import _client, _member, _org, _sample

BASE = "/api/v1/visualize"


def _model(monkeypatch, content: str | None) -> list:
    """Replace the AI gateway: answer ``content``, or fail like an unavailable provider (None)."""
    from apps.ai_gateway import service
    from apps.ai_gateway.adapters import AIGatewayError

    calls: list = []

    class Outcome:
        usage = type("U", (), {"input_tokens": 300, "output_tokens": 80})()
        run = type("R", (), {"provider_cost_usd": Decimal("0.0003")})()
        model = type("M", (), {"name": "test-model"})()

    def fake(**kwargs):
        calls.append(kwargs)
        if content is None:
            raise AIGatewayError("down")
        outcome = Outcome()
        outcome.content = content
        return outcome

    monkeypatch.setattr(service, "generate_completion", fake)
    return calls


def _render(api, spec: dict, visuals: list[dict], filters=None) -> list[dict]:
    context = dashboard_export.context_for(spec)
    body = {
        "visuals": [dashboard_export.prepare_visual(v, spec) for v in visuals],
        "filters": filters or [],
        "parameters": context["parameters"],
        "calculations": [{"name": k, "expression": v} for k, v in context["calculations"].items()],
    }
    response = api.post(f"{BASE}/visuals/", body, format="json")
    assert response.status_code == 200, response.content
    return response.json()["visuals"]


def _balance(org) -> Decimal:
    return CreditWallet.objects.get(organization=org).balance


@pytest.mark.django_db
def test_templates_guess_a_mapping_and_build_working_dashboards():
    org, owner = _org("tpl")
    api = _client(owner, org)
    dataset = _sample(api)
    gallery = api.get(f"{BASE}/dashboards/templates/", {"datasetId": dataset["id"]}).json()["templates"]
    assert len(gallery) == 9
    sales = next(t for t in gallery if t["id"] == "sales")
    assert sales["guess"]["date"] == "date" and sales["guess"]["amount"] == "revenue"
    assert all(t["roles"] and t["visualTypes"] for t in gallery)

    response = api.post(
        f"{BASE}/dashboards/from-template/",
        {"templateId": "sales", "datasetId": dataset["id"], "mapping": sales["guess"]},
        format="json",
    )
    assert response.status_code == 201, response.content
    built = response.json()
    assert built["role"] == "owner" and "Sales" in built["name"]
    page = built["spec"]["pages"][0]
    outputs = _render(
        api, built["spec"], [v for v in page["visuals"] if v["type"] not in visual_plan.LOCAL_TYPES]
    )
    assert outputs and all(o["status"] == "ok" for o in outputs), [o for o in outputs if o["status"] != "ok"]

    bad = api.post(
        f"{BASE}/dashboards/from-template/",
        {
            "templateId": "sales",
            "datasetId": dataset["id"],
            "mapping": {**sales["guess"], "amount": "region"},
        },
        format="json",
    )
    assert bad.status_code == 400 and "numeric" in bad.json()["message"]
    missing = {"templateId": "nope", "datasetId": dataset["id"], "mapping": {}}
    assert api.post(f"{BASE}/dashboards/from-template/", missing, format="json").status_code == 404

    viewer = _client(_member(org, "tpl", Role.RoleType.VIEWER), org)
    denied = viewer.post(
        f"{BASE}/dashboards/from-template/",
        {"templateId": "sales", "datasetId": dataset["id"], "mapping": sales["guess"]},
        format="json",
    )
    assert denied.status_code == 403
    stranger_org, stranger = _org("tpl-other")
    foreign = _client(stranger, stranger_org).get(
        f"{BASE}/dashboards/templates/", {"datasetId": dataset["id"]}
    )
    assert foreign.status_code == 404


@pytest.mark.django_db
def test_ai_page_keeps_only_valid_visuals_and_falls_back_to_rules(monkeypatch):
    org, owner = _org("aipage")
    api = _client(owner, org)
    dataset = _sample(api)
    proposal = {
        "title": "Regional performance overview today",
        "visuals": [
            {"type": "kpi", "values": [{"field": "revenue", "agg": "sum"}]},
            {"type": "bar", "category": "region", "values": [{"field": "profit", "agg": "sum"}], "top": 5},
            {"type": "hologram", "category": "region"},
            {"type": "bar", "category": "made_up", "values": [{"field": "revenue"}]},
            {"type": "line", "category": "date", "values": [{"field": "revenue", "agg": "drop table"}]},
        ],
        "slicers": ["region", "revenue", "made_up"],
    }
    calls = _model(monkeypatch, "Here you go: " + json.dumps(proposal))
    body = api.post(
        f"{BASE}/ai/page/", {"datasetId": dataset["id"], "prompt": "regional performance"}, format="json"
    ).json()
    assert len(calls) == 1 and body["source"] == "ai" and Decimal(body["usage"]["credits"]) > 0
    assert "regional performance" in calls[0]["messages"][1].content
    visuals = body["page"]["visuals"]
    types = [v["type"] for v in visuals]
    assert types == ["slicer", "kpi", "bar", "line"], types
    line = visuals[-1]
    assert (
        line["data"]["wells"]["values"][0]["agg"] == "sum"
        and line["data"]["wells"]["category"][0]["grain"] == "month"
    )
    assert visuals[2]["format"]["topN"]["n"] == 5
    assert body["page"]["name"] == "Regional performance overview today"
    assert all(o["status"] == "ok" for o in _render(api, {}, visuals))

    _model(monkeypatch, None)
    before = _balance(org)
    fallback = api.post(
        f"{BASE}/ai/page/",
        {"datasetId": dataset["id"], "prompt": "Profit by product and channel, with the monthly trend"},
        format="json",
    ).json()
    assert fallback["source"] == "rules" and _balance(org) == before
    fields = {
        ref["field"]
        for v in fallback["page"]["visuals"]
        for refs in v["data"]["wells"].values()
        for ref in refs
    }
    assert {"profit", "product", "channel", "date"} <= fields
    assert any(v["type"] == "line" for v in fallback["page"]["visuals"])


@pytest.mark.django_db
def test_ask_answers_with_a_visual_and_numbers_from_the_data(monkeypatch):
    org, owner = _org("aiask")
    api = _client(owner, org)
    dataset = _sample(api)
    _model(monkeypatch, None)
    answer = api.post(
        f"{BASE}/ai/ask/",
        {"datasetId": dataset["id"], "question": "Which products bring the most revenue in West?"},
        format="json",
    ).json()
    assert answer["source"] == "rules"
    visual = answer["visual"]
    assert visual["type"] == "bar" and visual["data"]["wells"]["category"][0]["field"] == "product"
    assert visual["data"]["wells"]["values"][0]["field"] == "revenue"
    assert answer["filters"] == [{"field": "region", "op": "in", "value": ["West"]}]
    assert answer["output"]["status"] == "ok" and answer["answer"]
    frame = samples.retail_sales()
    leader = frame[frame.region == "West"].groupby("product").revenue.sum().idxmax()
    assert leader in answer["answer"], answer["answer"]

    _model(monkeypatch, json.dumps({"visual": {"type": "card", "values": [{"field": "made_up"}]}}))
    vague = api.post(f"{BASE}/ai/ask/", {"datasetId": dataset["id"], "question": "hmm?"}, format="json")
    assert (
        vague.status_code == 200
        and vague.json()["visual"]["data"]["wells"]["values"][0]["field"] != "made_up"
    )


@pytest.mark.django_db
def test_auto_format_uses_keywords_without_the_model_and_the_model_otherwise(monkeypatch):
    org, owner = _org("aifmt")
    api = _client(owner, org)
    dataset = _sample(api)
    spec = api.post(f"{BASE}/dashboards/auto/", {"datasetId": dataset["id"]}, format="json").json()["spec"]
    calls = _model(monkeypatch, json.dumps({"theme": "ocean", "mode": "light", "density": "compact"}))
    styled = api.post(
        f"{BASE}/ai/format/", {"prompt": "Executive, dark and compact please", "spec": spec}, format="json"
    ).json()
    assert calls == [] and styled["source"] == "rules" and styled["usage"]["credits"] == "0"
    assert (styled["spec"]["theme"], styled["spec"]["mode"], styled["spec"]["density"]) == (
        "executive",
        "dark",
        "compact",
    )
    assert styled["changes"]["tidy"] is True
    assert dashboard_spec.clean_spec(styled["spec"]) == styled["spec"]

    modelled = api.post(
        f"{BASE}/ai/format/", {"prompt": "make it feel like the sea", "spec": spec}, format="json"
    )
    assert len(calls) == 1 and modelled.json()["spec"]["theme"] == "ocean"


@pytest.mark.django_db
def test_explain_change_names_the_members_that_moved_the_measure():
    org, owner = _org("explain")
    api = _client(owner, org)
    dataset = _sample(api)
    response = api.post(
        f"{BASE}/explain/",
        {
            "datasetId": dataset["id"],
            "measure": {"field": "revenue", "agg": "sum"},
            "dateField": "date",
            "period": ["2025-11-01T00:00:00", "2025-11-30T23:59:59"],
            "grain": "month",
        },
        format="json",
    )
    assert response.status_code == 200, response.content
    body = response.json()
    frame = samples.retail_sales()
    day = frame.date.astype("datetime64[ns]")
    now = frame[(day >= "2025-11-01") & (day <= "2025-11-30")].revenue.sum()
    before = frame[(day >= "2025-10-01") & (day <= "2025-10-31")].revenue.sum()
    assert body["current"] == pytest.approx(now) and body["previous"] == pytest.approx(before)
    assert body["headline"].startswith("Revenue is up" if now > before else "Revenue is down")
    assert 1 <= len(body["drivers"]) <= 3 and len(body["lines"]) == len(body["drivers"])
    assert body["previousPeriod"] == ["2025-10-01T00:00:00", "2025-10-31T23:59:59"], "the previous month"
    changes = [d["change"] for d in body["drivers"]]
    assert changes == sorted(changes, reverse=now > before)
    assert len(set(changes)) == len(changes), "a country of one region is not listed twice"
    assert all(
        "%" not in line or 0 < d["share"] <= 1 for line, d in zip(body["lines"], body["drivers"], strict=True)
    )

    bad = api.post(
        f"{BASE}/explain/",
        {
            "datasetId": dataset["id"],
            "measure": {"field": "revenue"},
            "dateField": "region",
            "period": ["2025-11-01", "2025-11-30"],
        },
        format="json",
    )
    assert bad.status_code == 400


@pytest.mark.django_db
def test_summary_visual_and_suggestions():
    org, owner = _org("summary")
    api = _client(owner, org)
    dataset = _sample(api)
    summary = {
        "id": "s",
        "type": "summary",
        "layout": {"x": 0, "y": 0, "w": 8, "h": 8},
        "data": {
            "datasetId": dataset["id"],
            "wells": {"category": [{"field": "region"}], "values": [{"field": "revenue"}]},
        },
    }
    [output] = _render(api, {}, [dashboard_spec.clean_visual(summary, "Summary")])
    assert output["status"] == "ok", output
    bullets = output["summary"]["bullets"]
    assert 2 <= len(bullets) <= 3 and all(len(b.split()) <= 12 for b in bullets)
    assert "Revenue" in bullets[0]

    suggestions = api.post(f"{BASE}/ai/suggest/", {"datasetId": dataset["id"]}, format="json").json()[
        "suggestions"
    ]
    assert len(suggestions) >= 3 and all(s["visual"]["type"] == "chart" and s["reason"] for s in suggestions)
    existing = [suggestions[0]["visual"]]
    again = api.post(f"{BASE}/ai/suggest/", {"datasetId": dataset["id"], "visuals": existing}, format="json")
    assert suggestions[0]["name"] not in [s["name"] for s in again.json()["suggestions"]]


@pytest.mark.django_db
def test_share_links_are_read_only_hashed_scoped_and_revocable():
    org, owner = _org("share")
    api = _client(owner, org)
    dataset = _sample(api)
    dashboard = api.post(f"{BASE}/dashboards/auto/", {"datasetId": dataset["id"]}, format="json").json()
    url = f"{BASE}/dashboards/{dashboard['id']}/share-links/"
    created = api.post(url, {"kind": "embed", "name": "Intranet"}, format="json")
    assert created.status_code == 201, created.content
    token = created.json()["token"]
    link = DashboardShareLink.objects.get()
    assert token not in link.token_hash and len(link.token_hash) == 64 and link.prefix == token[:8]
    listed = api.get(url).json()
    assert listed[0]["kind"] == "embed" and "token" not in listed[0]

    public = APIClient()
    shared = public.get(f"{BASE}/shared/{token}/")
    assert shared.status_code == 200
    spec = shared.json()["spec"]
    assert spec["settings"]["allowPersonalize"] is False and shared.json()["kind"] == "embed"
    ids = [v["id"] for v in spec["pages"][0]["visuals"]]
    drawn = public.post(f"{BASE}/shared/{token}/visuals/", {"visualIds": ids}, format="json")
    assert drawn.status_code == 200, drawn.content
    outputs = drawn.json()["visuals"]
    assert outputs and all(o["status"] == "ok" for o in outputs), [o for o in outputs if o["status"] != "ok"]
    east = public.post(
        f"{BASE}/shared/{token}/visuals/",
        {"visualIds": ["kpi-0"], "filters": [{"field": "region", "op": "in", "value": ["East"]}]},
        format="json",
    ).json()["visuals"][0]["kpi"]["value"]
    total = next(o for o in outputs if o["id"] == "kpi-0")["kpi"]["value"]
    assert 0 < east < total

    unused = sorted(set(samples.retail_sales().columns) - dashboard_spec.fields_used(dashboard["spec"]))
    assert unused, "the auto-dashboard leaves some columns unused"
    probe = {"visualIds": ["kpi-0"], "filters": [{"field": unused[0], "op": "in", "value": ["x"]}]}
    assert public.post(f"{BASE}/shared/{token}/visuals/", probe, format="json").status_code == 400
    other = api.post(
        f"{BASE}/dashboards/auto/", {"datasetId": dataset["id"], "name": "Other"}, format="json"
    ).json()
    foreign_visual = other["spec"]["pages"][0]["visuals"][0]["id"] + "-nope"
    assert (
        public.post(
            f"{BASE}/shared/{token}/visuals/", {"visualIds": [foreign_visual]}, format="json"
        ).status_code
        == 404
    )
    assert public.put(f"{BASE}/shared/{token}/", {}, format="json").status_code == 405
    assert public.get(f"{BASE}/shared/{token[:-2]}xx/").status_code == 404

    # The viewer's state per visual: drill to a level of the visual's own hierarchy, a click elsewhere.
    trend = next(
        v for v in spec["pages"][0]["visuals"] if (v["data"]["wells"].get("category") or [{}])[0].get("grain")
    )
    yearly = public.post(
        f"{BASE}/shared/{token}/visuals/",
        {
            "visuals": [
                {
                    "id": trend["id"],
                    "level": 0,
                    "filters": [{"field": "region", "op": "in", "value": ["East"]}],
                }
            ]
        },
        format="json",
    )
    assert yearly.status_code == 200, yearly.content
    assert len(yearly.json()["visuals"][0]["data"]["rows"]) == 2, "two years of data"
    sneaky = {
        "visuals": [
            {
                "id": trend["id"],
                "filters": [
                    {
                        "field": "region",
                        "op": "or",
                        "value": [{"field": unused[0], "op": "in", "value": ["x"]}],
                    }
                ],
            }
        ]
    }
    assert public.post(f"{BASE}/shared/{token}/visuals/", sneaky, format="json").status_code == 400

    viewer_user = _member(org, "share", Role.RoleType.VIEWER)
    viewer = _client(viewer_user, org)
    api.patch(f"{BASE}/dashboards/{dashboard['id']}/", {"isShared": True}, format="json")
    assert viewer.get(url).status_code == 403 and viewer.post(url, {}, format="json").status_code == 403
    assert viewer.delete(f"{url}{link.id}/").status_code == 403
    stranger_org, stranger = _org("share-other")
    assert _client(stranger, stranger_org).delete(f"{url}{link.id}/").status_code == 404

    assert api.delete(f"{url}{link.id}/").status_code == 204
    assert public.get(f"{BASE}/shared/{token}/").status_code == 404
    assert (
        public.post(f"{BASE}/shared/{token}/visuals/", {"visualIds": ids}, format="json").status_code == 404
    )

    expiring = api.post(url, {"kind": "link"}, format="json").json()
    DashboardShareLink.objects.filter(id=expiring["id"]).update(
        expires_at=dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
    )
    assert public.get(f"{BASE}/shared/{expiring['token']}/").status_code == 404
    past = api.post(url, {"expiresAt": "2020-01-01T00:00:00Z"}, format="json")
    assert past.status_code == 400
