"""GitHub adapter (GitHub App) with least-privilege, per-call tokens.

* Authentication is a GitHub App: an RS256 App JWT is exchanged for an
  installation token restricted to **one repository** and the **minimum
  permissions** the operation needs (``contents: read`` for reads; ``contents:
  write`` / ``pull_requests: write`` for writes).
* The tenant's ``github`` credential stores the installation id and the
  repository allowlist; nothing outside the allowlist is reachable.
* Writes are branch/PR-first: only branches under ``TOOL_GITHUB_BRANCH_PREFIX``
  can be created or committed to, and the default/protected branches never.
* Branch, commit and pull-request tools are side-effecting (human approval).
"""

from __future__ import annotations

import base64
import re
import time
from typing import Any
from urllib.parse import quote

import jwt
from django.conf import settings
from django.core.cache import cache

from apps.tools.credentials import credential_for
from apps.tools.egress import safe_request
from apps.tools.gateway import ToolDenied, ToolExecutionError
from apps.tools.registry import EDITOR, ToolContext, ToolSpec
from apps.tools.registry import register as register_spec

REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
PROTECTED = frozenset({"main", "master", "trunk", "develop", "production", "release"})
READ = {"contents": "read", "metadata": "read"}
WRITE_CONTENTS = {"contents": "write", "metadata": "read"}
WRITE_PULLS = {"pull_requests": "write", "contents": "read", "metadata": "read"}


def _api_host() -> list[str]:
    host = re.sub(r"^https://", "", settings.GITHUB_API_BASE).split("/")[0]
    return [host]


def _app_jwt() -> str:
    if not settings.GITHUB_APP_ID or not settings.GITHUB_APP_PRIVATE_KEY:
        raise ToolDenied("NOT_CONFIGURED", "The GitHub App is not configured (GITHUB_APP_ID/PRIVATE_KEY).")
    now = int(time.time())
    key = settings.GITHUB_APP_PRIVATE_KEY.replace("\\n", "\n")
    return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": str(settings.GITHUB_APP_ID)}, key, "RS256")


def _request(method: str, path: str, token: str, json: Any = None) -> Any:
    response = safe_request(
        method,
        f"{settings.GITHUB_API_BASE.rstrip('/')}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "jt-code-tools",
        },
        json=json,
        allowed_hosts=_api_host(),
    )
    if response.status_code >= 400:
        detail = ""
        try:
            detail = str((response.json() or {}).get("message", ""))[:200]
        except ValueError:
            detail = response.text[:200]
        raise ToolExecutionError(f"GitHub returned HTTP {response.status_code}: {detail}")
    return response.json() if response.content else {}


def _scope(ctx: ToolContext, repository: str) -> tuple[str, str, str]:
    """Return ``(owner, repo, installation_id)`` after allowlist checks."""
    if not REPO.fullmatch(repository or ""):
        raise ToolDenied("INVALID_ARGUMENTS", "repository must look like 'owner/name'.")
    credential, _secret = credential_for(ctx.organization_id, "github")
    allowed = {str(repo).lower() for repo in credential.metadata.get("repositories", [])}
    if repository.lower() not in allowed:
        raise ToolDenied("REPOSITORY_NOT_ALLOWED", f"{repository} is not in this organization's allowlist.")
    installation = str(credential.metadata.get("installation_id", ""))
    if not installation.isdigit():
        raise ToolDenied("NOT_CONFIGURED", "The GitHub connection has no installation_id.")
    owner, repo = repository.split("/", 1)
    return owner, repo, installation


def installation_token(installation_id: str, repo: str, permissions: dict[str, str]) -> str:
    """A repository- and permission-scoped installation token (cached until near expiry)."""
    cache_key = f"gh-token:{installation_id}:{repo}:{sorted(permissions.items())}"
    if cached := cache.get(cache_key):
        return str(cached)
    body = _request(
        "POST",
        f"/app/installations/{installation_id}/access_tokens",
        _app_jwt(),
        json={"repositories": [repo], "permissions": permissions},
    )
    token = str(body.get("token", ""))
    if not token:
        raise ToolExecutionError("GitHub did not issue an installation token.")
    cache.set(cache_key, token, timeout=45 * 60)  # tokens live 60 minutes
    return token


def _require_managed_branch(branch: str) -> None:
    prefix = settings.TOOL_GITHUB_BRANCH_PREFIX
    if not branch.startswith(prefix) or branch.lower() in PROTECTED or ".." in branch:
        raise ToolDenied(
            "BRANCH_NOT_ALLOWED",
            f"Writes are only allowed on new branches named '{prefix}…', never {branch!r}.",
        )


def list_repositories(ctx: ToolContext, arguments: dict[str, Any]) -> str:
    credential, _secret = credential_for(ctx.organization_id, "github")
    repos = credential.metadata.get("repositories", [])
    return "Repositories available to this organization:\n" + "\n".join(f"- {repo}" for repo in repos)


def read_file(ctx: ToolContext, arguments: dict[str, Any]) -> str:
    owner, repo, installation = _scope(ctx, arguments["repository"])
    token = installation_token(installation, repo, READ)
    path = quote(arguments["path"].lstrip("/"))
    ref = f"?ref={quote(arguments['ref'])}" if arguments.get("ref") else ""
    body = _request("GET", f"/repos/{owner}/{repo}/contents/{path}{ref}", token)
    if not isinstance(body, dict) or body.get("type") != "file":
        raise ToolExecutionError("Path is not a file.")
    if int(body.get("size", 0)) > settings.TOOL_MAX_RESPONSE_BYTES:
        raise ToolExecutionError("File is too large to read.")
    return base64.b64decode(body.get("content", "")).decode("utf-8", errors="replace")


def list_tree(ctx: ToolContext, arguments: dict[str, Any]) -> str:
    owner, repo, installation = _scope(ctx, arguments["repository"])
    token = installation_token(installation, repo, READ)
    ref = quote(arguments.get("ref") or "HEAD")
    body = _request("GET", f"/repos/{owner}/{repo}/git/trees/{ref}?recursive=1", token)
    paths = [item["path"] for item in body.get("tree", []) if item.get("type") == "blob"][:500]
    suffix = "\n(truncated)" if body.get("truncated") or len(paths) == 500 else ""
    return "\n".join(paths) + suffix


def create_branch(ctx: ToolContext, arguments: dict[str, Any]) -> str:
    owner, repo, installation = _scope(ctx, arguments["repository"])
    branch = arguments["branch"]
    _require_managed_branch(branch)
    token = installation_token(installation, repo, WRITE_CONTENTS)
    base = arguments.get("from_ref") or _request("GET", f"/repos/{owner}/{repo}", token)["default_branch"]
    sha = _request("GET", f"/repos/{owner}/{repo}/git/ref/heads/{quote(base)}", token)["object"]["sha"]
    _request(
        "POST", f"/repos/{owner}/{repo}/git/refs", token, json={"ref": f"refs/heads/{branch}", "sha": sha}
    )
    return f"Created branch {branch} from {base} ({sha[:7]})."


def commit_file(ctx: ToolContext, arguments: dict[str, Any]) -> str:
    owner, repo, installation = _scope(ctx, arguments["repository"])
    branch = arguments["branch"]
    _require_managed_branch(branch)
    token = installation_token(installation, repo, WRITE_CONTENTS)
    path = quote(arguments["path"].lstrip("/"))
    payload: dict[str, Any] = {
        "message": arguments["message"],
        "content": base64.b64encode(arguments["content"].encode()).decode(),
        "branch": branch,
    }
    try:
        existing = _request("GET", f"/repos/{owner}/{repo}/contents/{path}?ref={quote(branch)}", token)
        payload["sha"] = existing.get("sha")
    except ToolExecutionError:
        pass  # new file
    body = _request("PUT", f"/repos/{owner}/{repo}/contents/{path}", token, json=payload)
    return f"Committed {arguments['path']} to {branch} ({str(body.get('commit', {}).get('sha', ''))[:7]})."


def create_pull_request(ctx: ToolContext, arguments: dict[str, Any]) -> str:
    owner, repo, installation = _scope(ctx, arguments["repository"])
    head = arguments["head"]
    _require_managed_branch(head)
    token = installation_token(installation, repo, WRITE_PULLS)
    base = arguments.get("base") or _request("GET", f"/repos/{owner}/{repo}", token)["default_branch"]
    body = _request(
        "POST",
        f"/repos/{owner}/{repo}/pulls",
        token,
        json={"title": arguments["title"], "head": head, "base": base, "body": arguments.get("body", "")},
    )
    return f"Opened pull request #{body.get('number')}: {body.get('html_url', '')}"


_REPO = {"type": "string", "pattern": REPO.pattern, "maxLength": 140}
_BRANCH = {"type": "string", "minLength": 1, "maxLength": 200, "pattern": r"^[A-Za-z0-9._/-]+$"}
_PATH = {"type": "string", "minLength": 1, "maxLength": 500, "pattern": r"^(?!.*\.\.)[^\x00]+$"}


def register() -> None:
    reads = [
        (
            "github.list_repositories",
            "List repositories this organization allows the agent to access.",
            {"type": "object", "properties": {}},
            list_repositories,
        ),
        (
            "github.read_file",
            "Read a text file from an allowed repository.",
            {
                "type": "object",
                "properties": {"repository": _REPO, "path": _PATH, "ref": _BRANCH},
                "required": ["repository", "path"],
            },
            read_file,
        ),
        (
            "github.list_tree",
            "List file paths in an allowed repository.",
            {
                "type": "object",
                "properties": {"repository": _REPO, "ref": _BRANCH},
                "required": ["repository"],
            },
            list_tree,
        ),
    ]
    for name, description, parameters, handler in reads:
        register_spec(
            ToolSpec(
                name=name, description=description, parameters=parameters, handler=handler, provider="github"
            )
        )
    writes = [
        (
            "github.create_branch",
            "Create a new working branch (requires approval).",
            {
                "type": "object",
                "properties": {"repository": _REPO, "branch": _BRANCH, "from_ref": _BRANCH},
                "required": ["repository", "branch"],
            },
            create_branch,
        ),
        (
            "github.commit_file",
            "Create or update one file on a working branch (requires approval).",
            {
                "type": "object",
                "properties": {
                    "repository": _REPO,
                    "branch": _BRANCH,
                    "path": _PATH,
                    "content": {"type": "string", "maxLength": 200000},
                    "message": {"type": "string", "minLength": 1, "maxLength": 500},
                },
                "required": ["repository", "branch", "path", "content", "message"],
            },
            commit_file,
        ),
        (
            "github.create_pull_request",
            "Open a pull request from a working branch (requires approval).",
            {
                "type": "object",
                "properties": {
                    "repository": _REPO,
                    "head": _BRANCH,
                    "base": _BRANCH,
                    "title": {"type": "string", "minLength": 1, "maxLength": 250},
                    "body": {"type": "string", "maxLength": 20000},
                },
                "required": ["repository", "head", "title"],
            },
            create_pull_request,
        ),
    ]
    for name, description, parameters, handler in writes:
        register_spec(
            ToolSpec(
                name=name,
                description=description,
                parameters=parameters,
                handler=handler,
                side_effect=True,
                min_role=EDITOR,
                provider="github",
            )
        )
