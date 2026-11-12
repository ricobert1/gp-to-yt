# photos2yt

Move the videos from a Google Photos Takeout export to a YouTube channel, then
sort them into playlists by date and keywords.

- **ingest** scans the Takeout folder (read-only), reads each video's sidecar
  JSON and ffprobe metadata, and queues it in a local SQLite database.
- **upload** uploads queued videos (resumable), sets title, description and
  `recordingDate`, and reads back the privacy YouTube actually applied.
- **curate** creates playlists and adds videos in date order from `rules.yaml`.
- **reconcile** checks that uploaded videos still exist and refreshes their privacy.
- **report** writes `manifest.csv` (file → video id → URL → privacy → playlists)
  and `summary.txt`.

Day-to-day steps are in [RUNBOOK.md](RUNBOOK.md).

The local database is the source of truth. The tool keeps its own quota counter
(there is no API for remaining quota) and never uses `search.list`. Every
command that changes something has `--dry-run`.

Contents: [Google Cloud setup](#1-google-cloud-setup) ·
[Install and first auth](#2-install-and-authorize-on-a-desktop) ·
[Configure](#3-configure) · [Commands](#4-commands) ·
[Docker](#5-running-with-docker) · [Nightly schedule](#6-nightly-schedule) (cron, Mac launchd) ·
[Private-upload lock](#7-the-private-upload-lock-and-the-api-audit) ·
[3-video test run](#8-end-to-end-test-on-a-3-video-sample) ·
[Local cleanup](#9-cleaning-up-local-originals-photos2yt-cleanup) ·
[Offload checklist](#10-manual-offload-checklist)

---

## 1. Google Cloud setup

Done once, in the [Google Cloud console](https://console.cloud.google.com/),
signed in with the Google account that owns (or manages) the YouTube channel.

1. **Create a project** (any name, e.g. `photos2yt`).
2. **Enable the API:** APIs & Services → Library → **YouTube Data API v3** → Enable.
3. **OAuth consent screen** (newer consoles call this area *Google Auth Platform*):
   - User type **External**; fill in app name, support email and developer email.
   - Scope: `https://www.googleapis.com/auth/youtube` (this one scope covers
     uploads and playlists).
   - **Publishing status: In production.** This matters: while the app is in
     *Testing*, refresh tokens expire after 7 days and the nightly job stops
     working every week. You do not need Google's app verification for your
     own use; you will see an "unverified app" warning once, during the first
     authorization (Advanced → continue).
4. **Create credentials:** Credentials → Create credentials → OAuth client ID →
   application type **Desktop app**. Download the JSON and save it as
   `credentials.json` next to your `config.yaml`.
5. **Quota:** the default is 10,000 units per day, reset at midnight Pacific
   time. Check the current cost of `videos.insert` in Google's
   [quota table](https://developers.google.com/youtube/v3/determine_quota_cost)
   and set `quota.upload_cost_units` (and `quota.daily_upload_limit` if uploads
   now have their own daily bucket) to match. The tool also stops cleanly when
   YouTube answers `quotaExceeded`.

`credentials.json` and `token.json` are secrets: they are in `.gitignore` and
`.dockerignore`, and `token.json` is written with permissions 600.

## 2. Install and authorize on a desktop

The first authorization opens a browser, so do it on a computer with one (not
on the NAS). You need [uv](https://docs.astral.sh/uv/) and, for ingest,
`ffprobe` (part of ffmpeg: `brew install ffmpeg` on macOS).

```bash
git clone https://github.com/ricobert1/gp-to-yt.git
cd gp-to-yt
uv sync --extra dev
cp config.yaml.example config.yaml        # then edit it, see section 3
uv run photos2yt -c config.yaml auth      # opens the browser; saves token.json
uv run photos2yt -c config.yaml auth --check   # prints the channel name (1 unit)
```

`auth --check` must show the channel you want to upload to. To run on a NAS or
in Docker, copy `token.json` (and `credentials.json`) there afterwards; a token
from a production-status app keeps working until you revoke it. The tool
refreshes and re-saves it as needed, so its folder must be writable.

## 3. Configure

Start from `config.yaml.example`. Relative paths are relative to the config
file. The important keys:

| Key | Meaning |
|---|---|
| `paths.staging_dir` | The extracted Takeout `Google Photos` folder (only read, never changed). |
| `paths.db_path` | The SQLite queue (`queue.db`). Back it up: it is the record of what was uploaded. |
| `paths.takeout_inbox` | Folder `unpack` reads Takeout zips from (optional; default `gp-to-yt-inbox` next to the staging folder; not Downloads/Desktop/Documents). |
| `paths.uploaded_dir` | Where `cleanup` moves originals (optional; default `<staging_dir>-uploaded` next to the staging folder). |
| `paths.rules` | Playlist rules for `curate`. If this key is set the file must exist (see below). |
| `timezone` | Your local time zone; decides local dates for titles and playlist rules. |
| `youtube.privacy` | Requested privacy (`private`, `unlisted`, `public`). See section 7. |
| `youtube.upload_order` | `largest_first` (default: biggest files first, frees the most Google Photos storage soonest) or `oldest_first`. Playlists are ordered by date either way. |
| `youtube.title_template` | Title format; `{title}` is the Google Photos caption, else a meaningful file name. |
| `quota.*` | Daily unit limit and per-call costs. |

Playlist rules: the easy way is `make-rules`. Copy `rules-input.yaml.example`
to `rules-input.yaml` next to `config.yaml`, fill in your years, holidays,
birthdays and trips, and run `photos2yt make-rules`. It writes a validated
`rules.yaml` (moving holidays such as Thanksgiving and Easter are worked out for
every year; trips become every day from start to end). Check the result with
`photos2yt curate --dry-run`. To write rules by hand instead, copy
`photos2yt/rules.yaml.example` to `rules.yaml` next to `config.yaml` and edit it. If you do not want playlists yet, either create a
`rules.yaml` containing just `playlists: []`, or delete the `rules:` line from
`config.yaml`. Otherwise `curate` (and so the nightly job) exits with an error
because the configured file is missing.

## 4. Commands

```text
photos2yt [-c config.yaml] <command>
  config check                validate config.yaml
  auth [--check]              authorize (browser) / show the authorized channel
  ingest [--dry-run]          queue new videos from staging_dir; writes ingest_report.csv
  upload [--limit N] [--dry-run] [--wait]
  curate [--dry-run] [--rules PATH]
  make-rules [--dry-run] [--force] [--input F] [--output F]
                              build rules.yaml from rules-input.yaml (years, holidays, birthdays, trips)
  retitle [--dry-run]         rebuild titles of videos not uploaded yet
  reconcile [--dry-run]       videos.list check of every uploaded video (1 unit per 50)
  report [--out-dir DIR]      manifest.csv + summary.txt (no API calls)
  unpack [--dry-run] [--inbox DIR]
                              extract videos + sidecars from Takeout zips in the inbox into staging_dir
  cleanup [--dry-run] [--delete --yes] [--dest DIR] [--max-age-days N]
                              move local originals confirmed on YouTube out of staging_dir
```

Without `-c`, the config comes from `$PHOTOS2YT_CONFIG`, else `./config.yaml`.
Exit code 1 means something failed (a video, a playlist add, a `videos.list`
call); a clean stop because the day's quota is used up is exit 0. Details of
each command's behaviour are in `STATUS.md`.

## 5. Running with Docker

The image is Python slim + ffmpeg, with photos2yt installed from `uv.lock`.
It runs as an unprivileged user and has three mount points:

| In the container | What | Mode |
|---|---|---|
| `/staging` | Takeout `Google Photos` folder | read-only |
| `/config` | `config.yaml`, `credentials.json`, `token.json`, `rules.yaml`; `report` writes `manifest.csv` / `summary.txt` here | read-write (token refresh) |
| `/data` | `queue.db` and `ingest_report.csv` | read-write |

Setup, in the repository folder on the machine that will run it:

```bash
cp .env.example .env              # set STAGING_DIR, PUID/PGID (id -u / id -g), UPLOAD_LIMIT
mkdir -p docker-data/config docker-data/data
cp config.yaml.example docker-data/config/config.yaml
cp credentials.json token.json docker-data/config/      # from the desktop, section 2
chmod 600 docker-data/config/token.json
```

In `docker-data/config/config.yaml` set the container paths:

```yaml
paths:
  staging_dir: /staging
  db_path: /data/queue.db
  credentials: credentials.json   # relative to /config
  token: token.json
  rules: rules.yaml               # create it, or remove this line (section 3)
```

Then:

```bash
docker compose build
docker compose run --rm photos2yt --help
docker compose run --rm photos2yt config check
docker compose run --rm photos2yt auth --check
docker compose run --rm photos2yt ingest --dry-run
docker compose run --rm photos2yt upload --dry-run
```

Notes:

- `docker-data/` and `.env` are in `.gitignore`; the build never copies config,
  tokens or databases into the image (`.dockerignore`).
- The folders must be writable by `PUID:PGID`. If Docker created them (as
  root) before you did, fix it with `sudo chown -R "$(id -u):$(id -g)" docker-data`.
- The database stores the **absolute path** of every video. A `queue.db`
  built on your Mac (paths like `/Users/...`) cannot upload from the container
  (paths `/staging/...`). Run ingest where you will upload, or start the
  container with a fresh database.
- `auth` (the browser flow) does not work inside the container; authorize on a
  desktop and copy `token.json` in.

## 6. Nightly schedule

`photos2yt-nightly` (from `scripts/nightly.sh`, installed in the image) runs:

```text
[unpack]  →  ingest  →  upload --limit $UPLOAD_LIMIT  →  curate  →  [reconcile]  →  [cleanup]  →  report
```

(`UNPACK=0` skips unpack; `RECONCILE=1` and `CLEANUP=1` turn those on. On the Mac,
`scripts/launchd.py install --cleanup` sets both. In Docker, staging is read-only,
so use `UNPACK=0` and leave cleanup off.)

- If **ingest** fails, nothing else runs (exit 1).
- If **upload** fails for some videos, the others were uploaded, so curate and
  report still run; the job still exits **1** so the failure is noticed.
- Any failed step makes the exit code 1; all good is 0. A quota stop is not a
  failure.
- `UPLOAD_LIMIT` defaults to 5: with 10,000 units and 1,600 per upload that uses
  8,005 units and leaves about 2,000 for playlist adds (50 each).

Run it by hand:

```bash
docker compose run --rm --entrypoint photos2yt-nightly photos2yt
```

Schedule it with the host's cron (or the NAS's task scheduler running the same
command). Quota resets at midnight **Pacific**, so pick a time shortly after
that in your local time, e.g. 03:30 for US Eastern:

```cron
30 3 * * * cd /volume1/docker/gp-to-yt && docker compose run --rm --entrypoint photos2yt-nightly photos2yt >> docker-data/nightly.log 2>&1
```

Most schedulers can e-mail on a non-zero exit; that is how failures reach you.

### Without Docker

The same script runs with uv. From the repository folder:

```bash
PATH="$PWD/.venv/bin:$PATH" PHOTOS2YT_CONFIG="$PWD/config.yaml" sh scripts/nightly.sh; echo "exit $?"
```

Put `UPLOAD_LIMIT=0` in front for a run that uploads nothing.

### On a Mac: launchd (no Docker, no Homebrew)

`scripts/launchd.py` installs a launchd job (macOS's built-in scheduler) that
runs the line above every night:

```bash
uv run python scripts/launchd.py install            # default: 00:30 Pacific in your time zone, 5 uploads
uv run python scripts/launchd.py install --time 03:45 --limit 3   # or choose
uv run python scripts/launchd.py run-now            # one run right away, to test it
uv run python scripts/launchd.py status             # time, last exit code, last log lines
uv run python scripts/launchd.py uninstall          # stop and remove it
```

- Output goes to `logs/nightly.log` in the repository folder. A run that
  exits non-zero shows a macOS notification ("photos2yt: The nightly run
  failed").
- The job uses this folder's `.venv` and `config.yaml`, and the `ffprobe`
  found on your PATH when you ran `install`. If you move the folder or
  change `ffprobe`, run `install` again.
- If the Mac is asleep at the scheduled time, the job runs when it wakes. If
  it is shut down, that night is skipped. It runs only while you are logged in.
- The time is fixed in your Mac's local time. In a US time zone it stays
  right across daylight-saving changes; elsewhere, re-run `install` after a
  change.
- **macOS privacy:** scheduled jobs cannot read the Desktop, Documents or
  Downloads folders, or external drives, unless allowed. If `run-now` logs
  "Operation not permitted", either move the repository and the Takeout
  folder elsewhere (e.g. `~/photos2yt/`), or allow it in System Settings →
  Privacy & Security → Full Disk Access (add `/bin/sh`; that gives every
  shell script full disk access, so moving the folders is the safer choice).

## 7. The private-upload lock and the API audit

Google forces every upload to **private** when it comes from an API project
that was created after 28 July 2020 and has not passed the YouTube API
compliance audit, whatever privacy you request. photos2yt asks for
`youtube.privacy`, reads back what YouTube applied, and if they differ prints a
**PRIVATE-UPLOAD LOCK** warning. `reconcile` re-checks this for all videos, and
`report` lists every video whose privacy differs from the config under "needs
attention".

Your choices:

- **Accept private** and change videos by hand when you want (below).
- **Apply for the audit** ([YouTube API Services audit and quota
  extension form](https://support.google.com/youtube/contact/yt_api_form)) if
  you need unlisted/public uploads from the API. It takes time and is not
  guaranteed.

(The first real upload for this project kept `unlisted`, so this project does
not appear to be locked; keep an eye on the warning anyway.)

### Changing privacy by hand in YouTube Studio

1. Open [YouTube Studio](https://studio.youtube.com/) → **Content**.
2. Tick the videos (the top checkbox selects the page; use the filter bar, e.g.
   *Visibility: Private*, to find them).
3. **Edit** → **Visibility** → choose Unlisted or Public → **Update videos**.
4. Run `photos2yt reconcile` so the database (and the next `report`) shows the
   new privacy.

Playlists keep their own privacy (set in `rules.yaml` when created); change it
in Studio → Content → Playlists.

## 8. End-to-end test on a 3-video sample

Do this once before the full migration. It uses about 5,000 units (3 uploads,
3 verifications, 1 playlist, a few adds, 1 reconcile call), so run it on a day with quota left.

1. Make a sample folder with three short videos and their sidecars copied
   from your Takeout (keep the `.json` files next to the videos), e.g.
   `~/p2y-sample/Google Photos/`. Include one with a caption and one called
   `IMG_xxxx` without a caption.
2. Make a test config: copy your `config.yaml` to `sample.yaml`, set
   `staging_dir` to the sample folder and `db_path: sample.db`, and use a rules
   file with one rule matching one sample date and a **private** test playlist.
3. Dry runs (no API calls, nothing written):
   ```bash
   uv run photos2yt -c sample.yaml ingest --dry-run
   uv run photos2yt -c sample.yaml ingest
   uv run photos2yt -c sample.yaml upload --dry-run
   ```
   Expect 3 queued videos with readable titles (the caption, or the default
   title for `IMG_xxxx`) and an estimate of 4,803 units.
4. Real run:
   ```bash
   uv run photos2yt -c sample.yaml upload --limit 3
   uv run photos2yt -c sample.yaml curate --dry-run
   uv run photos2yt -c sample.yaml curate
   uv run photos2yt -c sample.yaml reconcile
   uv run photos2yt -c sample.yaml report
   ```
   Expect: 3 uploads, no PRIVATE-UPLOAD LOCK banner (unless your project is
   locked, section 7), the playlist created with the matching video in it,
   reconcile reporting 3 present, and `manifest.csv` with 3 rows and
   `https://youtu.be/...` links.
5. Check in Studio: titles, recording dates (video details → *Recording date*;
   right after upload it can take a while to appear), privacy, and the
   playlist. Re-run `upload`, `curate` and `reconcile`: upload and curate find
   nothing to do and use no quota.
6. Same sample through Docker: set `STAGING_DIR` in `.env` to the sample
   folder, put a `sample.yaml` with the container paths (section 5, with
   `db_path: /data/sample.db`) in `docker-data/config/`, then:
   ```bash
   docker compose run --rm photos2yt -c /config/sample.yaml upload --dry-run
   docker compose run --rm photos2yt -c /config/sample.yaml report
   docker compose run --rm -e PHOTOS2YT_CONFIG=/config/sample.yaml -e UPLOAD_LIMIT=0 \
     --entrypoint photos2yt-nightly photos2yt; echo "exit $?"
   ```
   The nightly run should print every step and end with `exit 0`. (A
   database made by step 3 on your desktop has desktop paths, so ingest it
   again in the container: start with a new `sample.db`.)
7. Delete the three test videos and the test playlist in Studio if you do
   not want them, and delete `sample.db`.

## 9. Cleaning up local originals (`photos2yt cleanup`)

Frees disk space by moving the Takeout copies of videos that are safely on
YouTube out of `staging_dir`, into `paths.uploaded_dir` (default: a folder
named `<staging folder>-uploaded` next to it), keeping their sub-folders.

```bash
uv run photos2yt -c config.yaml reconcile          # confirms which videos exist on YouTube
uv run photos2yt -c config.yaml cleanup --dry-run  # shows what would move, and why others stay
uv run photos2yt -c config.yaml cleanup            # moves them
```

A file is moved only if its video is UPLOADED or PLAYLISTED, `reconcile`
found it on YouTube in the last 7 days (`--max-age-days`), it has no open
reconcile problem (missing, recording date, rejected), and the file still has
the size it had at ingest. Duplicate copies go with their original; a sidecar
`.json` goes when every video using it goes. Each move is logged to
`cleanup_log.csv` next to the database; the database itself is not changed.

`--delete --yes` deletes instead of moving. Prefer moving, check the files,
then delete the `-uploaded` folder yourself; YouTube re-encodes videos, so it
is not a backup of the originals. Cleanup does not run in the nightly job, and
it cannot run in Docker as set up here (staging is mounted read-only).

## 10. Manual offload checklist

There is no API for deleting from Google Photos, and YouTube re-encodes
uploads (it is not a lossless backup), so the last steps are manual.

1. Keep the original Takeout files somewhere safe (another disk or cloud
   storage). YouTube is the viewing copy, not the archive.
2. Run `photos2yt reconcile`, then `photos2yt report`.
3. In `summary.txt`, check: no FAILED or UPLOADING rows, PENDING is 0, and
   "Needs attention" is empty or understood (missing videos, recording dates,
   privacy).
4. In `manifest.csv`, spot-check a few rows: open the URL, compare with the
   original.
5. Back up `queue.db` and `manifest.csv` with the originals.
6. Optionally run `photos2yt cleanup` (section 9) to move the local copies out of the
   staging folder.
7. Only then delete the videos from Google Photos (photos.google.com →
   select → Delete; they stay in the Bin for 60 days).
