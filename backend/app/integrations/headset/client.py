"""Clients for the Headset MCP server.

HeadsetSource is the four calls the sync needs. Two implementations:

  McpHeadsetClient   - talks MCP over streamable HTTP (JSON-RPC 2.0) to the URL
                       in HEADSET_MCP_URL with a bearer token. Written against the
                       MCP spec; run `headset-sync --start ... --end ...` with your
                       own endpoint to exercise it.
  StaticSource       - answers from dicts you hand it. Used by the tests and by
                       anyone replaying pulls made through another MCP client.

Tool results come back as JSON text inside `result.content[0].text` (or as
`structuredContent`); both are handled.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Protocol

import httpx

TOOL_STORES = "retailer_get_stores"
TOOL_INVENTORY = "retailer_get_inventory"
TOOL_BY_DIMENSION = "retailer_sales_by_dimension"
TOOL_TREND = "retailer_sales_trend"

ALL_MEASURES = [
    "total_revenue", "total_gross_sales", "total_units", "total_discounts", "total_cost", "total_profit", "transaction_count",
]


class HeadsetError(RuntimeError):
    pass


class HeadsetSource(Protocol):
    def get_stores(self) -> dict: ...
    def get_inventory(self, **arguments: Any) -> dict: ...
    def sales_by_dimension(self, **arguments: Any) -> dict: ...
    def sales_trend(self, **arguments: Any) -> dict: ...


class StaticSource:
    """A HeadsetSource backed by a callable or fixed results per tool name."""

    def __init__(self, handlers: dict[str, Any]):
        self.handlers = handlers
        self.calls: list[tuple[str, dict]] = []

    def _call(self, tool: str, arguments: dict) -> dict:
        self.calls.append((tool, arguments))
        h = self.handlers.get(tool)
        if h is None:
            raise HeadsetError(f"no handler for {tool}")
        return h(arguments) if callable(h) else h

    def get_stores(self) -> dict:
        return self._call(TOOL_STORES, {})

    def get_inventory(self, **arguments: Any) -> dict:
        return self._call(TOOL_INVENTORY, arguments)

    def sales_by_dimension(self, **arguments: Any) -> dict:
        return self._call(TOOL_BY_DIMENSION, arguments)

    def sales_trend(self, **arguments: Any) -> dict:
        return self._call(TOOL_TREND, arguments)


class McpHeadsetClient:
    """Minimal MCP streamable-HTTP client: initialize once, then tools/call."""

    PROTOCOL_VERSION = "2025-06-18"

    def __init__(self, url: str, token: str = "", timeout: float = 180.0, client: httpx.Client | None = None,
                 token_provider=None):
        if not url:
            raise HeadsetError("HEADSET_MCP_URL is not set")
        self.url = url
        self.token = token
        self._token_provider = token_provider   # () -> str; OAuth store, refreshed on use
        self._http = client or httpx.Client(timeout=timeout)
        self._session_id: str | None = None
        self._next_id = 0
        self._initialized = False

    # ---- transport ----

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        tok = self.token or (self._token_provider() if self._token_provider else "")
        if tok:
            h["Authorization"] = f"Bearer {tok}"
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    RETRIES = 3

    def _post(self, body: dict) -> dict | None:
        # Headset's heavier reports can take a minute or more; a slow or reset
        # connection is retried with a growing pause rather than failing the sync.
        last: Exception | None = None
        for attempt in range(self.RETRIES):
            try:
                resp = self._http.post(self.url, headers=self._headers(), content=json.dumps(body))
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                last = e
                time.sleep(5 * (attempt + 1))
                continue
            if resp.status_code in (429, 502, 503, 504) and attempt < self.RETRIES - 1:
                retry_after = resp.headers.get("Retry-After")
                time.sleep(float(retry_after) if retry_after and retry_after.isdigit() else 10 * (attempt + 1))
                continue
            break
        else:
            raise HeadsetError(f"MCP request failed after {self.RETRIES} attempts: {last}")
        if resp.status_code >= 400:
            raise HeadsetError(f"MCP HTTP {resp.status_code}: {resp.text[:500]}")
        sid = resp.headers.get("Mcp-Session-Id")
        if sid:
            self._session_id = sid
        if resp.status_code == 202 or not resp.content:
            return None
        ctype = resp.headers.get("Content-Type", "")
        if "text/event-stream" in ctype:
            return self._last_sse_message(resp.text, body.get("id"))
        return resp.json()

    @staticmethod
    def _last_sse_message(text: str, want_id: Any) -> dict | None:
        found = None
        for block in text.split("\n\n"):
            data = "\n".join(line[5:].strip() for line in block.splitlines() if line.startswith("data:"))
            if not data:
                continue
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == want_id:
                found = msg
        return found

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        msg = self._post({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params or {}})
        if msg is None:
            raise HeadsetError(f"MCP {method}: empty response")
        if "error" in msg:
            raise HeadsetError(f"MCP {method}: {msg['error']}")
        return msg.get("result", {})

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        self._rpc("initialize", {
            "protocolVersion": self.PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "aurora-ai", "version": "0.2.0"},
        })
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._initialized = True

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        self._ensure_initialized()
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        if result.get("isError"):
            raise HeadsetError(f"{name}: {result.get('content')}")
        if isinstance(result.get("structuredContent"), dict):
            return result["structuredContent"]
        for part in result.get("content", []):
            if part.get("type") == "text":
                try:
                    return json.loads(part["text"])
                except json.JSONDecodeError:
                    raise HeadsetError(f"{name}: non-JSON text result: {part['text'][:200]}")
        raise HeadsetError(f"{name}: no usable content in result")

    # ---- HeadsetSource ----

    def get_stores(self) -> dict:
        return self.call_tool(TOOL_STORES)

    def get_inventory(self, **arguments: Any) -> dict:
        return self.call_tool(TOOL_INVENTORY, arguments)

    def sales_by_dimension(self, **arguments: Any) -> dict:
        return self.call_tool(TOOL_BY_DIMENSION, arguments)

    def sales_trend(self, **arguments: Any) -> dict:
        return self.call_tool(TOOL_TREND, arguments)


def oauth_store_path() -> str:
    from app.config import settings
    return str(Path(settings.headset_data_dir) / "oauth.json")


def client_from_settings() -> McpHeadsetClient:
    """Static HEADSET_MCP_TOKEN when set; otherwise the OAuth tokens saved by `headset-login`."""
    from app.config import settings
    from app.integrations.headset.oauth import TokenStore, access_token

    if settings.headset_mcp_token:
        return McpHeadsetClient(settings.headset_mcp_url, settings.headset_mcp_token)
    store = TokenStore(oauth_store_path())
    http = httpx.Client(timeout=180.0)
    if not store.status().get("logged_in"):
        raise HeadsetError("no Headset credential: set HEADSET_MCP_TOKEN or run `python -m app.cli headset-login`")
    return McpHeadsetClient(settings.headset_mcp_url, "", client=http, token_provider=lambda: access_token(http, store))
