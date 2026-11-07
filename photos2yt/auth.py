"""OAuth 2.0 authentication and token management for YouTube Data API v3."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build

from photos2yt.config import Config, load_config

YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube"
SCOPES = [YOUTUBE_SCOPE]


class AuthError(Exception):
    """Raised when authentication, token validation, or credential refresh fails."""
    pass


def validate_credentials_file(credentials_path: Path | str) -> dict[str, Any]:
    """Validate that the credentials file exists and is a desktop ('installed') OAuth client.

    Never logs or displays the secrets contained within the credentials file.

    Raises:
        AuthError: If the file is missing, unreadable, or not a desktop ('installed') client credential.
    """
    path = Path(credentials_path)
    if not path.exists():
        raise AuthError(
            f"OAuth credentials file not found: {path}. "
            "Please download desktop client credentials from the Google Cloud Console."
        )
    if not path.is_file():
        raise AuthError(f"OAuth credentials path is not a file: {path}")

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        raise AuthError(f"Failed to parse credentials file at {path}: {e}") from e

    if not isinstance(data, dict):
        raise AuthError(f"Invalid format in credentials file at {path}: expected JSON object.")

    if "installed" not in data:
        if "web" in data:
            raise AuthError(
                f"Credentials file '{path}' is configured for a 'web' application. "
                "photos2yt requires a Desktop ('installed') OAuth client credential."
            )
        raise AuthError(
            f"Credentials file '{path}' is missing the 'installed' client configuration. "
            "Expected top-level 'installed' key."
        )

    return data


def _write_secure_token_file(token_path: Path | str, content: str) -> None:
    """Write token JSON with chmod 600 permissions set at creation time.

    Uses os.open with O_CREAT and mode 0o600 so the file is never created
    with permissive permissions, and explicitly enforces 0o600 on the descriptor.
    """
    path = Path(token_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    try:
        os.chmod(fd, 0o600)
        with open(fd, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def save_token(token_path: Path | str, credentials_or_json: Credentials | str) -> None:
    """Save OAuth credentials to token.json with chmod 600 permissions.

    Never logs or displays token or secret values.
    """
    if isinstance(credentials_or_json, str):
        content = credentials_or_json
    else:
        content = credentials_or_json.to_json()
    _write_secure_token_file(token_path, content)


def get_credentials(
    token_path: Path | str | None = None,
    config: Config | None = None,
) -> Credentials:
    """Load credentials from token.json, auto-refreshing expired access tokens.

    If refreshed, saves the updated token back to token.json with 600 permissions.
    This function NEVER launches a browser or interactive flow; if authentication
    is missing or revoked, an AuthError is raised directing the user to run 'photos2yt auth'.

    Args:
        token_path: Path to token.json. If None, read from config.paths.token.
        config: Optional Config object. If None and token_path is None, loads from config.yaml.

    Returns:
        A valid google.oauth2.credentials.Credentials instance.

    Raises:
        AuthError: If the token is missing, corrupted, or revoked/expired without valid refresh.
    """
    if token_path is None:
        if config is None:
            config = load_config()
        token_path = config.paths.token
    else:
        token_path = Path(token_path)

    if not token_path.exists():
        raise AuthError(
            f"OAuth token file not found at '{token_path}'. "
            "Please run 'photos2yt auth' first to authenticate."
        )

    try:
        creds = Credentials.from_authorized_user_file(str(token_path), scopes=SCOPES)
    except Exception as e:
        raise AuthError(
            f"Failed to read OAuth token from '{token_path}'. "
            "The token file may be corrupted. Please re-run 'photos2yt auth' to re-authenticate."
        ) from e

    if not creds:
        raise AuthError(
            f"No valid credentials found in '{token_path}'. "
            "Please re-run 'photos2yt auth'."
        )

    if creds.valid:
        return creds

    if creds.refresh_token:
        try:
            request = Request()
            creds.refresh(request)
        except RefreshError as e:
            raise AuthError(
                "OAuth refresh token is invalid or has been revoked (or expired). "
                "Please re-run 'photos2yt auth' to re-authenticate."
            ) from e
        except Exception as e:
            raise AuthError(
                f"Failed to refresh OAuth token: {e}. "
                "Please re-run 'photos2yt auth' to re-authenticate."
            ) from e

        # Persist refreshed token with 0600 permissions
        save_token(token_path, creds)
        return creds

    raise AuthError(
        "OAuth token is expired and cannot be refreshed (no refresh token available). "
        "Please re-run 'photos2yt auth'."
    )


def authenticate(
    config: Config | None = None,
    credentials_path: Path | str | None = None,
    token_path: Path | str | None = None,
    no_browser: bool = False,
) -> Credentials:
    """Run interactive OAuth installed-app flow and save token with chmod 600 permissions.

    Args:
        config: Optional Config instance.
        credentials_path: Path to desktop credentials.json (defaults to config.paths.credentials).
        token_path: Path where token.json will be saved (defaults to config.paths.token).
        no_browser: If True, uses console/manual local-server flow without opening browser.

    Returns:
        Authorized Credentials instance.

    Raises:
        AuthError: If credentials.json is missing, wrong type, or the flow fails.
    """
    if config is None and (credentials_path is None or token_path is None):
        config = load_config()

    if credentials_path is None:
        assert config is not None
        credentials_path = config.paths.credentials
    else:
        credentials_path = Path(credentials_path)

    if token_path is None:
        assert config is not None
        token_path = config.paths.token
    else:
        token_path = Path(token_path)

    # Validate client credentials format and type
    validate_credentials_file(credentials_path)

    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(credentials_path),
            scopes=SCOPES,
        )
        creds = flow.run_local_server(
            port=0,
            open_browser=not no_browser,
        )
    except Exception as e:
        raise AuthError(f"OAuth authorization flow failed: {e}") from e

    save_token(token_path, creds)
    print(f"Authentication successful! Token saved to {token_path}")
    return creds


def get_youtube_service(
    config: Config | None = None,
    credentials: Credentials | None = None,
    token_path: Path | str | None = None,
) -> Resource:
    """Construct an authorized YouTube Data API v3 service resource."""
    creds = credentials or get_credentials(token_path=token_path, config=config)
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def check_authorization(
    config: Config | None = None,
    token_path: Path | str | None = None,
    credentials: Credentials | None = None,
) -> dict[str, str]:
    """Check authorization by calling channels.list?part=snippet&mine=true (1 quota unit).

    Prints only the channel title and ID, and never any secrets or tokens.
    Records 1 quota unit in quota_log if the quota module is available.

    Returns:
        Dict with 'id' and 'title'.

    Raises:
        AuthError: If authentication fails or YouTube API returns an error.
    """
    if config is None and token_path is None and credentials is None:
        config = load_config()

    creds = credentials or get_credentials(token_path=token_path, config=config)

    try:
        service = build("youtube", "v3", credentials=creds, cache_discovery=False)
        request = service.channels().list(part="snippet", mine=True)
        response = request.execute()
    except Exception as e:
        raise AuthError(f"Failed to query YouTube channels: {e}") from e

    items = response.get("items", [])
    if not items:
        print("No YouTube channel found for the authenticated account.")
        channel_info = {"id": "", "title": ""}
    else:
        channel_id = str(items[0].get("id", ""))
        channel_title = str(items[0].get("snippet", {}).get("title", ""))
        print(f"Channel Title: {channel_title}")
        print(f"Channel ID: {channel_id}")
        channel_info = {"id": channel_id, "title": channel_title}

    # Record 1 quota unit if quota module is available
    try:
        from photos2yt import quota
        if hasattr(quota, "record_usage"):
            quota.record_usage(config, units=1)
        elif hasattr(quota, "record"):
            quota.record(config, units=1)
        else:
            # TODO: Record 1 unit in quota_log when quota module (Task 5) is implemented
            pass
    except (ImportError, Exception):
        # TODO: Record 1 unit in quota_log when quota module (Task 5) is implemented
        pass

    return channel_info
