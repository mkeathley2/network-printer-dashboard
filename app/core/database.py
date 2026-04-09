"""
SQLAlchemy engine, session factory, and declarative base.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Generator

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


# Flask-SQLAlchemy instance (bound to Base above)
db = SQLAlchemy(model_class=Base)

# Standalone engine + session factory for use outside Flask request context
# (e.g. background scheduler jobs). Populated by init_standalone_engine().
_StandaloneSession: sessionmaker | None = None


def init_standalone_engine(db_url: str) -> None:
    """Create a standalone SQLAlchemy engine for background jobs."""
    global _StandaloneSession
    engine = create_engine(
        db_url,
        pool_recycle=3600,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )
    _StandaloneSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)


@contextmanager
def get_db() -> Generator[Session, None, None]:
    """Context manager yielding a standalone DB session (for scheduler jobs)."""
    if _StandaloneSession is None:
        raise RuntimeError("Standalone engine not initialized. Call init_standalone_engine() first.")
    session = _StandaloneSession()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
