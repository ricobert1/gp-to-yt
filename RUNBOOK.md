# photos2yt runbook

Day-to-day operation on the Mac (no Docker). Run every command from the
repository folder (`~/gp-to-yt`). Setup and background are in `README.md`;
behaviour details and decisions are in `STATUS.md`.

## Already set up (one-time)

- Google Cloud project, YouTube Data API v3, OAuth desktop credentials,
  `token.json` (README sections 1-2).
- `config.yaml`: `quota.daily_upload_limit: 100` (Google's separate "Video
  Uploads per day" limit), `upload_cost_units` low, `youtube.upload_order:
  largest_first` (the default).
- Nightly launchd job: 03:30 local time (00:30 Pacific, just after the quota
  reset), `UPLOAD_LIMIT` set with `scripts/launchd.py install --limit N`
  (check with `status`).

## 1. Get videos into staging (manual, per batch)

There is no API to download from Google Photos; this step is manual.

1. [takeout.google.com](https://takeout.google.com): select **Google Photos**
   only, export, download the zips.
2. Unzip, and move the videos **with their `.json` sidecars** into
   `~/gp-to-yt-staging` (sub-folders are fine; keep each sidecar next to its
   video).
3. Preview what will be queued:
   ```bash
   uv run photos2yt -c config.yaml ingest --dry-run
   ```

Files already handled are never uploaded twice: re-downloaded copies are
recognised by content and marked as duplicates.

## 1b. Playlists (once, and whenever you add a person, trip or year)

Playlists come from `rules.yaml`, built from `rules-input.yaml`:
```bash
cp rules-input.yaml.example rules-input.yaml     # first time only; then edit it
```
```bash
uv run photos2yt -c config.yaml make-rules --dry-run
```
```bash
uv run photos2yt -c config.yaml make-rules --force
```
```bash
uv run photos2yt -c config.yaml curate --dry-run
```
`--force` replaces the existing `rules.yaml` (edit `rules-input.yaml`, not
`rules.yaml`). A video can be in several playlists ("All Family Videos" if
`all_playlist` is set, its year, a holiday, a birthday, a trip); each playlist stays in date order. Each playlist add costs
50 quota units, so after a big upload night curate may stop on quota and finish
the next night; that is normal.

## 2. Upload and organise (automatic, nightly)

At 03:30 the job runs `ingest` -> `upload --limit N` (largest first) ->
`curate` -> `report`. The Mac must be logged in; if it is asleep the job runs
when it wakes, if it is shut down that night is skipped. Keep it plugged in
for long runs.

Run it now instead of waiting:
```bash
uv run python scripts/launchd.py run-now
```
```bash
tail -f logs/nightly.log
```
(Ctrl+C stops watching; the job keeps running.)

Run it in the terminal, or step by step:
```bash
PATH="$PWD/.venv/bin:$PATH" PHOTOS2YT_CONFIG="$PWD/config.yaml" sh scripts/nightly.sh; echo "exit $?"
```
```bash
uv run photos2yt -c config.yaml upload --dry-run
```
Prefix the first with `UPLOAD_LIMIT=20` (or `0`) to change the number of
uploads, or `RECONCILE=1` to also run reconcile.

Change the nightly upload limit or time:
```bash
uv run python scripts/launchd.py install --limit 80
```
```bash
uv run python scripts/launchd.py install --time 02:15 --limit 80
```

## 3. Check on it (next morning)

```bash
uv run python scripts/launchd.py status
```
Want: `last exit code = 0` and "Needs attention: nothing". `summary.txt` has
counts by status, today's quota and the attention list; `manifest.csv` lists
every uploaded video with its `https://youtu.be/` link. A failed run also
shows a macOS notification.

What's left to upload:
```bash
grep -A8 "Videos by status" summary.txt
```

## 4. Free local disk space (weekly, or whenever)

```bash
uv run photos2yt -c config.yaml reconcile
```
```bash
uv run photos2yt -c config.yaml cleanup --dry-run
```
```bash
uv run photos2yt -c config.yaml cleanup
```
Only videos reconcile confirmed on YouTube in the last 7 days move, to
`~/gp-to-yt-staging-uploaded` (logged in `cleanup_log.csv`). Check them, then
delete that folder yourself. YouTube re-encodes, so keep a copy of the
originals elsewhere if you want full quality. Videos uploaded minutes ago wait
until YouTube finishes processing; run reconcile again later.

## 5. Delete from Google Photos (manual, at the end)

README section 10: check `summary.txt` (nothing PENDING, FAILED or
UPLOADING; attention list empty), spot-check `manifest.csv` links, back up
`queue.db` and `manifest.csv`, then delete in Google Photos (items stay in its
Bin for 60 days).

## Updating the tool

```bash
git pull origin main
```
If git refuses (local changes, untracked files), do not force it: look at
`git status` / `git diff` first. After an update that adds a database
migration, run `reconcile` once before `cleanup`.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Nightly runs, exit 0, uploads nothing | Nothing waiting (`PENDING 0`). Add a new Takeout batch (step 1). |
| "local quota budget for today is used up" but the Cloud console shows plenty left | The local counter is out of step with Google (e.g. after a config change). It resets at midnight Pacific; to fix today, back up `queue.db` and correct today's `quota_log` row to Google's figure. |
| `quotaExceeded` from YouTube | Google's real limit hit; the run stops cleanly and continues the next night. |
| `curate` fails: rules file not found | `paths.rules` is set but the file is missing: create `rules.yaml` (`playlists: []` for none) or remove the line. |
| PRIVATE-UPLOAD LOCK warning | Project forced to private: change privacy in Studio (README section 7), then `reconcile`. |
| `recordingDate ... not visible yet` | Normal right after upload; `reconcile` re-checks it later. |
| Cleanup moves nothing | Run `reconcile` first; see the dry run's "not confirmed safe" list for the reason per file. |
| launchd log says "Operation not permitted" | macOS privacy: keep the repo and staging out of Desktop/Documents/Downloads and external drives. |
| Unknown command (e.g. `cleanup`) | Local code is old: `git pull origin main`. |
