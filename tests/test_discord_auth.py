"""Проверка членства в гильдии во всех трёх режимах."""

from __future__ import annotations

import httpx
import pytest

from app.auth.discord import DiscordAuthError, DiscordClient, DiscordUser
from app.config import Settings

GUILD = "555"
USER = DiscordUser(id="42", username="tester", global_name="Тестер", avatar=None, discriminator="0")


def make_settings(**overrides: object) -> Settings:
    base = {
        "secret_key": "x",
        "discord_auth_enabled": True,
        "discord_client_id": "1",
        "discord_client_secret": "2",
        "discord_guild_id": GUILD,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def client_with(settings: Settings, handler) -> DiscordClient:
    transport = httpx.MockTransport(handler)
    return DiscordClient(settings, client=httpx.AsyncClient(transport=transport))


async def test_member_via_oauth_is_allowed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(f"/users/@me/guilds/{GUILD}/member")
        return httpx.Response(200, json={"roles": ["10", "20"], "nick": "Кицунэ"})

    membership = await client_with(make_settings(), handler).check_membership(USER, "token")
    assert membership.allowed is True
    assert membership.roles == ["10", "20"]
    assert membership.nick == "Кицунэ"


async def test_non_member_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Unknown Guild"})

    membership = await client_with(make_settings(), handler).check_membership(USER, "token")
    assert membership.allowed is False
    assert "не состоит" in membership.reason


async def test_falls_back_to_guild_list_without_member_scope() -> None:
    """Без scope guilds.members.read членство определяется по списку серверов."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/member"):
            return httpx.Response(403, json={"message": "Missing Access"})
        return httpx.Response(200, json=[{"id": GUILD}, {"id": "999"}])

    membership = await client_with(make_settings(), handler).check_membership(USER, "token")
    assert membership.allowed is True
    assert membership.roles == []


async def test_role_requirement_blocks_wrong_role() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"roles": ["77"]})

    settings = make_settings(discord_required_role_ids="10,20")
    membership = await client_with(settings, handler).check_membership(USER, "token")
    assert membership.allowed is False
    assert "роли" in membership.reason


async def test_role_requirement_allows_right_role() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"roles": ["20", "77"]})

    settings = make_settings(discord_required_role_ids="10,20")
    membership = await client_with(settings, handler).check_membership(USER, "token")
    assert membership.allowed is True


async def test_bot_mode_uses_bot_token() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["Authorization"]
        seen["path"] = request.url.path
        return httpx.Response(200, json={"roles": ["10"]})

    settings = make_settings(discord_guild_check="bot", discord_bot_token="botsecret")
    membership = await client_with(settings, handler).check_membership(USER, "token")
    assert membership.allowed is True
    assert seen["auth"] == "Bot botsecret"
    assert seen["path"] == f"/api/v10/guilds/{GUILD}/members/42"


async def test_check_off_skips_the_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - не должен вызваться
        raise AssertionError("В режиме off запросов в Discord быть не должно")

    settings = make_settings(discord_guild_check="off")
    membership = await client_with(settings, handler).check_membership(USER, "token")
    assert membership.allowed is True
    assert membership.checked is False


async def test_rate_limit_is_reported_clearly() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"retry_after": 5})

    with pytest.raises(DiscordAuthError, match="rate limit"):
        await client_with(make_settings(), handler).check_membership(USER, "token")


def test_avatar_url_falls_back_to_default() -> None:
    assert USER.avatar_url().startswith("https://cdn.discordapp.com/embed/avatars/")
    with_avatar = DiscordUser(id="42", username="t", avatar="a_deadbeef")
    assert with_avatar.avatar_url().endswith(".gif?size=128")
