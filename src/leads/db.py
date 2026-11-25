"""Database engine, sessions and run bookkeeping."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from leads.settings import get_settings


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    return create_engine(get_settings().database_url, pool_pre_ping=True)


@lru_cache(maxsize=1)
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session: commits on success, rolls back on error."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def lock_leads(session: Session) -> None:
    """Wait until no other job changes leads in bulk; held until this transaction ends.

    Without it two jobs updating the same leads in a different order deadlock.
    """
    session.execute(text("SELECT pg_advisory_xact_lock(748302)"))


def check_connection() -> dict[str, str]:
    """Return PostgreSQL / PostGIS versions. Raises if the DB is unreachable."""
    with get_engine().connect() as conn:
        info = {"postgres": str(conn.execute(text("select version()")).scalar_one())}
        try:
            info["postgis"] = str(conn.execute(text("select postgis_lib_version()")).scalar_one())
        except DBAPIError:
            info["postgis"] = "not installed (run: uv run alembic upgrade head)"
        return info


def start_run(flow: str) -> uuid.UUID:
    """Insert a ``runs`` row with status 'running' and return its id."""
    from leads.models import Run

    run = Run(run_id=uuid.uuid4(), flow=flow, started_at=datetime.now(UTC), status="running")
    with session_scope() as session:
        session.add(run)
    return run.run_id


def save_counts(run_id: uuid.UUID, counts: dict[str, Any]) -> None:
    """Store the counters of a run that is still going."""
    from leads.models import Run

    with session_scope() as session:
        if run := session.get(Run, run_id):
            run.counts = dict(counts)


def finish_run(run_id: uuid.UUID, status: str, counts: dict[str, Any] | None = None) -> None:
    """Mark a run finished ('success' / 'failed') and store its counters."""
    from leads.models import Run

    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            return
        run.status = status
        run.finished_at = datetime.now(UTC)
        run.counts = counts or {}
