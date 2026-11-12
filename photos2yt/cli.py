"""Command line interface for photos2yt."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional, Sequence

from photos2yt import ECHOED_TO_TERMINAL
from photos2yt.auth import (
    AuthError,
    authenticate,
    check_authorization,
    get_credentials,
    get_youtube_service,
)
from photos2yt.config import ConfigError, load_config
from photos2yt.takeout import TakeoutError, ingest


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
    ingest_parser.add_argument(
        "--report",
        dest="report_path",
        default=None,
        help="Path to write ingest CSV report (default: ingest_report.csv)",
    )

    # upload command (Task 6)
    upload_parser = subparsers.add_parser("upload", help="Upload pending videos to YouTube")
    upload_parser.add_argument("--limit", type=int, default=None, help="Maximum number of videos to upload")
    upload_parser.add_argument("--dry-run", action="store_true", help="Simulate uploads without calling YouTube API")
    upload_parser.add_argument("--wait", action="store_true", help="Wait until quota reset instead of exiting")

    # curate command (Task 7)
    curate_parser = subparsers.add_parser("curate", help="Organize uploaded videos into playlists based on rules")
    curate_parser.add_argument("--dry-run", action="store_true",
                               help="Print the plan; no API calls, no credentials, database opened read-only")
    curate_parser.add_argument("--rules", dest="rules_path", default=None,
                               help="Playlist rules YAML (default: paths.rules from config, "
                                    "else rules.yaml next to the config file)")

    # retitle command
    retitle_parser = subparsers.add_parser(
        "retitle",
        help="Rebuild titles of videos not yet uploaded from the current title_template",
    )
    retitle_parser.add_argument("--dry-run", action="store_true",
                                help="Show old -> new titles; database opened read-only")

    # reconcile command (Task 8)
    reconcile_parser = subparsers.add_parser(
        "reconcile",
        help="Check uploaded videos still exist on YouTube (videos.list) and refresh privacy",
    )
    reconcile_parser.add_argument("--dry-run", action="store_true",
                                  help="List what would be checked and the estimated units; "
                                       "no API calls, no credentials, database opened read-only")

    # cleanup command: local originals confirmed on YouTube
    cleanup_parser = subparsers.add_parser(
        "cleanup",
        help="Move (or delete) local originals that reconcile confirmed are on YouTube",
    )
    cleanup_parser.add_argument("--dry-run", action="store_true",
                                help="List what would be moved/deleted; changes nothing")
    cleanup_parser.add_argument("--dest", default=None,
                                help="Destination folder (default: paths.uploaded_dir, "
                                     "else <staging_dir>-uploaded)")
    cleanup_parser.add_argument("--delete", action="store_true",
                                help="Delete permanently instead of moving (needs --yes)")
    cleanup_parser.add_argument("--yes", action="store_true",
                                help="Confirm --delete")
    cleanup_parser.add_argument("--max-age-days", type=int, default=7,
                                help="Only files whose video reconcile confirmed within this many "
                                     "days (default 7)")

    # unpack: Takeout zips -> staging
    unpack_parser = subparsers.add_parser(
        "unpack",
        help="Extract videos and sidecars from Google Takeout zips in the inbox into staging_dir",
    )
    unpack_parser.add_argument("--dry-run", action="store_true",
                               help="List the zips and what would be extracted; writes nothing")
    unpack_parser.add_argument("--inbox", default=None,
                               help="Folder with takeout-*.zip files (default: paths.takeout_inbox, "
                                    "else gp-to-yt-inbox next to staging_dir)")

    # make-rules: build rules.yaml from a short input file
    mr_parser = subparsers.add_parser(
        "make-rules",
        help="Build rules.yaml (year, holiday, birthday, trip, keyword playlists) from rules-input.yaml",
    )
    mr_parser.add_argument("--input", default=None,
                           help="Input file (default: rules-input.yaml next to the config file)")
    mr_parser.add_argument("--output", default=None,
                           help="Rules file to write (default: paths.rules, else rules.yaml "
                                "next to the config file)")
    mr_parser.add_argument("--force", action="store_true", help="Replace an existing rules file")
    mr_parser.add_argument("--dry-run", action="store_true",
                           help="List the playlists that would be written; writes nothing")

    # report command (Task 8)
    report_parser = subparsers.add_parser(
        "report", help="Write manifest.csv and summary.txt from the database (no API calls)"
    )
    report_parser.add_argument("--out-dir", dest="out_dir", default=None,
                               help="Directory for manifest.csv and summary.txt "
                                    "(default: the config file's directory)")

    return parser


def run_upload_command(
    config,
    *,
    limit: Optional[int] = None,
    dry_run: bool = False,
    wait: bool = False,
    youtube=None,
    sleep=None,
) -> int:
    """Implements `photos2yt upload`. Returns the process exit code.

    --dry-run: read-only, no credentials, no API client, no DB writes.
    Otherwise credentials come from auth.get_credentials() (never a browser
    flow). Exit code 1 if any video failed, else 0 (a clean stop on quota or
    the channel upload limit is not a failure).
    """
    import time

    from photos2yt.db import get_connection, init_db
    from photos2yt.quota import QuotaTracker
    from photos2yt.uploader import dry_run as upload_dry_run
    from photos2yt.uploader import run_uploads

    if limit is not None and limit < 0:
        sys.stderr.write("--limit must be zero or positive\n")
        return 2

    if dry_run:
        upload_dry_run(config, limit=limit)
        return 0

    if youtube is None:
        creds = get_credentials(config=config)
        youtube = get_youtube_service(config=config, credentials=creds)

    conn = init_db(get_connection(config.paths.db_path))
    try:
        tracker = QuotaTracker(conn, config)
        summary = run_uploads(
            conn, config, tracker, youtube,
            limit=limit, wait=wait, sleep=sleep or time.sleep,
        )
    finally:
        conn.close()
    return 1 if summary.failed else 0


class _StderrHandler(logging.StreamHandler):
    """Writes to whatever sys.stderr is at emit time (works with redirection)."""

    def __init__(self) -> None:
        super().__init__()

    @property
    def stream(self):  # type: ignore[override]
        return sys.stderr

    @stream.setter
    def stream(self, _value) -> None:
        pass


def _not_echoed(record: logging.LogRecord) -> bool:
    return not getattr(record, ECHOED_TO_TERMINAL, False)


def configure_logging() -> None:
    """Show photos2yt WARNING+ log records on stderr, once.

    Records whose message was already printed to stdout (marked with
    ECHOED_TO_TERMINAL) are skipped so each warning appears once on the
    terminal. Idempotent: calling main() repeatedly adds no extra handler.
    """
    pkg_logger = logging.getLogger("photos2yt")
    if any(isinstance(h, _StderrHandler) for h in pkg_logger.handlers):
        return
    handler = _StderrHandler()
    handler.setLevel(logging.WARNING)
    handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    handler.addFilter(_not_echoed)
    pkg_logger.addHandler(handler)


def run_curate_command(
    config,
    *,
    dry_run: bool = False,
    rules_path: Optional[str] = None,
    youtube_factory=None,
    sleep=None,
) -> int:
    """Implements `photos2yt curate`. Returns the process exit code.

    --dry-run: no credentials, no API client, and the database is opened
    read-only (never created or migrated). Otherwise credentials are only
    loaded if there is an API call to make. Exit code 1 if any add or
    playlist creation failed (or the rules are invalid), else 0; a clean stop
    on quota is not a failure.
    """
    from photos2yt.curator import (
        RulesError,
        build_plan,
        format_plan,
        load_rules,
        resolve_rules_path,
        run_curation,
    )
    from photos2yt.db import get_connection, get_readonly_connection, init_db
    from photos2yt.quota import QuotaTracker

    path, explicit = resolve_rules_path(config, rules_path)
    if not path.exists():
        if explicit:
            sys.stderr.write(f"Rules file not found: {path}\n")
            return 1
        print(f"No rules file at {path}; nothing to curate "
              "(copy photos2yt/rules.yaml.example there, or set paths.rules).")
        return 0
    try:
        rules = load_rules(path)
    except RulesError as err:
        sys.stderr.write(f"Rules error:\n{err}\n")
        return 1

    if dry_run:
        try:
            conn = get_readonly_connection(config.paths.db_path)
        except FileNotFoundError:
            print(f"[DRY RUN] No database at {config.paths.db_path}; nothing to curate.")
            return 0
        try:
            plan = build_plan(conn, rules, config=config)
            print(format_plan(plan, config, QuotaTracker(conn, config)))
        finally:
            conn.close()
        return 0

    if youtube_factory is None:
        def youtube_factory():
            creds = get_credentials(config=config)
            return get_youtube_service(config=config, credentials=creds)

    conn = init_db(get_connection(config.paths.db_path))
    try:
        tracker = QuotaTracker(conn, config)
        summary = run_curation(conn, config, tracker, youtube_factory, rules, sleep=sleep)
    finally:
        conn.close()
    return 1 if summary.failed else 0


def run_retitle_command(config, *, dry_run: bool = False) -> int:
    """Implements `photos2yt retitle`. Database only, never calls the API."""
    from photos2yt.db import get_connection, get_readonly_connection, init_db
    from photos2yt.retitle import apply_retitle, build_retitle_plan, print_plan

    if dry_run:
        try:
            conn = get_readonly_connection(config.paths.db_path)
        except FileNotFoundError:
            print(f"[DRY RUN] No database at {config.paths.db_path}; nothing to retitle.")
            return 0
        try:
            print_plan(build_retitle_plan(conn, config), print, dry_run=True)
        finally:
            conn.close()
        return 0

    if not config.paths.db_path.exists():
        print(f"No database at {config.paths.db_path}; nothing to retitle.")
        return 0
    conn = init_db(get_connection(config.paths.db_path))
    try:
        plan = build_retitle_plan(conn, config)
        apply_retitle(conn, plan)
        print_plan(plan, print, dry_run=False)
    finally:
        conn.close()
    return 0


def run_reconcile_command(
    config,
    *,
    dry_run: bool = False,
    youtube_factory=None,
    sleep=None,
) -> int:
    """Implements `photos2yt reconcile`. Returns the process exit code.

    --dry-run: no credentials, no API client, database opened read-only (never
    created or migrated). Otherwise credentials are loaded only if there is a
    video to check. Exit code 1 if any videos.list call failed, else 0 (a
    clean quota stop and videos needing attention are not failures).
    """
    from photos2yt.db import get_connection, get_readonly_connection, init_db
    from photos2yt.quota import QuotaTracker
    from photos2yt.reconcile import format_dry_run, run_reconcile

    db_path = config.paths.db_path
    if dry_run:
        try:
            conn = get_readonly_connection(db_path)
        except FileNotFoundError:
            print(f"[DRY RUN] No database at {db_path}; nothing to reconcile.")
            return 0
        try:
            print(format_dry_run(conn, config, QuotaTracker(conn, config)))
        finally:
            conn.close()
        return 0

    if not db_path.exists():
        print(f"No database at {db_path}; nothing to reconcile.")
        return 0

    if youtube_factory is None:
        def youtube_factory():
            creds = get_credentials(config=config)
            return get_youtube_service(config=config, credentials=creds)

    conn = init_db(get_connection(db_path))
    try:
        tracker = QuotaTracker(conn, config)
        summary = run_reconcile(conn, config, tracker, youtube_factory, sleep=sleep)
    finally:
        conn.close()
    return 1 if summary.failed_batches else 0


def run_cleanup_command(
    config,
    *,
    dry_run: bool = False,
    dest: Optional[str] = None,
    delete: bool = False,
    yes: bool = False,
    max_age_days: int = 7,
    now=None,
) -> int:
    """Implements `photos2yt cleanup`. The database is only read (read-only
    connection, never created or migrated). Exit 1 if any file failed, 2 for
    bad options."""
    from photos2yt.cleanup import CleanupError, LOG_NAME, apply_plan, build_plan, format_plan
    from photos2yt.db import get_readonly_connection

    if delete and not yes and not dry_run:
        sys.stderr.write("--delete removes files permanently; add --yes to confirm "
                         "(or use --dry-run first).\n")
        return 2
    if max_age_days < 0:
        sys.stderr.write("--max-age-days must be zero or positive\n")
        return 2
    try:
        conn = get_readonly_connection(config.paths.db_path)
    except FileNotFoundError:
        print(f"No database at {config.paths.db_path}; nothing to clean up.")
        return 0
    try:
        plan = build_plan(conn, config, max_age_days=max_age_days, delete=delete,
                          dest=Path(dest) if dest else None, now=now)
    except CleanupError as err:
        sys.stderr.write(f"{err}\n")
        return 1
    finally:
        conn.close()
    print(format_plan(plan, dry_run=dry_run))
    if dry_run or not plan.actions:
        return 0
    log_path = Path(config.paths.db_path).resolve().parent / LOG_NAME
    summary = apply_plan(plan, log_path, now=now)
    return 1 if summary.failed else 0


def default_report_dir(config) -> Path:
    """Next to the config file, else the current directory."""
    if getattr(config, "config_path", None):
        return Path(config.config_path).resolve().parent
    return Path.cwd()


def run_report_command(config, *, out_dir: Optional[str | Path] = None) -> int:
    """Implements `photos2yt report`. Reads the database only (read-only
    connection, never created or migrated); writes manifest.csv and
    summary.txt into ``out_dir``."""
    from photos2yt.db import get_readonly_connection
    from photos2yt.report import format_summary, write_report

    target = Path(out_dir) if out_dir is not None else default_report_dir(config)
    try:
        conn = get_readonly_connection(config.paths.db_path)
    except FileNotFoundError:
        print(f"No database at {config.paths.db_path}; nothing to report.")
        return 0
    try:
        report, manifest, summary = write_report(conn, config, target)
    finally:
        conn.close()
    print(format_summary(report))
    print("")
    print(f"Manifest written to: {manifest} ({len(report.manifest_rows)} video(s))")
    print(f"Summary written to:  {summary}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    configure_logging()
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

        if args.subcommand == "ingest":
            report_p = Path(args.report_path).resolve() if args.report_path else None
            summary = ingest(config=config, dry_run=args.dry_run, report_path=report_p)

            mode_str = "[DRY RUN] " if args.dry_run else ""
            print(f"{mode_str}Ingest complete:")
            print(f"  Total videos scanned:    {summary.total_scanned}")
            print(f"  New pending for upload:  {summary.new_pending}")
            print(f"  Skipped (duplicates):    {summary.skipped}")
            print(f"  Already in database:     {summary.already_present}")
            print(f"  Long files (> 15 min):   {summary.long_file_count}")
            print(f"  Sidecar matcher tiers:   {dict(summary.matched_by_tier)}")
            print(f"  Date sources used:       {dict(summary.date_source_counts)}")
            if summary.report_path:
                print(f"  CSV report written to:   {summary.report_path}")
            return 0

        if args.subcommand == "upload":
            return run_upload_command(config, limit=args.limit, dry_run=args.dry_run, wait=args.wait)

        if args.subcommand == "retitle":
            return run_retitle_command(config, dry_run=args.dry_run)

        if args.subcommand == "curate":
            return run_curate_command(config, dry_run=args.dry_run, rules_path=args.rules_path)

        if args.subcommand == "reconcile":
            return run_reconcile_command(config, dry_run=args.dry_run)

        if args.subcommand == "cleanup":
            return run_cleanup_command(
                config, dry_run=args.dry_run, dest=args.dest, delete=args.delete,
                yes=args.yes, max_age_days=args.max_age_days,
            )

        if args.subcommand == "unpack":
            from photos2yt.unpack import run_unpack
            summary = run_unpack(config, inbox=Path(args.inbox) if args.inbox else None,
                                 dry_run=args.dry_run)
            return 1 if summary.zips_failed else 0

        if args.subcommand == "make-rules":
            from photos2yt.rulegen import default_paths, make_rules_file
            default_in, default_out = default_paths(config)
            return make_rules_file(
                Path(args.input) if args.input else default_in,
                Path(args.output) if args.output else default_out,
                force=args.force, dry_run=args.dry_run,
            )

        if args.subcommand == "report":
            return run_report_command(config, out_dir=args.out_dir)

        print(f"Command '{args.subcommand}' not yet implemented. Configuration loaded successfully.")
        return 0
    except ConfigError as err:
        sys.stderr.write(f"Configuration error:\n{err}\n")
        return 1
    except AuthError as err:
        sys.stderr.write(f"Authentication error:\n{err}\n")
        return 1
    except TakeoutError as err:
        sys.stderr.write(f"Takeout error:\n{err}\n")
        return 1
    except Exception as err:
        sys.stderr.write(f"Unexpected error: {err}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
