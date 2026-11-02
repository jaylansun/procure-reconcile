from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


def normalize_database_url(url: str) -> str:
    if url.startswith("postgresql+psycopg://"):
        return url
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    raise ValueError("Only PostgreSQL is supported")


class Database:
    def __init__(self, url: str | None):
        self.engine: Engine | None = None
        self.sessions: sessionmaker[Session] | None = None
        if url:
            self.engine = create_engine(
                normalize_database_url(url),
                pool_size=5,
                max_overflow=5,
                pool_timeout=5,
                pool_pre_ping=True,
                connect_args={"connect_timeout": 3},
                hide_parameters=True,
            )
            self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    def close(self) -> None:
        if self.engine:
            self.engine.dispose()
