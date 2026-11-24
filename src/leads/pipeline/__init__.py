"""Processing pipeline: raw zone -> normalised -> filtered -> matched leads."""

from __future__ import annotations

import uuid
from typing import Any


def process_run(run_id: uuid.UUID | str) -> dict[str, Any]:
    """Process one collect run end to end and upsert into PostgreSQL.

    See :mod:`leads.pipeline.process`. Sanctions screening runs as its own step.
    """
    from leads.pipeline.process import process_run as _process_run

    return _process_run(run_id)
