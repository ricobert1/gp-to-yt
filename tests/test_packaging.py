"""Tests for Task 9 packaging: scripts/nightly.sh and the Docker files.

The nightly script is run with a stub `photos2yt` on PATH that logs its
arguments and exits with a code chosen per subcommand, so no real command,
API or database is involved. Everything is written under tmp_path.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NIGHTLY = ROOT / "scripts" / "nightly.sh"

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")


def run_nightly(tmp_path: Path, codes: dict[str, int] | None = None, **env: str):
    """Run nightly.sh with a stub photos2yt. Returns (exit code, logged calls, output)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "calls.log"
    lines = ['#!/bin/sh', f'echo "$*" >> "{log}"', 'case "$1" in']
    for sub, code in (codes or {}).items():
        lines.append(f'  {sub}) exit {code} ;;')
    lines += ['esac', 'exit 0']
    stub = bin_dir / "photos2yt"
    stub.write_text("\n".join(lines) + "\n")
    stub.chmod(0o755)
    full_env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}", **env}
    proc = subprocess.run(["sh", str(NIGHTLY)], env=full_env, capture_output=True, text=True,
                          cwd=tmp_path, timeout=30)
    calls = log.read_text().splitlines() if log.exists() else []
    return proc.returncode, calls, proc.stdout + proc.stderr


def test_all_steps_succeed(tmp_path):
    code, calls, _ = run_nightly(tmp_path)
    assert code == 0
    assert calls == ["unpack", "ingest", "upload --limit 5", "curate", "report"]


def test_upload_limit_from_env(tmp_path):
    _, calls, _ = run_nightly(tmp_path, UPLOAD_LIMIT="2")
    assert "upload --limit 2" in calls


def test_ingest_failure_stops_the_run(tmp_path):
    code, calls, out = run_nightly(tmp_path, {"ingest": 1})
    assert code == 1
    assert calls == ["unpack", "ingest"]
    assert "not uploading" in out


def test_upload_failure_still_curates_and_reports_but_exits_1(tmp_path):
    code, calls, out = run_nightly(tmp_path, {"upload": 1})
    assert code == 1
    assert calls == ["unpack", "ingest", "upload --limit 5", "curate", "report"]
    assert "FAILED (exit 1): photos2yt upload" in out


def test_curate_failure_exits_1(tmp_path):
    code, calls, _ = run_nightly(tmp_path, {"curate": 1})
    assert code == 1 and calls[-1] == "report"


def test_reconcile_optional(tmp_path):
    _, calls, _ = run_nightly(tmp_path, RECONCILE="1")
    assert calls == ["unpack", "ingest", "upload --limit 5", "curate", "reconcile", "report"]


def test_cleanup_optional_after_reconcile(tmp_path):
    _, calls, _ = run_nightly(tmp_path, RECONCILE="1", CLEANUP="1")
    assert calls == ["unpack", "ingest", "upload --limit 5", "curate", "reconcile", "cleanup",
                     "report"]


def test_unpack_failure_continues_but_exits_1(tmp_path):
    code, calls, out = run_nightly(tmp_path, {"unpack": 1})
    assert code == 1
    assert calls == ["unpack", "ingest", "upload --limit 5", "curate", "report"]


def test_unpack_can_be_switched_off(tmp_path):
    _, calls, _ = run_nightly(tmp_path, UNPACK="0")
    assert calls[0] == "ingest"


def test_dockerignore_keeps_secrets_and_data_out_of_the_image():
    patterns = {
        line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    for required in ("config.yaml", "rules.yaml", "credentials*.json", "token*.json",
                     "*.db", ".git", ".venv", "docker-data", "*.csv"):
        assert required in patterns, f".dockerignore must exclude {required}"


def test_dockerfile_basics():
    text = (ROOT / "Dockerfile").read_text()
    assert text.startswith("#") and "FROM python:3.12-slim" in text
    assert "ffmpeg" in text
    assert "uv sync --frozen --no-dev" in text
    assert "USER 1000:1000" in text
    assert 'ENTRYPOINT ["photos2yt"]' in text
    # never copy the whole context (would pull in anything .dockerignore misses)
    assert "COPY . " not in text


def test_compose_mounts_staging_read_only():
    text = (ROOT / "docker-compose.yml").read_text()
    assert ":/staging:ro" in text
    assert ":/config\"" in text and ":/data\"" in text
