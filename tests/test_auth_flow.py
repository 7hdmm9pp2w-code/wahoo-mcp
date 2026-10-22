"""Tests for state checking, secret-vs-PKCE and the paste login flow."""

import os
import stat
from unittest.mock import patch
from urllib.parse import urlencode

import pytest

os.environ.setdefault("WAHOO_CLIENT_ID", "test_client_id")
os.environ.setdefault("WAHOO_CLIENT_SECRET", "test_client_secret")
os.environ.setdefault("WAHOO_TOKEN_FILE", "test_token.json")

from src import auth  # noqa: E402
from src.token_store import TokenData, TokenStore  # noqa: E402

REDIRECT = "https://localhost:8080/callback"


def _callback_url(**query) -> str:
    return f"{REDIRECT}?{urlencode(query)}"


class TestParseCallbackUrl:
    def test_returns_code_for_matching_state(self):
        with patch.object(auth, "STATE", "s1"):
            assert auth.parse_callback_url(_callback_url(code="c1", state="s1")) == "c1"

    def test_ignores_surrounding_whitespace(self):
        with patch.object(auth, "STATE", "s1"):
            url = f"  {_callback_url(code='c1', state='s1')}\n"
            assert auth.parse_callback_url(url) == "c1"

    def test_rejects_wrong_state(self):
        with patch.object(auth, "STATE", "s1"):
            with pytest.raises(ValueError, match="State mismatch"):
                auth.parse_callback_url(_callback_url(code="c1", state="other"))

    def test_rejects_missing_state(self):
        with patch.object(auth, "STATE", "s1"):
            with pytest.raises(ValueError, match="State mismatch"):
                auth.parse_callback_url(_callback_url(code="c1"))

    def test_reports_oauth_error(self):
        with pytest.raises(ValueError, match="access_denied"):
            auth.parse_callback_url(_callback_url(error="access_denied"))

    def test_rejects_missing_code(self):
        with patch.object(auth, "STATE", "s1"):
            with pytest.raises(ValueError, match="No authorization code"):
                auth.parse_callback_url(_callback_url(state="s1"))


class TestSecretOrPkce:
    def test_confidential_app_sends_secret_only(self):
        with patch.object(auth, "USE_PKCE", False):
            data = auth.build_token_request("c1")
            params = auth.build_auth_params()
        assert data["client_secret"] == auth.CLIENT_SECRET
        assert "code_verifier" not in data
        assert "code_challenge" not in params

    def test_public_app_sends_pkce_only(self):
        with patch.object(auth, "USE_PKCE", True):
            data = auth.build_token_request("c1")
            params = auth.build_auth_params()
        assert data["code_verifier"] == auth.code_verifier
        assert "client_secret" not in data
        assert params["code_challenge"] == auth.code_challenge
        assert params["code_challenge_method"] == "S256"

    def test_auth_params_carry_state_and_scopes(self):
        params = auth.build_auth_params()
        assert params["state"] == auth.STATE
        assert "workouts_write" in params["scope"]


class TestPasteFlowSelection:
    @pytest.mark.parametrize(
        ("scheme", "host", "expected"),
        [
            ("https", "localhost", True),
            ("https", "127.0.0.1", True),
            ("http", "localhost", False),
            ("https", "wahoo.example.com", False),
        ],
    )
    def test_use_paste_flow(self, scheme, host, expected):
        with (
            patch.object(auth, "REDIRECT_SCHEME", scheme),
            patch.object(auth, "REDIRECT_HOST", host),
        ):
            assert auth.use_paste_flow() is expected

    async def test_paste_flow_stores_tokens(self):
        url = _callback_url(code="c1", state="s1")
        token_response = {"access_token": "a", "refresh_token": "r", "expires_in": 7200}
        with (
            patch.object(auth, "STATE", "s1"),
            patch.object(auth, "webbrowser"),
            patch("builtins.input", return_value=url),
            patch.object(
                auth, "_exchange_code_for_token", return_value=token_response
            ) as exchange,
            patch.object(auth, "token_store") as store,
        ):
            assert await auth.paste_flow() is True
        exchange.assert_awaited_once_with("c1")
        store.save.assert_called_once()

    async def test_paste_flow_rejects_foreign_url(self):
        url = _callback_url(code="c1", state="forged")
        with (
            patch.object(auth, "STATE", "s1"),
            patch.object(auth, "webbrowser"),
            patch("builtins.input", return_value=url),
            patch.object(auth, "_exchange_code_for_token") as exchange,
        ):
            assert await auth.paste_flow() is False
        exchange.assert_not_called()


class TestCallbackSuccessPage:
    def test_page_does_not_contain_tokens(self):
        text = auth._create_success_response().text
        assert "Authentication Successful" in text
        assert auth.code_verifier not in text


class TestTokenFilePermissions:
    def test_new_file_is_owner_only(self, tmp_path):
        token_file = tmp_path / "token.json"
        TokenStore(str(token_file)).save(TokenData(access_token="a"))
        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600

    def test_existing_loose_file_is_tightened(self, tmp_path):
        token_file = tmp_path / "token.json"
        token_file.write_text("{}")
        token_file.chmod(0o644)
        TokenStore(str(token_file)).save(TokenData(access_token="a"))
        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
