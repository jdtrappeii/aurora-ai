"""OAuth for the Headset MCP server, so the nightly sync needs no browser.

The MCP authorization spec (2025-06-18) is plain OAuth 2.1:
  1. the resource (the MCP URL) advertises its authorization server through
     RFC 9728 protected-resource metadata, discovered from a 401 challenge or
     /.well-known/oauth-protected-resource
  2. the authorization server publishes RFC 8414 metadata
  3. clients without pre-issued credentials register with RFC 7591 dynamic
     client registration
  4. authorization code + PKCE, with the MCP URL as the RFC 8707 `resource`
  5. refresh tokens keep the session alive without anyone present

`headset-login` runs steps 1-4 once, from a terminal: it prints the sign-in
link, the operator opens it in any browser, signs in, and pastes the address
the browser lands on (the redirect never has to be reachable). Tokens live in
one JSON file on the data volume and are refreshed on use.

Nothing here is Headset-specific; it works against any MCP server that
follows the spec. A static HEADSET_MCP_TOKEN still wins when set.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import httpx

CLIENT_NAME = "Aurora AI"
NEEDS_CLIENT_ID = ("The provider does not allow self-registration. Ask them to issue an OAuth client for this server "
                   f"(client name '{CLIENT_NAME}', redirect URI http://localhost:8765/callback, grant types authorization_code and "
                   "refresh_token, PKCE public client or a confidential client with a secret), then set HEADSET_OAUTH_CLIENT_ID "
                   "(and HEADSET_OAUTH_CLIENT_SECRET if given) and run headset-login again.")
REDIRECT_URI = "http://localhost:8765/callback"   # never listened on; the operator pastes the landing address
REFRESH_MARGIN_S = 120


class OAuthError(RuntimeError):
    pass


@dataclass
class Discovery:
    resource: str
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None
    scopes: list[str] = field(default_factory=list)
    cimd_supported: bool = False   # client id may be the URL of a published metadata document


def _well_known(base: str, suffix: str) -> str:
    u = urlparse(base)
    root = f"{u.scheme}://{u.netloc}"
    path = u.path.rstrip("/")
    # RFC 9728 / 8414: path-aware form first (/.well-known/x/<path>), then the root form
    return f"{root}/.well-known/{suffix}{path}" if path else f"{root}/.well-known/{suffix}"


def _get_json(http: httpx.Client, url: str) -> dict | None:
    try:
        r = http.get(url, headers={"Accept": "application/json"}, follow_redirects=True)
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except ValueError:
        return None


def discover(http: httpx.Client, mcp_url: str) -> Discovery:
    """Find the authorization server for an MCP URL."""
    as_urls: list[str] = []
    resource = mcp_url
    # 1. the resource's own metadata, possibly pointed to by a 401 challenge
    try:
        r = http.post(mcp_url, headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
                      content=json.dumps({"jsonrpc": "2.0", "id": 0, "method": "ping"}))
        www = r.headers.get("WWW-Authenticate", "")
        if "resource_metadata=" in www:
            meta_url = www.split("resource_metadata=", 1)[1].split(",")[0].strip().strip('"')
            meta = _get_json(http, meta_url)
            if meta:
                resource = meta.get("resource", resource)
                as_urls += meta.get("authorization_servers", [])
    except httpx.HTTPError:
        pass
    if not as_urls:
        for cand in (_well_known(mcp_url, "oauth-protected-resource"), _well_known(mcp_url.rstrip("/").rsplit("/", 1)[0] + "/", "oauth-protected-resource")):
            meta = _get_json(http, cand)
            if meta and meta.get("authorization_servers"):
                resource = meta.get("resource", resource)
                as_urls += meta["authorization_servers"]
                break
    if not as_urls:
        u = urlparse(mcp_url)
        as_urls = [f"{u.scheme}://{u.netloc}"]   # the MCP host is its own authorization server
    # 2. the authorization server's metadata
    scopes: list[str] = []
    for issuer in as_urls:
        for cand in (_well_known(issuer, "oauth-authorization-server"), _well_known(issuer, "openid-configuration")):
            meta = _get_json(http, cand)
            if meta and meta.get("authorization_endpoint") and meta.get("token_endpoint"):
                return Discovery(resource=resource, issuer=meta.get("issuer", issuer),
                                 authorization_endpoint=meta["authorization_endpoint"], token_endpoint=meta["token_endpoint"],
                                 registration_endpoint=meta.get("registration_endpoint"), scopes=list(meta.get("scopes_supported") or scopes),
                                 cimd_supported=bool(meta.get("client_id_metadata_document_supported")))
    raise OAuthError(f"no OAuth metadata found for {mcp_url} (tried {as_urls}); ask the provider for a static token instead")


def register_client(http: httpx.Client, disc: Discovery) -> dict:
    """RFC 7591 dynamic registration: a public client using PKCE."""
    if not disc.registration_endpoint:
        raise OAuthError(NEEDS_CLIENT_ID)
    body = {"client_name": CLIENT_NAME, "redirect_uris": [REDIRECT_URI], "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "token_endpoint_auth_method": "none"}
    r = http.post(disc.registration_endpoint, json=body, headers={"Accept": "application/json"})
    if r.status_code not in (200, 201):
        hint = NEEDS_CLIENT_ID if "registration" in r.text.casefold() or r.status_code in (400, 401, 403) else ""
        raise OAuthError(f"client registration failed: HTTP {r.status_code} {r.text[:300]}\n{hint}")
    data = r.json()
    if not data.get("client_id"):
        raise OAuthError("client registration returned no client_id")
    return {"client_id": data["client_id"], "client_secret": data.get("client_secret")}


def pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def request_scopes(disc: Discovery) -> list[str]:
    """Only what the sync needs: offline_access for a refresh token, when the
    server offers it. Identity scopes (openid, email, ...) are not requested."""
    return ["offline_access"] if "offline_access" in disc.scopes else []


def authorize_url(disc: Discovery, client_id: str, challenge: str, state: str, scopes: list[str] | None = None) -> str:
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT_URI, "code_challenge": challenge,
              "code_challenge_method": "S256", "state": state, "resource": disc.resource}
    sc = " ".join(scopes if scopes is not None else request_scopes(disc))
    if sc:
        params["scope"] = sc
    sep = "&" if "?" in disc.authorization_endpoint else "?"
    return f"{disc.authorization_endpoint}{sep}{urlencode(params)}"


def code_from_landing(landing: str, expect_state: str | None = None) -> str:
    """The address the browser lands on after sign-in, pasted back by the operator."""
    landing = landing.strip()
    q = parse_qs(urlparse(landing).query) if "?" in landing else {}
    code = (q.get("code") or [landing if landing and " " not in landing and "=" not in landing else ""])[0]
    if not code:
        raise OAuthError("no code found in what was pasted; paste the full address from the browser's address bar")
    if expect_state and q.get("state") and q["state"][0] != expect_state:
        raise OAuthError("state mismatch: start the login again")
    return code


def exchange(http: httpx.Client, disc: Discovery, client: dict, code: str, verifier: str) -> dict:
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI, "client_id": client["client_id"],
            "code_verifier": verifier, "resource": disc.resource}
    if client.get("client_secret"):
        data["client_secret"] = client["client_secret"]
    r = http.post(disc.token_endpoint, data=data, headers={"Accept": "application/json"})
    if r.status_code != 200:
        raise OAuthError(f"token exchange failed: HTTP {r.status_code} {r.text[:300]}")
    return _stamp(r.json())


def refresh(http: httpx.Client, disc: Discovery, client: dict, refresh_token: str) -> dict:
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client["client_id"], "resource": disc.resource}
    if client.get("client_secret"):
        data["client_secret"] = client["client_secret"]
    r = http.post(disc.token_endpoint, data=data, headers={"Accept": "application/json"})
    if r.status_code != 200:
        raise OAuthError(f"token refresh failed: HTTP {r.status_code} {r.text[:300]}; run headset-login again")
    tok = _stamp(r.json())
    tok.setdefault("refresh_token", refresh_token)   # servers may not rotate it
    return tok


def _stamp(tok: dict) -> dict:
    if not tok.get("access_token"):
        raise OAuthError("token response carried no access_token")
    tok["obtained_at"] = int(time.time())
    return tok


class TokenStore:
    """One JSON file: discovery, client registration and the current tokens."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> dict | None:
        if not self.path.exists():
            return None
        return json.loads(self.path.read_text())

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.chmod(0o600)
        tmp.replace(self.path)

    def status(self) -> dict:
        d = self.load()
        if not d or not d.get("token"):
            return {"logged_in": False, "path": str(self.path)}
        t = d["token"]
        exp = t.get("obtained_at", 0) + int(t.get("expires_in", 0) or 0)
        return {"logged_in": True, "path": str(self.path), "issuer": d["discovery"]["issuer"], "resource": d["discovery"]["resource"],
                "expires_in_s": (exp - int(time.time())) if t.get("expires_in") else None, "has_refresh_token": bool(t.get("refresh_token")),
                "scope": t.get("scope")}


def login(http: httpx.Client, mcp_url: str, store: TokenStore, prompt=input, echo=print, client_id: str | None = None,
          client_secret: str | None = None) -> dict:
    """Interactive: discover, register (unless a client id is given), print the
    sign-in link, take the landing address back, exchange, save."""
    disc = discover(http, mcp_url)
    existing = store.load() or {}
    client = existing.get("client") if existing.get("discovery", {}).get("issuer") == disc.issuer else None
    if client_id:
        client = {"client_id": client_id, "client_secret": client_secret}
    if not client:
        try:
            client = register_client(http, disc)
        except OAuthError as e:
            if disc.cimd_supported:
                raise OAuthError(str(e) + "\nThis server also accepts a client identified by a published metadata document: set "
                                 "HEADSET_OAUTH_CLIENT_ID to the https address of your client.json (see docs/oauth/client.json and the README).")
            raise
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    echo("\nOpen this address in a browser, sign in to Headset, and approve the access:\n")
    echo("  " + authorize_url(disc, client["client_id"], challenge, state))
    echo("\nThe browser will then try to open http://localhost:8765/callback?... and show an error page.")
    echo("That is expected. Copy the whole address from the browser's address bar and paste it here.\n")
    landing = prompt("Landing address: ")
    code = code_from_landing(landing, state)
    tok = exchange(http, disc, client, code, verifier)
    store.save({"mcp_url": mcp_url, "discovery": disc.__dict__, "client": client, "token": tok})
    return store.status()


def describe(http: httpx.Client, mcp_url: str) -> dict:
    """What the provider's authorization server offers: paste-able into a request for a client id."""
    try:
        d = discover(http, mcp_url)
    except OAuthError as e:
        return {"mcp_url": mcp_url, "error": str(e)}
    out = {"mcp_url": mcp_url, "resource": d.resource, "issuer": d.issuer, "authorization_endpoint": d.authorization_endpoint,
           "token_endpoint": d.token_endpoint, "registration_endpoint": d.registration_endpoint, "scopes_supported": d.scopes,
           "client_id_metadata_document_supported": d.cimd_supported, "refresh_tokens_via": "offline_access" if "offline_access" in d.scopes else "not advertised",
           "redirect_uri_aurora_uses": REDIRECT_URI, "client_name_aurora_uses": CLIENT_NAME}
    if d.registration_endpoint:
        try:
            r = http.post(d.registration_endpoint, json={"client_name": CLIENT_NAME, "redirect_uris": [REDIRECT_URI],
                                                       "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
                                                       "token_endpoint_auth_method": "none"}, headers={"Accept": "application/json"})
            out["self_registration"] = "allowed" if r.status_code in (200, 201) else f"refused: HTTP {r.status_code} {r.text[:160]}"
        except httpx.HTTPError as e:
            out["self_registration"] = f"unreachable: {e}"
    else:
        out["self_registration"] = "not offered"
    return out


def access_token(http: httpx.Client, store: TokenStore) -> str | None:
    """A valid access token from the store, refreshed when close to expiry. None when not logged in."""
    d = store.load()
    if not d or not d.get("token"):
        return None
    tok = d["token"]
    exp = tok.get("obtained_at", 0) + int(tok.get("expires_in", 0) or 0)
    if tok.get("expires_in") and time.time() > exp - REFRESH_MARGIN_S:
        if not tok.get("refresh_token"):
            raise OAuthError("Headset access token expired and no refresh token was issued; run headset-login again")
        disc = Discovery(**d["discovery"])
        tok = refresh(http, disc, d["client"], tok["refresh_token"])
        d["token"] = tok
        store.save(d)
    return tok["access_token"]


def import_from_claude_code(path: str | Path, mcp_url: str, store: TokenStore) -> dict:
    """Reuse the token Claude Code obtained for the same MCP server (its
    ~/.claude/.credentials.json, mcpOAuth section). The token is copied into
    Aurora's store with its expiry; a refresh token is copied when present.
    Nothing else in that file is read."""
    data = json.loads(Path(path).read_text())
    entries = (data.get("mcpOAuth") or {}).values()
    want = mcp_url.rstrip("/")
    match = next((e for e in entries if str(e.get("serverUrl", "")).rstrip("/") == want), None)
    if match is None:
        raise OAuthError(f"no Claude Code credential for {mcp_url}; run `claude` and authenticate the server with /mcp first")
    if not match.get("accessToken"):
        raise OAuthError("the Claude Code credential has no access token")
    now = int(time.time())
    exp_ms = match.get("expiresAt")
    expires_in = max(0, int(exp_ms / 1000) - now) if exp_ms else 0
    disc = match.get("discoveryState") or {}
    token = {"access_token": match["accessToken"], "token_type": "Bearer", "obtained_at": now, "scope": match.get("scope") or None,
             "source": "claude-code"}
    if expires_in:
        token["expires_in"] = expires_in
    if match.get("refreshToken"):
        token["refresh_token"] = match["refreshToken"]
    existing = store.load() or {}
    discovery = existing.get("discovery") or {"resource": mcp_url, "issuer": match.get("issuer") or disc.get("authorizationServerUrl") or "",
                                              "authorization_endpoint": "", "token_endpoint": "", "registration_endpoint": None, "scopes": []}
    store.save({"mcp_url": mcp_url, "discovery": discovery, "client": {"client_id": match.get("clientId"), "client_secret": None}, "token": token})
    st = store.status()
    st["refresh_token_available"] = bool(token.get("refresh_token"))
    return st
