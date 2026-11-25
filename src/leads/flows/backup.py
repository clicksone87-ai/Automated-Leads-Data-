"""Backup check flow (03:30 local).

The ``backup`` container runs ``pg_dump`` at 03:00 into ./backups (the worker image has
no PostgreSQL client). This flow verifies a fresh, non-empty dump exists so a broken
backup shows up as a failed run in the Prefect UI.
"""

from __future__ import annotations

import time
from pathlib import Path

from prefect import flow, get_run_logger

from leads.alerts import on_flow_failure
from leads.db import finish_run, start_run
from leads.settings import get_settings

MAX_AGE_HOURS = 26
MIN_SIZE_BYTES = 1024


def latest_backup(backups_dir: Path) -> Path | None:
    dumps = sorted(backups_dir.glob("leads_*.dump"), key=lambda p: p.stat().st_mtime)
    return dumps[-1] if dumps else None


def verify_backup(backups_dir: Path, *, now: float | None = None) -> Path:
    """Return the newest dump, or raise if it is missing, stale or tiny."""
    newest = latest_backup(backups_dir)
    if newest is None:
        raise RuntimeError(f"No backups found in {backups_dir}")
    age_h = ((now or time.time()) - newest.stat().st_mtime) / 3600
    if age_h > MAX_AGE_HOURS:
        raise RuntimeError(f"Newest backup {newest.name} is {age_h:.1f} h old")
    if newest.stat().st_size < MIN_SIZE_BYTES:
        raise RuntimeError(f"Newest backup {newest.name} is only {newest.stat().st_size} bytes")
    return newest


@flow(
    name="backup-check",
    log_prints=True,
    on_failure=[on_flow_failure],
    on_crashed=[on_flow_failure],
)
def backup_check() -> str:
    run_id = start_run("backup-check")
    try:
        path = verify_backup(get_settings().backups_dir)
    except Exception:
        finish_run(run_id, "failed")
        raise
    finish_run(run_id, "success", {"bytes": path.stat().st_size})
    get_run_logger().info("backup ok: %s (%d bytes)", path.name, path.stat().st_size)
    return path.name
