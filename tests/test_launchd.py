"""Tests for scripts/launchd.py (macOS schedule). launchctl is faked and the
LaunchAgents folder lives under tmp_path, so these run on any OS."""

from __future__ import annotations

import importlib.util
import plistlib
import subprocess
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("p2y_launchd", ROOT / "scripts" / "launchd.py")
launchd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(launchd)


class FakeLaunchctl:
    def __init__(self, fail: set[str] | None = None):
        self.calls: list[list[str]] = []
        self.fail = fail or set()

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        code = 1 if cmd[1] in self.fail else 0
        return subprocess.CompletedProcess(cmd, code, stdout="", stderr="boom" if code else "")


@pytest.fixture
def repo(tmp_path) -> Path:
    r = tmp_path / "my repo"  # a space on purpose
    (r / ".venv" / "bin").mkdir(parents=True)
    (r / ".venv" / "bin" / "photos2yt").write_text("")
    (r / "scripts").mkdir()
    (r / "config.yaml").write_text("")
    return r


@pytest.mark.parametrize("tz,day,expected", [
    ("America/New_York", date(2026, 11, 11), (3, 30)),
    ("America/New_York", date(2026, 7, 1), (3, 30)),
    ("America/Los_Angeles", date(2026, 11, 11), (0, 30)),
    ("Europe/London", date(2026, 11, 11), (8, 30)),
])
def test_default_time_is_half_past_midnight_pacific(tz, day, expected):
    assert launchd.default_local_time(day, ZoneInfo(tz)) == expected


def test_parse_time():
    assert launchd.parse_time("03:05") == (3, 5)
    for bad in ("3", "24:00", "12:60", "ab:cd"):
        with pytest.raises(Exception):
            launchd.parse_time(bad)


def test_build_plist(repo):
    data = launchd.build_plist(repo, 3, 30, limit=4, ffprobe="/usr/local/bin/ffprobe")
    plistlib.loads(plistlib.dumps(data))  # valid plist
    assert data["Label"] == "com.photos2yt.nightly"
    assert data["StartCalendarInterval"] == {"Hour": 3, "Minute": 30}
    env = data["EnvironmentVariables"]
    assert env["PATH"].split(":")[:2] == [str(repo / ".venv" / "bin"), "/usr/local/bin"]
    assert env["PHOTOS2YT_CONFIG"] == str(repo / "config.yaml")
    assert env["UPLOAD_LIMIT"] == "4"
    assert data["WorkingDirectory"] == str(repo)
    assert data["StandardOutPath"] == str(repo / "logs" / "nightly.log")
    cmd = data["ProgramArguments"]
    assert cmd[:2] == ["/bin/sh", "-c"]
    assert f"'{repo / 'scripts' / 'nightly.sh'}'" in cmd[2]  # quoted: path has a space
    assert "|| /usr/bin/osascript" in cmd[2]


def test_failure_notification_only_on_nonzero_exit(repo, tmp_path):
    """Run the generated shell command with stubs: osascript only on failure."""
    data = launchd.build_plist(repo, 3, 30, limit=1, ffprobe=None)
    marker = tmp_path / "notified"
    for code in (0, 1):
        (repo / "scripts" / "nightly.sh").write_text(f"exit {code}\n")
        cmd = data["ProgramArguments"][2].replace("/usr/bin/osascript", f"touch {marker};: ")
        subprocess.run(["/bin/sh", "-c", cmd], check=False)
        assert marker.exists() == (code == 1)


def test_install_writes_and_loads(repo, tmp_path):
    fake = FakeLaunchctl()
    out: list[str] = []
    home = tmp_path / "home"
    assert launchd.install(3, 30, limit=5, repo=repo, home=home, runner=fake,
                           ffprobe="/opt/x/bin/ffprobe", out=out.append) == 0
    target = home / "Library" / "LaunchAgents" / "com.photos2yt.nightly.plist"
    data = plistlib.loads(target.read_bytes())
    assert data["StartCalendarInterval"] == {"Hour": 3, "Minute": 30}
    assert [c[1] for c in fake.calls] == ["bootout", "bootstrap"]
    assert fake.calls[1][-1] == str(target)
    assert (repo / "logs").is_dir()
    assert any("03:30" in line for line in out)


def test_install_requires_venv_and_config(repo, tmp_path):
    (repo / "config.yaml").unlink()
    out: list[str] = []
    assert launchd.install(3, 30, limit=5, repo=repo, home=tmp_path, runner=FakeLaunchctl(),
                           ffprobe="/x/ffprobe", out=out.append) == 1
    assert "config.yaml" in out[0]


def test_install_warns_without_ffprobe(repo, tmp_path):
    out: list[str] = []
    launchd.install(3, 30, limit=5, repo=repo, home=tmp_path, runner=FakeLaunchctl(),
                    ffprobe=None, out=out.append)
    assert any("ffprobe not found" in line for line in out)


def test_install_reports_bootstrap_failure(repo, tmp_path):
    out: list[str] = []
    assert launchd.install(3, 30, limit=5, repo=repo, home=tmp_path,
                           runner=FakeLaunchctl(fail={"bootstrap"}), ffprobe="/x/ffprobe",
                           out=out.append) == 1
    assert "bootstrap failed" in out[-1]


def test_uninstall_and_status(repo, tmp_path):
    home = tmp_path / "home"
    launchd.install(1, 15, limit=2, repo=repo, home=home, runner=FakeLaunchctl(),
                    ffprobe="/x/ffprobe", out=lambda s: None)
    out: list[str] = []
    launchd.status(home=home, runner=FakeLaunchctl(), out=out.append)
    assert "Time: 01:15 daily; UPLOAD_LIMIT=2; RECONCILE=0; CLEANUP=0" in out
    fake = FakeLaunchctl()
    out = []
    launchd.uninstall(home=home, runner=fake, out=out.append)
    assert not launchd.plist_path(home).exists()
    assert fake.calls[0][1] == "bootout"
    out = []
    launchd.status(home=home, runner=FakeLaunchctl(), out=out.append)
    assert out == ["Not installed."]


def test_reconcile_and_cleanup_switches(repo):
    data = launchd.build_plist(repo, 3, 30, limit=5, ffprobe=None, reconcile=True, cleanup=True)
    assert data["EnvironmentVariables"]["RECONCILE"] == "1"
    assert data["EnvironmentVariables"]["CLEANUP"] == "1"
    off = launchd.build_plist(repo, 3, 30, limit=5, ffprobe=None)
    assert off["EnvironmentVariables"]["RECONCILE"] == "0"
    assert off["EnvironmentVariables"]["CLEANUP"] == "0"
