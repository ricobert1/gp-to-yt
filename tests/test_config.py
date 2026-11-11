"""Unit tests for configuration loading and validation."""

from pathlib import Path
import pytest

from photos2yt.cli import main
from photos2yt.config import (
    Config,
    ConfigError,
    load_config,
    validate_and_parse_config,
)


def test_load_example_config():
    """Verify config.yaml.example loads cleanly and matches schema."""
    example_path = Path(__file__).parent.parent / "config.yaml.example"
    cfg = load_config(example_path)

    assert isinstance(cfg, Config)
    assert cfg.timezone == "America/New_York"
    assert cfg.tzinfo.key == "America/New_York"
    assert cfg.youtube.privacy == "unlisted"
    assert cfg.youtube.category_id == "22"
    assert cfg.youtube.made_for_kids is False
    assert cfg.youtube.title_template == "{title} – {weekday}, {month_name} {day_num}, {year} {time}"
    assert cfg.youtube.default_title == "Family Video"

    assert cfg.quota.daily_unit_limit == 10000
    assert cfg.quota.upload_cost_units == 1600
    assert cfg.quota.playlist_insert_cost == 50
    assert cfg.quota.playlist_item_insert_cost == 50
    assert cfg.quota.daily_upload_limit is None

    assert ".mp4" in cfg.video_extensions
    assert ".mov" in cfg.video_extensions
    assert len(cfg.video_extensions) == 6


def test_missing_file_raises_config_error(tmp_path):
    missing_file = tmp_path / "does_not_exist.yaml"
    with pytest.raises(ConfigError, match="Config file not found"):
        load_config(missing_file)


def test_malformed_yaml_raises_config_error(tmp_path):
    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text("paths: [unclosed list", encoding="utf-8")
    with pytest.raises(ConfigError, match="Error parsing YAML"):
        load_config(bad_yaml)


def test_non_dict_root():
    with pytest.raises(ConfigError, match="must be a dictionary"):
        validate_and_parse_config(["item1", "item2"])


def test_missing_required_sections():
    with pytest.raises(ConfigError) as exc_info:
        validate_and_parse_config({})
    err = str(exc_info.value)
    assert "Missing required top-level section: 'paths'" in err
    assert "Missing required top-level key: 'timezone'" in err
    assert "Missing required top-level section: 'youtube'" in err
    assert "Missing required top-level section: 'quota'" in err
    assert "Missing required top-level key: 'video_extensions'" in err


def test_missing_paths_fields():
    raw = {
        "paths": {"staging_dir": "/staging"},
        "timezone": "America/New_York",
        "youtube": {
            "privacy": "unlisted",
            "category_id": "22",
            "made_for_kids": False,
            "title_template": "{date} - {title}",
            "default_title": "Family Video",
        },
        "quota": {
            "daily_unit_limit": 10000,
            "upload_cost_units": 1600,
            "playlist_insert_cost": 50,
            "playlist_item_insert_cost": 50,
        },
        "video_extensions": [".mp4"],
    }
    with pytest.raises(ConfigError) as exc_info:
        validate_and_parse_config(raw)
    err = str(exc_info.value)
    assert "paths.db_path is required" in err
    assert "paths.credentials is required" in err
    assert "paths.token is required" in err


def test_invalid_timezone():
    raw = {
        "paths": {
            "staging_dir": "/staging",
            "db_path": "queue.db",
            "credentials": "credentials.json",
            "token": "token.json",
        },
        "timezone": "Mars/Olympus_Mons",
        "youtube": {
            "privacy": "unlisted",
            "category_id": "22",
            "made_for_kids": False,
            "title_template": "{date} - {title}",
            "default_title": "Family Video",
        },
        "quota": {
            "daily_unit_limit": 10000,
            "upload_cost_units": 1600,
            "playlist_insert_cost": 50,
            "playlist_item_insert_cost": 50,
        },
        "video_extensions": [".mp4"],
    }
    with pytest.raises(ConfigError, match="Invalid timezone 'Mars/Olympus_Mons'"):
        validate_and_parse_config(raw)


def test_invalid_privacy():
    raw = {
        "paths": {
            "staging_dir": "/staging",
            "db_path": "queue.db",
            "credentials": "credentials.json",
            "token": "token.json",
        },
        "timezone": "America/New_York",
        "youtube": {
            "privacy": "confidential",
            "category_id": "22",
            "made_for_kids": False,
            "title_template": "{date} - {title}",
            "default_title": "Family Video",
        },
        "quota": {
            "daily_unit_limit": 10000,
            "upload_cost_units": 1600,
            "playlist_insert_cost": 50,
            "playlist_item_insert_cost": 50,
        },
        "video_extensions": [".mp4"],
    }
    with pytest.raises(ConfigError, match="youtube.privacy must be one of"):
        validate_and_parse_config(raw)


def test_invalid_quota_types():
    raw = {
        "paths": {
            "staging_dir": "/staging",
            "db_path": "queue.db",
            "credentials": "credentials.json",
            "token": "token.json",
        },
        "timezone": "America/New_York",
        "youtube": {
            "privacy": "unlisted",
            "category_id": "22",
            "made_for_kids": False,
            "title_template": "{date} - {title}",
            "default_title": "Family Video",
        },
        "quota": {
            "daily_unit_limit": -5,
            "upload_cost_units": "expensive",
            "playlist_insert_cost": 50,
            "playlist_item_insert_cost": 50,
            "daily_upload_limit": 0,
        },
        "video_extensions": [".mp4"],
    }
    with pytest.raises(ConfigError) as exc_info:
        validate_and_parse_config(raw)
    err = str(exc_info.value)
    assert "quota.daily_unit_limit must be a non-negative integer" in err
    assert "quota.upload_cost_units must be a non-negative integer" in err
    assert "quota.daily_upload_limit must be null or a positive integer" in err


def test_video_extensions_normalization():
    raw = {
        "paths": {
            "staging_dir": "/staging",
            "db_path": "queue.db",
            "credentials": "credentials.json",
            "token": "token.json",
        },
        "timezone": "America/New_York",
        "youtube": {
            "privacy": "unlisted",
            "category_id": "22",
            "made_for_kids": False,
            "title_template": "{date} - {title}",
            "default_title": "Family Video",
        },
        "quota": {
            "daily_unit_limit": 10000,
            "upload_cost_units": 1600,
            "playlist_insert_cost": 50,
            "playlist_item_insert_cost": 50,
        },
        "video_extensions": ["MP4", ".MOV", "mkv"],
    }
    cfg = validate_and_parse_config(raw)
    assert cfg.video_extensions == (".mp4", ".mov", ".mkv")


def test_cli_config_check_valid():
    example_path = str(Path(__file__).parent.parent / "config.yaml.example")
    ret = main(["-c", example_path, "config", "check"])
    assert ret == 0


def test_cli_config_check_invalid(tmp_path, capsys):
    bad_config = tmp_path / "bad.yaml"
    bad_config.write_text("paths: {}", encoding="utf-8")
    ret = main(["-c", str(bad_config), "config", "check"])
    assert ret != 0
    captured = capsys.readouterr()
    assert "Configuration error:" in captured.err
    assert "paths.staging_dir is required" in captured.err


def test_cli_help_flag(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "photos2yt" in captured.out
    assert "Google Photos to YouTube Migration" in captured.out


def test_cli_no_args_prints_help(capsys):
    ret = main([])
    assert ret == 0
    captured = capsys.readouterr()
    assert "photos2yt" in captured.out
    assert "Google Photos to YouTube Migration" in captured.out


# --- youtube.upload_chunk_size (Task 6) ---

def _raw_with_youtube(**youtube_extra):
    youtube = {
        "privacy": "unlisted",
        "category_id": "22",
        "made_for_kids": False,
        "title_template": "{date} - {title}",
        "default_title": "Family Video",
    }
    youtube.update(youtube_extra)
    return {
        "paths": {
            "staging_dir": "/staging",
            "db_path": "queue.db",
            "credentials": "credentials.json",
            "token": "token.json",
        },
        "timezone": "America/New_York",
        "youtube": youtube,
        "quota": {
            "daily_unit_limit": 10000,
            "upload_cost_units": 1600,
            "playlist_insert_cost": 50,
            "playlist_item_insert_cost": 50,
        },
        "video_extensions": [".mp4"],
    }


def test_upload_chunk_size_defaults_to_8_mib():
    cfg = validate_and_parse_config(_raw_with_youtube())
    assert cfg.youtube.upload_chunk_size == 8 * 1024 * 1024


def test_example_config_sets_upload_chunk_size():
    example_path = Path(__file__).parent.parent / "config.yaml.example"
    assert load_config(example_path).youtube.upload_chunk_size == 8 * 1024 * 1024


def test_upload_chunk_size_custom_value():
    cfg = validate_and_parse_config(_raw_with_youtube(upload_chunk_size=256 * 1024 * 4))
    assert cfg.youtube.upload_chunk_size == 1024 * 1024


@pytest.mark.parametrize("bad", [0, -262144, 1000, 262145, "8MiB", True, 1.5])
def test_upload_chunk_size_invalid(bad):
    with pytest.raises(ConfigError, match="youtube.upload_chunk_size"):
        validate_and_parse_config(_raw_with_youtube(upload_chunk_size=bad))


def test_channel_verified_defaults_false_and_parses():
    assert validate_and_parse_config(_raw_with_youtube()).youtube.channel_verified is False
    cfg = validate_and_parse_config(_raw_with_youtube(channel_verified=True))
    assert cfg.youtube.channel_verified is True


def test_channel_verified_must_be_bool():
    with pytest.raises(ConfigError, match="youtube.channel_verified"):
        validate_and_parse_config(_raw_with_youtube(channel_verified="yes"))


def test_upload_order_defaults_to_largest_first_and_parses():
    assert validate_and_parse_config(_raw_with_youtube()).youtube.upload_order == "largest_first"
    cfg = validate_and_parse_config(_raw_with_youtube(upload_order="oldest_first"))
    assert cfg.youtube.upload_order == "oldest_first"


@pytest.mark.parametrize("bad", ["biggest", "", None, 1])
def test_upload_order_invalid(bad):
    with pytest.raises(ConfigError, match="upload_order"):
        validate_and_parse_config(_raw_with_youtube(upload_order=bad))
