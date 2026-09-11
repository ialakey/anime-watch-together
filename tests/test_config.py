"""Настройки: переключатель Discord и производные значения."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_guest_mode_by_default() -> None:
    """Без настроек сайт поднимается и пускает гостей — ничего заполнять не нужно."""
    settings = Settings(secret_key="x")
    assert settings.discord_auth_enabled is False
    assert settings.catalog_source == "animego"
    assert settings.tracking_enabled is True


def test_discord_requires_credentials() -> None:
    with pytest.raises(ValidationError) as error:
        Settings(secret_key="x", discord_auth_enabled=True)
    message = str(error.value)
    assert "DISCORD_CLIENT_ID" in message
    assert "DISCORD_GUILD_ID" in message


def test_discord_bot_mode_requires_token() -> None:
    with pytest.raises(ValidationError, match="DISCORD_BOT_TOKEN"):
        Settings(
            secret_key="x",
            discord_auth_enabled=True,
            discord_client_id="1",
            discord_client_secret="2",
            discord_guild_id="3",
            discord_guild_check="bot",
        )


def test_roles_need_a_real_check() -> None:
    with pytest.raises(ValidationError, match="DISCORD_REQUIRED_ROLE_IDS"):
        Settings(
            secret_key="x",
            discord_auth_enabled=True,
            discord_client_id="1",
            discord_client_secret="2",
            discord_guild_check="off",
            discord_required_role_ids="10,20",
        )


def test_guild_check_off_asks_for_the_minimum() -> None:
    settings = Settings(
        secret_key="x",
        discord_auth_enabled=True,
        discord_client_id="1",
        discord_client_secret="2",
        discord_guild_check="off",
    )
    assert settings.oauth_scopes == ["identify"]


def test_oauth_mode_asks_for_member_scope() -> None:
    settings = Settings(
        secret_key="x",
        discord_auth_enabled=True,
        discord_client_id="1",
        discord_client_secret="2",
        discord_guild_id="3",
    )
    assert settings.oauth_scopes == ["identify", "guilds", "guilds.members.read"]
    assert settings.redirect_uri.endswith("/auth/discord/callback")


def test_id_lists_are_parsed_from_csv() -> None:
    settings = Settings(secret_key="x", discord_admin_ids=" 1 , 2 ,, 3 ")
    assert settings.discord_admin_ids == ["1", "2", "3"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", []),
        ("480455883655217152", ["480455883655217152"]),
        ("480455883655217152,1361398846822813946", ["480455883655217152", "1361398846822813946"]),
        (" 1 , 2 ,, 3 ", ["1", "2", "3"]),
    ],
)
def test_id_lists_are_parsed_from_env(monkeypatch, raw: str, expected: list[str]) -> None:
    """Через окружение, а не аргументом: pydantic-settings разбирает списки иначе.

    Без NoDecode он пробует json.loads и падает на всём, кроме одного числа, —
    в том числе на пустой строке, которая стоит в .env.example.
    """
    monkeypatch.setenv("DISCORD_ADMIN_IDS", raw)
    monkeypatch.setenv("DISCORD_REQUIRED_ROLE_IDS", raw)

    settings = Settings(secret_key="x")

    assert settings.discord_admin_ids == expected
    assert settings.discord_required_role_ids == expected


def test_base_url_loses_trailing_slash() -> None:
    settings = Settings(secret_key="x", base_url="https://example.com/")
    assert settings.base_url == "https://example.com"
    assert settings.redirect_uri == "https://example.com/auth/discord/callback"
