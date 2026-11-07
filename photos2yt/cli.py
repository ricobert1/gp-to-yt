"""Command line interface for photos2yt."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from photos2yt.auth import AuthError, authenticate, check_authorization
from photos2yt.config import ConfigError, load_config


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="photos2yt",
        description="Google Photos to YouTube Migration & Curation Tool",
    )
    parser.add_argument(
        "-c",
        "--config",
        dest="config_path",
        default=None,
        help="Path to YAML configuration file (default: config.yaml or $PHOTOS2YT_CONFIG)",
    )

    subparsers = parser.add_subparsers(dest="subcommand", help="Available subcommands")

    # config command (validate config)
    config_parser = subparsers.add_parser("config", help="Manage and inspect configuration")
    config_subparsers = config_parser.add_subparsers(dest="config_action", help="Config actions")
    config_subparsers.add_parser("check", help="Validate the configuration file")

    # auth command (Task 3)
    auth_parser = subparsers.add_parser("auth", help="Authorize with YouTube Data API")
    auth_parser.add_argument("--check", action="store_true", help="Check current channel authorization")
    auth_parser.add_argument("--no-browser", action="store_true", help="Use console-based OAuth flow")

    # ingest command (Task 4)
    ingest_parser = subparsers.add_parser("ingest", help="Ingest videos and sidecars from staging directory")
    ingest_parser.add_argument("--dry-run", action="store_true", help="Scan and report without writing to database")

    # upload command (Task 6)
    upload_parser = subparsers.add_parser("upload", help="Upload pending videos to YouTube")
    upload_parser.add_argument("--limit", type=int, default=None, help="Maximum number of videos to upload")
    upload_parser.add_argument("--dry-run", action="store_true", help="Simulate uploads without calling YouTube API")
    upload_parser.add_argument("--wait", action="store_true", help="Wait until quota reset instead of exiting")

    # curate command (Task 7)
    curate_parser = subparsers.add_parser("curate", help="Organize uploaded videos into playlists based on rules")
    curate_parser.add_argument("--dry-run", action="store_true", help="Simulate curation without modifying playlists")

    # reconcile command (Task 8)
    subparsers.add_parser("reconcile", help="Reconcile local database with remote YouTube videos")

    # report command (Task 8)
    subparsers.add_parser("report", help="Generate summary and manifest report")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = create_parser()
    args = parser.parse_args(argv)

    if not args.subcommand:
        parser.print_help()
        return 0

    # Handler for 'config check' or subcommands that need config
    try:
        if args.subcommand == "config":
            if args.config_action == "check" or args.config_action is None:
                config = load_config(args.config_path)
                print(f"Configuration is valid: {config.config_path}")
                return 0

        # For other commands, we will load config as well
        config = load_config(args.config_path)

        if args.subcommand == "auth":
            if args.check:
                check_authorization(config=config)
                return 0
            else:
                authenticate(config=config, no_browser=args.no_browser)
                return 0

        print(f"Command '{args.subcommand}' not yet implemented. Configuration loaded successfully.")
        return 0
    except ConfigError as err:
        sys.stderr.write(f"Configuration error:\n{err}\n")
        return 1
    except AuthError as err:
        sys.stderr.write(f"Authentication error:\n{err}\n")
        return 1
    except Exception as err:
        sys.stderr.write(f"Unexpected error: {err}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
