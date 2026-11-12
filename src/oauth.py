"""Single-user OAuth 2.1 authorization server for the remote MCP endpoint.

Claude's custom connectors register themselves (Dynamic Client Registration),
send the user to /login, and get tokens after the owner enters the password
from WAHOO_OAUTH_PASSWORD. Only redirect URIs on an allowlist can register, so
a stranger can neither finish a login nor receive a code.

State (clients, refresh and access tokens) is kept in a JSON file so a
container restart does not force a new login. Tokens are stored only as
SHA-256 hashes.
"""

import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

logger = logging.getLogger(__name__)

SCOPE = "wahoo"
ACCESS_TOKEN_SECONDS = 3600
REFRESH_TOKEN_SECONDS = 90 * 24 * 3600
AUTH_CODE_SECONDS = 300
LOGIN_REQUEST_SECONDS = 600
MAX_CLIENTS = 20
MIN_PASSWORD_LENGTH = 12
# Failed password attempts allowed per window before the login locks
MAX_FAILED_LOGINS = 5
FAILED_LOGIN_WINDOW_SECONDS = 900

# Claude web/desktop/mobile callback, and loopback for Claude Code (any port)
DEFAULT_REDIRECT_ALLOWLIST = (
    "https://claude.ai/api/mcp/auth_callback,http://localhost,http://127.0.0.1"
)
LOOPBACK_HOSTS = {"localhost", "127.0.0.1"}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def redirect_uri_allowed(uri: str, allowlist: list[str]) -> bool:
    """Exact match, except loopback entries, which match any port and path."""
    parts = urlsplit(uri)
    for allowed in allowlist:
        allowed_parts = urlsplit(allowed)
        if allowed_parts.hostname in LOOPBACK_HOSTS and not allowed_parts.path:
            if parts.scheme == "http" and parts.hostname == allowed_parts.hostname:
                return True
        elif uri == allowed:
            return True
    return False


@dataclass
class _LoginRequest:
    client_id: str
    params: AuthorizationParams
    expires_at: float


@dataclass
class SingleUserOAuthProvider:
    """OAuthAuthorizationServerProvider for exactly one human user."""

    issuer_url: str
    resource_url: str
    password: str
    state_file: Path
    redirect_allowlist: list[str]
    static_token: str | None = None
    _login_requests: dict[str, _LoginRequest] = field(default_factory=dict)
    _codes: dict[str, AuthorizationCode] = field(default_factory=dict)
    _failed_logins: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        if len(self.password) < MIN_PASSWORD_LENGTH:
            raise ValueError(
                f"WAHOO_OAUTH_PASSWORD must have at least {MIN_PASSWORD_LENGTH} "
                "characters"
            )
        self._state = self._load()

    # --- persistence -----------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if self.state_file.exists():
            return json.loads(self.state_file.read_text())
        return {"clients": {}, "access_tokens": {}, "refresh_tokens": {}}

    def _save(self) -> None:
        self._expire()
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.state_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self._state, f, indent=2)

    def _expire(self) -> None:
        now = time.time()
        for kind in ("access_tokens", "refresh_tokens"):
            self._state[kind] = {
                key: value
                for key, value in self._state[kind].items()
                if value["expires_at"] > now
            }

    # --- clients ---------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = self._state["clients"].get(client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris:
            if not redirect_uri_allowed(str(uri), self.redirect_allowlist):
                raise RegistrationError(
                    error="invalid_redirect_uri",
                    error_description=f"redirect_uri not allowed: {uri}",
                )
        clients = self._state["clients"]
        if len(clients) >= MAX_CLIENTS:
            oldest = min(clients, key=lambda cid: clients[cid]["client_id_issued_at"])
            del clients[oldest]
        clients[client_info.client_id] = client_info.model_dump(mode="json")
        self._save()
        logger.info(f"Registered OAuth client {client_info.client_name!r}")

    # --- authorization ---------------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if params.resource and params.resource.rstrip("/") != self.resource_url:
            raise AuthorizeError(
                error="invalid_request",
                error_description=f"unknown resource {params.resource}",
            )
        now = time.time()
        self._login_requests = {
            key: value
            for key, value in self._login_requests.items()
            if value.expires_at > now
        }
        request_id = secrets.token_urlsafe(32)
        self._login_requests[request_id] = _LoginRequest(
            client_id=client.client_id,
            params=params,
            expires_at=now + LOGIN_REQUEST_SECONDS,
        )
        return f"{self.issuer_url}/login?request={request_id}"

    def _login_locked(self) -> bool:
        cutoff = time.time() - FAILED_LOGIN_WINDOW_SECONDS
        self._failed_logins = [t for t in self._failed_logins if t > cutoff]
        return len(self._failed_logins) >= MAX_FAILED_LOGINS

    async def login_page(self, request: Request) -> Response:
        """GET shows the password form, POST checks it and issues the code."""
        if request.method == "GET":
            request_id = request.query_params.get("request", "")
            pending = self._login_requests.get(request_id)
            if pending is None:
                return _page("Login abgelaufen. Bitte in Claude neu verbinden.", 400)
            client = await self.get_client(pending.client_id)
            name = client.client_name if client and client.client_name else "Claude"
            return _login_form(request_id, name)

        form = await request.form()
        request_id = str(form.get("request", ""))
        pending = self._login_requests.get(request_id)
        if pending is None or pending.expires_at < time.time():
            return _page("Login abgelaufen. Bitte in Claude neu verbinden.", 400)
        if self._login_locked():
            return _page("Zu viele Fehlversuche. Bitte 15 Minuten warten.", 429)
        if not hmac.compare_digest(
            str(form.get("password", "")).encode(), self.password.encode()
        ):
            self._failed_logins.append(time.time())
            logger.warning("Wrong OAuth login password")
            return _login_form(request_id, "Claude", error="Falsches Passwort.")

        del self._login_requests[request_id]
        code = secrets.token_urlsafe(32)
        params = pending.params
        self._codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [SCOPE],
            expires_at=time.time() + AUTH_CODE_SECONDS,
            client_id=pending.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        logger.info("OAuth login approved")
        return RedirectResponse(
            construct_redirect_uri(
                str(params.redirect_uri), code=code, state=params.state
            ),
            status_code=302,
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self._codes.get(authorization_code)
        if code is None or code.client_id != client.client_id:
            return None
        return code

    # --- tokens ----------------------------------------------------------

    def _issue(
        self, client_id: str, scopes: list[str], resource: str | None
    ) -> OAuthToken:
        now = int(time.time())
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self._state["access_tokens"][_hash(access)] = {
            "client_id": client_id,
            "scopes": scopes,
            "resource": resource,
            "expires_at": now + ACCESS_TOKEN_SECONDS,
        }
        self._state["refresh_tokens"][_hash(refresh)] = {
            "client_id": client_id,
            "scopes": scopes,
            "resource": resource,
            "expires_at": now + REFRESH_TOKEN_SECONDS,
        }
        self._save()
        return OAuthToken(
            access_token=access,
            token_type="Bearer",  # noqa: S106
            expires_in=ACCESS_TOKEN_SECONDS,
            refresh_token=refresh,
            scope=" ".join(scopes),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if self._codes.pop(authorization_code.code, None) is None:
            raise TokenError(
                error="invalid_grant", error_description="code already used"
            )
        return self._issue(
            client.client_id, authorization_code.scopes, authorization_code.resource
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        data = self._state["refresh_tokens"].get(_hash(refresh_token))
        if data is None or data["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=data["client_id"],
            scopes=data["scopes"],
            expires_at=data["expires_at"],
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # Rotate: the used refresh token stops working
        data = self._state["refresh_tokens"].pop(_hash(refresh_token.token), None)
        if data is None:
            raise TokenError(error="invalid_grant", error_description="unknown token")
        return self._issue(client.client_id, scopes or data["scopes"], data["resource"])

    async def load_access_token(self, token: str) -> AccessToken | None:
        if self.static_token and hmac.compare_digest(
            token.encode(), self.static_token.encode()
        ):
            return AccessToken(token=token, client_id="static-token", scopes=[SCOPE])
        data = self._state["access_tokens"].get(_hash(token))
        if data is None or data["expires_at"] < time.time():
            return None
        # Audience check: only tokens issued for this MCP endpoint
        if data["resource"] and data["resource"].rstrip("/") != self.resource_url:
            return None
        return AccessToken(
            token=token,
            client_id=data["client_id"],
            scopes=data["scopes"],
            expires_at=data["expires_at"],
            resource=data["resource"],
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        key = _hash(token.token)
        self._state["access_tokens"].pop(key, None)
        self._state["refresh_tokens"].pop(key, None)
        self._save()


def _page(message: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8><title>wahoo-mcp</title>"
        f"<body style='font-family:sans-serif;max-width:28rem;margin:4rem auto'>"
        f"<p>{html.escape(message)}</p></body>",
        status_code=status,
    )


def _login_form(request_id: str, client_name: str, error: str = "") -> HTMLResponse:
    error_html = f"<p style='color:#b00'>{html.escape(error)}</p>" if error else ""
    return HTMLResponse(
        f"""<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>wahoo-mcp Login</title>
<body style="font-family:sans-serif;max-width:28rem;margin:4rem auto;padding:0 1rem">
<h1>wahoo-mcp</h1>
<p><b>{html.escape(client_name)}</b> möchte auf deinen Wahoo-Server zugreifen
(Pläne anlegen und Workouts terminieren).</p>
{error_html}
<form method=post action=login>
<input type=hidden name=request value="{html.escape(request_id)}">
<label>Passwort<br><input type=password name=password autofocus required
style="width:100%;font-size:1.1rem;padding:.4rem"></label>
<p><button type=submit style="font-size:1.1rem;padding:.4rem 1rem">
Zugriff erlauben</button></p>
</form></body>""",
        status_code=401 if error else 200,
        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"},
    )
