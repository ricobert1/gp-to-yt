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

## Next
- Manual: upload one real short file (`photos2yt upload --limit 1`), check the
  private-lock warning and recordingDate check against reality.
- Task 7B / Task 8 (not started, per instructions).

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
- New db helpers: update_video_verification, get_video_by_youtube_id,
  get_readonly_connection.
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
- The task said curator.py's `_Runner._fail` also does logger.error plus out; the curator
  in this repository has no logger, `_Runner` or `_fail` (it only prints), so nothing to
  change there. If that code lands later, mark its log records with ECHOED_TO_TERMINAL too.
- `--wait` applies to the local can_afford check only; a quotaExceeded from YouTube still
  stops the run (as specified), it does not sleep.
- An unexpected exception in the middle of an upload leaves the row UPLOADING; the next
  run's recovery picks it up.

## Open questions
- recordingDetails.location may be deprecated by the API; kept because the spec requires it.
- Confirm current videos.insert quota cost against Google's table before first real upload.
- Whether the API project is subject to the private-upload lock (the first real upload will
  show it: watch for the PRIVATE-UPLOAD LOCK banner).
- Is 4 pages (200 newest uploads) the right adoption search depth? It costs up to 5 units
  per stuck row.
- claude_code_task_breakdown.md (the main spec) is not in the repository yet.
