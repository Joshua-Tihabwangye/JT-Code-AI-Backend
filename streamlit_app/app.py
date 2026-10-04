"""Read-only JT-Code analytics viewer (separately deployed Streamlit service).

* No Django imports, ORM access or database credentials.
* Authentication: either an authentication proxy forwards the user's
  ``Authorization`` header, or the user signs in with Supabase Auth
  (email/password) here. The Supabase session lives only in this browser
  session; the service holds only the publishable key, no privileged keys.
* Every JT-Code API call is a ``GET`` against curated, tenant-scoped endpoints
  (``/visualizations/`` and ``/analysis/runs/``); the server enforces dataset
  ACLs. The organization comes from ``X-Organization-ID`` or ``?org=``.
"""

from __future__ import annotations

import os
import time
from typing import Any
from urllib.parse import urlparse

import httpx
import streamlit as st

API_BASE = os.environ.get("JT_CODE_API_BASE_URL", "http://localhost:8000/api/v1").rstrip("/")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_PUBLISHABLE_KEY = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "")
DEPLOYMENT = os.environ.get("JT_CODE_STREAMLIT_ENV", "development").lower()
MAX_PAGES = 20
TIMEOUT = httpx.Timeout(15, connect=5)


def supabase_sign_in(email: str, password: str) -> dict[str, Any]:
    """Exchange credentials for a Supabase session (the only non-GET request)."""
    with httpx.Client(timeout=TIMEOUT, follow_redirects=False) as client:
        response = client.post(
            f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
            headers={"apikey": SUPABASE_PUBLISHABLE_KEY},
            json={"email": email, "password": password},
        )
    response.raise_for_status()
    session = response.json()
    session["expires_at"] = time.time() + float(session.get("expires_in", 3600))
    return session


def access_token() -> str:
    """Forwarded bearer token, or the signed-in Supabase session token."""
    forwarded = st.context.headers.get("Authorization", "")
    if forwarded.lower().startswith("bearer "):
        return forwarded.split(" ", 1)[1]
    session = st.session_state.get("supabase_session")
    if session and session.get("expires_at", 0) > time.time() + 30:
        return str(session["access_token"])
    st.session_state.pop("supabase_session", None)
    return ""


def organization_id() -> str:
    return st.context.headers.get("X-Organization-ID", "") or st.query_params.get("org", "")


def api_headers(token: str) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if org := organization_id():
        headers["X-Organization-ID"] = org
    return headers


def api_get_all(path: str, headers: dict[str, str]) -> list[dict[str, Any]]:
    """GET a list endpoint and follow ``next`` links (bounded)."""
    items: list[dict[str, Any]] = []
    url: str | None = f"{API_BASE}{path}"
    with httpx.Client(timeout=TIMEOUT, follow_redirects=False) as client:
        for _page in range(MAX_PAGES):
            if url is None:
                break
            response = client.get(url, headers=headers)
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, list):
                return items + payload
            items.extend(payload.get("results", []))
            next_url = payload.get("next")
            url = next_url if next_url and next_url.startswith(API_BASE) else None
    return items


def fetch_visualizations(headers: dict[str, str]) -> list[dict[str, Any]]:
    return api_get_all("/visualizations/", headers)


def fetch_runs(headers: dict[str, str]) -> list[dict[str, Any]]:
    return api_get_all("/analysis/runs/", headers)


def configuration_error() -> str | None:
    if DEPLOYMENT in {"staging", "production"}:
        if urlparse(API_BASE).scheme != "https":
            return "JT_CODE_API_BASE_URL must use HTTPS outside development."
        if SUPABASE_URL and urlparse(SUPABASE_URL).scheme != "https":
            return "SUPABASE_URL must use HTTPS outside development."
    return None


def sign_in_form() -> None:
    if not (SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY):
        st.error(
            "Sign-in is unavailable: configure SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY or an auth proxy."
        )
        st.stop()
    with st.form("sign-in"):
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in")
    if submitted:
        try:
            st.session_state["supabase_session"] = supabase_sign_in(email, password)
            st.rerun()
        except httpx.HTTPError:
            st.error("Sign-in failed. Check your email and password.")
    st.stop()


def render() -> None:
    st.set_page_config(page_title="JT-Code Analytics", layout="wide")
    st.title("JT-Code Analytics")
    st.caption("Approved analysis outputs from the authenticated JT-Code API.")
    if error := configuration_error():
        st.error(error)
        st.stop()
    token = access_token()
    if not token:
        sign_in_form()
    headers = api_headers(token)
    try:
        visualizations = fetch_visualizations(headers)
        runs = fetch_runs(headers)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {401, 403}:
            st.session_state.pop("supabase_session", None)
            st.error("Your session is not authorized to view analytics for this organization.")
        else:
            st.error("The analytics API returned an error.")
        st.stop()
    except httpx.HTTPError, ValueError:
        st.error("The analytics API is temporarily unavailable.")
        st.stop()

    charts_tab, results_tab = st.tabs(["Charts", "Analysis results"])
    with charts_tab:
        ready = [item for item in visualizations if item.get("status") == "ready"]
        if not ready:
            st.info("No completed visualizations are available.")
        for item in ready:
            st.subheader(item.get("title") or f"{item['kind'].title()} chart")
            if item.get("plotly_spec"):
                st.plotly_chart(item["plotly_spec"], width="stretch")
            elif item.get("spec_url"):
                st.link_button("Open interactive chart spec", item["spec_url"])
            if item.get("artifact_url"):
                st.link_button("Download static PNG", item["artifact_url"])
    with results_tab:
        completed = [run for run in runs if run.get("status") == "completed"]
        if not completed:
            st.info("No completed analysis runs are available.")
        for run in completed:
            schema = run.get("result_schema") or {}
            st.subheader(f"Run {run['id'][:8]} · {schema.get('rows', 0)} rows")
            st.dataframe(run.get("result_preview") or [], width="stretch")
            if run.get("result_download_url"):
                st.link_button("Download CSV", run["result_download_url"])


if __name__ == "__main__" or st.runtime.exists():
    render()
