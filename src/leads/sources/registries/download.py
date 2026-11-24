"""Downloads of large registry files: resumable, atomic, reused while fresh."""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import structlog

log = structlog.get_logger(__name__)

CHUNK_BYTES = 1 << 20
FRESH_DAYS = 25.0  # registries are collected monthly
RETRIES = 8


def is_fresh(path: Path, *, max_age_days: float = FRESH_DAYS) -> bool:
    return path.exists() and time.time() - path.stat().st_mtime < max_age_days * 86_400


def _fetch_once(client: httpx.Client, url: str, part: Path) -> None:
    done = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={done}-"} if done else {}
    with client.stream("GET", url, headers=headers) as response:
        if done and response.status_code == 416:  # nothing left to fetch
            return
        response.raise_for_status()
        resumed = response.status_code == 206
        encoded = "Content-Encoding" in response.headers
        length = None if encoded else response.headers.get("Content-Length")
        expected = int(length) + (done if resumed else 0) if length else None
        with part.open("ab" if resumed else "wb") as fh:
            for chunk in response.iter_bytes(CHUNK_BYTES):
                fh.write(chunk)
    if expected is not None and part.stat().st_size < expected:
        raise httpx.ReadError(f"incomplete: {part.stat().st_size} of {expected} bytes")


def download(
    url: str,
    dest: Path,
    *,
    max_age_days: float = FRESH_DAYS,
    client: httpx.Client | None = None,
    pause_seconds: float = 5.0,
) -> Path:
    """Download ``url`` to ``dest``, resuming a partial file. Reuses a fresh ``dest``."""
    if is_fresh(dest, max_age_days=max_age_days):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    if part.exists() and not is_fresh(part, max_age_days=1):
        part.unlink()  # the published file has probably changed since
    own = client or httpx.Client(
        timeout=120.0, follow_redirects=True, headers={"Accept-Encoding": "identity"}
    )
    try:
        for attempt in range(1, RETRIES + 1):
            try:
                _fetch_once(own, url, part)
                break
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else 0
                if attempt == RETRIES or (400 <= status < 500 and status != 429):
                    raise
                log.warning("download_retry", url=url, attempt=attempt, error=str(exc))
                time.sleep(pause_seconds * attempt)
    finally:
        if client is None:
            own.close()
    part.replace(dest)
    return dest


def get_text(url: str, *, timeout: float = 60.0) -> str:
    response = httpx.get(url, timeout=timeout, follow_redirects=True)
    response.raise_for_status()
    return response.text
