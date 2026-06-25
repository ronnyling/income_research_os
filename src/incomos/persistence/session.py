"""Database session context manager for IncomOS persistence.

Provides a thin synchronous context manager around SQLAlchemy.  The engine
is initialised once (lazy) and reused across calls.

Graceful degradation:
  If the database is unreachable or DATABASE_URL is not configured, db_session()
  yields None and logs a warning.  All callers must handle the None case —
  the pipeline runs in read-only / in-memory mode when DB is unavailable.

Usage:
    from incomos.persistence.session import db_session

    with db_session() as conn:
        if conn is None:
            logger.warning("DB unavailable — skipping persistence")
        else:
            queries.upsert_stock(conn, record)
            conn.commit()
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import Connection

from incomos.core.exceptions import DatabaseUnavailableError

logger = logging.getLogger(__name__)

_engine = None


def _get_or_create_engine():
    global _engine
    if _engine is not None:
        return _engine
    try:
        from incomos.persistence.db import get_engine
        _engine = get_engine()
        return _engine
    except Exception as exc:
        raise DatabaseUnavailableError(str(exc)) from exc


@contextmanager
def db_session() -> Generator[Connection, None, None]:
    """Context manager yielding an open SQLAlchemy Connection.

    Raises DatabaseUnavailableError if the database cannot be reached.
    The connection is committed on clean exit and rolled back on exception.
    """
    engine = _get_or_create_engine()  # raises DatabaseUnavailableError if unavailable
    try:
        with engine.connect() as conn:
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    except DatabaseUnavailableError:
        raise
    except Exception as exc:
        raise DatabaseUnavailableError(str(exc)) from exc
