"""The OAuth login and refresh against a fake MCP server + authorization server."""
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.integrations.headset.client import McpHeadsetClient
from app.integrations.headset.oauth import (OAuthError, TokenStore, access_token, code_from_landing, discover, login)

MCP = "https://mcp.example.test/mcp"


def fake_server(state: dict):
    """A resource that challenges with RFC 9728 metadata, an AS with RFC 8414 metadata,
    dynamic registration, PKCE-checked token exchange and rotating refresh."""
    def handler(req: httpx.Request) -> httpx.Response:
        u, p = req.url, req.url.path
        if u.host == "mcp.example.test":
            if p == "/.well-known/oauth-protected-resource/mcp":
                return httpx.Response(200, json={"resource": MCP, "authorization_servers": ["https://auth.example.test"]})
            if p == "/mcp":
                auth = req.headers.get("authorization", "")
                if auth != f"Bearer {state['access']}":
                    return httpx.Response(401, headers={"WWW-Authenticate": f'Bearer resource_metadata="https://mcp.example.test/.well-known/oauth-protected-resource/mcp"'})
                body = json.loads(req.content)
                state["calls"].append(body["method"])
                if "id" not in body:   # a notification
                    return httpx.Response(202)
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "fake"}}
                                                 if body["method"] == "initialize" else {"content": [{"type": "text", "text": json.dumps({"stores": [{"storeId": 1, "name": "FL - X"}]})}]}})
        if u.host == "auth.example.test":
            if p == "/.well-known/oauth-authorization-server":
                return httpx.Response(200, json={"issuer": "https://auth.example.test", "authorization_endpoint": "https://auth.example.test/authorize",
                                                 "token_endpoint": "https://auth.example.test/token", "registration_endpoint": "https://auth.example.test/register",
                                                 "scopes_supported": ["retailer:read"]})
            if p == "/register":
                body = json.loads(req.content)
                assert body["token_endpoint_auth_method"] == "none" and body["redirect_uris"] == ["http://localhost:8765/callback"]
                state["registered"] += 1
                return httpx.Response(201, json={"client_id": "cid-1"})
            if p == "/token":
                form = parse_qs(req.content.decode())
                assert form["resource"] == [MCP] and form["client_id"] == ["cid-1"]
                if form["grant_type"] == ["authorization_code"]:
                    assert form["code"] == ["the-code"] and form["code_verifier"] and form["redirect_uri"] == ["http://localhost:8765/callback"]
                    state["access"] = "at-1"
                    return httpx.Response(200, json={"access_token": "at-1", "token_type": "Bearer", "expires_in": 3600, "refresh_token": "rt-1", "scope": "retailer:read"})
                if form["grant_type"] == ["refresh_token"]:
                    assert form["refresh_token"] == [state["refresh"]]
                    state["refreshed"] += 1
                    state["access"], state["refresh"] = "at-2", "rt-2"
                    return httpx.Response(200, json={"access_token": "at-2", "token_type": "Bearer", "expires_in": 3600, "refresh_token": "rt-2"})
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_login_discovers_registers_and_exchanges(tmp_path):
    state = {"access": None, "refresh": "rt-1", "registered": 0, "refreshed": 0, "calls": []}
    http = fake_server(state)
    store = TokenStore(tmp_path / "oauth.json")
    shown = []
    st = login(http, MCP, store, prompt=lambda _: "http://localhost:8765/callback?code=the-code&state=" + shown[-1],
               echo=lambda line: shown.append(parse_qs(urlparse(line.strip()).query).get("state", [None])[0]) if "authorize?" in line else None)
    assert st["logged_in"] and st["issuer"] == "https://auth.example.test" and st["has_refresh_token"] and state["registered"] == 1
    saved = json.loads((tmp_path / "oauth.json").read_text())
    assert saved["client"]["client_id"] == "cid-1" and saved["token"]["access_token"] == "at-1" and oct((tmp_path / "oauth.json").stat().st_mode)[-3:] == "600"

    # the client uses the stored token and calls the MCP server
    c = McpHeadsetClient(MCP, "", client=http, token_provider=lambda: access_token(http, store))
    assert c.get_stores()["stores"][0]["name"] == "FL - X" and "initialize" in state["calls"]

    # near expiry the token is refreshed transparently, and the rotated refresh token is kept
    saved["token"]["obtained_at"] = int(time.time()) - 3600 + 30
    store.save(saved)
    assert access_token(http, store) == "at-2" and state["refreshed"] == 1
    assert json.loads((tmp_path / "oauth.json").read_text())["token"]["refresh_token"] == "rt-2"
    assert c.get_stores()["stores"][0]["name"] == "FL - X"

    # a second login on the same issuer reuses the registration
    login(http, MCP, store, prompt=lambda _: "the-code", echo=lambda _: None)
    assert state["registered"] == 1


def test_discovery_falls_back_to_host_and_reports_clearly():
    def handler(req):
        return httpx.Response(404)
    with pytest.raises(OAuthError, match="no OAuth metadata"):
        discover(httpx.Client(transport=httpx.MockTransport(handler)), "https://nowhere.test/mcp")
    assert code_from_landing("  http://localhost:8765/callback?code=abc&state=s  ", "s") == "abc"
    assert code_from_landing("abc") == "abc"
    with pytest.raises(OAuthError, match="state mismatch"):
        code_from_landing("http://localhost:8765/callback?code=abc&state=other", "s")
    with pytest.raises(OAuthError, match="no code"):
        code_from_landing("http://localhost:8765/callback?error=access_denied")


def test_issued_client_id_skips_registration_and_status_describes_provider(tmp_path):
    from app.integrations.headset.oauth import describe
    state = {"access": None, "refresh": "rt-1", "registered": 0, "refreshed": 0, "calls": []}
    http = fake_server(state)

    # a provider that refuses self-registration is described, with the ask spelled out
    def refusing(req):
        if req.url.path == "/register":
            return httpx.Response(400, json={"statusCode": 400, "error": "Bad Request", "message": "dynamic client registration is disabled"})
        return http._transport.handler(req)
    rhttp = httpx.Client(transport=httpx.MockTransport(refusing))
    d = describe(rhttp, MCP)
    assert d["issuer"] == "https://auth.example.test" and d["scopes_supported"] == ["retailer:read"] and d["self_registration"].startswith("refused: HTTP 400")
    with pytest.raises(OAuthError, match="issue an OAuth client"):
        login(rhttp, MCP, TokenStore(tmp_path / "o.json"), prompt=lambda _: "x", echo=lambda _: None)

    # with an issued client id the registration endpoint is never called
    store = TokenStore(tmp_path / "oauth.json")
    st = login(rhttp, MCP, store, prompt=lambda _: "http://localhost:8765/callback?code=the-code", echo=lambda _: None, client_id="cid-1")
    assert st["logged_in"] and state["registered"] == 0
    assert json.loads((tmp_path / "oauth.json").read_text())["client"] == {"client_id": "cid-1", "client_secret": None}


def test_import_token_from_claude_code(tmp_path):
    from app.integrations.headset.oauth import import_from_claude_code
    creds = {"claudeAiOauth": {"accessToken": "never-read"},
             "mcpOAuth": {"headset|abc": {"serverName": "headset", "serverUrl": "https://mcp.headset.io", "accessToken": "cc-token",
                                          "clientId": "https://example/client.json", "issuer": "https://auth.headset.io/",
                                          "expiresAt": int((time.time() + 3600) * 1000), "scope": ""}}}
    (tmp_path / "creds.json").write_text(json.dumps(creds))
    store = TokenStore(tmp_path / "oauth.json")
    st = import_from_claude_code(tmp_path / "creds.json", "https://mcp.headset.io/", store)
    assert st["logged_in"] and st["refresh_token_available"] is False and 3500 < st["expires_in_s"] <= 3600
    saved = json.loads((tmp_path / "oauth.json").read_text())
    assert saved["token"]["access_token"] == "cc-token" and "never-read" not in json.dumps(saved)
    assert access_token(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))), store) == "cc-token"
    with pytest.raises(OAuthError, match="no Claude Code credential"):
        import_from_claude_code(tmp_path / "creds.json", "https://other.test", store)


def test_metadata_document_client_and_offline_access(tmp_path):
    from app.integrations.headset.oauth import authorize_url, describe, request_scopes
    state = {"access": None, "refresh": "rt-1", "registered": 0, "refreshed": 0, "calls": []}
    base = fake_server(state)._transport.handler
    cimd = "https://owner.github.io/aurora-ai/oauth/client.json"

    def handler(req):
        if req.url.path == "/.well-known/oauth-authorization-server":
            r = base(req); m = r.json(); m["client_id_metadata_document_supported"] = True; m["scopes_supported"] = ["openid", "offline_access", "email"]
            return httpx.Response(200, json=m)
        if req.url.path == "/register":
            return httpx.Response(400, json={"message": "dynamic client registration is disabled"})
        if req.url.path == "/token":
            form = parse_qs(req.content.decode())
            assert form["client_id"] == [cimd]
            return httpx.Response(200, json={"access_token": "at-cimd", "token_type": "Bearer", "expires_in": 86400, "refresh_token": "rt-cimd"})
        return base(req)
    http = httpx.Client(transport=httpx.MockTransport(handler))
    d = describe(http, MCP)
    assert d["client_id_metadata_document_supported"] is True and d["refresh_tokens_via"] == "offline_access"
    disc = discover(http, MCP)
    assert request_scopes(disc) == ["offline_access"]
    url = authorize_url(disc, cimd, "chal", "st")
    q = parse_qs(urlparse(url).query)
    assert q["client_id"] == [cimd] and q["scope"] == ["offline_access"] and q["resource"] == [MCP] and q["code_challenge_method"] == ["S256"]
    with pytest.raises(OAuthError, match="published metadata document"):
        login(http, MCP, TokenStore(tmp_path / "x.json"), prompt=lambda _: "c", echo=lambda _: None)
    st = login(http, MCP, TokenStore(tmp_path / "o.json"), prompt=lambda _: "http://localhost:8765/callback?code=the-code", echo=lambda _: None, client_id=cimd)
    assert st["logged_in"] and st["has_refresh_token"] and 86000 < st["expires_in_s"] <= 86400
