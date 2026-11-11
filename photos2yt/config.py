"""Configuration schema, validation, and loading for photos2yt."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
import zoneinfo
import yaml


class ConfigError(Exception):
    """Raised when configuration file is missing, invalid YAML, or fails validation."""
    pass


VALID_PRIVACY_VALUES = {"private", "unlisted", "public"}

# Resumable upload chunks must be a multiple of 256 KiB (YouTube/Google API rule).
UPLOAD_CHUNK_ALIGNMENT = 256 * 1024
DEFAULT_UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MiB


@dataclass(frozen=True)
class PathsConfig:
    staging_dir: Path
    db_path: Path
    credentials: Path
    token: Path


@dataclass(frozen=True)
class YouTubeConfig:
    privacy: str
    category_id: str
    made_for_kids: bool
    title_template: str
    default_title: str
    upload_chunk_size: int = DEFAULT_UPLOAD_CHUNK_SIZE
    channel_verified: bool = False  # verified channels may upload videos > 15 min


@dataclass(frozen=True)
class QuotaConfig:
    daily_unit_limit: int
    upload_cost_units: int
    playlist_insert_cost: int
    playlist_item_insert_cost: int
    daily_upload_limit: Optional[int] = None


@dataclass(frozen=True)
class Config:
    paths: PathsConfig
    timezone: str
    youtube: YouTubeConfig
    quota: QuotaConfig
    video_extensions: tuple[str, ...]
    config_path: Optional[Path] = None

    @property
    def tzinfo(self) -> zoneinfo.ZoneInfo:
        return zoneinfo.ZoneInfo(self.timezone)


def _resolve_path(raw_path: str | Path, base_dir: Optional[Path]) -> Path:
    p = Path(raw_path).expanduser()
    if not p.is_absolute() and base_dir is not None:
        return (base_dir / p).resolve()
    return p.resolve() if p.is_absolute() else p


def validate_and_parse_config(raw: Any, base_dir: Optional[Path] = None, config_path: Optional[Path] = None) -> Config:
    """Validate a raw dictionary against the photos2yt configuration schema."""
    if not isinstance(raw, dict):
        raise ConfigError(f"Root configuration must be a dictionary/mapping, got {type(raw).__name__}")

    errors: list[str] = []

    # 1. Validate 'paths'
    paths_data = raw.get("paths")
    paths_obj: Optional[PathsConfig] = None
    if paths_data is None:
        errors.append("Missing required top-level section: 'paths'")
    elif not isinstance(paths_data, dict):
        errors.append(f"'paths' must be a mapping, got {type(paths_data).__name__}")
    else:
        for field in ("staging_dir", "db_path", "credentials", "token"):
            val = paths_data.get(field)
            if val is None or not str(val).strip():
                errors.append(f"paths.{field} is required and cannot be empty")
        
        if not errors:  # If no errors so far in paths, construct PathsConfig
            paths_obj = PathsConfig(
                staging_dir=_resolve_path(str(paths_data["staging_dir"]), base_dir),
                db_path=_resolve_path(str(paths_data["db_path"]), base_dir),
                credentials=_resolve_path(str(paths_data["credentials"]), base_dir),
                token=_resolve_path(str(paths_data["token"]), base_dir),
            )

    # 2. Validate 'timezone'
    tz = raw.get("timezone")
    if tz is None or not str(tz).strip():
        errors.append("Missing required top-level key: 'timezone'")
    else:
        tz_str = str(tz).strip()
        try:
            zoneinfo.ZoneInfo(tz_str)
        except Exception:
            errors.append(f"Invalid timezone '{tz_str}': must be a valid IANA timezone (e.g. 'America/New_York')")

    # 3. Validate 'youtube'
    yt_data = raw.get("youtube")
    yt_obj: Optional[YouTubeConfig] = None
    if yt_data is None:
        errors.append("Missing required top-level section: 'youtube'")
    elif not isinstance(yt_data, dict):
        errors.append(f"'youtube' must be a mapping, got {type(yt_data).__name__}")
    else:
        privacy = yt_data.get("privacy")
        if privacy is None or privacy not in VALID_PRIVACY_VALUES:
            errors.append(
                f"youtube.privacy must be one of {sorted(VALID_PRIVACY_VALUES)}, got {repr(privacy)}"
            )

        cat_id = yt_data.get("category_id")
        if cat_id is None or str(cat_id).strip() == "":
            errors.append("youtube.category_id is required (e.g. '22')")

        made_for_kids = yt_data.get("made_for_kids")
        if not isinstance(made_for_kids, bool):
            errors.append(f"youtube.made_for_kids must be a boolean (true/false), got {repr(made_for_kids)}")

        title_tpl = yt_data.get("title_template")
        if not isinstance(title_tpl, str) or not title_tpl.strip():
            errors.append("youtube.title_template is required and must be a non-empty string")

        default_title = yt_data.get("default_title")
        if not isinstance(default_title, str) or not default_title.strip():
            errors.append("youtube.default_title is required and must be a non-empty string")

        chunk_size = yt_data.get("upload_chunk_size", DEFAULT_UPLOAD_CHUNK_SIZE)
        chunk_ok = (
            isinstance(chunk_size, int)
            and not isinstance(chunk_size, bool)
            and chunk_size > 0
            and chunk_size % UPLOAD_CHUNK_ALIGNMENT == 0
        )
        if not chunk_ok:
            errors.append(
                "youtube.upload_chunk_size must be a positive integer number of bytes and a "
                f"multiple of {UPLOAD_CHUNK_ALIGNMENT} (256 KiB), e.g. 8388608 for 8 MiB; "
                f"got {repr(chunk_size)}"
            )

        channel_verified = yt_data.get("channel_verified", False)
        if not isinstance(channel_verified, bool):
            errors.append(
                f"youtube.channel_verified must be a boolean (true/false), got {repr(channel_verified)}"
            )

        if yt_data and all(k in yt_data for k in ("privacy", "category_id", "made_for_kids", "title_template", "default_title")):
            if privacy in VALID_PRIVACY_VALUES and isinstance(made_for_kids, bool):
                yt_obj = YouTubeConfig(
                    privacy=str(privacy),
                    category_id=str(cat_id),
                    made_for_kids=made_for_kids,
                    title_template=str(title_tpl),
                    default_title=str(default_title),
                    upload_chunk_size=chunk_size if chunk_ok else DEFAULT_UPLOAD_CHUNK_SIZE,
                    channel_verified=channel_verified if isinstance(channel_verified, bool) else False,
                )

    # 4. Validate 'quota'
    quota_data = raw.get("quota")
    quota_obj: Optional[QuotaConfig] = None
    if quota_data is None:
        errors.append("Missing required top-level section: 'quota'")
    elif not isinstance(quota_data, dict):
        errors.append(f"'quota' must be a mapping, got {type(quota_data).__name__}")
    else:
        for field in ("daily_unit_limit", "upload_cost_units", "playlist_insert_cost", "playlist_item_insert_cost"):
            val = quota_data.get(field)
            if val is None or isinstance(val, bool) or not isinstance(val, int) or val < 0:
                errors.append(f"quota.{field} must be a non-negative integer, got {repr(val)}")

        daily_upload_limit = quota_data.get("daily_upload_limit")
        if daily_upload_limit is not None:
            if isinstance(daily_upload_limit, bool) or not isinstance(daily_upload_limit, int) or daily_upload_limit <= 0:
                errors.append(
                    f"quota.daily_upload_limit must be null or a positive integer, got {repr(daily_upload_limit)}"
                )

        if quota_data and all(
            k in quota_data and isinstance(quota_data[k], int) and not isinstance(quota_data[k], bool) and quota_data[k] >= 0
            for k in ("daily_unit_limit", "upload_cost_units", "playlist_insert_cost", "playlist_item_insert_cost")
        ):
            quota_obj = QuotaConfig(
                daily_unit_limit=quota_data["daily_unit_limit"],
                upload_cost_units=quota_data["upload_cost_units"],
                playlist_insert_cost=quota_data["playlist_insert_cost"],
                playlist_item_insert_cost=quota_data["playlist_item_insert_cost"],
                daily_upload_limit=daily_upload_limit,
            )

    # 5. Validate 'video_extensions'
    exts_data = raw.get("video_extensions")
    exts_tuple: tuple[str, ...] = ()
    if exts_data is None:
        errors.append("Missing required top-level key: 'video_extensions'")
    elif not isinstance(exts_data, (list, tuple)):
        errors.append(f"'video_extensions' must be a list of extension strings, got {type(exts_data).__name__}")
    elif len(exts_data) == 0:
        errors.append("'video_extensions' cannot be empty")
    else:
        normalized_exts: list[str] = []
        for i, item in enumerate(exts_data):
            if not isinstance(item, str) or not item.strip():
                errors.append(f"video_extensions[{i}] must be a non-empty string, got {repr(item)}")
            else:
                s = item.strip().lower()
                if not s.startswith("."):
                    s = f".{s}"
                normalized_exts.append(s)
        exts_tuple = tuple(normalized_exts)

    if errors:
        msg = "Configuration validation failed:\n  - " + "\n  - ".join(errors)
        raise ConfigError(msg)

    assert paths_obj is not None
    assert yt_obj is not None
    assert quota_obj is not None

    return Config(
        paths=paths_obj,
        timezone=str(tz).strip(),
        youtube=yt_obj,
        quota=quota_obj,
        video_extensions=exts_tuple,
        config_path=config_path,
    )


def load_config(path: str | Path | None = None) -> Config:
    """Load and validate configuration from a YAML file.
    
    If path is None, looks at PHOTOS2YT_CONFIG environment variable,
    falling back to 'config.yaml' in the current working directory.
    """
    if path is None:
        env_val = os.environ.get("PHOTOS2YT_CONFIG")
        target_path = Path(env_val) if env_val else Path("config.yaml")
    else:
        target_path = Path(path)

    if not target_path.exists():
        raise ConfigError(f"Config file not found: {target_path}")

    try:
        with open(target_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError(f"Error parsing YAML from {target_path}:\n{e}") from e
    except OSError as e:
        raise ConfigError(f"Error reading config file {target_path}: {e}") from e

    base_dir = target_path.parent.resolve()
    return validate_and_parse_config(raw, base_dir=base_dir, config_path=target_path.resolve())
