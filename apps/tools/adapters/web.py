"""Browser/search adapter: public web fetch and search behind the egress policy."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any

from django.conf import settings

from apps.tools.egress import safe_request
from apps.tools.gateway import ToolDenied, ToolExecutionError
from apps.tools.registry import ToolContext, ToolSpec
from apps.tools.registry import register as register_spec

_TEXT_TYPES = ("text/html", "text/plain", "application/json", "application/xhtml+xml", "text/markdown")


class _TextExtractor(HTMLParser):
    """Collect visible text, skipping scripts, styles and other non-content blocks."""

    SKIP = {"script", "style", "noscript", "template", "svg", "head"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skipping = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self.SKIP:
            self._skipping += 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skipping:
            self._skipping -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skipping and data.strip():
            self.parts.append(data.strip())


def html_to_text(html: str) -> tuple[str, str]:
    parser = _TextExtractor()
    parser.feed(html)
    return parser.title.strip(), re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


def _allowed_domains(ctx: ToolContext) -> list[str] | None:
    domains = ctx.config.get("allowed_domains")
    return [str(domain).lower() for domain in domains] if isinstance(domains, list) and domains else None


def fetch(ctx: ToolContext, arguments: dict[str, Any]) -> str:
    response = safe_request("GET", arguments["url"], allowed_hosts=_allowed_domains(ctx))
    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type not in _TEXT_TYPES:
        raise ToolDenied("UNSUPPORTED_CONTENT", f"Refusing to read {content_type or 'unknown'} content.")
    if response.status_code >= 400:
        raise ToolExecutionError(f"The page returned HTTP {response.status_code}.")
    if content_type in {"text/html", "application/xhtml+xml"}:
        title, text = html_to_text(response.text)
    else:
        title, text = "", response.text
    limit = int(arguments.get("max_chars") or 8000)
    return f"Title: {title or '(none)'}\nURL: {response.url}\n\n{text[:limit]}"


def search(ctx: ToolContext, arguments: dict[str, Any]) -> str:
    if not settings.SEARCH_API_KEY:
        raise ToolDenied("NOT_CONFIGURED", "Web search is not configured (SEARCH_API_KEY).")
    response = safe_request(
        "GET",
        settings.SEARCH_API_BASE,
        params={"q": arguments["query"], "count": int(arguments.get("count") or 5)},
        headers={"Accept": "application/json", "X-Subscription-Token": settings.SEARCH_API_KEY},
    )
    if response.status_code >= 400:
        raise ToolExecutionError(f"Search provider returned HTTP {response.status_code}.")
    results = ((response.json() or {}).get("web") or {}).get("results") or []
    if not results:
        return "No results."
    return "\n".join(
        f"[{index}] {item.get('title', '')}\n{item.get('url', '')}\n{item.get('description', '')}"
        for index, item in enumerate(results[:10], start=1)
    )


def register() -> None:
    register_spec(
        ToolSpec(
            name="web.fetch",
            description="Fetch a public web page over HTTPS and return its readable text.",
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "format": "uri", "maxLength": 2000, "pattern": "^https://"},
                    "max_chars": {"type": "integer", "minimum": 200, "maximum": 20000},
                },
                "required": ["url"],
            },
            handler=fetch,
            timeout_seconds=float(settings.EXTERNAL_API_TIMEOUT_SECONDS) + 5,
            default_enabled=settings.BROWSER_TOOL_ENABLED,
        )
    )
    register_spec(
        ToolSpec(
            name="web.search",
            description="Search the public web and return titles, URLs and snippets.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 400},
                    "count": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                "required": ["query"],
            },
            handler=search,
            timeout_seconds=float(settings.EXTERNAL_API_TIMEOUT_SECONDS) + 5,
            default_enabled=settings.BROWSER_TOOL_ENABLED,
        )
    )
