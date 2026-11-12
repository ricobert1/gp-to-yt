# Status

## Completed
- Task 1: scaffold and config (0af48e3)
- Task 2: SQLite queue and state machine (923c339)
- Task 3: OAuth auth module (c39af5b)
- Task 4: Takeout ingest (3e871e2), duplicate handling fix (6d80e88)
- Checkpoint after real-sample verification (6bdf3b1)
- Task 5: quota tracker (see next commit; migration 3 adds quota_log.exhausted_at)
- Task 7A: curator (b583053, from task6-rewrite)
- Task 6 part 1: uploader core, rewritten from scratch (upload_one: request body, resumable
  MediaFileUpload + next_chunk loop, session URI persisted via db.update_upload_session,
  tenacity per-chunk retries, error classification by JSON reason). Awaiting human review.
- Task 6 part 2: post-upload verification, crash recovery, `photos2yt upload` run loop and
  read-only dry run. Awaiting human review. Not yet tested against the real API: the
  task's manual acceptance step ("upload one real short file") is still to do.
  Update 2026-11-11: done by the human, see "Real-API checks" below.
- Task 8: `photos2yt reconcile` and `photos2yt report` (6b0f348, PR #6). Checked by the
  human against the real channel (dry run, reconcile, report).
- Task 9: Dockerfile, docker-compose.yml, nightly script, README (this branch). Awaiting
  human review and a build on the human's machine (see "Packaging (Task 9)").
- Task 7B: curator real run (`photos2yt curate`), plus curate --dry-run read-only fix and
  rules path from config/CLI. Awaiting human review of the diff. Checked against the real
  API (see below).

## Real-API checks (2026-11-11, by the human)
- `upload --limit 1`: one short video uploaded, exit 0. Requested `unlisted`; no
  PRIVATE-UPLOAD LOCK banner and no recordingDate warning were printed, so this project
  does not appear to be forced to private.
- `curate --dry-run` with a one-rule rules.yaml (private test playlist, single local
  date): plan was correct (1 playlist, 1 add at position 0, 100 units); queue.db hash
  unchanged; no new files.
- `curate`: playlist created and stored, video added at position 0, membership added=1,
  row PLAYLISTED, quota counter +100. Playlist and video visible in Studio.
- `curate` re-run: no API calls, quota counter unchanged.
- Ordering test, first run on a new playlist: the first add got HTTP 409
  SERVICE_UNAVAILABLE "The operation was aborted." (not retried at the time, so counted as
  a failure); the second add went in at position 0. Now retried (see curator section).

## Next
- Manual (optional): curate a rule matching 2-3 videos and check the order in Studio.
- Manual (Task 9, Docker, optional): `docker compose build` and
  `docker compose run --rm photos2yt --help` if Docker is used later. Still never built
  with the apt ffmpeg step anywhere.

## Packaging (Task 9)
- Dockerfile: python:3.12-slim + apt ffmpeg (ffprobe) + tzdata; uv 0.11.32 copied from the
  official `astral/uv` image (Docker Hub; same image as ghcr.io/astral-sh/uv); deps installed
  with `uv sync --frozen --no-dev` from uv.lock into /opt/venv (dependency layer cached
  separately from the package). Only pyproject.toml, uv.lock, README.md, photos2yt/ and
  scripts/nightly.sh are copied (no `COPY .`), and .dockerignore also excludes config,
  rules, credentials, tokens, databases, CSVs, .git and .venv. Runs as uid/gid 1000 by default;
  PHOTOS2YT_CONFIG=/config/config.yaml; WORKDIR /data (ingest_report.csv lands next to the DB);
  ENTRYPOINT photos2yt, CMD --help.
- docker-compose.yml: one service; settings from .env (.env.example): STAGING_DIR mounted
  read-only at /staging, CONFIG_DIR at /config (writable: the token is re-saved on refresh;
  report writes manifest.csv/summary.txt there), DATA_DIR at /data (queue.db). Bind mounts,
  not a named volume, so the DB is a plain folder to back up and file ownership follows
  `user: PUID:PGID`. docker-data/ and .env are gitignored.
- scripts/nightly.sh (installed as photos2yt-nightly): ingest -> upload --limit
  $UPLOAD_LIMIT (default 5) -> curate -> [reconcile if RECONCILE=1] -> report. Deviation from
  the spec's literal `ingest && upload --limit N && curate`: ingest failing stops the run, but
  an upload exit 1 (some videos failed, the rest uploaded) still lets curate and report run;
  the job exits 1 if any step failed, 0 otherwise. With plain `&&` one bad video would leave
  that night's good uploads out of their playlists until the next run.
- Real run without Docker (2026-11-11, by the human, on the Mac with uv; Docker is not
  installed there and Homebrew is not wanted): scripts/nightly.sh with
  PATH=.venv/bin and PHOTOS2YT_CONFIG=config.yaml, first with UPLOAD_LIMIT=0 and then a
  real run; every step ran and the job exited 0. The Docker image remains unbuilt.
- macOS schedule: scripts/launchd.py (run with `uv run python`) writes
  ~/Library/LaunchAgents/com.photos2yt.nightly.plist via plistlib and loads it with
  `launchctl bootstrap gui/<uid>`; also `run-now` (kickstart), `status` (`launchctl print`:
  last exit code, plus the last log lines) and `uninstall` (bootout + delete). The job runs
  `/bin/sh scripts/nightly.sh || osascript display notification`, so a failed run shows a
  macOS notification (launchd itself reports nothing). Environment: PATH = repo .venv/bin +
  the folder of the ffprobe found at install time + system dirs; PHOTOS2YT_CONFIG =
  repo/config.yaml; UPLOAD_LIMIT; WorkingDirectory = repo (ingest_report.csv there); output
  appended to logs/nightly.log (gitignored). Default time: 00:30 America/Los_Angeles
  converted to the Mac's zone at install time (StartCalendarInterval is local time; correct
  across DST in US zones). Tested with a fake launchctl and a temp home, and on the real Mac
  (2026-11-11, by the human): `install` chose 03:30 local (= 00:30 Pacific, Mac in US
  Eastern), `run-now` ran the whole nightly job from launchd (repo at ~/gp-to-yt, not a
  TCC-protected folder), `status` showed "Loaded in launchd: yes", last exit code 0, and the
  log ended with the report (6 videos in the manifest, nothing needing attention) and
  "nightly done, exit 0". The failure notification has not been seen yet (no failed run). Known macOS limit: TCC blocks launchd jobs from Desktop/Documents/Downloads and
  removable volumes; README says to move the folders or grant Full Disk Access.
- Scheduling: host cron / NAS task scheduler runs
  `docker compose run --rm --entrypoint photos2yt-nightly photos2yt`; no cron daemon inside
  the container (simpler, and the scheduler's e-mail-on-failure uses the exit code).
- Verified in the agent sandbox (Docker 29.9): image built from the same Dockerfile except the
  apt step (deb.debian.org is blocked there; Python's tzdata was present in the base image);
  `--help` runs; uid 1000; /app holds no config/token/DB; zoneinfo works; `config check`,
  `upload/curate/reconcile --dry-run` and `report` run through compose on a 3-video sample
  (ingested natively with source paths under /staging, because the sandbox image had no
  ffprobe); staging is read-only; a staging path with a space works; files are owned by
  PUID; the nightly job exits 1 when ingest fails; upload without token.json gives the
  "run photos2yt auth" error, exit 1. Not verified: the apt ffmpeg install and a containerised
  ingest; any real API call from the container.
- README rewritten: Google Cloud setup (API, consent screen In production, Desktop client),
  desktop auth then copying token.json, config, commands, Docker, nightly schedule,
  private-upload lock and audit, changing privacy in Studio, 3-video end-to-end run,
  manual offload checklist.
- Gotchas documented: `paths.rules` set (as in config.yaml.example) but the file missing
  makes curate, and so the nightly job, exit 1; the DB stores absolute source paths, so a
  queue.db built on the desktop cannot upload from the container; `auth` cannot run inside
  the container (no browser).

## How cleanup behaves (local originals, 2026-11-11)
- Requested by the human, who also approved the AGENT_RULES ground-rule exception (section
  0) that allows it. `photos2yt cleanup [--dry-run] [--dest DIR] [--delete --yes]
  [--max-age-days N]`.
- Moves by default: source file -> paths.uploaded_dir (new optional config key, relative to the
  config file; default "<staging_dir>-uploaded" beside staging_dir) at the same relative path.
  A destination inside staging_dir (or containing it) is refused, since ingest would find the
  files again. `--delete` deletes permanently and needs `--yes` (exit 2 without it).
- Eligible rows: UPLOADED/PLAYLISTED, youtube_video_id set, verified_at within max-age-days
  (default 7), and last_error not a "[reconcile" note. Their duplicates (duplicate_of an
  eligible row) go too. Files must exist, resolve inside staging_dir and have size_bytes
  from ingest; otherwise they are listed as "left alone". Missing files count as "already
  gone" (normal after an earlier cleanup). Sidecars move only when every row using the
  sidecar is eligible. A destination that exists is never overwritten.
- New migration 4: videos.verified_at (UTC ISO). reconcile sets it (db.mark_verified) for every
  video videos.list returned; missing videos keep their old value. The post-upload verification
  does not set it (a just-uploaded video can still be rejected while processing).
- The database is opened read-only (never migrated by cleanup); a database without verified_at
  gets "run reconcile once". Each move/delete is appended to cleanup_log.csv next to the DB
  (time, action, row, video id, source, destination). Exit 1 if any file failed.
- Not part of the nightly job. In Docker the staging mount is read-only, so cleanup is for
  native runs.
- Real run (2026-11-12, by the human, on the Mac): after updating, `reconcile` confirmed all 7
  uploaded videos (setting verified_at); `cleanup --dry-run` listed those 7 (0.40 GB, 7
  sidecars) and nothing else; `cleanup` moved them to ~/gp-to-yt-staging-uploaded
  successfully. Lesson from the run: a reconcile done with the pre-migration-4 code does not
  set verified_at, so after upgrading run reconcile again before cleanup.

## How reconcile behaves (Task 8)
- Checks rows in status UPLOADED **and PLAYLISTED** that have a youtube_video_id. PLAYLISTED
  rows are uploaded videos too (PLAYLISTED only means the playlist adds are done), and they are
  the majority once curate has run, so checking only UPLOADED would miss most deletions.
  Rows in other statuses have nothing on YouTube to check (an UPLOADING row's id is handled by
  upload's crash recovery).
- `videos.list(part=status,processingDetails,recordingDetails, id=<up to 50 comma-separated
  ids>)`, 1 unit per call (same cost constant as the uploader's verification call). No
  maxResults (not allowed with id), no paging, never search.list. Repeated ids are sent once.
- Per returned video:
  - actual_privacy refreshed (db.update_video_verification); a change since the last stored
    value is printed; a privacy different from `youtube.privacy` in config is flagged
    (private-upload lock, AGENT_RULES fact 1, or changed in Studio). Not written to
    last_error: actual_privacy already holds it, and report derives it from there.
  - recordingDate re-check: `recordingDetails.recordingDate` vs taken_utc, UTC date part only
    (YouTube may drop the time). Missing (including "still processing", which is said in the
    note) or different -> flagged and noted. This is the follow-up to the 2026-11-11 finding
    that videos.list can omit the date right after upload. YouTube is never modified.
  - status.uploadStatus deleted/failed/rejected -> flagged and noted (with rejectionReason /
    failureReason if given). Costs nothing extra (status part is fetched anyway).
- An id not returned by videos.list = **missing** (deleted or removed on YouTube). Decision:
  the row keeps its status and is not deleted. There is no legal transition out of
  PLAYLISTED, and UPLOADED -> PLAYLISTED would be a lie; moving it back to PENDING would
  re-upload automatically, which a human should decide (the video may have been removed on
  purpose). Instead last_error gets a note `[reconcile YYYY-MM-DD] missing on YouTube (...)`
  (db.set_reconcile_note; never touches status, only UPLOADED/PLAYLISTED rows). report lists
  it under "needs attention" and in the manifest's attention column.
- Notes: problems for one row are joined with "; " into one `[reconcile <UTC date>] ...`
  note. A later clean check clears the note, but only a last_error that starts with
  `[reconcile` (an older non-reconcile last_error, e.g. from a failed earlier attempt, is
  kept unless a new problem overwrites it).
- Quota: can_afford(1) before every call; recorded after every call that got a response
  (also rejected ones, like curate). quotaExceeded -> mark_exhausted(), stop, exit 0; rows
  not reached are counted as "not checked" and left unchanged. A day already marked
  exhausted stops before any call. Retries: the uploader's (5xx, 429, rate-limit 403s,
  network; Retry-After honoured). Other errors fail that batch only: its rows are left
  unchanged (never marked missing) and the run continues. Exit 1 if any call failed;
  videos needing attention are not a failure (exit 0).
- Credentials: the API client is built lazily, so a DB with nothing to check needs no
  token. No DB file -> "nothing to reconcile", the DB is not created.
- `--dry-run`: read-only DB (get_readonly_connection; never created or migrated), no
  credentials, no API calls. Prints each row that would be checked, the number of calls,
  estimated units and remaining quota from the local counter.

## How report behaves (Task 8)
- `photos2yt report [--out-dir DIR]`: reads the DB only (read-only connection; never created
  or migrated; no API calls, no credentials). Writes `manifest.csv` and `summary.txt` into
  DIR, default the config file's directory (CWD if the config has no path), and prints the
  summary. Both names are in .gitignore (they contain file names).
- manifest.csv: one row per UPLOADED/PLAYLISTED video with an id, ordered by taken_utc.
  Columns: original_filename, video_id, url (`https://youtu.be/<id>`), privacy
  (actual_privacy as last read back; empty = never verified), playlists (`; `-separated,
  playlist_membership added=1 joined with playlists), then extra columns status,
  taken_local_date, source_path (file names repeat across Takeout folders) and attention.
- summary.txt: counts by status (+ how many SKIPPED are duplicates), total, number in the
  manifest; quota today (Pacific day, local counter: units of daily_unit_limit, uploads of
  daily_upload_limit, exhausted flag); "needs attention": FAILED (last_error), UPLOADING
  (interrupted; next upload recovers it), SKIPPED that are not duplicates (e.g. > 15 min),
  reconcile notes (missing, recordingDate, uploadStatus), privacy different from config,
  privacy never verified, playlist adds not done yet (membership added=0).
- Run `reconcile` first for fresh privacy / missing information; report does not call the API.

## How the curator behaves (Task 7)
- Rules generator (2026-11-12, human request: no Google Photos albums to reuse, so playlists are
  built from dates): `photos2yt make-rules [--dry-run] [--force] [--input] [--output]`
  (photos2yt/rulegen.py) expands rules-input.yaml (gitignored; example
  rules-input.yaml.example) into rules.yaml: one playlist per year; holidays (fixed dates as
  month/day; moving ones - Easter, Mother's/Father's Day, Memorial/Labor Day, Thanksgiving -
  as exact dates for every year in `years`); one playlist per birthday (month/day, optional
  window_days either side, year-independent, Feb 29 handled); trips as every day start..end;
  keyword playlists on captions. Output is written via a temp file, validated with
  curator.load_rules, and never overwrites without --force. Uses only existing condition
  fields, so curate itself is unchanged. Holidays are US ones; add more in HOLIDAYS.
- "All videos" playlist (2026-11-12, human request): new rule condition `every_video: true`
  (validated: only `true` allowed) adds no restriction, so a condition with only that field
  matches every video, including ones with no local date. make-rules writes it from the
  optional `all_playlist: "<name>"` input key, as the first playlist. Costs one more playlist
  add (50 units) per video.
- Rules file: `curate --rules PATH`, else `paths.rules` in config.yaml (new optional key,
  resolved relative to the config file), else `rules.yaml` next to the config file. An
  explicitly given file that is missing is an error (exit 1); a missing default file just
  prints "nothing to curate" (exit 0). Invalid rules: exit 1 with the reason.
- Rules validation tightened: `match` must have non-empty `any` and/or `all` lists and no
  other keys; within one condition every field must hold (AND); local_year now validated
  and matched; local_month/local_day work alone; keyword lists must be non-empty strings;
  playlist names <= 150 chars, no `<>`.
- Candidates: rows UPLOADED or PLAYLISTED with a youtube_video_id. Matching uses
  taken_local_date only. Order is (taken_utc, id).
- Run, per rule in file order (only if it has videos to add):
  1. playlist from the `playlists` table, else `playlists.insert(part=snippet,status)` with
     the rule's privacy; the id is stored immediately.
  2. members not yet added are inserted in date order with `playlistItems.insert(part=snippet)`
     and an explicit position = number of already-added members (per the DB) that sort
     before the video. Recomputed per insert from what actually succeeded, so a failed add
     does not throw later positions off. Full backfill = 0, 1, 2, ...
  3. before each insert a membership row (added=0, planned position) is written; on success
     it becomes added=1 and other added members at or after that position shift by one
     (db.record_playlist_item_added), so `position` tracks the expected playlist index.
- Failures: a non-quota HTTP error (or retries exhausted on 5xx/429/rate limit/network) on
  one add is counted and the run continues; the row stays added=0 and is retried on the next
  run. A failed playlists.insert skips only that playlist. playlistNotFound (playlist
  deleted on YouTube) skips the rest of that playlist and says how to re-create it.
- Retries: like the uploader (5xx, 429, rate-limit 403s, network errors), plus HTTP 409
  reason SERVICE_UNAVAILABLE ("The operation was aborted."), which YouTube returned on a
  real playlistItems.insert right after playlists.insert (2026-11-11). Other 409s fail.
- Quota: `can_afford(cost)` before every call (local stop, day not marked exhausted);
  quotaExceeded -> `mark_exhausted()` and stop. Costs from config (playlist_insert_cost,
  playlist_item_insert_cost) recorded after every call that got an HTTP response, including
  rejected ones (conservative: YouTube may charge for those).
- After the adds (also after a quota stop) UPLOADED rows whose matching playlists all have
  added=1 become PLAYLISTED. Rows matching no rule stay UPLOADED. A PLAYLISTED row that
  matches a newly added rule is still added to that playlist (status stays PLAYLISTED).
- Idempotent: a re-run with nothing to add makes zero API calls and does not even build the
  API client, so it needs no credentials.
- Exit code 1 if any add or playlist creation failed, else 0 (a quota stop is not a failure).
- `--dry-run`: no credentials, no API client, DB opened with db.get_readonly_connection (a
  missing DB is not created; an old schema is not migrated; no files change). Prints each
  playlist (would create / exists), each add with its target position, date and title,
  estimated units, and remaining quota today from the local counter.
- Never uses search.list or any read call; the local DB is the source of truth.

## Curator limitations / risks
- Positions assume the YouTube playlist contains exactly the videos the DB says were added.
  Videos added or removed by hand in Studio shift positions, so later adds can land out of
  order, or be rejected if a position is past the end (then they fail and are retried). Re-ordering is the
  separate `playlistItems.update` command in the spec (not built).
- Duplicate risk: if a playlistItems.insert succeeds but the process dies before the DB
  write, or a retried insert's first attempt actually succeeded, the next run adds the video
  again. Same for playlists.insert (a second playlist with the same name). The window is
  small; no reads are done to check, to save quota.

## Titles and `photos2yt retitle`
- Google Takeout's sidecar `title` is just the file name, so titles used to read
  "2020-12-24 - IMG_6791". Now `{title}` in youtube.title_template is chosen in this order:
  1. first non-empty line of the Google Photos description (the caption), if any;
  2. the sidecar title / file name, unless it is a camera-generated name (IMG_1234,
     VID_..., PXL_..., MVI_, DSC, GOPR, DJI, timestamp names, UUIDs, digits only, ...;
     see metadata.is_meaningless_name);
  3. youtube.default_title.
- New template placeholders (local time): {day_num} {month_name} {month_short} {weekday}
  {weekday_short} {time} {default_title}, plus the old {title} {date} {year} {month} {day}.
- New example template: "{title} – {weekday}, {month_name} {day_num}, {year} {time}" ->
  "Family Video – Thursday, December 24, 2020 6:15 PM". The time keeps titles nearly
  unique, which crash recovery relies on (it adopts an upload by exact title). Two videos
  started in the same minute still get the same title.
- A long caption is shortened so the date part still fits in 100 characters; '<' and
  '>' are removed (YouTube rejects them).
- Existing config.yaml files keep their own template; change it by hand.
- `photos2yt retitle [--dry-run]` rebuilds titles with the current config for rows that
  are PENDING, FAILED or SKIPPED and have no saved upload session. UPLOADING rows are left
  alone (crash recovery needs the exact title sent); UPLOADED/PLAYLISTED videos are on
  YouTube already (rename in Studio). A row whose sidecar can no longer be read keeps its
  title (the caption would be lost). Database only, no API calls; --dry-run opens the DB
  read-only.

## How the uploader behaves (Task 6)
- Upload order (2026-11-11, human request): new optional config key `youtube.upload_order`,
  `largest_first` (default) or `oldest_first`. largest_first = PENDING rows by size_bytes
  descending, ties by (taken_utc, id); oldest_first = the old (taken_utc, id). The dry run
  lists rows in the same order with their size in MB. Curate is unaffected: playlist
  positions come from recording dates, not upload order. Videos > 15 min on an unverified
  channel are still skipped without using a --limit slot (large files are often long ones).
- Run order: recover rows stuck in UPLOADING, then upload PENDING rows by (taken_utc, id).
- Before each upload: `can_afford(upload_cost_units, uploads=1)`. If not: stop, or with
  `--wait` sleep until `seconds_until_reset()` once and re-check (stop if still unaffordable).
- quotaExceeded / uploadLimitExceeded stop the run cleanly (rows back to PENDING). Any other
  per-video failure (4xx, retries exhausted, unexpected exception) is counted and the run
  continues. Exit code 1 if any video failed, else 0 (a clean quota stop is not a failure).
- Videos > 15 min are marked SKIPPED (reason in last_error) unless
  `youtube.channel_verified: true` (new config flag, default false). Skips do not use up
  a `--limit` slot.
- Verification after each upload: `videos.list(part=status,processingDetails,recordingDetails)`
  (1 unit, recorded). actual_privacy stored via db.update_video_verification. A privacy
  mismatch prints and logs a "PRIVATE-UPLOAD LOCK" banner; a different recordingDate, or
  one still missing after processingStatus is "succeeded", prints a warning (a missing one
  while the video is still processing only prints an info line, see notes). Verification failures only warn; the row stays UPLOADED.
- `--dry-run`: no credentials, no API client, no DB writes. The DB is opened read-only (and
  `immutable` when there is no -wal file, so not even -wal/-shm files appear); a missing DB
  is not created. Prints count, titles, requested privacy, skips and estimated units
  (upload_cost_units + 1 verification per video).
- Credentials come from auth.get_credentials() (never a browser flow). Tokens and the
  session URI are never printed or logged.

## Crash recovery and duplicate-upload risk
For each row stuck in UPLOADING (skipped entirely if today's quota is marked exhausted):
1. Saved session URI: resume it. googleapiclient has no public resume API, so the uploader
   sets the public `resumable_uri` and the private `_in_error_state` flag on a fresh
   insert request; next_chunk() then asks the server how many bytes it has and continues.
   If the upload had in fact finished, the server returns the video, so no duplicate.
   If the library ever drops `_in_error_state`, resume is refused, not guessed.
2. No session, or the session is gone (400/404/410): look in the channel's uploads
   playlist (channels.list + playlistItems.list, 1 unit each, newest 4 pages = 200
   videos; never search.list) for a video with exactly the title we would send that is
   not already in the DB.
   - exactly one match: adopt it (UPLOADED + verification), no upload;
   - several matches: FAILED for manual review (no duplicate);
   - check failed (quota/API error): row stays UPLOADING, retried next run;
   - no match: back to PENDING with the session cleared, uploaded again from scratch.
**Remaining duplicate risk:** a video that finished uploading but (a) whose session expired
(about a week) or was never saved, AND (b) that is not visible in the newest 200 uploads
under exactly the same title (e.g. YouTube is still processing it and it has not appeared
in the uploads playlist yet, the title was edited in Studio, or more than 200 newer
uploads exist) will be uploaded again. Running recovery soon after a crash keeps this small.

## Known deviations / notes
- Quota recording in auth check is best-effort (errors swallowed).
- made_for_kids must be false in config.yaml.
- db.update_upload_session() (from task6-rewrite) persists the session URI; no-op unless
  the row is UPLOADING, None clears it.
- New db helpers: mark_verified and migration 4 (videos.verified_at) for cleanup;
  update_video_verification, get_video_by_youtube_id,
  get_readonly_connection, record_playlist_item_added (Task 7B), set_reconcile_note and
  RECONCILE_NOTE_PREFIX (Task 8). No schema change in Task 8.
- curate --dry-run with no rules configured now says "no playlist rules are configured"
  instead of "every matching video is already in its playlists".
- Task 8 imports helpers from uploader.py (parse_http_error, _is_retryable, _retry_wait,
  _safe_error_text, _format_recording_date, VERIFY_COST_UNITS, PRIVATE_LOCK_EXPLANATION)
  but does not change uploader.py.
- Tests no longer write photos2yt/rules.yaml (all rules files under tmp_path; an autouse
  fixture fails any test that touches it). .gitignore now ignores `rules.yaml` (local
  rules next to config.yaml) instead of photos2yt/rules.yaml.
- The old `python -m photos2yt.curator` entry point is gone; use `photos2yt curate`.
- Upload chunk size is config: youtube.upload_chunk_size (bytes, default 8 MiB, multiple of
  256 KiB). upload_one(chunksize=...) can override it.
- Rate limiting is retried like 5xx: HTTP 429 and 403 rateLimitExceeded /
  userRateLimitExceeded; Retry-After honoured (capped at 300 s). quotaExceeded never retried.
- Transient errors that exhaust retries mark the row FAILED (session URI kept); the run
  continues with the next video.
- Quota accounting: upload_cost_units is recorded once when an upload (or a resumed upload)
  completes. Adopted videos record only the list calls, not an upload cost (the insert was
  charged on an earlier day/run and was never recorded).
- recordingDate check compares the UTC date only, because YouTube may drop the time part.
- recordingDate missing right after upload (fix after the first real uploads, 2026-11-11):
  for 1 of 3 uploads verify_upload warned "did not stick (missing)" although YouTube Studio
  showed the correct date. While the video is still processing, recordingDetails can be
  absent from videos.list. Now: if recordingDate is missing AND processingStatus is not
  "succeeded" (or absent), print an info line ("not visible yet (video still processing)")
  and set VerificationResult.recording_date_pending; no "did not stick" warning. Chosen
  over a delayed re-check because it costs no extra quota (a re-check would be 1 more unit
  per upload and processing can take longer than any short wait). A recordingDate that
  *differs* still warns, as does one missing after processing succeeded. Nothing re-checks
  these yet; a future reconcile step (videos.list, 1 unit) could.
- Warnings printed twice (fixed): `_warn` prints via `out` and logs; with no logging
  configured, Python's logging.lastResort also wrote the record to stderr. The `photos2yt`
  package logger now has a NullHandler (no lastResort). `photos2yt` CLI main() installs one
  stderr handler (WARNING+, `LEVEL: message`) on the `photos2yt` logger that skips records
  marked `extra={ECHOED_TO_TERMINAL: True}` (set by `_warn`), so echoed warnings show
  once and log-only warnings (e.g. tenacity retry notices) still reach the terminal once.
  Records still propagate to any logging the user configures.
- curator.py `_Runner._fail` had the same double print (logger.error + out); its log record
  is now marked ECHOED_TO_TERMINAL too.
- `--wait` applies to the local can_afford check only; a quotaExceeded from YouTube still
  stops the run (as specified), it does not sleep.
- An unexpected exception in the middle of an upload leaves the row UPLOADING; the next
  run's recovery picks it up.

## Open questions
- recordingDetails.location may be deprecated by the API; kept because the spec requires it.
- Confirm current videos.insert quota cost against Google's table before first real upload.
- Private-upload lock: first real upload kept `unlisted` (no banner). Confirm in Studio
  over the next few uploads.
- Is 4 pages (200 newest uploads) the right adoption search depth? It costs up to 5 units
  per stuck row.
- Deleting from Google Photos automatically: not possible (the Photos Library API has no
  delete call, and since March 2025 it can only see media the app itself created). Manual.
- Docker: should a missing `paths.rules` file from config be treated like the default (print
  "nothing to curate", exit 0) instead of an error, so a nightly run without playlists is
  green? Currently documented instead of changed.
- Docker: publish a prebuilt image (e.g. GHCR) or keep building locally? Currently local.
- Reconcile: should a missing video (deleted on YouTube) make the exit code non-zero, so a
  nightly cron run alerts? Currently exit 0 and it is only reported.
- Reconcile: should a missing video be re-queued for upload (would need a new
  UPLOADED/PLAYLISTED -> PENDING transition and clearing its playlist memberships)?
  Currently left for a human.
- Report: the manifest lists only videos on YouTube. Duplicate files (SKIPPED with
  duplicate_of) are covered by their original's row but not listed themselves; should they
  get a manifest row pointing at the original's video id (useful for the Google Photos
  deletion checklist)?
- Curator: should a playlist deleted on YouTube be re-created automatically? Currently it
  is reported and left for a human (delete its row in `playlists`).
