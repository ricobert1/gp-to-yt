# Status

## Completed
- Task 1: scaffold and config (0af48e3)
- Task 2: SQLite queue and state machine (923c339)
- Task 3: OAuth auth module (c39af5b)
- Task 4: Takeout ingest (3e871e2), duplicate handling fix (6d80e88)
- Checkpoint after real-sample verification (6bdf3b1)
- Task 5: quota tracker (see next commit; migration 3 adds quota_log.exhausted_at)
- Task 6 part 1: uploader core, rewritten from scratch (upload_one: request body, resumable
  MediaFileUpload + next_chunk loop, session URI persisted via db.update_upload_session,
  tenacity per-chunk retries on 500/502/503/504 + network errors, error classification by
  JSON reason: quotaExceeded / uploadLimitExceeded / other 4xx). Awaiting human review.
  The earlier Task 6 implementation was broken (undefined names, no real resumable upload,
  no retries) and has been replaced.

## Next
- Task 6 part 2: post-upload verification read-back (videos.list; actual_privacy; private-lock
  and recordingDate warnings), crash recovery (resume stored upload_session_uri / stale
  UPLOADING rows), CLI `photos2yt upload [--limit N] [--dry-run] [--wait]` run loop with
  can_afford checks.
- Task 7: curator (declarative rules for playlists)

## Known deviations / notes
- Quota recording in auth check is best-effort (errors swallowed).
- made_for_kids must be false in config.yaml.

- db.update_upload_session() (from task6-rewrite) is used to persist the session URI; it is a
  no-op unless the row is UPLOADING, and None clears it. Covered in tests/test_db.py.
- Upload chunk size is config: youtube.upload_chunk_size (bytes, default 8388608 = 8 MiB,
  must be a multiple of 256 KiB). upload_one(chunksize=...) can still override it.
- Rate limiting is retried like 5xx: HTTP 429 and 403 rateLimitExceeded /
  userRateLimitExceeded. Retry-After is honoured (capped at 300 s), otherwise exponential
  backoff. 403 quotaExceeded is never retried.
- Transient errors that exhaust retries mark the row FAILED (session URI kept) and raise
  UploadRetriesExhaustedError so the run loop stops.

## Open questions
- recordingDetails.location may be deprecated by the API; kept because the spec requires it.
- Confirm current videos.insert quota cost against Google's table before first real upload.
- Whether the API project is subject to the private-upload lock (find out in Task 6 first upload).
