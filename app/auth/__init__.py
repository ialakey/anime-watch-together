"""Авторизация: Discord OAuth2 и гостевой режим."""

from app.auth.discord import AccessDenied, DiscordAuthError, DiscordClient, DiscordUser, Membership
from app.auth.session import SESSION_KEY, SessionUser, login_guest, upsert_user

__all__ = [
    "SESSION_KEY",
    "AccessDenied",
    "DiscordAuthError",
    "DiscordClient",
    "DiscordUser",
    "Membership",
    "SessionUser",
    "login_guest",
    "upsert_user",
]
