"""Unit tests for photos2yt.auth OAuth flow, token storage, and credential handling."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials

from photos2yt.auth import (
    AuthError,
    authenticate,
    check_authorization,
    get_credentials,
    save_token,
    validate_credentials_file,
    YOUTUBE_SCOPE,
)
from photos2yt.cli import main
from photos2yt.config import Config, PathsConfig, QuotaConfig, YouTubeConfig


@pytest.fixture
def dummy_config(tmp_path: Path) -> Config:
    """Fixture providing a valid Config pointing to temporary files."""
    staging = tmp_path / "staging"
    staging.mkdir()
    creds_file = tmp_path / "credentials.json"
    token_file = tmp_path / "token.json"
    db_file = tmp_path / "test.db"

    return Config(
        paths=PathsConfig(
            staging_dir=staging,
            db_path=db_file,
            credentials=creds_file,
            token=token_file,
        ),
        timezone="America/New_York",
        youtube=YouTubeConfig(
            privacy="unlisted",
            category_id="22",
            made_for_kids=False,
            title_template="{date} - {title}",
            default_title="Family Video",
        ),
        quota=QuotaConfig(
            daily_unit_limit=10000,
            upload_cost_units=1600,
            playlist_insert_cost=50,
            playlist_item_insert_cost=50,
        ),
        video_extensions=(".mp4", ".mov"),
        config_path=tmp_path / "config.yaml",
    )


@pytest.fixture
def desktop_credentials_file(tmp_path: Path) -> Path:
    """Fixture creating a valid desktop ('installed') OAuth credentials file."""
    path = tmp_path / "desktop_credentials.json"
    content = {
        "installed": {
            "client_id": "test-client-id.apps.googleusercontent.com",
            "project_id": "test-project",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_secret": "test-client-secret-xyz",
            "redirect_uris": ["http://localhost"],
        }
    }
    path.write_text(json.dumps(content), encoding="utf-8")
    return path


def test_missing_credentials_file(tmp_path: Path):
    """Refuse to proceed if credentials.json is missing."""
    non_existent = tmp_path / "missing_credentials.json"
    with pytest.raises(AuthError, match="OAuth credentials file not found"):
        validate_credentials_file(non_existent)

    with pytest.raises(AuthError, match="OAuth credentials file not found"):
        authenticate(
            credentials_path=non_existent,
            token_path=tmp_path / "token.json",
        )


def test_wrong_client_type_web(tmp_path: Path):
    """Refuse to proceed if credentials.json is a 'web' application credential."""
    web_creds = tmp_path / "web_credentials.json"
    web_creds.write_text(
        json.dumps({
            "web": {
                "client_id": "web-client-id.apps.googleusercontent.com",
                "client_secret": "super-secret-web-key",
            }
        }),
        encoding="utf-8",
    )

    with pytest.raises(AuthError, match="configured for a 'web' application.*requires a Desktop"):
        validate_credentials_file(web_creds)

    with pytest.raises(AuthError, match="configured for a 'web' application.*requires a Desktop"):
        authenticate(
            credentials_path=web_creds,
            token_path=tmp_path / "token.json",
        )


def test_wrong_client_type_missing_installed_key(tmp_path: Path):
    """Refuse to proceed if credentials.json lacks the 'installed' key."""
    other_creds = tmp_path / "other_credentials.json"
    other_creds.write_text(
        json.dumps({"some_other_type": {"client_id": "abc"}}),
        encoding="utf-8",
    )

    with pytest.raises(AuthError, match="missing the 'installed' client configuration"):
        validate_credentials_file(other_creds)


def test_invalid_json_credentials(tmp_path: Path):
    """Refuse to proceed if credentials.json is not valid JSON."""
    bad_json = tmp_path / "corrupted.json"
    bad_json.write_text("{invalid-json", encoding="utf-8")

    with pytest.raises(AuthError, match="Failed to parse credentials file"):
        validate_credentials_file(bad_json)


def test_token_created_with_600_permissions(tmp_path: Path, desktop_credentials_file: Path):
    """token.json must be created with chmod 600 permissions."""
    token_path = tmp_path / "token.json"

    # Mock InstalledAppFlow to return fake credentials without launching a browser
    mock_creds = MagicMock(spec=Credentials)
    mock_creds.to_json.return_value = json.dumps({
        "token": "mock-access-token",
        "refresh_token": "mock-refresh-token",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "test-client-id",
        "client_secret": "test-client-secret",
        "scopes": [YOUTUBE_SCOPE],
    })

    mock_flow = MagicMock()
    mock_flow.run_local_server.return_value = mock_creds

    with patch("photos2yt.auth.InstalledAppFlow.from_client_secrets_file", return_value=mock_flow) as mock_flow_init:
        res = authenticate(
            credentials_path=desktop_credentials_file,
            token_path=token_path,
        )

    mock_flow_init.assert_called_once_with(str(desktop_credentials_file), scopes=[YOUTUBE_SCOPE])
    mock_flow.run_local_server.assert_called_once_with(port=0, open_browser=True)

    assert token_path.exists()
    mode = stat.S_IMODE(token_path.stat().st_mode)
    assert mode == 0o600


def test_no_browser_option(tmp_path: Path, desktop_credentials_file: Path):
    """--no-browser should pass open_browser=False to run_local_server."""
    token_path = tmp_path / "token.json"

    mock_creds = MagicMock(spec=Credentials)
    mock_creds.to_json.return_value = "{}"

    mock_flow = MagicMock()
    mock_flow.run_local_server.return_value = mock_creds

    with patch("photos2yt.auth.InstalledAppFlow.from_client_secrets_file", return_value=mock_flow):
        authenticate(
            credentials_path=desktop_credentials_file,
            token_path=token_path,
            no_browser=True,
        )

    mock_flow.run_local_server.assert_called_once_with(port=0, open_browser=False)


def test_refresh_path_rewrites_token_and_keeps_600(tmp_path: Path):
    """Expired credentials must be refreshed and rewritten to disk with 0600 mode preserved."""
    token_path = tmp_path / "token.json"
    initial_content = json.dumps({
        "token": "expired-access-token",
        "refresh_token": "valid-refresh-token",
    })
    save_token(token_path, initial_content)
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600

    refreshed_content = json.dumps({
        "token": "new-shiny-access-token",
        "refresh_token": "valid-refresh-token",
    })

    # Create mock credentials that represent an expired token with a refresh token
    mock_creds = MagicMock(spec=Credentials)
    mock_creds.valid = False
    mock_creds.expired = True
    mock_creds.refresh_token = "valid-refresh-token"
    mock_creds.to_json.return_value = refreshed_content

    def do_refresh(request):
        mock_creds.valid = True
        mock_creds.expired = False

    mock_creds.refresh.side_effect = do_refresh

    with patch("photos2yt.auth.Credentials.from_authorized_user_file", return_value=mock_creds):
        with patch("photos2yt.auth.Request"):
            creds = get_credentials(token_path=token_path)

    assert creds is mock_creds
    mock_creds.refresh.assert_called_once()

    # Verify token on disk was updated and retains 0600 permissions
    assert token_path.read_text(encoding="utf-8") == refreshed_content
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600


def test_revoked_refresh_error_message(tmp_path: Path):
    """When refresh token is revoked or invalid, raise clear actionable error instructing to re-run auth."""
    token_path = tmp_path / "token.json"
    save_token(token_path, "{}")

    mock_creds = MagicMock(spec=Credentials)
    mock_creds.valid = False
    mock_creds.expired = True
    mock_creds.refresh_token = "revoked-token"
    mock_creds.refresh.side_effect = RefreshError("invalid_grant: Token has been expired or revoked.")

    with patch("photos2yt.auth.Credentials.from_authorized_user_file", return_value=mock_creds):
        with patch("photos2yt.auth.Request"):
            with pytest.raises(AuthError) as exc_info:
                get_credentials(token_path=token_path)

    err = str(exc_info.value)
    assert "OAuth refresh token is invalid or has been revoked" in err
    assert "photos2yt auth" in err
    # Ensure raw secret details are never exposed
    assert "revoked-token" not in err


def test_get_credentials_never_triggers_interactive_flow(tmp_path: Path):
    """get_credentials() must NEVER launch an interactive or browser OAuth flow."""
    # Scenario 1: Token file does not exist
    missing_token = tmp_path / "nonexistent_token.json"
    with patch("photos2yt.auth.InstalledAppFlow") as mock_flow:
        with pytest.raises(AuthError, match="OAuth token file not found"):
            get_credentials(token_path=missing_token)
        mock_flow.assert_not_called()

    # Scenario 2: Token file is invalid JSON / corrupted
    corrupt_token = tmp_path / "corrupt_token.json"
    save_token(corrupt_token, "{not-json")
    with patch("photos2yt.auth.InstalledAppFlow") as mock_flow:
        with pytest.raises(AuthError, match="Failed to read OAuth token"):
            get_credentials(token_path=corrupt_token)
        mock_flow.assert_not_called()

    # Scenario 3: Token is expired and has no refresh token
    no_refresh_token = tmp_path / "no_refresh.json"
    save_token(no_refresh_token, "{}")
    mock_creds = MagicMock(spec=Credentials)
    mock_creds.valid = False
    mock_creds.refresh_token = None
    with patch("photos2yt.auth.Credentials.from_authorized_user_file", return_value=mock_creds):
        with patch("photos2yt.auth.InstalledAppFlow") as mock_flow:
            with pytest.raises(AuthError, match="OAuth token is expired and cannot be refreshed"):
                get_credentials(token_path=no_refresh_token)
            mock_flow.assert_not_called()


def test_check_authorization_prints_channel_and_nothing_secret(dummy_config: Config, capsys):
    """photos2yt auth --check calls channels.list and prints channel title and ID, leaking nothing secret."""
    mock_creds = MagicMock(spec=Credentials)
    mock_creds.valid = True

    mock_service = MagicMock()
    mock_channels = MagicMock()
    mock_list_req = MagicMock()
    mock_list_req.execute.return_value = {
        "items": [
            {
                "id": "UC_TEST_CHANNEL_12345",
                "snippet": {
                    "title": "Family Archive Channel",
                    "description": "Private family archive",
                },
            }
        ]
    }
    mock_channels.list.return_value = mock_list_req
    mock_service.channels.return_value = mock_channels

    with patch("photos2yt.auth.get_credentials", return_value=mock_creds):
        with patch("photos2yt.auth.build", return_value=mock_service) as mock_build:
            res = check_authorization(config=dummy_config)

    mock_build.assert_called_once_with("youtube", "v3", credentials=mock_creds, cache_discovery=False)
    mock_channels.list.assert_called_once_with(part="snippet", mine=True)

    assert res == {"id": "UC_TEST_CHANNEL_12345", "title": "Family Archive Channel"}

    captured = capsys.readouterr()
    assert "Channel Title: Family Archive Channel" in captured.out
    assert "Channel ID: UC_TEST_CHANNEL_12345" in captured.out

    # Verify no secret keywords or dump of dictionary
    assert "token" not in captured.out.lower()
    assert "secret" not in captured.out.lower()


def test_check_authorization_empty_channel(dummy_config: Config, capsys):
    """--check handles the case where no channel is found for the user."""
    mock_creds = MagicMock(spec=Credentials)
    mock_creds.valid = True

    mock_service = MagicMock()
    mock_channels = MagicMock()
    mock_list_req = MagicMock()
    mock_list_req.execute.return_value = {"items": []}
    mock_channels.list.return_value = mock_list_req
    mock_service.channels.return_value = mock_channels

    with patch("photos2yt.auth.get_credentials", return_value=mock_creds):
        with patch("photos2yt.auth.build", return_value=mock_service):
            res = check_authorization(config=dummy_config)

    assert res == {"id": "", "title": ""}
    captured = capsys.readouterr()
    assert "No YouTube channel found" in captured.out


def test_cli_auth_check_invokes_check_authorization(tmp_path: Path, desktop_credentials_file: Path, capsys):
    """CLI photos2yt auth --check should call check_authorization and output channel."""
    config_path = tmp_path / "config.yaml"
    token_path = tmp_path / "token.json"
    config_content = f"""
paths:
  staging_dir: {tmp_path}
  db_path: {tmp_path}/queue.db
  credentials: {desktop_credentials_file}
  token: {token_path}
timezone: America/New_York
youtube:
  privacy: unlisted
  category_id: "22"
  made_for_kids: false
  title_template: "{{date}} - {{title}}"
  default_title: "Family Video"
quota:
  daily_unit_limit: 10000
  upload_cost_units: 1600
  playlist_insert_cost: 50
  playlist_item_insert_cost: 50
video_extensions: [.mp4]
"""
    config_path.write_text(config_content.strip(), encoding="utf-8")

    with patch("photos2yt.cli.check_authorization") as mock_check:
        ret = main(["-c", str(config_path), "auth", "--check"])

    assert ret == 0
    mock_check.assert_called_once()


def test_cli_auth_invokes_authenticate(tmp_path: Path, desktop_credentials_file: Path):
    """CLI photos2yt auth should call authenticate."""
    config_path = tmp_path / "config.yaml"
    token_path = tmp_path / "token.json"
    config_content = f"""
paths:
  staging_dir: {tmp_path}
  db_path: {tmp_path}/queue.db
  credentials: {desktop_credentials_file}
  token: {token_path}
timezone: America/New_York
youtube:
  privacy: unlisted
  category_id: "22"
  made_for_kids: false
  title_template: "{{date}} - {{title}}"
  default_title: "Family Video"
quota:
  daily_unit_limit: 10000
  upload_cost_units: 1600
  playlist_insert_cost: 50
  playlist_item_insert_cost: 50
video_extensions: [.mp4]
"""
    config_path.write_text(config_content.strip(), encoding="utf-8")

    with patch("photos2yt.cli.authenticate") as mock_auth:
        ret = main(["-c", str(config_path), "auth", "--no-browser"])

    assert ret == 0
    mock_auth.assert_called_once()
    assert mock_auth.call_args[1]["no_browser"] is True


def test_valid_token_not_refreshed(tmp_path: Path):
    """If token is already valid, get_credentials returns it without refreshing."""
    token_path = tmp_path / "token.json"
    save_token(token_path, "{}")

    mock_creds = MagicMock(spec=Credentials)
    mock_creds.valid = True

    with patch("photos2yt.auth.Credentials.from_authorized_user_file", return_value=mock_creds):
        creds = get_credentials(token_path=token_path)

    assert creds is mock_creds
    mock_creds.refresh.assert_not_called()


def test_save_token_enforces_600_on_preexisting_permissive_file(tmp_path: Path):
    """save_token must reset permissions to 600 even if the existing file had 0644."""
    token_path = tmp_path / "token.json"
    token_path.write_text("old", encoding="utf-8")
    os.chmod(token_path, 0o644)
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o644

    save_token(token_path, '{"token": "new"}')
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600


def test_check_authorization_api_error_raises_auth_error(dummy_config: Config):
    """API errors during check_authorization should be wrapped in AuthError."""
    mock_creds = MagicMock(spec=Credentials)
    mock_creds.valid = True

    mock_service = MagicMock()
    mock_channels = MagicMock()
    mock_list_req = MagicMock()
    mock_list_req.execute.side_effect = RuntimeError("API service unavailable")
    mock_channels.list.return_value = mock_list_req
    mock_service.channels.return_value = mock_channels

    with patch("photos2yt.auth.get_credentials", return_value=mock_creds):
        with patch("photos2yt.auth.build", return_value=mock_service):
            with pytest.raises(AuthError, match="Failed to query YouTube channels"):
                check_authorization(config=dummy_config)


def test_cli_auth_error_handling(tmp_path: Path, desktop_credentials_file: Path, capsys):
    """CLI should catch AuthError, write to stderr, and exit with code 1."""
    config_path = tmp_path / "config.yaml"
    token_path = tmp_path / "token.json"
    config_content = f"""
paths:
  staging_dir: {tmp_path}
  db_path: {tmp_path}/queue.db
  credentials: {desktop_credentials_file}
  token: {token_path}
timezone: America/New_York
youtube:
  privacy: unlisted
  category_id: "22"
  made_for_kids: false
  title_template: "{{date}} - {{title}}"
  default_title: "Family Video"
quota:
  daily_unit_limit: 10000
  upload_cost_units: 1600
  playlist_insert_cost: 50
  playlist_item_insert_cost: 50
video_extensions: [.mp4]
"""
    config_path.write_text(config_content.strip(), encoding="utf-8")

    with patch("photos2yt.cli.authenticate", side_effect=AuthError("Mocked auth failure")):
        ret = main(["-c", str(config_path), "auth"])

    assert ret == 1
    captured = capsys.readouterr()
    assert "Authentication error:" in captured.err
    assert "Mocked auth failure" in captured.err
