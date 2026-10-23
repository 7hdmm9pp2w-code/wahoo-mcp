"""Tests for the HTTP transport, static token and single-user OAuth."""

import base64
import hashlib
import json
import secrets
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.testclient import TestClient

from src.http_app import AuthConfig, build_app
from src.oauth import MAX_FAILED_LOGINS, SingleUserOAuthProvider, redirect_uri_allowed

PUBLIC_URL = "http://localhost:8000"
PASSWORD = "correct horse battery"
STATIC_TOKEN = "s" * 40
CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}


def _client(**config) -> TestClient:
    return TestClient(build_app(AuthConfig(**config)), follow_redirects=False)


def _initialize(client: TestClient, token: str | None = None):
    headers = dict(MCP_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return client.post("/mcp", json=INITIALIZE, headers=headers)


class TestConfig:
    def test_refuses_to_start_without_access_control(self):
        with pytest.raises(ValueError, match="Refusing to serve"):
            build_app(AuthConfig())

    def test_short_static_token_is_rejected(self):
        with pytest.raises(ValueError, match="at least 32"):
            build_app(AuthConfig(static_token="short"))

    def test_oauth_needs_public_url(self):
        with pytest.raises(ValueError, match="WAHOO_PUBLIC_URL"):
            build_app(AuthConfig(oauth_password=PASSWORD))

    def test_short_password_is_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="at least 12"):
            build_app(
                AuthConfig(
                    public_url=PUBLIC_URL,
                    oauth_password="short",
                    state_file=tmp_path / "s.json",
                )
            )


class TestStaticToken:
    def test_health_needs_no_token(self):
        with _client(static_token=STATIC_TOKEN) as client:
            assert client.get("/health").json() == {"status": "ok"}

    def test_mcp_without_token_is_401(self):
        with _client(static_token=STATIC_TOKEN) as client:
            assert _initialize(client).status_code == 401

    def test_mcp_with_wrong_token_is_401(self):
        with _client(static_token=STATIC_TOKEN) as client:
            assert _initialize(client, "x" * 40).status_code == 401

    def test_mcp_with_token_initializes(self):
        with _client(static_token=STATIC_TOKEN) as client:
            response = _initialize(client, STATIC_TOKEN)
        assert response.status_code == 200
        assert response.json()["result"]["serverInfo"]["name"] == "wahoo-mcp"

    def test_trailing_slash_works_without_redirect(self):
        with _client(static_token=STATIC_TOKEN) as client:
            headers = MCP_HEADERS | {"Authorization": f"Bearer {STATIC_TOKEN}"}
            response = client.post("/mcp/", json=INITIALIZE, headers=headers)
        assert response.status_code == 200


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    return verifier, challenge


@pytest.fixture
def oauth(tmp_path):
    with _client(
        public_url=PUBLIC_URL,
        oauth_password=PASSWORD,
        static_token=STATIC_TOKEN,
        state_file=tmp_path / "oauth_state.json",
    ) as client:
        yield client


def _register(client: TestClient, redirect_uri: str = CLAUDE_CALLBACK):
    return client.post(
        "/register",
        json={
            "client_name": "Claude",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )


def _login_request(client: TestClient, client_id: str, challenge: str) -> str:
    response = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CLAUDE_CALLBACK,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "st4te",
            "resource": PUBLIC_URL + "/mcp",
        },
    )
    assert response.status_code == 302
    location = urlsplit(response.headers["location"])
    assert location.path == "/login"
    return parse_qs(location.query)["request"][0]


def _authorize(client: TestClient) -> tuple[str, dict]:
    """Run register → authorize → login → token; return client_id and tokens."""
    client_id = _register(client).json()["client_id"]
    verifier, challenge = _pkce()
    request_id = _login_request(client, client_id, challenge)

    assert "Zugriff erlauben" in client.get(f"/login?request={request_id}").text
    response = client.post("/login", data={"request": request_id, "password": PASSWORD})
    assert response.status_code == 302
    callback = urlsplit(response.headers["location"])
    query = parse_qs(callback.query)
    assert f"{callback.scheme}://{callback.netloc}{callback.path}" == CLAUDE_CALLBACK
    assert query["state"] == ["st4te"]

    tokens = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": query["code"][0],
            "redirect_uri": CLAUDE_CALLBACK,
            "client_id": client_id,
            "code_verifier": verifier,
            "resource": PUBLIC_URL + "/mcp",
        },
    )
    assert tokens.status_code == 200, tokens.text
    return client_id, tokens.json()


class TestOAuthDiscovery:
    def test_401_points_to_resource_metadata(self, oauth):
        response = _initialize(oauth)
        assert response.status_code == 401
        assert (
            f'resource_metadata="{PUBLIC_URL}/.well-known/oauth-protected-resource"'
            in response.headers["www-authenticate"]
        )

    def test_protected_resource_metadata(self, oauth):
        data = oauth.get("/.well-known/oauth-protected-resource").json()
        assert data["resource"] == PUBLIC_URL + "/mcp"
        assert data["authorization_servers"][0].rstrip("/") == PUBLIC_URL

    def test_authorization_server_metadata(self, oauth):
        data = oauth.get("/.well-known/oauth-authorization-server").json()
        assert data["code_challenge_methods_supported"] == ["S256"]
        assert data["registration_endpoint"] == PUBLIC_URL + "/register"
        assert "none" in data["token_endpoint_auth_methods_supported"]


class TestOAuthFlow:
    def test_full_flow_gives_working_token(self, oauth):
        _, tokens = _authorize(oauth)
        assert _initialize(oauth, tokens["access_token"]).status_code == 200

    def test_refresh_rotates(self, oauth):
        client_id, tokens = _authorize(oauth)
        refresh = {
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": client_id,
        }
        renewed = oauth.post("/token", data=refresh)
        assert renewed.status_code == 200
        assert _initialize(oauth, renewed.json()["access_token"]).status_code == 200
        reused = oauth.post("/token", data=refresh)
        assert reused.status_code == 400
        assert reused.json()["error"] == "invalid_grant"

    def test_code_cannot_be_used_twice(self, oauth):
        client_id = _register(oauth).json()["client_id"]
        verifier, challenge = _pkce()
        request_id = _login_request(oauth, client_id, challenge)
        response = oauth.post(
            "/login", data={"request": request_id, "password": PASSWORD}
        )
        code = parse_qs(urlsplit(response.headers["location"]).query)["code"][0]
        exchange = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CLAUDE_CALLBACK,
            "client_id": client_id,
            "code_verifier": verifier,
        }
        assert oauth.post("/token", data=exchange).status_code == 200
        assert oauth.post("/token", data=exchange).status_code == 400

    def test_wrong_pkce_verifier_is_rejected(self, oauth):
        client_id = _register(oauth).json()["client_id"]
        _, challenge = _pkce()
        request_id = _login_request(oauth, client_id, challenge)
        response = oauth.post(
            "/login", data={"request": request_id, "password": PASSWORD}
        )
        code = parse_qs(urlsplit(response.headers["location"]).query)["code"][0]
        token = oauth.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": CLAUDE_CALLBACK,
                "client_id": client_id,
                "code_verifier": "wrong" * 10,
            },
        )
        assert token.status_code == 400

    def test_static_token_works_next_to_oauth(self, oauth):
        assert _initialize(oauth, STATIC_TOKEN).status_code == 200

    def test_unknown_redirect_uri_cannot_register(self, oauth):
        response = _register(oauth, "https://evil.example.com/callback")
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_redirect_uri"

    def test_wrong_password_and_lockout(self, oauth):
        client_id = _register(oauth).json()["client_id"]
        _, challenge = _pkce()
        request_id = _login_request(oauth, client_id, challenge)
        for _ in range(MAX_FAILED_LOGINS):
            wrong = oauth.post(
                "/login", data={"request": request_id, "password": "nope"}
            )
            assert wrong.status_code == 401
        locked = oauth.post(
            "/login", data={"request": request_id, "password": PASSWORD}
        )
        assert locked.status_code == 429

    def test_foreign_resource_is_rejected(self, oauth):
        client_id = _register(oauth).json()["client_id"]
        _, challenge = _pkce()
        response = oauth.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": CLAUDE_CALLBACK,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": "https://other.example.com/mcp",
            },
        )
        assert "error=invalid_request" in response.headers["location"]


class TestOAuthState:
    async def test_state_survives_restart_and_stores_only_hashes(self, tmp_path):
        state_file = tmp_path / "oauth_state.json"
        with _client(
            public_url=PUBLIC_URL, oauth_password=PASSWORD, state_file=state_file
        ) as client:
            _, tokens = _authorize(client)

        stored = state_file.read_text()
        assert tokens["access_token"] not in stored
        assert tokens["refresh_token"] not in stored
        assert oct(state_file.stat().st_mode & 0o777) == "0o600"

        provider = SingleUserOAuthProvider(
            issuer_url=PUBLIC_URL,
            resource_url=PUBLIC_URL + "/mcp",
            password=PASSWORD,
            state_file=state_file,
            redirect_allowlist=[CLAUDE_CALLBACK],
        )
        assert await provider.load_access_token(tokens["access_token"]) is not None
        assert json.loads(stored)["clients"]


class TestRedirectAllowlist:
    allowlist = [CLAUDE_CALLBACK, "http://localhost"]

    @pytest.mark.parametrize(
        ("uri", "allowed"),
        [
            (CLAUDE_CALLBACK, True),
            ("https://claude.ai/api/mcp/auth_callback/x", False),
            ("http://localhost:53682/callback", True),
            ("https://localhost:53682/callback", False),
            ("http://127.0.0.1:5000/callback", False),
            ("http://localhost.evil.com/callback", False),
        ],
    )
    def test_matching(self, uri, allowed):
        assert redirect_uri_allowed(uri, self.allowlist) is allowed
