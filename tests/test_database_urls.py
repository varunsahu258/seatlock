from app.core.database import normalize_database_url


def test_neon_postgres_url_is_supported_by_asyncpg() -> None:
    url = normalize_database_url(
        "postgresql://user:pass@example.neon.tech/db"
        "?sslmode=require&channel_binding=require&connect_timeout=30"
    )

    assert url.drivername == "postgresql+asyncpg"
    assert url.query["ssl"] == "require"
    assert "sslmode" not in url.query
    assert "channel_binding" not in url.query
    assert url.query["connect_timeout"] == "30"


def test_postgres_scheme_is_normalized_for_asyncpg() -> None:
    url = normalize_database_url("postgres://user:pass@localhost:5432/seatlock")

    assert url.drivername == "postgresql+asyncpg"
