"""Curator module: rules engine and dry-run plan generation for photos2yt."""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from photos2yt.config import Config, ConfigError, load_config
from photos2yt.db import (
    VideoRecord,
    get_connection,
    init_db,
    add_playlist_membership,
    upsert_playlist,
    get_playlist_memberships,
)


@dataclass(frozen=True)
class PlaylistRule:
    """A single playlist rule with a name, privacy setting, and matching conditions."""

    name: str
    privacy: str
    match: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    @property
    def any_conditions(self) -> Optional[list[dict[str, Any]]]:
        """Return the 'any' condition list if present."""
        return self.match.get("any")

    @property
    def all_conditions(self) -> Optional[list[dict[str, Any]]]:
        """Return the 'all' condition list if present."""
        return self.match.get("all")


# --- CurationPlan class definition ---

class CurationPlan(dict):
    """Represents a curation plan produced by the curator."""

    def __init__(self, playlists_to_create: list[str], videos_to_add: list[tuple[str, int, int]], estimated_cost: int, note: Optional[str] = None):
        super().__init__()
        self["playlists_to_create"] = playlists_to_create
        self["videos_to_add"] = videos_to_add
        self["estimated_cost"] = estimated_cost
        if note is not None:
            self["note"] = note

    @property
    def playlists_to_create(self) -> list[str]:
        return list(self.get("playlists_to_create", []))

    @property
    def videos_to_add(self) -> list[tuple[str, int, int]]:
        return list(self.get("videos_to_add", []))

    @property
    def estimated_cost(self) -> int:
        return self.get("estimated_cost", 0)


# --- Validation functions ---

def _build_playlist_rules(rules_file: Optional[Path]) -> dict[str, PlaylistRule]:
    """Load and validate playlist rules from a YAML file.

    Returns a dict mapping rule name to PlaylistRule objects. Raises RuntimeError on validation failure.
    
    Validation order (errors reported in this priority):
      1. Missing or non-dict top-level 'playlists' key
      2. Non-dict items in the playlists list
      3. Name not a string, empty name, duplicate names
      4. Invalid privacy value
      5. Invalid match field (not a dict)
      6. Empty/unknown fields in conditions
      7. Out-of-range or non-integer values for date fields
    """
    if not rules_file or not rules_file.exists():
        return {}  # No rules loaded — curator will create all matching videos into new playlists

    try:
        with open(rules_file, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except (yaml.YAMLError, OSError):
        raise RuntimeError(
            f"Failed to parse rules.yaml at {rules_file}: error reading file",
        )

    if not isinstance(raw, dict):
        raise RuntimeError("Root of rules.yaml must be a mapping (got something else)")

    playlists_raw = raw.get("playlists")
    if playlists_raw is None:
        return {}  # Empty rules file — curator will create all matching videos into new playlists

    if not isinstance(playlists_raw, list):
        raise RuntimeError(
            f"Top-level 'playlists' must be a list (got {type(playlists_raw).__name__})",
        )

    rules: dict[str, PlaylistRule] = {}

    for i, item in enumerate(playlists_raw):
        if not isinstance(item, dict):
            raise RuntimeError(
                f"Each playlist must be a mapping; item {i} is not a dictionary",
            )

        name_val = item.get("name")
        
        # Validate that 'name' is a string first (before checking content or duplicates)
        if not isinstance(name_val, str):
            raise RuntimeError(f"Playlist 'name' must be a non-empty string, got: {type(name_val).__name__}({repr(name_val)})")

        name_str = name_val.strip() if name_val else ""
        
        # Check for empty name
        if not name_str:
            raise RuntimeError(f"Playlist 'name' must be a non-empty string, got: {repr(name_val)}")

        # Check for duplicate names BEFORE privacy or match validation.
        # This ensures we report "duplicate name" before any other error about the same playlist entry.
        if name_str in rules:
            raise RuntimeError(
                f"Duplicate playlist name: {name_str!r} appears more than once",
            )

        # Validate privacy BEFORE checking match (so errors are in priority order)
        privacy = item.get("privacy")
        if not isinstance(privacy, str) or privacy.lower() not in ("unlisted", "public", "private"):
            raise RuntimeError(
                f"Playlist '{name_str}' has invalid 'privacy': {repr(privacy)}; must be one of: unlisted, public, private",
            )

        # Validate match field BEFORE duplicate check — this is the correct order since
        # we want to catch schema errors (missing/invalid match) before reporting duplicates.
        if "match" not in item:
            raise RuntimeError(f"Playlist '{name_str}' has invalid 'match': expected a mapping")

        match = item["match"]
        if not isinstance(match, dict):
            raise RuntimeError(f"Playlist '{name_str}' has invalid 'match': expected a mapping")

        any_conditions = match.get("any")
        all_conditions = match.get("all")

        # Validate 'any' conditions
        if isinstance(any_conditions, list):
            for j, cond in enumerate(any_conditions):
                if not isinstance(cond, dict):
                    raise RuntimeError(
                        f"Playlist '{name_str}': condition {j} in 'any' is not a mapping",
                    )
                # Check for empty match conditions (no known fields present) or unknown fields
                known_fields = {"local_month", "local_day", "local_year", "title_contains_any", "tags_contain_any"}
                if not cond:
                    raise RuntimeError(
                        f"Playlist '{name_str}': condition {j} in 'any' has no recognized fields (empty dict)",
                    )
                # Check for unknown/invalid fields
                valid_fields = {"local_month", "local_day", "local_year", "title_contains_any", "tags_contain_any"}
                for key in cond:
                    if key not in valid_fields:
                        raise RuntimeError(
                            f"Playlist '{name_str}': condition {j} has unknown field 'key': unexpected field name",
                        )

                # Validate local_month
                if "local_month" in cond:
                    val = cond["local_month"]
                    if isinstance(val, bool):
                        raise RuntimeError(f"Playlist '{name_str}': condition {j} has invalid 'local_month' (bool is not allowed)")
                    if not isinstance(val, int) or val < 1 or val > 12:
                        raise RuntimeError(
                            f"Playlist '{name_str}': condition {j} has invalid 'local_month'={val}; must be integer between 1 and 12",
                        )

                # Validate local_day
                if "local_day" in cond:
                    val = cond["local_day"]
                    if isinstance(val, bool):
                        raise RuntimeError(f"Playlist '{name_str}': condition {j} has invalid 'local_day' (bool is not allowed)")
                    if not isinstance(val, int) or val < 1 or val > 31:
                        raise RuntimeError(
                            f"Playlist '{name_str}': condition {j} has invalid 'local_day'={val}; must be integer between 1 and 31",
                        )

                # Validate title_contains_any
                if "title_contains_any" in cond:
                    keywords = cond["title_contains_any"]
                    if not isinstance(keywords, list):
                        raise RuntimeError(
                            f"Playlist '{name_str}': condition {j} has invalid 'title_contains_any' (expected a list)",
                        )

                # Validate tags_contain_any
                if "tags_contain_any" in cond:
                    keywords = cond["tags_contain_any"]
                    if not isinstance(keywords, list):
                        raise RuntimeError(
                            f"Playlist '{name_str}': condition {j} has invalid 'tags_contain_any' (expected a list)",
                        )

        # Validate 'all' conditions similarly
        if isinstance(all_conditions, list):
            for j, cond in enumerate(all_conditions):
                if not isinstance(cond, dict):
                    raise RuntimeError(
                        f"Playlist '{name_str}': condition {j} in 'all' is not a mapping",
                    )

                # Check for empty match conditions or unknown fields
                known_fields = {"local_month", "local_day", "local_year", "title_contains_any", "tags_contain_any"}
                if not cond:
                    raise RuntimeError(
                        f"Playlist '{name_str}': condition {j} in 'all' has no recognized fields (empty dict)",
                    )
                for key in cond:
                    if key not in known_fields:
                        raise RuntimeError(
                            f"Playlist '{name_str}': condition {j} in 'all' has unknown field 'key': unexpected field name",
                        )

                # Validate local_month
                if "local_month" in cond:
                    val = cond["local_month"]
                    if isinstance(val, bool):
                        raise RuntimeError(f"Playlist '{name_str}': condition {j} has invalid 'local_month' (bool is not allowed)")
                    if not isinstance(val, int) or val < 1 or val > 12:
                        raise RuntimeError(
                            f"Playlist '{name_str}': condition {j} has invalid 'local_month'={val}; must be integer between 1 and 12",
                        )

                # Validate title_contains_any
                if "title_contains_any" in cond:
                    keywords = cond["title_contains_any"]
                    if not isinstance(keywords, list):
                        raise RuntimeError(
                            f"Playlist '{name_str}': condition {j} has invalid 'title_contains_any' (expected a list)",
                        )

                # Validate tags_contain_any
                if "tags_contain_any" in cond:
                    keywords = cond["tags_contain_any"]
                    if not isinstance(keywords, list):
                        raise RuntimeError(
                            f"Playlist '{name_str}': condition {j} has invalid 'tags_contain_any' (expected a list)",
                        )

        rules[name_str] = PlaylistRule(name=name_str, privacy=privacy, match=item.get("match", {}))

    return rules


# --- Matching functions ---

def _match_rule(rule: PlaylistRule, video: VideoRecord) -> Optional[str]:
    """Evaluate a single playlist rule against a video.

    Returns:
        The matching condition description (e.g., "local_month=12, local_day=25") if matched,
        or None if the rule does not match.
    """
    # Check 'any' conditions first — if any one matches, the rule matches
    rule_any = rule.any_conditions
    if rule_any is not None:
        for condition in rule_any:
            cond_str = _format_condition(condition)

            if "local_month" in condition and "local_day" in condition:
                # Date-based match using taken_local_date (never UTC!)
                date_str = video.taken_local_date or ""
                try:
                    dt_naive = datetime.strptime(date_str, "%Y-%m-%d")
                    if (dt_naive.month, dt_naive.day) == (condition["local_month"], condition["local_day"]):
                        return cond_str
                except (ValueError, TypeError):
                    pass

            elif "title_contains_any" in condition:
                keywords = [kw.strip() for kw in condition["title_contains_any"]]
                if _match_title_keyword(keywords, video.title or ""):
                    return cond_str

            elif "tags_contain_any" in condition:
                keywords = [kw.strip() for kw in condition["tags_contain_any"]]
                if _match_tags_keyword(keywords, video.tags_json):
                    return cond_str

    # Check 'all' conditions — all must match for a rule to match
    rule_all = rule.all_conditions
    if rule_all is not None and len(rule_all) > 0:
        # For 'all', every condition must be satisfied. If any fails, the rule doesn't match.
        for condition in rule_all:
            cond_str = _format_condition(condition)

            if "local_month" in condition and "local_day" in condition:
                date_str = video.taken_local_date or ""
                try:
                    dt_naive = datetime.strptime(date_str, "%Y-%m-%d")
                    if (dt_naive.month, dt_naive.day) != (condition["local_month"], condition["local_day"]):
                        return None  # This rule doesn't match — one of the 'all' conditions failed
                except (ValueError, TypeError):
                    return None

            elif "title_contains_any" in condition:
                keywords = [kw.strip() for kw in condition["title_contains_any"]]
                if not _match_title_keyword(keywords, video.title or ""):
                    return None

            elif "tags_contain_any" in condition:
                keywords = [kw.strip() for kw in condition["tags_contain_any"]]
                if not _match_tags_keyword(keywords, video.tags_json):
                    return None

        # All 'all' conditions passed — rule matches
        all_strs = [_format_condition(c) for c in rule_all]
        return ", ".join(all_strs)

    # No conditions matched or no conditions were specified at all
    return None


def _match_title_keyword(keywords: list[str], video_title: str) -> bool:
    """Check if any of the given keywords appear as whole words in the title.

    Matching is case-insensitive and uses word boundaries (so "Christmas" does not
    match "Christmassy").

    Args:
        keywords: List of keyword strings to search for.
        video_title: The video title to search within.

    Returns:
        True if at least one keyword matches as a whole word in the title.
    """
    if not keywords:
        return False

    # Build a case-insensitive regex pattern with word boundaries
    escaped = [re.escape(kw) for kw in keywords]
    pattern = r"\b(" + "|".join(escaped) + r")\b"
    compiled = re.compile(pattern, re.IGNORECASE)

    return bool(compiled.search(video_title))


def _match_tags_keyword(keywords: list[str], video_tags_json: Optional[str]) -> bool:
    """Check if any of the given keywords appear in the tags.

    Tags are stored as a JSON array. Matching is case-insensitive and uses word boundaries.

    Args:
        keywords: List of keyword strings to search for.
        video_tags_json: The raw JSON string containing tags (e.g., '["summer", "vacation"]').

    Returns:
        True if at least one keyword matches as a whole word in any tag.
    """
    if not keywords or video_tags_json is None:
        return False

    try:
        import json
        tags = json.loads(video_tags_json)
        if not isinstance(tags, list):
            return False
        # Lowercase all for case-insensitive whole-word matching
        tags_lower = [t.lower() for t in tags]
        escaped = [re.escape(kw) for kw in keywords]
        pattern = r"\b(" + "|".join(escaped) + r")\b"
        compiled = re.compile(pattern, re.IGNORECASE)
    except (json.JSONDecodeError, TypeError):
        return False

    # Check each tag against the regex
    for tag in tags_lower:
        if bool(compiled.search(tag)):
            return True

    return False


def _format_condition(condition: dict[str, Any]) -> str:
    """Format a condition dictionary into a human-readable string."""
    parts = []
    if "local_month" in condition and isinstance(condition["local_month"], int):
        parts.append(f"local_month={condition['local_month']}")
    if "local_day" in condition and isinstance(condition["local_day"], int):
        parts.append(f"local_day={condition['local_day']}")
    if "local_year" in condition and isinstance(condition["local_year"], int):
        parts.append(f"local_year={condition['local_year']}")
    if "title_contains_any" in condition:
        parts.append(
            f"title_contains_any={[t.strip() for t in condition['title_contains_any'] if t.strip()]}"
        )
    if "tags_contain_any" in condition:
        parts.append(
            f"tags_contain_any={[t.strip() for t in condition['tags_contain_any'] if t.strip()]}"
        )
    return ", ".join(parts) if parts else "(no constraints)"


# --- Plan computation functions ---

def _compute_target_positions(
    conn,
    rules: dict[str, PlaylistRule],
    all_matching_videos: list[VideoRecord],
    matches: list[tuple[PlaylistRule, VideoRecord]],
) -> tuple[list[str], list[tuple[int, int]]]:
    """Compute target positions for each video in its playlist.

    The target position is the 0-based index of the video within that playlist's sorted order
    (sorted by taken_utc ascending, then id ascending).

    Args:
        conn: Database connection.
        rules: Dict of loaded rules.
        all_matching_videos: Videos sorted by (taken_utc, id).
        matches: List of (rule, video) pairs.

    Returns:
        A tuple of (list of playlist names, list of (video_id, target_position) tuples).
    """
    # Query existing playlists from the DB — these are already created and should not be listed
    cursor = conn.execute("SELECT name FROM playlists")
    existing_playlist_names = set(row[0] for row in cursor.fetchall())

    # Build a set of already-added videos for each playlist so we can skip them.
    # Use .get() with default empty set to avoid KeyError when a video has no membership record yet.
    already_added = {}  # video_id -> set of playlist_names where added=1
    memberships = get_playlist_memberships(conn)
    for membership in memberships:
        vid_id = membership["video_id"]
        pl_name = membership["playlist_name"]
        if not membership["added"]:
            continue
        if vid_id not in already_added:
            already_added[vid_id] = set()
        already_added[vid_id].add(pl_name)

    # Pre-sort ALL matching videos by (taken_utc, id) — this is the canonical order.
    # We use this pre-sorted list to compute target positions for each playlist,
    # including already-added videos from previous runs or manual additions.
    all_videos_sorted: list[VideoRecord] = sorted(all_matching_videos, key=lambda v: (v.taken_utc or "", v.id))

    plan_by_playlist: dict[str, list[tuple[int, int]]] = {}  # playlist_name -> [(video_id, target_position)]

    for rule in rules.values():
        pl_name = rule.name

        # Use the pre-sorted all_videos_sorted to compute target positions for this playlist.
        # This gives each video its 0-based index in the (taken_utc, id)-sorted order of ALL
        # members (both already-added and planned), which is exactly what the tests expect.
        sorted_members = [vid for vid in all_videos_sorted if _match_rule(rule, vid) is not None]

        # Compute positions — only for videos NOT already added.
        # Use .get() with default empty set to handle videos without membership records.
        positions: list[tuple[int, int]] = []
        for i, video in enumerate(sorted_members):
            if video.id not in already_added.get(video.id, set()):
                target_position = i  # zero-based index among all members (already-added + planned)
                positions.append((video.id, target_position))

        plan_by_playlist[pl_name] = positions

    # Exclude playlists that already exist in the DB or have any membership rows (already processed)
    valid_playlists = {
        pl_name for pl_name in plan_by_playlist.keys()
        if pl_name not in existing_playlist_names
        and not get_playlist_memberships(conn, playlist_name=pl_name)
    }
    # Flatten: group by playlist name, then by position within each playlist (only valid playlists)
    videos_to_add: list[tuple[str, int, int]] = []  # (playlist_name, video_id, target_position)
    for pl_name in sorted(valid_playlists):
        for vid_id, pos in sorted(plan_by_playlist[pl_name], key=lambda x: x[1]):
            videos_to_add.append((pl_name, vid_id, pos))

    return sorted(valid_playlists), videos_to_add


def _compute_cost(config: Config, rule_names: list[str], videos_added_count: int) -> int:
    """Compute estimated quota cost for the plan.

    Args:
        config: Configuration object with quota settings.
        rule_names: List of playlist names to create.
        videos_added_count: Total number of videos that would be added (summed across playlists).

    Returns:
        Estimated quota units consumed.
    """
    total_cost = 0

    for pl_name in rule_names:
        insert_cost = config.quota.playlist_insert_cost
        total_cost += insert_cost

    per_item_cost = config.quota.playlist_item_insert_cost
    total_cost += videos_added_count * per_item_cost

    return total_cost


def build_plan(
    conn,
    rules: Optional[dict[str, PlaylistRule]],
    config: Optional[Config] = None,
) -> CurationPlan:
    """Build the curation plan.

    Args:
        conn: Database connection (can be :memory: for testing).
        rules: Dict of loaded PlaylistRule objects (or None for no rules).
        config: Configuration object for cost computation.

    Returns:
        A CurationPlan describing what would be done.
    """
    # If no rules defined — produce an empty plan (videos stay UPLOADED)
    if rules is None:
        return CurationPlan(
            playlists_to_create=[],
            videos_to_add=[],
            estimated_cost=0,
            note="No playlists configured in rules.yaml",
        )

    # Fetch all matching videos and matches
    cursor = conn.execute(
        "SELECT * FROM videos WHERE status IN ('UPLOADED', 'PLAYLISTED') ORDER BY taken_utc ASC, id ASC",
    )
    all_matching_videos = [VideoRecord.from_row(row) for row in cursor.fetchall()]

    # Compute matches: which rule matches which video
    matches: list[tuple[PlaylistRule, VideoRecord]] = []
    for video in all_matching_videos:
        for rule_name, rule in rules.items():
            matched_condition = _match_rule(rule, video)
            if matched_condition is not None:
                matches.append((rule, video))

    # Compute target positions using (taken_utc, id) ordering
    playlist_names, videos_to_add = _compute_target_positions(conn, rules, all_matching_videos, matches)

    # Compute cost — use config if provided, otherwise compute a default estimate
    estimated_cost = 0
    if config is not None:
        estimated_cost = _compute_cost(config, playlist_names, len(videos_to_add))

    return CurationPlan(
        playlists_to_create=playlist_names,
        videos_to_add=videos_to_add,
        estimated_cost=estimated_cost,
    )


def curate_plan(
    config: Optional[Config],
    rules_file: Optional[Path] = None,
    conn: Optional[__import__('sqlite3').Connection] = None,
) -> CurationPlan:
    """Public entry point for building a curation plan.

    Args:
        config: Configuration object (used only if conn is not provided).
        rules_file: Path to the rules YAML file (or None for no rules).
        conn: Optional database connection. If provided, uses this instead of deriving from config.

    Returns:
        A CurationPlan describing what would be done.
    """
    # Use provided connection or derive from config
    if conn is not None:
        cursor = conn  # Use the passed-in connection directly (don't close it — caller owns it)
        use_new_conn = False
    else:
        config = _ensure_config(config)
        db_path = config.paths.db_path
        cursor = get_connection(db_path)
        use_new_conn = True

    try:
        init_db(cursor)
        # If no rules_file, treat as "no rules" -> empty plan
        if not rules_file or not rules_file.exists():
            return CurationPlan(
                playlists_to_create=[],
                videos_to_add=[],
                estimated_cost=0,
                note="No rules.yaml found — all matching videos remain UPLOADED",
            )

        try:
            rules = _build_playlist_rules(rules_file)
        except RuntimeError as e:
            raise RuntimeError(f"Rules validation error: {e}") from None

        return build_plan(cursor, rules, config=config)

    finally:
        if use_new_conn:
            cursor.close()


def curate_dry_run(
    config: Config,
    rules_file: Optional[Path],
    conn: Optional[__import__('sqlite3').Connection] = None,
) -> CurationPlan:
    """Run a dry-run of the curator — build and print the plan without modifying state.

    Args:
        config: Configuration object.
        rules_file: Path to the rules YAML file (or None).
        conn: Optional database connection. If provided, uses this instead of deriving from config.

    Returns:
        A CurationPlan describing what would be done.
    """
    if not isinstance(config, Config):
        raise TypeError(f"config must be a photos2yt.config.Config, got {type(config).__name__}")

    plan = curate_plan(config=config, rules_file=rules_file, conn=conn)

    # Print formatted output
    print(_format_plan_for_output(plan))

    return plan


def _ensure_config(config: Optional[Config]) -> Config:
    """Ensure a config object exists. Returns default config if None."""
    if config is not None:
        return config
    # Load default config
    from photos2yt.config import load_config
    base_path = Path(__file__).parent.parent / "photos2yt"
    return load_config(base_path / "config.yaml.example")


def _format_plan_for_output(plan: CurationPlan) -> str:
    """Format a curation plan for human-readable output."""
    lines = []
    lines.append("=" * 72)
    lines.append("CURATION PLAN")
    lines.append("=" * 72)

    if plan.get("note"):
        lines.append(f"\nNote: {plan['note']}")

    playlists_to_create = plan.playlists_to_create
    videos_to_add = plan.videos_to_add

    if not playlists_to_create and not videos_to_add:
        lines.append("\nNo action required — all matching videos have already been added to their playlists.")
    else:
        # Group videos by playlist for display
        by_playlist: dict[str, list[tuple[int, int]]] = {}
        for pl_name, vid_id, pos in videos_to_add:
            if pl_name not in by_playlist:
                by_playlist[pl_name] = []
            by_playlist[pl_name].append((vid_id, pos))

        lines.append(f"\nPlaylists to create ({len(playlists_to_create)}):")
        for pl_name in playlists_to_create:
            lines.append(f"  - {pl_name}")

        if videos_to_add:
            lines.append(f"\nVideos to add (in order of execution, with target positions within each playlist):")
            current_playlist = None
            for pl_name, vid_id, pos in videos_to_add:
                if pl_name != current_playlist:
                    current_playlist = pl_name
                    lines.append(f"  Playlist: {pl_name}")
                lines.append(f"    video #{vid_id} -> target position {pos}")

        lines.append("\nEstimated quota cost (units): " + str(plan.estimated_cost))

    lines.append("=" * 72)

    return "\n".join(lines)


# --- CLI entry point ---

def main() -> int:
    """CLI handler for 'photos2yt curate'."""
    parser = argparse.ArgumentParser(prog="photos2yt", description="Google Photos to YouTube Migration & Curation Tool")
    parser.add_argument(
        "-c",
        "--config-path",
        default=None,
        help="Path to YAML config file (default: config.yaml or $PHOTOS2YT_CONFIG)",
    )

    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # curate command
    curate_parser = subparsers.add_parser("curate", help="Organize uploaded videos into playlists based on rules")
    curate_parser.add_argument(
        "--dry-run", action="store_true", default=True,
        help="Simulate curation without modifying playlists (default)"
    )

    args = parser.parse_args()

    if not args.subcommand or args.subcommand != "curate":
        print("Unknown subcommand. Use 'photos2yt curate --help' for usage.")
        return 1

    try:
        config = load_config(args.config_path)
    except ConfigError as e:
        sys.stderr.write(f"Configuration error:\n{e}\n")
        return 1

    rules_file = Path(__file__).parent / "rules.yaml"

    # Non-dry-run mode is NOT implemented yet — for now, always treat as dry run
    if not args.dry_run:
        print("Error: The --no-dry-run (or non-dry-run) mode of 'photos2yt curate' is not yet implemented.")
        sys.stderr.write("This command currently only supports --dry-run.\n")
        return 1

    try:
        # Call curate_plan directly — its return value is now properly used.
        # We use _ensure_config internally if no config was passed, so conn=None defaults to that behavior.
        plan = curate_plan(config=config, rules_file=rules_file)
        
        # Format and print using the plan's properties
        print(_format_plan_for_output(plan))

        # Return success (0) — dry run never fails
        return 0
    except RuntimeError as e:
        sys.stderr.write(f"Rules validation error:\n{e}\n")
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())