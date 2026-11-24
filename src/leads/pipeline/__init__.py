"""Processing pipeline: raw zone -> normalised -> filtered -> matched leads."""

from __future__ import annotations

import uuid


def process_run(run_id: uuid.UUID | str) -> dict[str, int]:
    """Process one collect run end to end and upsert into PostgreSQL.

    Steps (each lives in its own module):

    1. Read ``data/raw/source=*/run=<run_id>/*.parquet`` with Polars.
    2. :func:`leads.pipeline.normalise.normalise_frame` + country filter.
    3. :func:`leads.pipeline.match.cluster_records` -> one lead per business.
    4. :func:`leads.pipeline.age.resolve_age` + :func:`leads.pipeline.age.keep_lead`.
    5. Upsert ``leads`` and ``lead_sources`` (reuse existing lead_id when any source record
       already belongs to a lead), set ``first_seen`` / ``last_seen``.
    6. :func:`leads.pipeline.sanctions.screen_leads` sets ``sanctions_hit``.

    TODO(build step 2): implement steps 1, 2 and 5 for Overture + Foursquare test countries.
    """
    raise NotImplementedError("pipeline.process_run (build step 2)")
