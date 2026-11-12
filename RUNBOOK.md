# photos2yt runbook

Day-to-day operation on the Mac (no Docker). Run every command from the
repository folder (`~/gp-to-yt`). Setup and background are in `README.md`;
behaviour details and decisions are in `STATUS.md`.

## The process at a glance

| Step | Who | When |
|---|---|---|
| Request a Google Takeout export and save the zip(s) into `~/gp-to-yt-inbox` | **you** (no API exists) | monthly for new videos; per year for the backlog |
| unpack -> ingest -> upload (largest first) -> curate -> reconcile -> cleanup -> report | automatic (launchd) | nightly, 03:30 |
| Check the morning status | you | when you like |
| Delete the `-uploaded` folder, `inbox/unpacked` zips, and videos in Google Photos | **you** | when you are satisfied |

## One-time setup

Already done: Google Cloud project and `token.json` (README 1-2),
`config.yaml` quota numbers (`daily_upload_limit: 100`), `channel_verified:
true` (long videos), `rules-input.yaml` -> `rules.yaml` (step B).

Create the inbox, and install the nightly job with everything switched on:
```bash
mkdir -p ~/gp-to-yt-inbox
```
```bash
uv run python scripts/launchd.py install --limit 80 --cleanup
```
```bash
uv run python scripts/launchd.py status
```
`status` should show `UPLOAD_LIMIT=80; RECONCILE=1; CLEANUP=1`. (`--cleanup`
turns on reconcile too. Leave `--cleanup` off if you want to move files
yourself.) The inbox is `gp-to-yt-inbox` next to the staging folder unless
`paths.takeout_inbox` is set in `config.yaml`. It must not be in Downloads,
Desktop or Documents: macOS blocks scheduled jobs from reading those.

## A. Get a Takeout batch (you, ~5 minutes)

**New videos (monthly).** In [photos.google.com](https://photos.google.com):
search **Videos**, tick this month's first video, Shift-click the last, then
**+ -> Album -> New album** (e.g. `Takeout 2026-11`). In
[takeout.google.com](https://takeout.google.com): **Deselect all**, tick
**Google Photos**, **All photo albums included -> Deselect all**, tick only
that album, **OK**, **Next step**: *Send download link via email*, *Export
once*, **.zip**, **50 GB**, **Create export**.

**Backlog (one or a few years at a time).** Same Takeout steps, but tick the
built-in **"Photos from 2019"** (etc.) albums instead of making one. Do a year,
let it upload and clean up, then the next year; this keeps disk use down.

**Download into the inbox.** When Google's e-mail arrives, download every
part (`takeout-...-001.zip`, `-002.zip`, ...) and save or drag them into
`~/gp-to-yt-inbox`. Download **all parts before the night's run**: a video and
its sidecar can be in different parts, and a video ingested without its
sidecar loses its caption and Google Photos date.

Optional check (from Terminal, any time):
```bash
uv run photos2yt -c config.yaml unpack --dry-run
```

## B. Playlists (once, and when you add a person, trip or year)

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
Edit `rules-input.yaml`, never `rules.yaml` (`--force` replaces it). A video
can be in several playlists ("All Family Videos", its year, a holiday, a
birthday, a trip); each stays in date order. Each playlist add costs 50 quota
units, so after a big night curate may stop on quota and finish the next
night; that is normal.

## C. What the nightly job does (automatic)

1. **unpack**: every `takeout-*.zip` in the inbox -> only videos and `.json`
   sidecars into `~/gp-to-yt-staging/<export name>/` (all parts of one export
   in one folder); checks free disk space; never overwrites; finished zips
   move to `~/gp-to-yt-inbox/unpacked/`; log in `unpack_log.csv`. A zip that
   fails (corrupt, no space) stays in the inbox and is retried next night.
2. **ingest**: queues new videos (duplicates of anything already uploaded are
   recognised by content and skipped).
3. **upload**: up to `UPLOAD_LIMIT`, largest first.
4. **curate**: playlists from `rules.yaml`.
5. **reconcile**: confirms uploaded videos still exist on YouTube.
6. **cleanup**: moves confirmed originals to `~/gp-to-yt-staging-uploaded`
   (log in `cleanup_log.csv`). Videos uploaded the same night usually wait a
   day (YouTube still processing).
7. **report**: `summary.txt` and `manifest.csv`.

The Mac must be logged in; asleep at 03:30 means the job runs when it wakes;
shut down means that night is skipped. Keep it plugged in for big batches.

Run it now instead of waiting:
```bash
uv run python scripts/launchd.py run-now
```
```bash
tail -f logs/nightly.log
```
(Ctrl+C stops watching; the job keeps running.) To run in the Terminal
instead: `PATH="$PWD/.venv/bin:$PATH" PHOTOS2YT_CONFIG="$PWD/config.yaml" sh
scripts/nightly.sh` (prefix `UPLOAD_LIMIT=20`, `RECONCILE=1 CLEANUP=1` or
`UNPACK=0` to change what runs). Each step also runs on its own, e.g.
`uv run photos2yt -c config.yaml unpack`.

## D. Check on it (next morning)

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

## E. Clean up (you, when satisfied)

- `~/gp-to-yt-staging-uploaded`: originals already on YouTube. Look through,
  then delete the folder (keep a copy elsewhere if you want full quality;
  YouTube re-encodes).
- `~/gp-to-yt-inbox/unpacked`: Takeout zips already unpacked. Delete them.
- Google Photos (at the end): README section 10. Check `summary.txt`
  (nothing PENDING, FAILED or UPLOADING; attention list empty), spot-check
  `manifest.csv` links, back up `queue.db` and `manifest.csv`, then delete in
  Google Photos (items stay in its Bin for 60 days).

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
| Nightly runs, exit 0, uploads nothing | Nothing waiting (`PENDING 0`) and no zips in the inbox. Do step A. |
| unpack: "No Takeout zips" but you downloaded some | They are in `~/Downloads` (or still `.crdownload`/`.download`): move the finished `takeout-*.zip` files into `~/gp-to-yt-inbox`. |
| unpack: "not enough disk space" | Delete `~/gp-to-yt-staging-uploaded` and `inbox/unpacked` zips, or let cleanup catch up; it retries the next night. |
| unpack: "not a readable zip" / "extraction failed" | The download is incomplete or corrupt: download that part again (replace it in the inbox). |
| Videos uploaded with "Family Video" titles and odd dates | Ingested without their sidecars (a Takeout part was missing). Download all parts before the run. |
| "local quota budget for today is used up" but the Cloud console shows plenty left | The local counter is out of step with Google. It resets at midnight Pacific; to fix today, back up `queue.db` and correct today's `quota_log` row to Google's figure. |
| `quotaExceeded` from YouTube | Google's real limit hit; the run stops cleanly and continues the next night. |
| Long videos SKIPPED | `youtube.channel_verified: true` was not set when they were uploaded. |
| `curate` fails: rules file not found | `paths.rules` is set but the file is missing: run step B, or create `rules.yaml` with `playlists: []`. |
| PRIVATE-UPLOAD LOCK warning | Project forced to private: change privacy in Studio (README section 7), then `reconcile`. |
| `recordingDate ... not visible yet` | Normal right after upload; `reconcile` re-checks it later. |
| Cleanup moves nothing | Needs reconcile first (on by default with `--cleanup`); the dry run lists the reason per file. |
| launchd log says "Operation not permitted" | macOS privacy: keep the repo, staging and inbox out of Desktop/Documents/Downloads and external drives. |
| Unknown command (e.g. `unpack`) | Local code is old: `git pull origin main`. |
