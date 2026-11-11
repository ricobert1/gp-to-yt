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
- Task 8 (not started).

## How the curator behaves (Task 7)
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

## How the uploader behaves (Task 6)
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
  mismatch prints and logs a "PRIVATE-UPLOAD LOCK" banner; a missing or different
  recordingDate prints a warning. Verification failures only warn; the row stays UPLOADED.
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
- New db helpers: update_video_verification, get_video_by_youtube_id,
  get_readonly_connection, record_playlist_item_added (Task 7B).
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
- Curator: should a playlist deleted on YouTube be re-created automatically? Currently it
  is reported and left for a human (delete its row in `playlists`).
