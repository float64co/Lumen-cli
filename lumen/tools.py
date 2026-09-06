"""Tool implementations exposed to the model: web_fetch and search."""

import re
from html.parser import HTMLParser

import requests
from ddgs import DDGS

MAX_RESULT_CHARS = 6000


class _TextExtractor(HTMLParser):
    """Very small HTML-to-text extractor (stdlib only, no bs4 dependency)."""

    _SKIP_TAGS = {"script", "style", "noscript", "head"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS and self._skip > 0:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            text = data.strip()
            if text:
                self.parts.append(text)

    def get_text(self):
        return "\n".join(self.parts)


TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch a web page and return its readable text content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Absolute URL to fetch, including http:// or https://",
                    }
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": (
                "Search the web via DuckDuckGo and return the top results "
                "(title, url, snippet) for a query."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "max_results": {
                        "type": "integer",
                        "description": "Number of results to return (default 5, max 10)",
                    },
                },
                "required": ["query"],
            },
        },
    },
]


def web_fetch(url, max_chars=MAX_RESULT_CHARS):
    if not re.match(r"^https?://", url or "", re.IGNORECASE):
        return f"Error: refusing to fetch non-http(s) URL: {url!r}"
    try:
        resp = requests.get(
            url,
            timeout=15,
            headers={"User-Agent": "lumen-cli/0.1 (+https://ollama.com)"},
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        return f"Error fetching {url}: {e}"

    content_type = resp.headers.get("Content-Type", "")
    if "html" in content_type or resp.text.lstrip().startswith("<"):
        parser = _TextExtractor()
        try:
            parser.feed(resp.text)
        except Exception:
            pass
        text = parser.get_text()
    else:
        text = resp.text

    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "\n...[truncated]"
    return text or "(empty response body)"


def search(query, max_results=5):
    try:
        max_results = max(1, min(int(max_results or 5), 10))
    except (TypeError, ValueError):
        max_results = 5

    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
    except Exception as e:
        return f"Error searching for {query!r}: {e}"

    if not results:
        return f"No results found for {query!r}."

    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "(no title)")
        href = r.get("href", "")
        body = r.get("body", "")
        lines.append(f"{i}. {title}\n   {href}\n   {body}")
    return "\n".join(lines)


DISPATCH = {"web_fetch": web_fetch, "search": search}


def specs_for(enabled):
    if not enabled:
        return TOOL_SPECS
    return [s for s in TOOL_SPECS if enabled.get(s["function"]["name"], True)]


def all_tool_names():
    return [s["function"]["name"] for s in TOOL_SPECS]


def all_disabled():
    """A tools_enabled dict that turns every known tool off."""
    return {name: False for name in all_tool_names()}


def call_tool(name, arguments, enabled=None):
    arguments = arguments or {}
    if enabled is not None and not enabled.get(name, True):
        return f"Error: tool '{name}' is disabled in config.hcl."
    if name not in DISPATCH:
        return f"Error: unknown tool '{name}'."
    try:
        if name == "web_fetch":
            return web_fetch(arguments.get("url", ""))
        if name == "search":
            return search(arguments.get("query", ""), arguments.get("max_results", 5))
    except Exception as e:
        return f"Error running tool '{name}': {e}"
    return f"Error: tool '{name}' not handled."
