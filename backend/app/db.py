"""SQLAlchemy engine, session factory and declarative base."""

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    """Declarative base shared by all ORM models."""


def create_db_engine(database_url: str) -> Engine:
    url = make_url(database_url)
    kwargs: dict = {}
    if url.get_backend_name() == "sqlite":
        kwargs["connect_args"] = {"check_same_thread": False}
        if url.database in (None, "", ":memory:"):
            kwargs["poolclass"] = StaticPool
    return create_engine(url, **kwargs)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db(engine: Engine) -> None:
    # Import models so they are registered on Base.metadata.
    from app import models  # noqa: F401

    Base.metadata.create_all(engine)


def get_db(request: Request) -> Iterator[Session]:
    """FastAPI dependency yielding a session bound to the app's engine."""
    session_factory: sessionmaker[Session] = request.app.state.session_factory
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
