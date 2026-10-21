from collections.abc import AsyncIterator

from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings


def normalize_database_url(database_url: str) -> URL:
    """Normalize common hosted PostgreSQL URLs for SQLAlchemy's asyncpg driver."""
    url = make_url(database_url)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+asyncpg")

    query = dict(url.query)
    sslmode = query.pop("sslmode", None)
    query.pop("channel_binding", None)
    if sslmode and "ssl" not in query:
        query["ssl"] = sslmode
    return url.set(query=query)


engine = create_async_engine(normalize_database_url(settings.database_url), pool_pre_ping=True)
SessionFactory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        yield session
