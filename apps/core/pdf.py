"""HTML -> PDF for user-authored content, without file or network access.

Documents and conversions render Markdown, which passes raw HTML through. With
WeasyPrint's default fetcher, ``<link rel="attachment" href="file:///proc/self/environ">``
or ``<img src="http://169.254.169.254/...">`` would embed server files or fetch
internal URLs into the PDF. Only inline ``data:`` URIs are allowed.
"""

from __future__ import annotations


def html_to_pdf(html: str) -> bytes:
    from weasyprint import HTML
    from weasyprint.urls import URLFetcher

    fetcher = URLFetcher(allowed_protocols={"data"}, allow_redirects=False)
    return bytes(HTML(string=html, url_fetcher=fetcher).write_pdf())
