import asyncio
import gzip
import io
import json
import os
import zlib
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx

from ..errors import HarnessError


MAX_QUERY_CHARS = 400
MAX_RESULTS = 5
MAX_BODY_BYTES = 512 * 1024
SEARCH_TIMEOUT = 15
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"


def configured_search_provider():
    """Return only the provider name for the active environment."""
    if os.environ.get("BAT_SEARXNG_URL", "").strip():
        return "searxng"
    if os.environ.get("BRAVE_SEARCH_API_KEY", "").strip():
        return "brave"
    return None


def _http_url(value):
    if not isinstance(value, str) or len(value) > 2048 or not value.isprintable():
        return False
    if any(c.isspace() or c in '\\<>"' for c in value):
        return False
    try:
        parts = urlsplit(value)
        return (parts.scheme in ("http", "https") and bool(parts.hostname)
                and "@" not in parts.netloc and parts.port != 0)
    except ValueError:
        return False


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []
        self.hidden = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden = True
        elif tag in ("br", "p", "div", "li"):
            self.text.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = False
        elif tag in ("p", "div", "li"):
            self.text.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)


def _plain_text(value, limit):
    if not isinstance(value, str):
        return ""
    parser = _PlainText()
    try:
        parser.feed(value[:4 * limit])
        parser.close()
    except (ValueError, AssertionError):
        return ""
    text = " ".join("".join(parser.text).split())
    return "".join(c for c in text if c.isprintable())[:limit]


def _source_results(data, provider):
    if provider == "brave" and isinstance(data, dict):
        if "web" not in data and data.get("type") == "search":
            return []
        data = data.get("web")
    rows = data.get("results") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise HarnessError("search_invalid_response", "The search provider omitted its JSON results list. Check the configured service.", "tool")
    results = []
    seen = set()
    snippet_key = "content" if provider == "searxng" else "description"
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = row.get("url")
        if not _http_url(url) or url in seen:
            continue
        title = _plain_text(row.get("title"), 200)
        if title:
            seen.add(url)
            results.append({"title": title, "url": url, "snippet": _plain_text(row.get(snippet_key), 500)})
            if len(results) == MAX_RESULTS:
                break
    return results


def _check_status(status, provider):
    if 200 <= status < 300:
        return
    if 300 <= status < 400:
        raise HarnessError("search_redirect", "The search endpoint redirected. Configure the final search endpoint.", "tool")
    if status in (401, 403):
        hint = ("Check access and enable JSON in SearXNG search.formats." if provider == "searxng"
                else "Check the Brave Search API key and Web Search subscription.")
        raise HarnessError("search_access_denied", hint, "tool")
    if status == 429:
        raise HarnessError("search_rate_limited", "The search provider rate limit was reached. Wait or check the search quota.", "tool", retryable=True)
    if status >= 500:
        raise HarnessError("search_unavailable", "The search service is temporarily unavailable. Try again later.", "tool", retryable=True)
    raise HarnessError("search_request_rejected", f"The search provider returned HTTP {status}. Check the query and search configuration.", "tool")


async def _read_body(response):
    encoding = response.headers.get("Content-Encoding", "identity").lower()
    if encoding not in ("identity", "gzip"):
        raise HarnessError("search_invalid_response", "The search service returned an unsupported encoding. Configure identity or gzip output.", "tool")
    size_error = "Search results exceeded the response size limit. Narrow the query."
    body = bytearray()
    async for chunk in response.aiter_raw():
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise HarnessError("search_response_too_large", size_error, "tool")
        body.extend(chunk)
    if encoding == "gzip":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(body)) as compressed:
                body = compressed.read(MAX_BODY_BYTES + 1)
        except (EOFError, OSError, zlib.error):
            raise HarnessError("search_invalid_response", "The search service returned damaged gzip data. Check the endpoint or try again.", "tool") from None
        if len(body) > MAX_BODY_BYTES:
            raise HarnessError("search_response_too_large", size_error, "tool")
    return body


async def web_search(query):
    query = query.strip()
    if not query or not query.isprintable() or len(query) > MAX_QUERY_CHARS:
        raise HarnessError("invalid_tool_arguments", "Enter a single-line search query of 1 to 400 printable characters.", "tool")
    provider = configured_search_provider()
    headers = {"Accept": "application/json", "Accept-Encoding": "gzip, identity"}
    params = {"q": query}
    if provider == "searxng":
        url = os.environ["BAT_SEARXNG_URL"].strip()
        if not _http_url(url) or "?" in url or "#" in url:
            raise HarnessError("search_configuration", "Set BAT_SEARXNG_URL to the full HTTP(S) search endpoint without credentials, query or fragment.", "tool")
        params["format"] = "json"
    elif provider == "brave":
        url = BRAVE_URL
        token = os.environ["BRAVE_SEARCH_API_KEY"].strip()
        if not token.isascii() or not all(33 <= ord(c) <= 126 for c in token):
            raise HarnessError("search_configuration", "Check the characters in BRAVE_SEARCH_API_KEY.", "tool")
        headers["X-Subscription-Token"] = token
        params["count"] = MAX_RESULTS
    else:
        raise HarnessError("search_configuration", "Configure BAT_SEARXNG_URL or BRAVE_SEARCH_API_KEY to enable web search.", "tool")

    try:
        async with asyncio.timeout(SEARCH_TIMEOUT), httpx.AsyncClient(timeout=SEARCH_TIMEOUT, follow_redirects=False) as client:
            async with client.stream("GET", url, params=params, headers=headers) as response:
                _check_status(response.status_code, provider)
                body = await _read_body(response)
    except (TimeoutError, httpx.TimeoutException):
        raise HarnessError("search_timeout", "The search request timed out. Check the connection or try again later.", "tool", retryable=True) from None
    except (httpx.HTTPError, httpx.InvalidURL, OSError, ValueError):
        raise HarnessError("search_connection", "Could not reach the search provider. Check the endpoint, proxy and TLS configuration.", "tool", retryable=True) from None
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        raise HarnessError("search_invalid_response", "The search service returned invalid JSON. Check the endpoint and its JSON output settings.", "tool") from None
    return {"provider": provider, "results": _source_results(data, provider)}
