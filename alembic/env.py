from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import make_url
from alembic import context
from app.core.config import settings
from app.db.base import Base
from app import models  # noqa
config = context.config
if settings.ENV == 'test' and make_url(settings.DATABASE_URL).database != make_url(settings.SYNC_DATABASE_URL).database:
    raise RuntimeError('Test application and migration database names must match before connecting')
config.set_main_option("sqlalchemy.url", settings.SYNC_DATABASE_URL)
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata

def run_migrations_offline():
    context.configure(url=settings.SYNC_DATABASE_URL, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online():
    connectable = engine_from_config(config.get_section(config.config_ini_section), prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
