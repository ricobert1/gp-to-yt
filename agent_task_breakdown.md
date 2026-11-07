# Agent Task Breakdown: Google Photos → YouTube Migration & Curation

Revised from `technical_specification.md`. Written to work with any coding agent (Antigravity, Claude Code, etc.) and any model. Work the tasks in order. Each task has acceptance criteria; do not start the next until they pass. Commit after each task.

## 0. Ground Rules (copy into your agent's persistent rules/instructions file)

Put sections 0 and "Known Platform Facts" in whatever rules file or workspace instructions your agent reads at the start of every session, so they apply regardless of which model is running.

- **Done means:** the task's tests pass AND the human has reviewed the diff. Do not mark a task complete before both.

- Python 3.10+, package `photos2yt/`, CLI via `typer` or `argparse`, tests with `pytest`.
- Dependencies: `google-api-python-client`, `google-auth-oauthlib`, `google-auth-httplib2`, `tenacity`, `pyyaml`, `pytest`. External binary: `ffprobe` (from ffmpeg). Standard library `sqlite3`.
- All tunables live in `config.yaml` (see Task 1). No hardcoded quota numbers, time zones, paths, or title templates.
- Every command that mutates anything (uploads, playlist writes) supports `--dry-run`.
- Never delete or modify source files. Read-only on the staging directory.
- Secrets (`credentials.json`, `token.json`) stay out of git; `token.json` is chmod 600.
- Use `America/Los_Angeles` for quota reset logic (handles PST/PDT).

## Known Platform Facts the Code Must Respect

**Treat this section as authoritative.** The YouTube Data API has changed recently and model training data may be stale or inconsistent between models. If you believe a fact below is wrong or outdated, do NOT silently change the implementation: stop, state the disagreement and your evidence, and ask the human. Higher-risk areas for careful human review: timezone/local-date logic (Task 4) and idempotent state transitions (Tasks 2 and 6), since these fail quietly.

1. **Private lock:** uploads from an unverified API project created after 28 July 2020 are forced to `private` regardless of the requested privacy. The uploader must read back `status.privacyStatus` after upload and log a prominent warning if it differs from the request.
2. **Quota is in flux:** `videos.insert` was 1,600 units of a 10,000 pool; Google may have moved it to its own per-day bucket. Verify against Google's official quota table. Make `upload_cost_units`, `daily_unit_limit`, and `daily_upload_limit` config values. A `quotaExceeded` 403 is the authoritative stop signal. There is no API to read remaining quota, so the tool keeps its own counter.
3. **`recordingDate` location:** it is `recordingDetails.recordingDate`, not `snippet`. Request `part=snippet,status,recordingDetails`. `snippet.categoryId` is required.
4. **OAuth consent screen:** must be "In production" or refresh tokens expire after 7 days. Initial auth is done on a desktop with a browser; the resulting `token.json` is copied to the NAS.
5. **Limits:** title ≤ 100 chars; description ≤ 5,000 bytes, no `<` or `>`; tags ≤ 500 chars total; videos > 15 min require a verified channel.
6. **Never use `search.list` (100 units).** The local DB is the source of truth. Use `videos.list` (1 unit) only for reconciliation.
7. **Scope:** `https://www.googleapis.com/auth/youtube` alone covers uploads and playlists.

---

## Task 1: Project Scaffold & Config

**Build**
- Repo layout: `photos2yt/{config.py,db.py,auth.py,takeout.py,metadata.py,uploader.py,quota.py,curator.py,cli.py}`, `tests/`, `config.yaml.example`, and the agent rules file (Section 0 content).
- `config.yaml` schema:
  ```yaml
  paths: {staging_dir: ..., db_path: queue.db, credentials: credentials.json, token: token.json}
  timezone: America/New_York        # local-date rule for recordingDate / playlist rules
  youtube:
    privacy: unlisted               # requested; actual may be private (see fact 1)
    category_id: "22"
    made_for_kids: false
    title_template: "{date} - {title}"
    default_title: "Family Video"
  quota:
    daily_unit_limit: 10000
    upload_cost_units: 1600         # VERIFY against Google's current table
    playlist_insert_cost: 50
    playlist_item_insert_cost: 50
    daily_upload_limit: null        # set if Google uses a separate upload bucket
  video_extensions: [.mp4, .mov, .mkv, .m4v, .avi, .3gp]
  ```
- Config loader with validation and clear error messages.

**Accept:** `photos2yt --help` works; invalid config produces a readable error; unit tests for the loader.

## Task 2: SQLite Queue & State Machine

**Build** (`db.py`)
- Table `videos`: `id`, `source_path`, `sidecar_path`, `sha256` (unique), `size_bytes`, `duration_s`, `taken_utc`, `taken_local_date`, `title`, `description`, `tags_json`, `lat`, `lon`, `status`, `attempts`, `last_error`, `upload_session_uri`, `youtube_video_id`, `actual_privacy`, `created_at`, `updated_at`.
- Status values: `PENDING → UPLOADING → UPLOADED → PLAYLISTED`, plus `FAILED`, `SKIPPED` (duplicate / unmatched / too long).
- Table `quota_log`: `date_pacific`, `units_used`, `uploads_used`.
- Table `playlists`: `name`, `youtube_playlist_id`.
- Table `playlist_membership`: `video_id`, `playlist_name`, `added` flag, `position`.
- Transition helper that rejects illegal transitions.

**Accept:** unit tests cover every legal and illegal transition; schema migrates idempotently.

## Task 3: Auth

**Build** (`auth.py`, `photos2yt auth`)
- Installed-app OAuth flow, scope `youtube`. `--no-browser`/console fallback. Saves `token.json` with chmod 600. Auto-refreshes on use.
- `photos2yt auth --check` calls `channels.list?mine=true` (1 unit) and prints the channel title.

**Accept:** manual: auth completes, check prints the channel. Unit tests with mocked credentials.

## Task 4: Takeout Ingest

**Build** (`takeout.py`, `metadata.py`, `photos2yt ingest`)
- Walk `staging_dir` recursively for video files by extension.
- **Sidecar matcher**, tiered, stop at first hit:
  1. `<file>.supplemental-metadata.json`
  2. `<file>.json`
  3. Truncated-name variants (sidecar base name cut to ~46 chars)
  4. Duplicate-suffix handling: `name(1).mp4` ↔ `name.mp4(1).json`
  5. `-edited` files inherit the original's sidecar
  6. Fuzzy fallback within the same directory (log as low confidence)
- Parse sidecar: `photoTakenTime.timestamp` (epoch string), `geoData`, `description`, `title`.
- Fallback with no sidecar: `ffprobe` creation_time → file mtime. Record which source was used.
- Compute `taken_local_date` using `config.timezone` (UTC epoch converted to local), and also store `taken_utc`. Priority for timezone: explicit config default.
- SHA-256 each file; on duplicate hash, mark `SKIPPED` with reason.
- Duration via `ffprobe`; flag > 15 min.
- Build title via template (sanitize, truncate to 100), description (original description + `#family #<year> #<month>`, strip `<>`, ≤ 5,000 bytes), tags (≤ 500 chars total).
- Write `ingest_report.csv`: matched, fallback-used, unmatched, duplicates, long files.

**Accept:** fixture tree in `tests/fixtures/` covering each matcher tier; Dec 24 8 PM Eastern (= Dec 25 UTC) yields `taken_local_date = Dec 24`; re-running ingest is idempotent.

## Task 5: Quota Tracker

**Build** (`quota.py`)
- Compute the current Pacific-time "quota day". Read/write `quota_log`.
- `can_afford(units)` checks `daily_unit_limit` and optional `daily_upload_limit`.
- `record(units)` after each successful call; `mark_exhausted()` when a `quotaExceeded` 403 occurs (blocks until next Pacific midnight).
- `seconds_until_reset()`.

**Accept:** tests with a frozen clock across PST/PDT transitions.

## Task 6: Uploader

**Build** (`uploader.py`, `photos2yt upload [--limit N] [--dry-run] [--wait]`)
- Loop over `PENDING` ordered by `taken_utc`. Before each upload, `can_afford`; if not, stop (or `--wait` to sleep until reset).
- Build body with `snippet` (title, description, tags, categoryId), `status` (privacyStatus, selfDeclaredMadeForKids), `recordingDetails` (`recordingDate` as `YYYY-MM-DDTHH:MM:SSZ`, optional `location` from geoData if present and non-zero).
- `MediaFileUpload(chunksize=..., resumable=True)`. Persist the resumable session URI to the DB so a crash can resume. Set status `UPLOADING` before start.
- Retries via `tenacity`: exponential backoff on 500/502/503/504 and network errors.
- Error handling: `quotaExceeded` → `quota.mark_exhausted()`, leave `PENDING`, stop run. `uploadLimitExceeded` → stop run (channel-level cap, separate from API quota). Other 4xx → `FAILED` with `last_error`.
- On success: store `youtube_video_id`, set `UPLOADED`, record quota.
- Post-upload verification: `videos.list(part=status,processingDetails,recordingDetails)`; store `actual_privacy`; **warn loudly if actual ≠ requested** (private-lock). Warn if `recordingDate` didn't stick.

**Accept:** unit tests with a mocked API client for success, resume, quota error, upload-limit error, private-lock detection. Manual: upload one real short file.

## Task 7: Curator

**Build** (`curator.py`, `photos2yt curate [--dry-run]`)
- Declarative rules in `rules.yaml`:
  ```yaml
  playlists:
    - name: "Christmas Day Through the Years"
      privacy: unlisted
      match:
        any:
          - {local_month: 12, local_day: 25}
          - {tags_contain_any: ["Christmas Day"]}   # keep keyword rules narrow
  ```
- Evaluate against local DB rows (status `UPLOADED`) using `taken_local_date`, not UTC. No `search.list`.
- Ensure playlist exists: look up in `playlists` table; if absent, `playlists.insert` and store the id.
- Add members in chronological order. Compute the target `position` for each insert so the playlist ends up date-sorted; for a full backfill, sort the matching set by date and insert in that order. Re-ordering later uses `playlistItems.update` (50 units) and is a separate explicit command.
- Idempotent: skip videos already in `playlist_membership`. Retry failed adds independently; set video `PLAYLISTED` when all matching playlists are done.
- Quota-aware (uses `quota.py`).
- `--dry-run` prints which videos would join which playlists and in what order.

**Accept:** tests for rule matching including the Dec 24/25 timezone edge; ordering test; idempotent re-run makes zero API calls.

## Task 8: Reconcile & Reporting

**Build** (`photos2yt reconcile`, `photos2yt report`)
- `reconcile`: batch `videos.list` (up to 50 ids per call, 1 unit each) to confirm every `UPLOADED` id still exists, and refresh `actual_privacy`.
- `report`: writes `manifest.csv` (`original filename → video_id → URL → privacy → playlists`) and a summary (counts by status, quota used today, items needing attention).

**Accept:** manifest generated from a seeded test DB.

## Task 9: Packaging & Scheduling

**Build**
- `Dockerfile` (python slim + ffmpeg), `docker-compose.yml` mounting staging dir, config, token, and DB volume.
- Cron/compose example: nightly `ingest && upload --limit N && curate`, exit non-zero on failures.
- `README.md`: Google Cloud setup (enable YouTube Data API v3, consent screen **In production**, desktop credentials), first-run auth on a desktop then copy `token.json`, audit/private-lock explanation, how to flip privacy manually in Studio, and the manual offload checklist.

**Accept:** container runs `photos2yt --help`; documented end-to-end run on a 3-video sample.

---

## Manual Steps the Code Cannot Do

- Create the Cloud project, enable the API, create OAuth credentials, set the consent screen to production.
- Submit the YouTube API compliance audit if unlisted/public uploads are wanted (otherwise accept `private` and flip manually).
- Delete videos from Google Photos after verifying the manifest. There is no API for this, and YouTube re-encodes, so keep the originals.
