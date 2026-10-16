#!/usr/bin/env python3
"""
OAuth authentication helper for Wahoo API
Run this script to obtain an access token
"""

import asyncio
import base64
import hashlib
import logging
import os
import secrets
import sys
import time
import webbrowser
from http import HTTPStatus
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from aiohttp import web
from dotenv import load_dotenv

from src.scopes import OAUTH_SCOPES
from src.token_store import TokenData, TokenStore

# Load environment variables
load_dotenv()

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

print("\n=== Wahoo OAuth Authentication Helper ===\n")

# Get configuration from environment or use defaults
HOST = os.getenv("WAHOO_AUTH_HOST", "localhost")
PORT = int(os.getenv("WAHOO_AUTH_PORT", "8080"))
REDIRECT_HOST = os.getenv(
    "WAHOO_REDIRECT_HOST", HOST
)  # Defaults to AUTH_HOST if not specified
REDIRECT_PORT = int(
    os.getenv("WAHOO_REDIRECT_PORT", str(PORT))
)  # Defaults to AUTH_PORT if not specified
REDIRECT_SCHEME = os.getenv(
    "WAHOO_REDIRECT_SCHEME", "http"
)  # Support https for production
AUTH_URL = os.getenv("WAHOO_AUTH_URL", "https://api.wahooligan.com/oauth/authorize")
TOKEN_URL = os.getenv("WAHOO_TOKEN_URL", "https://api.wahooligan.com/oauth/token")

# Check for client credentials in environment first
CLIENT_ID = os.getenv("WAHOO_CLIENT_ID")
CLIENT_SECRET = os.getenv("WAHOO_CLIENT_SECRET")

if not CLIENT_ID:
    CLIENT_ID = input("Enter your Wahoo Client ID: ")
if not CLIENT_SECRET:
    CLIENT_SECRET = input("Enter your Wahoo Client Secret: ")

# Build redirect URI with potentially different host
if REDIRECT_PORT == 443 and REDIRECT_SCHEME == "https":  # noqa: PLR2004
    # Don't include port 443 for https
    REDIRECT_URI = f"{REDIRECT_SCHEME}://{REDIRECT_HOST}/callback"
elif REDIRECT_PORT == 80 and REDIRECT_SCHEME == "http":  # noqa: PLR2004
    # Don't include port 80 for http
    REDIRECT_URI = f"{REDIRECT_SCHEME}://{REDIRECT_HOST}/callback"
else:
    # Include port for non-standard ports
    REDIRECT_URI = f"{REDIRECT_SCHEME}://{REDIRECT_HOST}:{REDIRECT_PORT}/callback"

logger.info("Auth server configuration:")
logger.info(f"  Server binding: {HOST}:{PORT}")
logger.info(f"  Redirect URI: {REDIRECT_URI}")
if REDIRECT_HOST != HOST or REDIRECT_PORT != PORT:
    logger.info("  Note: Redirect host/port differs from server binding")

# Generate PKCE challenge
logger.info("Generating PKCE challenge...")
code_verifier = (
    base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("utf-8").rstrip("=")
)
code_challenge = (
    base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
    .decode("utf-8")
    .rstrip("=")
)
logger.info("PKCE challenge generated successfully")

# Confidential apps authenticate with the client secret, public apps with PKCE.
# Wahoo rejects token requests that mix both.
USE_PKCE = not CLIENT_SECRET

# Random state, checked on the callback to reject forged redirects.
STATE = secrets.token_urlsafe(32)

# Hosts for which the redirect can fail harmlessly and the URL is pasted back.
LOCAL_HOSTS = {"localhost", "127.0.0.1"}

access_token = None
refresh_token = None

# Initialize token store
token_file = os.getenv("WAHOO_TOKEN_FILE")
if not token_file:
    print("❌ Error: WAHOO_TOKEN_FILE environment variable is required")
    print(
        "Set it to the path where tokens should be stored "
        "(e.g., export WAHOO_TOKEN_FILE=token.json)"
    )
    sys.exit(1)

token_store = TokenStore(token_file)


def _handle_oauth_error(error: str, error_desc: str | None) -> web.Response:
    """Handle OAuth error responses."""
    logger.error(f"OAuth error: {error}")
    description = error_desc or "Unknown error"
    return web.Response(
        text=f"OAuth Error: {error} - {description}",
        status=HTTPStatus.BAD_REQUEST,
    )


def _handle_missing_code() -> web.Response:
    """Handle missing authorization code."""
    logger.error("No authorization code received in callback")
    return web.Response(
        text="Error: No authorization code received",
        status=HTTPStatus.BAD_REQUEST,
    )


def build_auth_params() -> dict[str, str]:
    """Query parameters for the authorization URL."""
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": OAUTH_SCOPES,
        "state": STATE,
    }
    if USE_PKCE:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    return params


def build_token_request(code: str) -> dict[str, str]:
    """Form data for exchanging the authorization code."""
    data = {
        "client_id": CLIENT_ID,
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }
    if USE_PKCE:
        data["code_verifier"] = code_verifier
    else:
        data["client_secret"] = CLIENT_SECRET
    return data


def parse_callback_url(url: str) -> str:
    """Extract the authorization code from a pasted callback URL."""
    query = parse_qs(urlsplit(url.strip()).query)
    if "error" in query:
        description = query.get("error_description", ["Unknown error"])[0]
        raise ValueError(f"OAuth error: {query['error'][0]} - {description}")
    if query.get("state", [None])[0] != STATE:
        raise ValueError("State mismatch: this URL does not belong to this login")
    code = query.get("code", [None])[0]
    if not code:
        raise ValueError("No authorization code in the URL")
    return code


async def _exchange_code_for_token(code: str) -> dict | None:
    """Exchange authorization code for access token."""
    logger.info("Exchanging authorization code for access token...")

    async with httpx.AsyncClient() as client:
        response = await client.post(TOKEN_URL, data=build_token_request(code))

        if response.status_code == HTTPStatus.OK:
            return response.json()
        else:
            logger.error(f"Token exchange failed with status {response.status_code}")
            logger.error(f"Response: {response.text}")
            return None


def _store_tokens(token_data: dict) -> tuple[str, str | None]:
    """Store tokens and return access_token and refresh_token."""
    access_token = token_data["access_token"]
    refresh_token = token_data.get("refresh_token")

    token_obj = TokenData(
        access_token=access_token,
        refresh_token=refresh_token,
        code_verifier=code_verifier if USE_PKCE else None,
    )
    if "expires_in" in token_data:
        token_obj.expires_at = time.time() + token_data["expires_in"]
    token_store.save(token_obj)

    return access_token, refresh_token


def _log_token_details(token_data: dict, refresh_token: str | None) -> None:
    """Log token details without exposing sensitive information."""
    logger.info("Successfully obtained access token")
    logger.info(f"Token type: {token_data.get('token_type', 'bearer')}")
    if "expires_in" in token_data:
        logger.info(f"Token expires in: {token_data['expires_in']} seconds")
    if "scope" in token_data:
        logger.info(f"Token scope: {token_data['scope']}")
    if refresh_token:
        logger.info("Refresh token obtained")


def _create_success_response() -> web.Response:
    """Create HTML success response. Tokens stay in the token file."""
    return web.Response(
        text="""
        <html>
        <body style="font-family: Arial, sans-serif; padding: 40px;">
        <h1 style="color: #2e7d32;">✅ Authentication Successful!</h1>
        <p>Your tokens have been saved to the token file.</p>
        <p>You can close this window and return to the terminal.</p>
        </body>
        </html>
    """,
        content_type="text/html",
    )


def _create_token_error_response(status_code: int, response_text: str) -> web.Response:
    """Create error response for token exchange failures."""
    return web.Response(
        text=f"Error exchanging code for token: {status_code} - {response_text}",
        status=HTTPStatus.INTERNAL_SERVER_ERROR,
    )


async def callback_handler(request):
    global access_token, refresh_token  # noqa: PLW0603
    logger.info(f"Received callback request from {request.remote}")

    code = request.query.get("code")
    error = request.query.get("error")

    if error:
        error_desc = request.query.get("error_description")
        return _handle_oauth_error(error, error_desc)

    if not code:
        return _handle_missing_code()

    if request.query.get("state") != STATE:
        logger.error("State mismatch in OAuth callback")
        return web.Response(
            text="Error: State mismatch, please start the login again",
            status=HTTPStatus.BAD_REQUEST,
        )

    logger.info(f"Received authorization code: {code[:10]}...")

    try:
        token_data = await _exchange_code_for_token(code)
        if not token_data:
            return _create_token_error_response(500, "Token exchange failed")

        access_token, refresh_token = _store_tokens(token_data)
        _log_token_details(token_data, refresh_token)
        return _create_success_response()

    except Exception as e:
        logger.exception("Error during token exchange")
        return web.Response(
            text=f"Error: {str(e)}", status=HTTPStatus.INTERNAL_SERVER_ERROR
        )


def use_paste_flow() -> bool:
    """An https redirect to localhost cannot be served by the plain-HTTP
    callback server, so the user pastes the redirected URL instead."""
    return REDIRECT_SCHEME == "https" and REDIRECT_HOST in LOCAL_HOSTS


async def paste_flow() -> bool:
    """Log in by pasting the redirected URL from the browser's address bar."""
    auth_url = f"{AUTH_URL}?{urlencode(build_auth_params())}"
    print("\n📌 Opening browser for authentication...")
    print("If browser doesn't open automatically, visit this URL:")
    print(f"\n{auth_url}\n")
    webbrowser.open(auth_url)

    print(
        f"After you approve access, the browser is redirected to {REDIRECT_URI} "
        "and shows a connection error. That is expected.\n"
        "Copy the full URL from the address bar and paste it here."
    )
    pasted = await asyncio.to_thread(input, "\nRedirected URL: ")

    try:
        code = parse_callback_url(pasted)
    except ValueError as e:
        print(f"\n❌ {e}")
        return False

    token_data = await _exchange_code_for_token(code)
    if not token_data:
        print("\n❌ Token exchange failed, see the log above.")
        return False

    _, refresh_token = _store_tokens(token_data)
    _log_token_details(token_data, refresh_token)
    print(f"\n✅ Success! Tokens saved to: {token_store.token_file}")
    return True


async def start_server():
    logger.info("Initializing OAuth callback server...")

    app = web.Application()
    app.router.add_get("/callback", callback_handler)

    # Add a root route for testing
    async def root_handler(request):
        return web.Response(
            text="Wahoo OAuth callback server is running. Waiting for OAuth callback..."
        )

    app.router.add_get("/", root_handler)

    runner = web.AppRunner(app)
    await runner.setup()

    try:
        site = web.TCPSite(runner, HOST, PORT)
        await site.start()
        logger.info(f"OAuth callback server started on http://{HOST}:{PORT}")
        logger.info(f"Callback endpoint: http://{HOST}:{PORT}/callback")
    except OSError as e:
        logger.error(f"Failed to start server: {e}")
        if "Address already in use" in str(e):
            logger.error(
                f"Port {PORT} is already in use. Please close any other "
                "applications using this port."
            )
        await runner.cleanup()
        return

    auth_url = f"{AUTH_URL}?{urlencode(build_auth_params())}"

    logger.info("Opening browser for Wahoo OAuth authentication...")
    print("\n📌 Opening browser for authentication...")
    print("If browser doesn't open automatically, visit this URL:")
    print(f"\n{auth_url}\n")

    # Small delay to ensure server is ready
    await asyncio.sleep(0.5)

    if not webbrowser.open(auth_url):
        logger.warning("Failed to open browser automatically")

    logger.info("Waiting for OAuth callback...")
    print("⏳ Waiting for authentication callback...")

    # Wait for callback with timeout
    timeout = 300  # 5 minutes
    start_time = asyncio.get_event_loop().time()

    while access_token is None:
        if asyncio.get_event_loop().time() - start_time > timeout:
            logger.error(
                "Authentication timeout - no callback received within 5 minutes"
            )
            print("\n❌ Authentication timeout. Please try again.")
            break
        await asyncio.sleep(1)

    if access_token:
        print("\n✅ Success! Your tokens have been obtained.")
        print(f"\n💾 Tokens have been saved to: {token_store.token_file}")
        print("\n💡 Your tokens will be automatically refreshed when needed.")

    logger.info("Shutting down OAuth callback server...")
    await runner.cleanup()
    logger.info("Server shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(paste_flow() if use_paste_flow() else start_server())
    except KeyboardInterrupt:
        logger.info("Authentication process cancelled by user")
        print("\n\nAuthentication cancelled.")
    except Exception as e:
        logger.exception("Unexpected error during authentication")
        print(f"\n❌ Error: {e}")
