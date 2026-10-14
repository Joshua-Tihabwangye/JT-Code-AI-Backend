"""Dashboards API: auto-dashboard, Python-built visuals over the Postgres store, versions, sharing,
personal views and themes."""

from __future__ import annotations

import pytest

from apps.visualize import store
from apps.visualize.models import Dashboard, DashboardVersion
from tests.test_visualize_api import _client, _member, _org, _sample

BASE = "/api/v1/visualize"


def _auto(api, dataset_id: str) -> dict:
    response = api.post(f"{BASE}/dashboards/auto/", {"datasetId": dataset_id}, format="json")
    assert response.status_code == 201, response.content
    return response.json()


def _visuals(api, dashboard: dict, filters=None, **extra) -> dict:
    page = dashboard["spec"]["pages"][0]
    response = api.post(
        f"{BASE}/visuals/",
        {"visuals": page["visuals"], "filters": filters or [], "mode": "dark", "theme": "midnight", **extra},
        format="json",
    )
    assert response.status_code == 200, response.content
    return response.json()


@pytest.mark.django_db
def test_auto_dashboard_draws_every_visual_from_the_query_store_and_caches_it():
    org, owner = _org("dash-auto")
    api = _client(owner, org)
    dataset = _sample(api)
    dashboard = _auto(api, dataset["id"])
    assert dashboard["role"] == "owner" and dashboard["version"] == 1
    visuals = dashboard["spec"]["pages"][0]["visuals"]
    types = [v["type"] for v in visuals]
    assert types.count("kpi") >= 6 and types.count("slicer") == 4
    assert {"combo", "donut", "bar", "matrix", "map"} <= set(types)
    assert max(v["layout"]["y"] + v["layout"]["h"] for v in visuals) <= 30, "fits a 1280×720 page"

    first = _visuals(api, dashboard)
    assert all(v["status"] == "ok" for v in first["visuals"]), [
        v for v in first["visuals"] if v["status"] != "ok"
    ]
    assert first["meta"]["sqlQueries"] > 0 and first["meta"]["engineRuns"] == 1, first["meta"]
    kpi = next(v for v in first["visuals"] if v["type"] == "kpi")
    assert kpi["kpi"]["display"] and kpi["kpi"]["delta"]["display"][0] in "▲▼■"

    again = _visuals(api, dashboard)
    assert again["meta"]["cachedVisuals"] == len(visuals) and again["meta"]["engineRuns"] == 0

    filtered = _visuals(api, dashboard, filters=[{"field": "region", "op": "in", "value": ["East"]}])
    total = next(v for v in first["visuals"] if v["id"] == "kpi-0")["kpi"]["value"]
    east = next(v for v in filtered["visuals"] if v["id"] == "kpi-0")["kpi"]["value"]
    assert 0 < east < total
    slicer = next(v for v in filtered["visuals"] if v["id"] == "slicer-1")
    assert len(slicer["slicer"]["options"]) > 1, "a slicer keeps listing its own options"


@pytest.mark.django_db
def test_gallery_charts_run_over_the_query_store():
    org, owner = _org("dash-chart")
    api = _client(owner, org)
    dataset = _sample(api)
    visual = {
        "id": "box",
        "type": "chart",
        "data": {"datasetId": dataset["id"], "wells": {}},
        "format": {"chart": {"chartType": "box", "fields": {"y": "revenue", "x": "region"}}},
        "layout": {"x": 0.25, "y": 1.5, "w": 6.75, "h": 8},
    }
    out = api.post(
        f"{BASE}/visuals/",
        {
            "visuals": [visual],
            "filters": [{"field": "channel", "op": "in", "value": ["Online"]}],
            "mode": "light",
        },
        format="json",
    ).json()
    assert out["visuals"][0]["status"] == "ok", out
    assert out["meta"]["sqlQueries"] == 1
    saved = api.post(
        f"{BASE}/dashboards/",
        {"name": "Free layout", "spec": {"pages": [{"id": "p", "visuals": [visual]}]}},
        format="json",
    )
    assert saved.json()["spec"]["pages"][0]["visuals"][0]["layout"] == {
        "x": 0.25,
        "y": 1.5,
        "w": 6.75,
        "h": 8,
        "z": 0,
    }


@pytest.mark.django_db
def test_the_engine_answers_when_the_query_store_is_unavailable(monkeypatch):
    org, owner = _org("dash-fallback")
    api = _client(owner, org)
    dashboard = _auto(api, _sample(api)["id"])

    def broken(dataset):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(store, "ensure_table", broken)
    out = _visuals(
        api, dashboard, filters=[{"field": "channel", "op": "in", "value": ["Online"]}], today="2026-01-15"
    )
    assert all(v["status"] == "ok" for v in out["visuals"])
    assert out["meta"]["sqlQueries"] == 0 and out["meta"]["engineRuns"] == 1


@pytest.mark.django_db
def test_saving_uses_versions_and_snapshots():
    org, owner = _org("dash-save")
    api = _client(owner, org)
    dashboard = _auto(api, _sample(api)["id"])
    url = f"{BASE}/dashboards/{dashboard['id']}/"
    spec = dashboard["spec"]
    spec["pages"][0]["name"] = "Sales"
    saved = api.patch(url, {"spec": spec, "version": 1}, format="json")
    assert saved.status_code == 200 and saved.json()["version"] == 2
    stale = api.patch(url, {"name": "Other", "version": 1}, format="json")
    assert stale.status_code == 409 and stale.json()["details"]["currentVersion"] == 2
    bad = api.patch(
        url, {"spec": {"pages": [{"id": "p", "visuals": [{"id": "v", "type": "hologram"}]}]}}, format="json"
    )
    assert bad.status_code == 400 and "hologram" in bad.json()["message"]
    snapshot = api.post(f"{url}versions/", {"name": "Before cleanup"}, format="json")
    assert snapshot.status_code == 201
    numbers = [v["number"] for v in api.get(f"{url}versions/").json()]
    assert numbers == sorted(numbers, reverse=True) and len(numbers) >= 2
    restored = api.post(f"{url}versions/1/restore/", {}, format="json")
    assert restored.status_code == 200 and restored.json()["spec"]["pages"][0]["name"] == "Overview"
    assert DashboardVersion.objects.filter(
        dashboard_id=dashboard["id"], name__startswith="Before restoring"
    ).exists()
    listed = api.get(f"{BASE}/dashboards/").json()
    rows = listed["results"] if isinstance(listed, dict) else listed
    assert rows[0]["pages"] == 1 and "spec" not in rows[0] and rows[0]["datasetIds"]


@pytest.mark.django_db
def test_sharing_viewers_personal_views_and_editors():
    org, owner = _org("dash-share")
    api = _client(owner, org)
    dataset = _sample(api)
    dashboard = _auto(api, dataset["id"])
    url = f"{BASE}/dashboards/{dashboard['id']}/"
    viewer = _member(org, "dash-share", "viewer")
    viewer_api = _client(viewer, org)
    assert viewer_api.get(url).status_code == 404, "private until shared"
    assert api.patch(url, {"isShared": True}, format="json").status_code == 200
    shared = viewer_api.get(url)
    assert shared.status_code == 200 and shared.json()["role"] == "viewer"
    assert viewer_api.patch(url, {"name": "Mine"}, format="json").status_code == 403
    # The viewer cannot open the dataset itself, but sees it inside the shared dashboard.
    blocked = _visuals(viewer_api, shared.json())
    assert all(v["status"] == "error" for v in blocked["visuals"] if v["type"] != "text")
    allowed = _visuals(viewer_api, shared.json(), dashboardId=dashboard["id"])
    assert all(v["status"] == "ok" for v in allowed["visuals"])
    # Personalize without touching the author's version.
    state = {"visualOverrides": {"trend": {"type": "line"}}, "slicers": {"slicer-1": ["East"]}}
    personal = viewer_api.put(f"{url}personal-view/", {"state": state}, format="json")
    assert (
        personal.status_code == 200 and personal.json()["state"]["visualOverrides"]["trend"]["type"] == "line"
    )
    assert viewer_api.get(f"{url}personal-view/").json()["state"]["slicers"] == {"slicer-1": ["East"]}
    assert Dashboard.objects.get(id=dashboard["id"]).spec["pages"][0]["visuals"][6]["type"] != "line"
    assert viewer_api.delete(f"{url}personal-view/").status_code == 204
    assert viewer_api.get(f"{url}personal-view/").status_code == 204
    # An editor member may change it.
    editor = _member(org, "dash-share-ed", "editor")
    assert (
        api.put(f"{url}members/", [{"userId": editor.id, "role": "editor"}], format="json").status_code == 200
    )
    editor_api = _client(editor, org)
    assert editor_api.patch(url, {"name": "Team overview"}, format="json").status_code == 200
    assert editor_api.delete(url).status_code == 403, "only the owner deletes"
    outsider_org, outsider = _org("dash-outsider")
    assert (
        api.put(f"{url}members/", [{"userId": outsider.id, "role": "viewer"}], format="json").status_code
        == 400
    )
    assert _client(outsider, outsider_org).get(url).status_code == 404


@pytest.mark.django_db
def test_themes_and_removed_reports():
    org, owner = _org("dash-themes")
    api = _client(owner, org)
    built_in = api.get(f"{BASE}/themes/builtin/").json()
    assert len(built_in["themes"]) == 13 and built_in["default"] == "executive"
    assert all(t["light"]["canvas"] and t["dark"]["canvas"] for t in built_in["themes"])
    custom = api.post(
        f"{BASE}/themes/",
        {
            "name": "Brand",
            "spec": {"dataColors": ["#112233", "#445566", "#778899", "javascript:alert(1)"], "radius": 99},
        },
        format="json",
    )
    assert custom.status_code == 201
    spec = custom.json()["spec"]
    assert spec["dataColors"] == ["#112233", "#445566", "#778899"] and spec["radius"] == 24
    assert api.get(f"{BASE}/reports/").status_code == 404, "reports were removed"


@pytest.mark.django_db
def test_dashboards_cannot_use_datasets_the_author_cannot_open():
    org, owner = _org("dash-ds")
    api = _client(owner, org)
    other_org, other = _org("dash-ds-other")
    foreign = _sample(_client(other, other_org))
    spec = {
        "pages": [
            {
                "id": "p1",
                "visuals": [{"id": "v1", "type": "card", "data": {"datasetId": foreign["id"], "wells": {}}}],
            }
        ]
    }
    response = api.post(f"{BASE}/dashboards/", {"name": "Sneaky", "spec": spec}, format="json")
    assert response.status_code == 400
