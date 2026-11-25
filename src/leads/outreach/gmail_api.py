"""Gmail API calls with patience for rate limits."""

from __future__ import annotations

import time
from typing import Any

ATTEMPTS = 5
BASE_WAIT = 2.0


def _retryable(exc: Exception, *, idempotent: bool) -> bool:
    from googleapiclient.errors import HttpError

    if not isinstance(exc, HttpError):
        return False
    status = int(exc.resp.status)
    if status == 429 or (status == 403 and b"ateLimitExceeded" in exc.content):
        return True  # refused before anything happened
    return idempotent and status >= 500


def execute(request: Any, *, idempotent: bool = True) -> Any:
    """Run a Gmail request, waiting longer each time Google asks to slow down.

    A call that creates something is not repeated after a server error: it may have worked.
    """
    for attempt in range(ATTEMPTS):
        try:
            return request.execute()
        except Exception as exc:
            if attempt == ATTEMPTS - 1 or not _retryable(exc, idempotent=idempotent):
                raise
            time.sleep(BASE_WAIT * 2**attempt)
    raise AssertionError("unreachable")
