import pytest

from app.core.config import DEFAULT_PAYMENT_WEBHOOK_SECRET, settings, validate_runtime_settings
from app.main import app


async def test_admin_routes_remain_open_when_token_is_unset(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "admin_token", None)

    response = await client.post("/admin/events", json={"name": "Local event"})

    assert response.status_code == 201


async def test_admin_routes_require_matching_bearer_token_when_configured(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "admin_token", "test-admin-token")
    payload = {"name": "Protected event"}

    missing = await client.post("/admin/events", json=payload)
    invalid = await client.post(
        "/admin/events", json=payload, headers={"Authorization": "Bearer wrong-token"}
    )
    valid = await client.post(
        "/admin/events", json=payload, headers={"Authorization": "Bearer test-admin-token"}
    )

    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert valid.status_code == 201


async def test_startup_rejects_default_payment_webhook_secret_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "payment_webhook_secret", DEFAULT_PAYMENT_WEBHOOK_SECRET)

    with pytest.raises(RuntimeError, match="PAYMENT_WEBHOOK_SECRET must be changed"):
        async with app.router.lifespan_context(app):
            pass


def test_production_accepts_replaced_payment_webhook_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "payment_webhook_secret", "test-random-secret")

    validate_runtime_settings()


def test_development_accepts_default_payment_webhook_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "payment_webhook_secret", DEFAULT_PAYMENT_WEBHOOK_SECRET)

    validate_runtime_settings()
