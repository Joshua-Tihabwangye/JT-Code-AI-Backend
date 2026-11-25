#!/usr/bin/env python3
"""Live security probes against the local Docker stack (``make docker-up``): the attacks of the audit's
penetration-surface table, run from outside the way an attacker would.

* **Tokens:** ``alg: none``, a wrong key, an expired token, a non-``authenticated`` role, a tampered
  payload and a correctly signed token with no live Supabase session must all be refused (401).
* **Tenancy:** user B naming A's workspace in ``X-Organization-ID`` is refused; mass assignment of
  ``is_staff``/``is_superuser``/``email``/plan through ``PATCH /accounts/me/`` changes nothing.
* **Input:** SQL metacharacters, NUL bytes and oversized values in query strings never cause a 5xx.
* **Browser surface:** CORS from a foreign origin is not allowed on the API or Supabase Auth; API
  responses carry CSP, ``nosniff``, frame protection, a referrer policy and ``no-store``; no server
  banner.
* **API keys:** a read key reads but cannot write, manage keys or reach the account, is pinned to its
  workspace and stops working when revoked.
* **Webhooks:** unsafe endpoint URLs are refused; a test event leaves through the worker and the egress
  layer and its answer is recorded.
* **Support:** cases stay in their workspace; customers reply and close but cannot assign.
* **Audit log:** admins read it; malformed filters are 400; the CSV export is scoped to the workspace.
* **Consents:** personal, never deleted; terms are not created or withdrawn through the API.
* **Gateway:** ``/rest/v1`` and ``/auth/v1/admin`` are closed on the public port.
* **Auth abuse:** a weak password is refused by GoTrue; repeated wrong-password sign-ins from one IP
  hit a rate limit.

Usage: scripts/security_probe.py    (stdlib only; secrets are read from .env.docker in-process, never printed)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from system_check import API, ENV, SUPABASE, create_confirmed_user, db, http  # noqa: E402

FOREIGN_ORIGIN = "https://evil.example"
results: list[tuple[str, bool, str]] = []


def probe(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  ({detail})" if detail else ""))


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def token(claims: dict[str, Any], *, secret: str | None, alg: str = "HS256") -> str:
    header = b64(json.dumps({"alg": alg, "typ": "JWT"}).encode())
    payload = b64(json.dumps(claims).encode())
    signing_input = f"{header}.{payload}".encode()
    signature = b64(hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()) if secret else ""
    return f"{header}.{payload}.{signature}"


def sign_in(email: str, password: str) -> dict[str, Any]:
    anon = ENV.get("SUPABASE_ANON_KEY", "")
    status, _, body = http(
        "POST",
        f"{SUPABASE}/auth/v1/token?grant_type=password",
        {"email": email, "password": password},
        {"apikey": anon, "Authorization": f"Bearer {anon}"},
    )
    if status != 200:
        raise SystemExit(f"sign-in failed: {status}")
    return body


def claims_of(jwt: str) -> dict[str, Any]:
    part = jwt.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def me(jwt: str, extra: dict[str, str] | None = None) -> int:
    return http("GET", f"{API}/accounts/me/", headers={"Authorization": f"Bearer {jwt}", **(extra or {})})[0]


def main() -> int:
    run = uuid.uuid4().hex[:8]
    users = {}
    for name in ("a", "b"):
        email, password = f"probe-{name}-{run}@jt-code.local", f"Probe-{run}-{name.upper()}-Passw0rd!"
        create_confirmed_user(email, password, "Probe", name.upper())
        session = sign_in(email, password)
        users[name] = {
            "email": email,
            "token": session["access_token"],
            "claims": claims_of(session["access_token"]),
        }
    a, b = users["a"], users["b"]
    print(f"Security probes ({a['email']}, {b['email']})")

    # --- Tokens
    print("tokens")
    probe("valid session accepted", me(a["token"]) == 200)
    secret = ENV.get("SUPABASE_JWT_SECRET", "")
    now = int(time.time())
    base = {**a["claims"], "iat": now, "exp": now + 600}
    forged = {
        "alg none": token(base, secret=None, alg="none"),
        "wrong signing key": token(base, secret="not-the-project-secret-" + run),
        "expired (real key)": token({**base, "iat": now - 7200, "exp": now - 3600}, secret=secret),
        "role service_role (real key)": token({**base, "role": "service_role"}, secret=secret),
        "role anon (real key)": token({**base, "role": "anon"}, secret=secret),
        "wrong audience (real key)": token({**base, "aud": "someone-else"}, secret=secret),
        "no live session (real key)": token({**base, "session_id": str(uuid.uuid4())}, secret=secret),
    }
    for label, jwt in forged.items():
        status = me(jwt)
        probe(f"{label} refused", status == 401, str(status))
    header, _, signature = a["token"].split(".")
    tampered = f"{header}.{b64(json.dumps({**a['claims'], 'sub': b['claims']['sub']}).encode())}.{signature}"
    status = me(tampered)
    probe("payload tampered to another user's id refused", status == 401, str(status))

    # --- Tenancy and mass assignment
    print("tenancy")
    auth_a, auth_b = {"Authorization": f"Bearer {a['token']}"}, {"Authorization": f"Bearer {b['token']}"}
    orgs = http("GET", f"{API}/organizations/", headers=auth_a)[2]
    org_a = next((o["id"] for o in orgs.get("organizations", []) if o.get("is_owner")), None)
    status, _, body = http(
        "GET", f"{API}/conversations/", headers={**auth_b, "X-Organization-ID": str(org_a)}
    )
    probe("B naming A's workspace (X-Organization-ID) refused", status in (403, 404), str(status))
    status = http("GET", f"{API}/conversations/", headers={**auth_b, "X-Organization-ID": "not-a-uuid"})[0]
    probe("malformed X-Organization-ID refused without 5xx", 400 <= status < 500, str(status))

    before = http("GET", f"{API}/accounts/me/", headers=auth_a)[2]
    http(
        "PATCH",
        f"{API}/accounts/me/",
        {
            "is_staff": True,
            "is_superuser": True,
            "isStaff": True,
            "email": f"attacker-{run}@evil.example",
            "plan": "enterprise",
            "credits": 10**9,
            "supabase_user_id": b["claims"]["sub"],
        },
        auth_a,
    )
    after = http("GET", f"{API}/accounts/me/", headers=auth_a)[2]
    stored = db(
        "import json; from apps.identity.models import User; "
        f"u = User.objects.get(supabase_user_id={a['claims']['sub']!r}); "
        "print(json.dumps({'staff': u.is_staff, 'superuser': u.is_superuser, 'email': u.email}))"
    )
    probe(
        "mass assignment (staff, superuser, e-mail, plan, id) ignored",
        not stored["staff"]
        and not stored["superuser"]
        and stored["email"] == a["email"]
        and after.get("plan") == before.get("plan"),
        f"staff={stored['staff']} superuser={stored['superuser']} email kept={stored['email'] == a['email']}",
    )

    # --- Hostile input
    print("input")
    for label, value in {
        "SQL metacharacters": "' OR 1=1; DROP TABLE identity_user;--",
        "NUL byte": "abc\x00def",
        "64 KiB value": "x" * 65536,
        "path traversal": "../../../../etc/passwd",
    }.items():
        query = urllib.parse.quote(value)
        status, _, body = http("GET", f"{API}/conversations/?q={query}", headers=auth_a)
        leaked = isinstance(body, str) and "Traceback" in body
        probe(f"{label} in a search query: no 5xx, no traceback", status < 500 and not leaked, str(status))

    # --- Browser surface
    print("browser surface")
    for label, url, request_headers in (
        ("API", f"{API}/conversations/", "authorization,content-type"),
        (
            "Supabase Auth",
            f"{SUPABASE}/auth/v1/token?grant_type=password",
            "apikey,authorization,content-type",
        ),
    ):
        status, response_headers, _ = http(
            "OPTIONS",
            url,
            headers={
                "Origin": FOREIGN_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": request_headers,
            },
        )
        allowed = {k.lower(): v for k, v in response_headers.items()}.get("access-control-allow-origin")
        probe(
            f"CORS from a foreign origin refused ({label})",
            allowed not in ("*", FOREIGN_ORIGIN),
            f"ACAO={allowed}",
        )

    status, response_headers, _ = http("GET", f"{API}/accounts/me/", headers=auth_a)
    lower = {k.lower(): v for k, v in response_headers.items()}
    csp = lower.get("content-security-policy", "")
    for label, ok in (
        ("Content-Security-Policy", bool(csp)),
        ("X-Content-Type-Options nosniff", lower.get("x-content-type-options") == "nosniff"),
        ("frame protection", lower.get("x-frame-options", "").upper() == "DENY" or "frame-ancestors" in csp),
        ("Referrer-Policy", bool(lower.get("referrer-policy"))),
        ("Cache-Control no-store on an authenticated response", "no-store" in lower.get("cache-control", "")),
        ("no server banner", "uvicorn" not in lower.get("server", "").lower()),
    ):
        probe(f"API header: {label}", ok)

    # --- Organization API keys (X-API-Key)
    print("api keys")
    status, _, created = http(
        "POST", f"{API}/api-keys/", {"name": f"probe-{run}", "scopes": ["read"]}, auth_a
    )
    probe(
        "a member creates a read key (shown once)",
        status == 201 and str(created.get("key", "")).startswith("jtk_"),
    )
    key = {"X-API-Key": created.get("key", "")}
    status = http("GET", f"{API}/conversations/", headers=key)[0]
    probe("the read key reads its workspace", status == 200, str(status))
    status = http("POST", f"{API}/conversations/", {"title": "via key"}, key)[0]
    probe("the read key cannot write", status == 403, str(status))
    status = http("POST", f"{API}/api-keys/", {"name": "persist", "scopes": ["read", "write"]}, key)[0]
    probe("a key cannot create keys", status == 403, str(status))
    status = http("GET", f"{API}/accounts/me/", headers=key)[0]
    probe("a key cannot reach the account", status == 403, str(status))
    status = http("GET", f"{API}/conversations/", headers={**key, "X-Organization-ID": str(uuid.uuid4())})[0]
    probe("a key is pinned to its workspace", status == 403, str(status))
    listed = http("GET", f"{API}/api-keys/", headers=auth_b)[2]
    rows = listed.get("results", listed) if isinstance(listed, dict) else listed
    probe("another workspace does not see the key", created.get("id") not in str(rows))
    http("POST", f"{API}/api-keys/{created.get('id')}/revoke/", headers=auth_a)
    status = http("GET", f"{API}/conversations/", headers=key)[0]
    probe("a revoked key stops working at once", status == 401, str(status))

    # --- Outbound webhooks
    print("webhooks")
    for label, url in (
        ("plain http", "http://example.com/jt-code-probe"),
        ("loopback", "https://localhost/jt-code-probe"),
        ("private network", "https://10.0.0.1/jt-code-probe"),
        ("non-443 port", "https://example.com:8443/jt-code-probe"),
    ):
        status = http("POST", f"{API}/webhooks/", {"name": "probe", "url": url}, auth_a)[0]
        probe(f"a webhook to a {label} address is refused", status == 400, str(status))
    status, _, hook = http(
        "POST",
        f"{API}/webhooks/",
        {"name": f"probe-{run}", "url": "https://example.com/jt-code-probe"},
        auth_a,
    )
    probe(
        "an admin adds a webhook (secret shown once)",
        status == 201 and str(hook.get("secret", "")).startswith("whsec_"),
    )
    if status == 201:
        http("POST", f"{API}/webhooks/{hook['id']}/test/", headers=auth_a)
        result: dict[str, Any] = {}
        for _ in range(40):
            listed = http("GET", f"{API}/webhooks/{hook['id']}/deliveries/", headers=auth_a)[2]
            rows = listed.get("results", listed) if isinstance(listed, dict) else listed
            if rows and rows[0].get("status") not in ("pending", "retrying"):
                result = rows[0]
                break
            time.sleep(1.5)
        # example.com (IANA documentation domain) refuses the POST: the signed request left through the
        # egress layer and its answer was recorded.
        probe(
            "a test event is sent by the worker and its answer recorded",
            result.get("event_type") == "webhook.test" and bool(result.get("response_status")),
            "{} HTTP {} {}".format(
                result.get("status"), result.get("response_status"), result.get("error_message", "")
            ),
        )
        http("DELETE", f"{API}/webhooks/{hook['id']}/", headers=auth_a)
    listed = http("GET", f"{API}/webhooks/", headers=auth_b)[2]
    probe("another workspace does not see the webhook", hook.get("id", "-") not in json.dumps(listed))

    # --- Support cases
    print("support")
    status, _, case = http(
        "POST",
        f"{API}/support-cases/",
        {"subject": f"probe {run}", "description": "Security probe.", "category": "other", "priority": "low"},
        auth_a,
    )
    probe("a member opens a support case", status == 201, str(status))
    case_id = case.get("id", "-") if isinstance(case, dict) else "-"
    status = http("GET", f"{API}/support-cases/{case_id}/", headers=auth_b)[0]
    probe("another workspace cannot read the case", status == 404, str(status))
    status = http("POST", f"{API}/support-cases/{case_id}/messages/", {"body": "hi"}, auth_b)[0]
    probe("another workspace cannot reply to the case", status == 404, str(status))
    status = http(
        "POST", f"{API}/support-cases/{case_id}/assign/", {"assigned_to_id": str(uuid.uuid4())}, auth_a
    )[0]
    probe("customers cannot assign cases", status == 404, str(status))
    status = http("POST", f"{API}/support-cases/{case_id}/messages/", {"body": "More detail."}, auth_a)[0]
    probe("the customer replies", status == 201, str(status))
    http("POST", f"{API}/support-cases/{case_id}/close/", headers=auth_a)
    status = http("POST", f"{API}/support-cases/{case_id}/messages/", {"body": "again"}, auth_a)[0]
    probe("a closed case takes no reply", status == 409, str(status))

    # --- Audit log
    print("audit log")
    status = http("GET", f"{API}/audit-events/", headers=auth_a)[0]
    probe("an admin reads the workspace audit log", status == 200, str(status))
    for label, params in (("actor", "actor=not-a-uuid"), ("date", "end_date=2026-13-45")):
        status = http("GET", f"{API}/audit-events/?{params}", headers=auth_a)[0]
        probe(f"a malformed {label} filter is a 400, not a 500", status == 400, str(status))
    status, response_headers, body = http("GET", f"{API}/audit-events/export/", headers=auth_a)
    content_type = {k.lower(): v for k, v in response_headers.items()}.get("content-type", "")
    probe(
        "the audit export is CSV",
        status == 200 and content_type.startswith("text/csv"),
        f"{status} {content_type}",
    )
    text = body if isinstance(body, str) else json.dumps(body)
    probe("the export holds no other workspace's entries", b["email"] not in text)

    # --- Consents
    print("consents")
    status, _, created = http(
        "POST", f"{API}/consents/", {"consent_type": "marketing", "status": "granted", "version": run}, auth_a
    )
    probe("a member records an optional consent", status == 201, str(status))
    consent_id = created.get("id", "-") if isinstance(created, dict) else "-"
    status = http("GET", f"{API}/consents/{consent_id}/", headers=auth_b)[0]
    probe("another user cannot read the consent", status == 404, str(status))
    status = http("DELETE", f"{API}/consents/{consent_id}/", headers=auth_a)[0]
    probe("consent records cannot be deleted", status == 405, str(status))
    status = http(
        "POST", f"{API}/consents/", {"consent_type": "terms", "status": "granted", "version": "x"}, auth_a
    )[0]
    probe("terms cannot be created or replaced through the API", status == 400, str(status))

    # --- Gateway
    print("gateway")
    anon = ENV.get("SUPABASE_ANON_KEY", "")
    for path in ("/rest/v1/", "/rest/v1/identity_user?select=*", "/auth/v1/admin/users"):
        status = http(
            "GET", f"{SUPABASE}{path}", headers={"apikey": anon, "Authorization": f"Bearer {anon}"}
        )[0]
        probe(f"public gateway closes {path}", status in (403, 404), str(status))

    # --- Auth abuse
    print("auth abuse")
    supa = {"apikey": anon, "Authorization": f"Bearer {anon}"}
    status, _, body = http(
        "POST",
        f"{SUPABASE}/auth/v1/signup",
        {"email": f"weak-{run}@example.com", "password": "abcdefg"},
        supa,
    )
    probe("weak password (7 letters) refused by GoTrue", status in (400, 422), f"{status} {str(body)[:80]}")
    limited_at = None
    for attempt in range(1, 61):
        status = http(
            "POST",
            f"{SUPABASE}/auth/v1/token?grant_type=password",
            {"email": f"nobody-{run}@example.com", "password": "Wrong-Passw0rd"},
            supa,
        )[0]
        if status == 429:
            limited_at = attempt
            break
    probe(
        "repeated wrong-password sign-ins are rate limited", limited_at is not None, f"429 after {limited_at}"
    )

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} probes passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
