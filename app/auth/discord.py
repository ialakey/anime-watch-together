"""Клиент Discord OAuth2 и проверка членства в гильдии.

Схема входа:

1. ``/auth/login`` -> редирект на Discord с ``state`` в сессии;
2. Discord возвращает пользователя на ``/auth/discord/callback?code=...``;
3. код меняется на access_token, по нему читается профиль;
4. профиль проверяется на членство в гильдии (и, опционально, на роли).

Проверка членства умеет три режима (``DISCORD_GUILD_CHECK``):

``oauth``
    Спрашиваем Discord от имени самого пользователя. Нужен scope
    ``guilds.members.read`` — тогда одним запросом получаем и факт членства,
    и список ролей. Если пользователь такой scope не выдал, откатываемся на
    список гильдий (``guilds``) — членство видно, роли нет.
``bot``
    Спрашиваем бота, который сам состоит в гильдии. Роли видны всегда,
    от пользователя нужен только ``identify``.
``off``
    Пускаем любого владельца Discord-аккаунта.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import Settings

log = logging.getLogger(__name__)

CDN_BASE = "https://cdn.discordapp.com"


class DiscordAuthError(Exception):
    """Ошибка, которую не стыдно показать пользователю."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class AccessDenied(DiscordAuthError):
    """Аккаунт валиден, но доступ на сайт ему не положен."""

    def __init__(self, message: str) -> None:
        super().__init__(message, status=403)


@dataclass(frozen=True)
class DiscordUser:
    """Профиль Discord в том объёме, который нужен сайту."""

    id: str
    username: str
    global_name: str | None = None
    avatar: str | None = None
    discriminator: str | None = None

    @property
    def display_name(self) -> str:
        return self.global_name or self.username

    def avatar_url(self, size: int = 128) -> str:
        if self.avatar:
            ext = "gif" if self.avatar.startswith("a_") else "png"
            return f"{CDN_BASE}/avatars/{self.id}/{self.avatar}.{ext}?size={size}"
        # дефолтная аватарка: для новых аккаунтов считается по id, для старых — по дискриминатору
        if self.discriminator and self.discriminator != "0":
            index = int(self.discriminator) % 5
        else:
            index = (int(self.id) >> 22) % 6
        return f"{CDN_BASE}/embed/avatars/{index}.png"

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> DiscordUser:
        return cls(
            id=str(payload["id"]),
            username=str(payload.get("username") or payload["id"]),
            global_name=payload.get("global_name"),
            avatar=payload.get("avatar"),
            discriminator=payload.get("discriminator"),
        )


@dataclass(frozen=True)
class Membership:
    """Результат проверки гильдии."""

    allowed: bool
    reason: str = ""
    roles: list[str] = field(default_factory=list)
    nick: str | None = None
    checked: bool = True
    """``False``, если проверка выключена (``DISCORD_GUILD_CHECK=off``)."""


def new_state() -> str:
    """Случайный ``state`` для защиты от CSRF в OAuth-редиректе."""
    return secrets.token_urlsafe(24)


class DiscordClient:
    """Тонкая обёртка над Discord API. Создаётся один раз на приложение."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(15.0),
            headers={"User-Agent": f"{settings.app_name} (+{settings.base_url})"},
        )
        self._own_client = client is None

    # ------------------------------------------------------------------ oauth
    def authorize_url(self, state: str, *, prompt: str = "consent") -> str:
        params = {
            "client_id": self.settings.discord_client_id,
            "redirect_uri": self.settings.redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.settings.oauth_scopes),
            "state": state,
            "prompt": prompt,
        }
        return f"{self.settings.discord_api_base}/oauth2/authorize?{urlencode(params)}"

    async def exchange_code(self, code: str) -> str:
        """Меняет ``code`` на ``access_token``."""
        data = {
            "client_id": self.settings.discord_client_id,
            "client_secret": self.settings.discord_client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.settings.redirect_uri,
        }
        response = await self._client.post(
            f"{self.settings.discord_api_base}/oauth2/token",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if response.status_code != 200:
            log.warning("Discord отклонил обмен кода: %s %s", response.status_code, response.text)
            raise DiscordAuthError(
                "Discord не принял код авторизации. Проверьте client_id, client_secret "
                "и совпадение redirect_uri с настройками приложения."
            )
        token = response.json().get("access_token")
        if not token:
            raise DiscordAuthError("Discord не вернул access_token.")
        return str(token)

    async def fetch_user(self, access_token: str) -> DiscordUser:
        response = await self._client.get(
            f"{self.settings.discord_api_base}/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if response.status_code != 200:
            raise DiscordAuthError("Не удалось получить профиль Discord.")
        return DiscordUser.from_payload(response.json())

    async def revoke(self, access_token: str) -> None:
        """Отзывает токен: сайту он больше не нужен, храним только свою сессию."""
        try:
            await self._client.post(
                f"{self.settings.discord_api_base}/oauth2/token/revoke",
                data={
                    "client_id": self.settings.discord_client_id,
                    "client_secret": self.settings.discord_client_secret,
                    "token": access_token,
                },
            )
        except httpx.HTTPError:  # отзыв — не критичная операция
            log.debug("Не удалось отозвать токен Discord", exc_info=True)

    # ------------------------------------------------------------- membership
    async def check_membership(self, user: DiscordUser, access_token: str) -> Membership:
        """Пускать ли этого пользователя на сайт."""
        mode = self.settings.discord_guild_check
        if mode == "off":
            return Membership(allowed=True, checked=False)
        if mode == "bot":
            membership = await self._member_via_bot(user.id)
        else:
            membership = await self._member_via_oauth(access_token)
        if not membership.allowed:
            return membership
        return self._check_roles(membership)

    def _check_roles(self, membership: Membership) -> Membership:
        required = self.settings.discord_required_role_ids
        if not required:
            return membership
        if set(required) & set(membership.roles):
            return membership
        return Membership(
            allowed=False,
            reason=(
                "Вы состоите в гильдии, но у вас нет роли, дающей доступ. "
                "Попросите администратора выдать нужную роль."
            ),
            roles=membership.roles,
            nick=membership.nick,
        )

    async def _member_via_oauth(self, access_token: str) -> Membership:
        guild_id = self.settings.discord_guild_id
        response = await self._client.get(
            f"{self.settings.discord_api_base}/users/@me/guilds/{guild_id}/member",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if response.status_code == 200:
            payload = response.json()
            return Membership(
                allowed=True,
                roles=[str(role) for role in payload.get("roles", [])],
                nick=payload.get("nick"),
            )
        if response.status_code == 404:
            return Membership(allowed=False, reason=_not_a_member_message())
        if response.status_code in (401, 403):
            # scope guilds.members.read не выдан — пробуем узнать членство по списку гильдий
            log.info(
                "guilds.members.read недоступен (%s), пробуем /users/@me/guilds",
                response.status_code,
            )
            return await self._member_via_guild_list(access_token)
        if response.status_code == 429:
            raise DiscordAuthError(
                "Discord временно ограничил запросы (rate limit). Попробуйте войти чуть позже.",
                status=503,
            )
        log.warning("Discord ответил %s на проверку участника", response.status_code)
        raise DiscordAuthError("Discord не смог подтвердить членство в гильдии.", status=502)

    async def _member_via_guild_list(self, access_token: str) -> Membership:
        response = await self._client.get(
            f"{self.settings.discord_api_base}/users/@me/guilds",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if response.status_code != 200:
            raise DiscordAuthError("Не удалось получить список ваших серверов Discord.", status=502)
        guild_ids = {str(guild.get("id")) for guild in response.json()}
        if self.settings.discord_guild_id in guild_ids:
            if self.settings.discord_required_role_ids:
                raise DiscordAuthError(
                    "Для проверки ролей нужен доступ guilds.members.read или режим "
                    "DISCORD_GUILD_CHECK=bot.",
                    status=500,
                )
            return Membership(allowed=True)
        return Membership(allowed=False, reason=_not_a_member_message())

    async def _member_via_bot(self, user_id: str) -> Membership:
        guild_id = self.settings.discord_guild_id
        response = await self._client.get(
            f"{self.settings.discord_api_base}/guilds/{guild_id}/members/{user_id}",
            headers={"Authorization": f"Bot {self.settings.discord_bot_token}"},
        )
        if response.status_code == 200:
            payload = response.json()
            return Membership(
                allowed=True,
                roles=[str(role) for role in payload.get("roles", [])],
                nick=payload.get("nick"),
            )
        if response.status_code == 404:
            return Membership(allowed=False, reason=_not_a_member_message())
        if response.status_code in (401, 403):
            raise DiscordAuthError(
                "Бот не может прочитать участников гильдии: проверьте DISCORD_BOT_TOKEN, "
                "что бот добавлен на сервер и что включён intent Server Members.",
                status=500,
            )
        log.warning("Discord ответил %s на запрос участника ботом", response.status_code)
        raise DiscordAuthError("Discord не смог подтвердить членство в гильдии.", status=502)

    # ------------------------------------------------------------- lifecycle
    async def aclose(self) -> None:
        if self._own_client:
            await self._client.aclose()


def _not_a_member_message() -> str:
    return (
        "Ваш аккаунт не состоит в Discord-гильдии этого сайта. "
        "Вступите на сервер и войдите ещё раз."
    )
