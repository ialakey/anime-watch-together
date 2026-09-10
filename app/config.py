"""Конфигурация приложения.

Все настройки читаются из переменных окружения (или из файла ``.env``).
Ключевой переключатель — ``DISCORD_AUTH_ENABLED``: при ``false`` сайт работает
в гостевом режиме (достаточно ввести имя), при ``true`` вход возможен только
через Discord и только для участников указанной гильдии.
"""

from __future__ import annotations

import functools
import secrets
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

GuildCheckMode = Literal["oauth", "bot", "off"]
CatalogSourceName = Literal["animego", "animedia"]
ControlMode = Literal["everyone", "host"]


def _split_csv(value: object) -> list[str]:
    """``"1, 2 ,3"`` -> ``["1", "2", "3"]``. Пустые элементы отбрасываются."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple)):
        return [str(part).strip() for part in value if str(part).strip()]
    return [str(value)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ app
    app_name: str = "Anime Watch Together"
    base_url: str = Field(
        default="http://localhost:8000",
        description="Внешний адрес сайта. Используется для OAuth redirect_uri и ссылок на комнаты.",
    )
    debug: bool = False
    secret_key: str = Field(
        default_factory=lambda: secrets.token_urlsafe(48),
        description="Ключ подписи сессий и ссылок на видео. В проде задавайте явно!",
    )
    session_max_age: int = Field(default=60 * 60 * 24 * 14, description="Срок жизни сессии, сек.")
    trust_proxy_headers: bool = Field(
        default=True,
        description="Учитывать X-Forwarded-* (нужно при работе за nginx/traefik).",
    )

    # -------------------------------------------------------------- discord
    discord_auth_enabled: bool = Field(
        default=False,
        description="Включает вход через Discord. При false работает гостевой режим.",
    )
    discord_client_id: str = ""
    discord_client_secret: str = ""
    discord_redirect_uri: str = Field(
        default="",
        description="Если пусто — берётся BASE_URL + /auth/discord/callback.",
    )
    discord_guild_id: str = Field(default="", description="ID гильдии, дающей доступ к сайту.")
    discord_guild_check: GuildCheckMode = Field(
        default="oauth",
        description=(
            "oauth — проверка токеном пользователя (scope guilds.members.read); "
            "bot — проверка ботом (нужен DISCORD_BOT_TOKEN, всегда видит роли); "
            "off — пускать любого владельца Discord-аккаунта."
        ),
    )
    discord_bot_token: str = Field(default="", description="Токен бота для режима проверки bot.")
    discord_required_role_ids: list[str] = Field(
        default_factory=list,
        description="Если задано — пускать только участников с одной из этих ролей.",
    )
    discord_admin_ids: list[str] = Field(
        default_factory=list, description="Discord ID администраторов сайта."
    )
    discord_api_base: str = "https://discord.com/api/v10"

    # -------------------------------------------------------------- catalog
    catalog_source: CatalogSourceName = Field(
        default="animego", description="Источник каталога аниме."
    )
    animego_mirror: str = Field(
        default="",
        description="Зеркало AnimeGO, если основной домен недоступен (например animego.me).",
    )
    animedia_base_url: str = "https://amd.online"
    http_proxy: str = Field(
        default="",
        description="Прокси для запросов к источникам, например socks5://127.0.0.1:9050.",
    )
    http_timeout: float = 25.0
    catalog_cache_ttl: int = Field(default=600, description="TTL кэша поиска/эпизодов, сек.")
    stream_cache_ttl: int = Field(
        default=240,
        description="TTL кэша прямых ссылок. Ссылки живут недолго — держите значение небольшим.",
    )
    search_limit: int = 24

    # ------------------------------------------------------------- streaming
    stream_proxy_enabled: bool = Field(
        default=True,
        description=(
            "Проксировать видео через сервер. Обычно обязательно: CDN отдаёт файл только "
            "по правильному Referer и привязывает ссылку к IP, который её получил."
        ),
    )
    stream_token_ttl: int = Field(default=60 * 60 * 6, description="Срок жизни ссылки плеера, сек.")
    stream_chunk_size: int = 64 * 1024
    stream_max_quality: int = Field(default=1080, description="Максимальная отдаваемая высота, px.")

    # ----------------------------------------------------------------- rooms
    room_idle_timeout: int = Field(
        default=60 * 60 * 3, description="Через сколько секунд простоя комната удаляется."
    )
    room_max_members: int = 25
    room_default_control: ControlMode = Field(
        default="everyone",
        description="Кто управляет плеером по умолчанию: everyone (все) или host (владелец).",
    )
    room_chat_history: int = 100
    room_sync_interval: float = Field(
        default=8.0, description="Как часто сервер рассылает опорное время, сек."
    )
    room_sync_tolerance: float = Field(
        default=1.5, description="Допустимое расхождение времени у зрителей, сек."
    )

    # -------------------------------------------------------------- tracking
    tracking_enabled: bool = True
    episode_completed_ratio: float = Field(
        default=0.85, description="Доля эпизода, после которой он считается просмотренным."
    )
    progress_report_interval: int = Field(
        default=15, description="Как часто клиент шлёт прогресс просмотра, сек."
    )

    # -------------------------------------------------------------------- db
    database_url: str = "sqlite+aiosqlite:///./data/anime_watch.db"
    db_echo: bool = False

    # --------------------------------------------------------------- helpers
    @field_validator("discord_required_role_ids", "discord_admin_ids", mode="before")
    @classmethod
    def _parse_id_lists(cls, value: object) -> list[str]:
        return _split_csv(value)

    @field_validator("base_url", "animedia_base_url")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @model_validator(mode="after")
    def _check_discord(self) -> Settings:
        if not self.discord_auth_enabled:
            return self
        missing = [
            name
            for name, value in (
                ("DISCORD_CLIENT_ID", self.discord_client_id),
                ("DISCORD_CLIENT_SECRET", self.discord_client_secret),
            )
            if not value
        ]
        if self.discord_guild_check != "off" and not self.discord_guild_id:
            missing.append("DISCORD_GUILD_ID")
        if self.discord_guild_check == "bot" and not self.discord_bot_token:
            missing.append("DISCORD_BOT_TOKEN")
        if missing:
            raise ValueError(
                "DISCORD_AUTH_ENABLED=true, но не заданы переменные: "
                + ", ".join(missing)
                + ". Заполните их или выключите авторизацию через Discord."
            )
        if self.discord_required_role_ids and self.discord_guild_check == "off":
            raise ValueError("DISCORD_REQUIRED_ROLE_IDS требует DISCORD_GUILD_CHECK=oauth или bot.")
        return self

    @property
    def redirect_uri(self) -> str:
        return self.discord_redirect_uri or f"{self.base_url}/auth/discord/callback"

    @property
    def oauth_scopes(self) -> list[str]:
        """Минимальный набор scope под выбранный режим проверки."""
        scopes = ["identify"]
        if self.discord_guild_check == "oauth":
            # guilds.members.read отдаёт и факт членства, и роли одним запросом
            scopes += ["guilds", "guilds.members.read"]
        return scopes

    @property
    def proxy_or_none(self) -> str | None:
        return self.http_proxy or None

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Синглтон настроек (кэшируется на процесс)."""
    return Settings()
