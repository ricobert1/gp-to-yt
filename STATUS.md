# Status

## Completed
- Task 1: scaffold and config (0af48e3)
- Task 2: SQLite queue and state machine (923c339)
- Task 3: OAuth auth module (c39af5b)
- Task 4: Takeout ingest (3e871e2), duplicate handling fix (6d80e88)
- Checkpoint after real-sample verification (6bdf3b1)
- Task 5: quota tracker (see next commit; migration 3 adds quota_log.exhausted_at)
- Task 6: uploader (resumable uploads, quota-aware, private-lock read-back check)

## Next
- Task 7: curator (declarative rules for playlists)

## Known deviations / notes
- Quota recording in auth check is best-effort (errors swallowed).
- made_for_kids must be false in config.yaml.

## Open questions
- Confirm current videos.insert quota cost against Google's table before first real upload.
- Whether the API project is subject to the private-upload lock (find out in Task 6 first upload).
