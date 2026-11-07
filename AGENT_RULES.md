# Ground Rules & Known Platform Facts

## 0. Ground Rules

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

1. **Private lock:** uploads from an unverified API project created after 28 July 2020 are forced to `private` regardless of the requested privacy. The uploader must read back `status.privacyStatus` after upload and log a prominent warning if it differs from the request. An audit (or manual Studio changes) is the way out.
2. **Quota is in flux:** `videos.insert` was 1,600 units of a 10,000 pool; Google may have moved it to its own per-day bucket. Verify against Google's official quota table. Make `upload_cost_units`, `daily_unit_limit`, and `daily_upload_limit` config values. A `quotaExceeded` 403 is the authoritative stop signal. There is no API to read remaining quota, so the tool keeps its own counter.
3. **`recordingDate` location:** it is `recordingDetails.recordingDate`, not `snippet`. Request `part=snippet,status,recordingDetails`. `snippet.categoryId` is required.
4. **OAuth consent screen:** must be "In production" or refresh tokens expire after 7 days. Initial auth is done on a desktop with a browser; the resulting `token.json` is copied to the NAS.
5. **Limits:** title ≤ 100 chars; description ≤ 5,000 bytes, no `<` or `>`; tags ≤ 500 chars total; videos > 15 min require a verified channel.
6. **Never use `search.list` (100 units).** The local DB is the source of truth. Use `videos.list` (1 unit) only for reconciliation.
7. **Scope:** `https://www.googleapis.com/auth/youtube` alone covers uploads and playlists.
