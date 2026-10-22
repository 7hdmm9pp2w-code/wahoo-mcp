"""Streamable HTTP transport for running the server remotely, e.g. on a NAS.

Run with ``python -m src.http_app``. Configuration:
- ``WAHOO_HTTP_HOST`` / ``WAHOO_HTTP_PORT``: bind address (default 0.0.0.0:8000)
- ``WAHOO_PUBLIC_URL``: public HTTPS base URL, e.g. https://wahoo.example.com
- ``WAHOO_OAUTH_PASSWORD``: enables OAuth for claude.ai connectors; the owner
  enters it on the login page
- ``WAHOO_MCP_TOKEN``: static bearer token (Claude Code, connectors with
  request headers); works alone or next to OAuth
- ``WAHOO_ALLOW_NO_AUTH=1``: run without access control (local tests only)
"""

import contextlib
import hmac
import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import uvicorn
from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import (
    BearerAuthBackend,
    RequireAuthMiddleware,
)
from mcp.server.auth.provider import AccessToken, ProviderTokenVerifier
from mcp.server.auth.routes import (
    build_metadata,
    cors_middleware,
    create_auth_routes,
    create_protected_resource_routes,
)
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from .oauth import DEFAULT_REDIRECT_ALLOWLIST, SCOPE, SingleUserOAuthProvider
from .server import app as mcp_server

logger = logging.getLogger(__name__)

MCP_PATH = "/mcp"
MIN_STATIC_TOKEN_LENGTH = 32


async def health(_: Request) -> JSONResponse:
    """Liveness probe for Docker; reveals nothing about the account."""
    return JSONResponse({"status": "ok"})


@dataclass
class StaticTokenVerifier:
    """Accepts exactly one bearer token, compared in constant time."""

    token: str

    async def verify_token(self, token: str) -> AccessToken | None:
        if hmac.compare_digest(token.encode(), self.token.encode()):
            return AccessToken(token=token, client_id="static-token", scopes=[SCOPE])
        return None


@dataclass
class AuthConfig:
    public_url: str | None = None
    oauth_password: str | None = None
    static_token: str | None = None
    allow_no_auth: bool = False
    state_file: Path = Path("/data/oauth_state.json")
    redirect_allowlist: tuple[str, ...] = tuple(DEFAULT_REDIRECT_ALLOWLIST.split(","))

    @classmethod
    def from_env(cls) -> "AuthConfig":
        allowlist = os.getenv(
            "WAHOO_OAUTH_REDIRECT_ALLOWLIST", DEFAULT_REDIRECT_ALLOWLIST
        )
        token_file = Path(os.getenv("WAHOO_TOKEN_FILE", "/data/token.json"))
        return cls(
            public_url=(os.getenv("WAHOO_PUBLIC_URL") or "").rstrip("/") or None,
            oauth_password=os.getenv("WAHOO_OAUTH_PASSWORD") or None,
            static_token=os.getenv("WAHOO_MCP_TOKEN") or None,
            allow_no_auth=os.getenv("WAHOO_ALLOW_NO_AUTH") == "1",
            state_file=Path(
                os.getenv("WAHOO_OAUTH_STATE_FILE")
                or token_file.with_name("oauth_state.json")
            ),
            redirect_allowlist=tuple(
                u.strip() for u in allowlist.split(",") if u.strip()
            ),
        )

    def validate(self) -> None:
        if self.oauth_password and not self.public_url:
            raise ValueError("WAHOO_OAUTH_PASSWORD needs WAHOO_PUBLIC_URL")
        if self.static_token and len(self.static_token) < MIN_STATIC_TOKEN_LENGTH:
            raise ValueError(
                f"WAHOO_MCP_TOKEN must have at least {MIN_STATIC_TOKEN_LENGTH} "
                "characters (e.g. `openssl rand -hex 32`)"
            )
        if not (self.oauth_password or self.static_token or self.allow_no_auth):
            raise ValueError(
                "Refusing to serve without access control: set WAHOO_OAUTH_PASSWORD "
                "(with WAHOO_PUBLIC_URL) and/or WAHOO_MCP_TOKEN, or "
                "WAHOO_ALLOW_NO_AUTH=1 for local tests"
            )


def _oauth_routes(
    provider: SingleUserOAuthProvider, config: AuthConfig
) -> list[BaseRoute]:
    issuer = AnyHttpUrl(config.public_url)
    registration = ClientRegistrationOptions(
        enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
    )
    revocation = RevocationOptions(enabled=True)

    # The SDK only advertises client_secret_post; Claude may register as a
    # public client ("none"), which the token endpoint already accepts.
    metadata = build_metadata(issuer, None, registration, revocation)
    metadata.token_endpoint_auth_methods_supported = ["client_secret_post", "none"]
    metadata_route = Route(
        "/.well-known/oauth-authorization-server",
        endpoint=cors_middleware(MetadataHandler(metadata).handle, ["GET", "OPTIONS"]),
        methods=["GET", "OPTIONS"],
    )
    sdk_routes = [
        route
        for route in create_auth_routes(
            provider, issuer, None, registration, revocation
        )
        if getattr(route, "path", None) != "/.well-known/oauth-authorization-server"
    ]
    resource_routes = create_protected_resource_routes(
        AnyHttpUrl(config.public_url + MCP_PATH), [issuer], [SCOPE]
    )
    login = Route("/login", endpoint=provider.login_page, methods=["GET", "POST"])
    return [metadata_route, *sdk_routes, *resource_routes, login]


def build_app(config: AuthConfig | None = None) -> Starlette:
    """ASGI app with the MCP endpoint at /mcp, /health and, if configured,
    the OAuth endpoints."""
    config = config or AuthConfig.from_env()
    config.validate()

    # Stateless: every request is self-contained, so restarts and several
    # clients need no session bookkeeping. Tools are single request/response.
    session_manager = StreamableHTTPSessionManager(
        app=mcp_server, stateless=True, json_response=True
    )

    class HandleMCP:
        """ASGI endpoint (a class, so Starlette routes pass scope/receive/send)."""

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            await session_manager.handle_request(scope, receive, send)

    handle_mcp = HandleMCP()

    routes: list[BaseRoute] = [Route("/health", health)]
    middleware: list[Middleware] = []
    endpoint: ASGIApp = handle_mcp

    verifier = None
    if config.oauth_password:
        provider = SingleUserOAuthProvider(
            issuer_url=config.public_url,
            resource_url=config.public_url + MCP_PATH,
            password=config.oauth_password,
            state_file=config.state_file,
            redirect_allowlist=list(config.redirect_allowlist),
            static_token=config.static_token,
        )
        routes += _oauth_routes(provider, config)
        # load_access_token also accepts the static token
        verifier = ProviderTokenVerifier(provider)
    elif config.static_token:
        verifier = StaticTokenVerifier(config.static_token)

    if verifier is not None:
        backend = BearerAuthBackend(verifier)
        middleware = [
            Middleware(AuthenticationMiddleware, backend=backend),
            Middleware(AuthContextMiddleware),
        ]
        resource_metadata = (
            AnyHttpUrl(config.public_url + "/.well-known/oauth-protected-resource")
            if config.oauth_password
            else None
        )
        endpoint = RequireAuthMiddleware(handle_mcp, [], resource_metadata)
    else:
        logger.warning("Serving /mcp WITHOUT access control (WAHOO_ALLOW_NO_AUTH=1)")

    # Both spellings, so clients never hit a redirect that could drop the body
    routes += [Route(MCP_PATH, endpoint), Route(MCP_PATH + "/", endpoint)]

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with session_manager.run():
            yield

    return Starlette(routes=routes, middleware=middleware, lifespan=lifespan)


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    uvicorn.run(
        build_app(),
        host=os.getenv("WAHOO_HTTP_HOST", "0.0.0.0"),  # noqa: S104 (container)
        port=int(os.getenv("WAHOO_HTTP_PORT", "8000")),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
