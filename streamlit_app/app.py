"""Read-only JT-Code analytics viewer.

This is a separately deployed process. It has no Django imports, ORM access,
database credentials, or write-capable API calls. An authentication proxy must
forward the user's Authorization and optional X-Organization-ID headers.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

import httpx
import streamlit as st

API_BASE = os.environ.get("JT_CODE_API_BASE_URL", "http://localhost:8000/api/v1").rstrip("/")
DEPLOYMENT = os.environ.get("JT_CODE_STREAMLIT_ENV", "development").lower()


def forwarded_headers() -> dict[str, str]:
    incoming = st.context.headers
    authorization = incoming.get("Authorization", "")
    # A static token is intentionally limited to local development. Production
    # must preserve each user's identity through the authentication proxy.
    if not authorization and DEPLOYMENT == "development":
        token = os.environ.get("JT_CODE_API_TOKEN", "")
        if token:
            authorization = f"Bearer {token}"
    headers = {"Authorization": authorization} if authorization else {}
    if organization_id := incoming.get("X-Organization-ID", ""):
        headers["X-Organization-ID"] = organization_id
    return headers


def validate_configuration() -> str | None:
    parsed = urlparse(API_BASE)
    if DEPLOYMENT in {"staging", "production"} and parsed.scheme != "https":
        return "JT_CODE_API_BASE_URL must use HTTPS outside development."
    if not forwarded_headers().get("Authorization"):
        return "Authentication was not forwarded by the access proxy."
    return None


def fetch_visualizations(headers: dict[str, str]) -> list[dict]:
    with httpx.Client(timeout=httpx.Timeout(15, connect=5), follow_redirects=False) as client:
        response = client.get(f"{API_BASE}/visualizations/", headers=headers)
        response.raise_for_status()
        payload = response.json()
    return payload.get("results", payload) if isinstance(payload, dict) else payload


st.set_page_config(page_title="JT-Code Analytics", layout="wide")
st.title("JT-Code Analytics")
st.caption("Approved analysis outputs from the authenticated JT-Code API.")

if configuration_error := validate_configuration():
    st.error(configuration_error)
    st.stop()

try:
    items = fetch_visualizations(forwarded_headers())
except httpx.HTTPStatusError as exc:
    if exc.response.status_code in {401, 403}:
        st.error("Your session is not authorized to view analytics.")
    else:
        st.error("The analytics API returned an error.")
    st.stop()
except httpx.HTTPError, ValueError:
    st.error("The analytics API is temporarily unavailable.")
    st.stop()

if not items:
    st.info("No completed visualizations are available.")
for item in items:
    if item.get("status") != "ready":
        continue
    st.subheader(f"{item['kind'].title()} chart")
    if item.get("plotly_spec"):
        st.plotly_chart(item["plotly_spec"], width="stretch")
    if item.get("artifact_url"):
        st.link_button("Download static PNG", item["artifact_url"])
