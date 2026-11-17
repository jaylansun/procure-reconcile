from alembic import context
from sqlalchemy import create_engine, pool

from app.config.settings import Settings
from app.persistence.database import normalize_database_url
from app.persistence.model_registry import metadata

target_metadata = metadata


def database_url() -> str:
    supplied = context.config.attributes.get("database_url")
    if supplied:
        return normalize_database_url(supplied)
    configured = Settings()
    if not configured.database_url:
        raise RuntimeError("PROCURE_DATABASE_URL is required for migrations")
    return normalize_database_url(configured.database_url.get_secret_value())


if context.is_offline_mode():
    context.configure(
        url=database_url(), target_metadata=target_metadata, literal_binds=True, dialect_opts={}
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(database_url(), poolclass=pool.NullPool, hide_parameters=True)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()
