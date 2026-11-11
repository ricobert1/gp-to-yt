"""Schedule the nightly job on macOS with launchd (no Docker, no Homebrew).

Run from the repository folder:

    uv run python scripts/launchd.py install [--time HH:MM] [--limit N]
    uv run python scripts/launchd.py run-now      # start one run immediately
    uv run python scripts/launchd.py status
    uv run python scripts/launchd.py uninstall

`install` writes ~/Library/LaunchAgents/com.photos2yt.nightly.plist and loads
it. The job runs scripts/nightly.sh (ingest -> upload -> curate -> report)
with this repository's .venv and config.yaml, appends output to
logs/nightly.log, and shows a macOS notification if the run exits non-zero.

Default time: 00:30 Pacific (just after the YouTube quota reset), converted
to this Mac's time zone at install time. If the Mac is asleep at that time,
launchd runs the job when it wakes; if it is shut down, that night is skipped.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from datetime import date, datetime, time
from pathlib import Path
from typing import Callable, Optional, Sequence
from zoneinfo import ZoneInfo

LABEL = "com.photos2yt.nightly"
REPO = Path(__file__).resolve().parent.parent
PACIFIC = ZoneInfo("America/Los_Angeles")
DEFAULT_PACIFIC_TIME = time(0, 30)
BASE_PATH = ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


def default_local_time(on: Optional[date] = None, local_tz=None) -> tuple[int, int]:
    """00:30 Pacific on ``on`` (default today) as (hour, minute) in local time."""
    day = on or date.today()
    pacific = datetime.combine(day, DEFAULT_PACIFIC_TIME, tzinfo=PACIFIC)
    local = pacific.astimezone(local_tz) if local_tz is not None else pacific.astimezone()
    return local.hour, local.minute


def parse_time(value: str) -> tuple[int, int]:
    try:
        hh, mm = value.split(":")
        hour, minute = int(hh), int(mm)
    except ValueError:
        raise argparse.ArgumentTypeError(f"time must be HH:MM (24-hour), got {value!r}")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise argparse.ArgumentTypeError(f"time must be HH:MM (24-hour), got {value!r}")
    return hour, minute


def build_plist(repo: Path, hour: int, minute: int, *, limit: int,
                ffprobe: Optional[str], config: Optional[Path] = None) -> dict:
    """The launchd job definition (a dict for plistlib)."""
    config = config or repo / "config.yaml"
    log = repo / "logs" / "nightly.log"
    path_dirs = [str(repo / ".venv" / "bin")]
    if ffprobe:
        path_dirs.append(str(Path(ffprobe).parent))
    path_dirs += [d for d in BASE_PATH if d not in path_dirs]
    notify = ('display notification "The nightly run failed; see logs/nightly.log" '
              'with title "photos2yt"')
    command = (f"/bin/sh {shlex.quote(str(repo / 'scripts' / 'nightly.sh'))} "
               f"|| /usr/bin/osascript -e {shlex.quote(notify)}")
    return {
        "Label": LABEL,
        "ProgramArguments": ["/bin/sh", "-c", command],
        "WorkingDirectory": str(repo),
        "EnvironmentVariables": {
            "PATH": ":".join(path_dirs),
            "PHOTOS2YT_CONFIG": str(config),
            "UPLOAD_LIMIT": str(limit),
        },
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "RunAtLoad": False,
    }


def plist_path(home: Optional[Path] = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _run(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), capture_output=True, text=True)


def install(hour: int, minute: int, *, limit: int, repo: Path = REPO,
            home: Optional[Path] = None, runner: Runner = _run,
            ffprobe: Optional[str] = "auto", out=print) -> int:
    if ffprobe == "auto":
        ffprobe = shutil.which("ffprobe")
    if not (repo / ".venv" / "bin" / "photos2yt").exists():
        out("No .venv with photos2yt in this folder; run `uv sync --extra dev` first.")
        return 1
    if not (repo / "config.yaml").exists():
        out(f"No config.yaml in {repo}; the job reads that file.")
        return 1
    if ffprobe is None:
        out("WARNING: ffprobe not found on PATH; ingest will fail in the scheduled job.")
    (repo / "logs").mkdir(exist_ok=True)
    target = plist_path(home)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = build_plist(repo, hour, minute, limit=limit, ffprobe=ffprobe)
    runner(["launchctl", "bootout", f"{_domain()}/{LABEL}"])  # unload an older copy; may fail
    target.write_bytes(plistlib.dumps(data))
    result = runner(["launchctl", "bootstrap", _domain(), str(target)])
    if result.returncode != 0:
        out(f"launchctl bootstrap failed: {(result.stderr or result.stdout).strip()}")
        return 1
    out(f"Installed {target}")
    out(f"Runs every day at {hour:02d}:{minute:02d} (this Mac's time), "
        f"uploading up to {limit} video(s).")
    out(f"Log: {repo / 'logs' / 'nightly.log'}")
    out("Test it now with: uv run python scripts/launchd.py run-now")
    return 0


def uninstall(*, home: Optional[Path] = None, runner: Runner = _run, out=print) -> int:
    target = plist_path(home)
    runner(["launchctl", "bootout", f"{_domain()}/{LABEL}"])
    if target.exists():
        target.unlink()
        out(f"Removed {target}; the nightly job will not run again.")
    else:
        out("Not installed.")
    return 0


def run_now(*, runner: Runner = _run, out=print) -> int:
    result = runner(["launchctl", "kickstart", f"{_domain()}/{LABEL}"])
    if result.returncode != 0:
        out("Could not start it (is it installed?): "
            f"{(result.stderr or result.stdout).strip()}")
        return 1
    out(f"Started. Follow it with: tail -f {REPO / 'logs' / 'nightly.log'}")
    return 0


def status(*, home: Optional[Path] = None, runner: Runner = _run, out=print) -> int:
    target = plist_path(home)
    if not target.exists():
        out("Not installed.")
        return 0
    data = plistlib.loads(target.read_bytes())
    when = data["StartCalendarInterval"]
    out(f"Installed: {target}")
    out(f"Time: {when['Hour']:02d}:{when['Minute']:02d} daily; "
        f"UPLOAD_LIMIT={data['EnvironmentVariables']['UPLOAD_LIMIT']}")
    result = runner(["launchctl", "print", f"{_domain()}/{LABEL}"])
    loaded = result.returncode == 0
    out(f"Loaded in launchd: {'yes' if loaded else 'no'}")
    for line in (result.stdout or "").splitlines():
        if "last exit code" in line:
            out(f"  {line.strip()}")
    log = Path(data["StandardOutPath"])
    if log.exists():
        lines = log.read_text(errors="replace").splitlines()[-5:]
        out(f"Last log lines ({log}):")
        for line in lines:
            out(f"  {line}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="launchd.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="action", required=True)
    p_install = sub.add_parser("install", help="write and load the launchd job")
    p_install.add_argument("--time", type=parse_time, default=None,
                           help="HH:MM in this Mac's time (default: 00:30 Pacific converted)")
    p_install.add_argument("--limit", type=int, default=5, help="uploads per run (default 5)")
    sub.add_parser("uninstall", help="unload and delete the launchd job")
    sub.add_parser("run-now", help="start one run immediately")
    sub.add_parser("status", help="show schedule, last exit code and last log lines")
    args = parser.parse_args(argv)

    if sys.platform != "darwin":
        print("launchd is macOS only; on Linux use cron (see README, section 6).")
        return 1
    if args.action == "install":
        if args.limit < 0:
            parser.error("--limit must be zero or positive")
        hour, minute = args.time if args.time else default_local_time()
        return install(hour, minute, limit=args.limit)
    if args.action == "uninstall":
        return uninstall()
    if args.action == "run-now":
        return run_now()
    return status()


if __name__ == "__main__":
    sys.exit(main())
