"""Database engine and session handling for the world model store."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator, Optional

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

_BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_REPO_ROOT = os.path.dirname(_BACKEND_DIR)
DEFAULT_DB_PATH = os.path.join(_REPO_ROOT, "data", "scenarioiq.db")
MIGRATIONS_DIR = os.path.join(_BACKEND_DIR, "migrations")

_engine: Optional[Engine] = None
_SessionLocal: Optional[sessionmaker] = None


def database_url() -> str:
    return os.environ.get("WORLD_DB_URL") or f"sqlite:///{DEFAULT_DB_PATH}"


def _enable_sqlite_fk(dbapi_conn, _record):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


def get_engine(url: Optional[str] = None) -> Engine:
    global _engine, _SessionLocal
    if _engine is None or (url and str(_engine.url) != url):
        url = url or database_url()
        if url.startswith("sqlite:///"):
            path = url[len("sqlite:///"):]
            if path and path != ":memory:":
                os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        _engine = create_engine(url, future=True)
        if _engine.dialect.name == "sqlite":
            event.listen(_engine, "connect", _enable_sqlite_fk)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    get_engine()
    assert _SessionLocal is not None
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def upgrade_to_head(url: Optional[str] = None) -> None:
    """Apply Alembic migrations. Called at app start and by tests."""
    from alembic import command
    from alembic.config import Config as AlembicConfig

    cfg = AlembicConfig(os.path.join(_BACKEND_DIR, "alembic.ini"))
    cfg.set_main_option("script_location", MIGRATIONS_DIR)
    cfg.set_main_option("sqlalchemy.url", url or database_url())
    cfg.attributes["skip_logging"] = True
    get_engine(url or database_url())
    command.upgrade(cfg, "head")


def reset_engine() -> None:
    """Drop the cached engine (tests switch databases)."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
