"""SQLAlchemy engine and synchronous session management."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool


class Database:
    """Own the process-wide engine and create short-lived repository sessions."""

    def __init__(self, database_url: str) -> None:
        url = make_url(database_url)
        engine_options: dict[str, object] = {"pool_pre_ping": True}
        if url.get_backend_name() == "sqlite":
            engine_options["connect_args"] = {"check_same_thread": False}
            if url.database in {None, "", ":memory:"}:
                engine_options["poolclass"] = StaticPool

        self.engine: Engine = create_engine(database_url, **engine_options)
        self.session_factory = sessionmaker(
            bind=self.engine,
            class_=Session,
            expire_on_commit=False,
            autoflush=False,
        )

    @property
    def dialect_name(self) -> str:
        return self.engine.dialect.name

    @property
    def is_postgresql(self) -> bool:
        return self.dialect_name == "postgresql"

    @property
    def is_sqlite(self) -> bool:
        return self.dialect_name == "sqlite"

    @contextmanager
    def session(self) -> Iterator[Session]:
        with self.session_factory() as session:
            yield session

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        with self.session_factory() as session:
            with session.begin():
                yield session

    def ping(self) -> None:
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))

    def close(self) -> None:
        self.engine.dispose()


__all__ = ["Database"]
