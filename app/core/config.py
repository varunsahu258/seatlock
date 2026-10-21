from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_PAYMENT_WEBHOOK_SECRET = "dev-payment-secret-change-me"


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://seatlock:seatlock@localhost:5432/seatlock"
    redis_url: str = "redis://localhost:6379/0"
    environment: str = "development"
    admin_token: str | None = None
    hold_ttl_seconds: int = 300
    payment_webhook_secret: str = DEFAULT_PAYMENT_WEBHOOK_SECRET
    hold_rate_capacity: int = 20
    hold_rate_per_second: float = 10.0
    queue_join_rate_capacity: int = 5
    queue_join_rate_per_second: float = 2.0
    admission_ttl_seconds: int = 600

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()


def validate_runtime_settings() -> None:
    if (
        settings.environment.lower() == "production"
        and settings.payment_webhook_secret == DEFAULT_PAYMENT_WEBHOOK_SECRET
    ):
        raise RuntimeError("PAYMENT_WEBHOOK_SECRET must be changed when ENVIRONMENT=production")
