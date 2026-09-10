"""Маршруты входа и выхода."""

from __future__ import annotations

import logging
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.auth.discord import AccessDenied, DiscordAuthError, new_state
from app.auth.session import SESSION_KEY, SessionUser, login_guest, upsert_user
from app.deps import DbDep, DiscordDep, SettingsDep

log = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

STATE_KEY = "oauth_state"
NEXT_KEY = "oauth_next"


def _safe_next(value: str | None) -> str:
    """Пускаем редирект только внутрь сайта — иначе это open redirect."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


@router.get("/discord/login", summary="Начать вход через Discord")
async def discord_login(
    request: Request,
    settings: SettingsDep,
    discord: DiscordDep,
    next: str = "/",
) -> RedirectResponse:
    if not settings.discord_auth_enabled:
        return RedirectResponse("/login", status_code=303)
    state = new_state()
    request.session[STATE_KEY] = state
    request.session[NEXT_KEY] = _safe_next(next)
    return RedirectResponse(discord.authorize_url(state), status_code=307)


@router.get("/discord/callback", summary="Возврат от Discord")
async def discord_callback(
    request: Request,
    settings: SettingsDep,
    discord: DiscordDep,
    db: DbDep,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> RedirectResponse:
    next_url = _safe_next(request.session.pop(NEXT_KEY, "/"))
    expected_state = request.session.pop(STATE_KEY, None)

    if error:
        message = (
            "Вы отменили вход через Discord."
            if error == "access_denied"
            else (error_description or error)
        )
        return _fail(message)
    if not code:
        return _fail("Discord не передал код авторизации.")
    if not expected_state or state != expected_state:
        return _fail("Проверка состояния входа не прошла. Попробуйте войти заново.")

    access_token = ""
    try:
        access_token = await discord.exchange_code(code)
        profile = await discord.fetch_user(access_token)
        membership = await discord.check_membership(profile, access_token)
        if not membership.allowed:
            raise AccessDenied(membership.reason)
    except DiscordAuthError as exc:
        log.info("Вход через Discord отклонён: %s", exc.message)
        return _fail(exc.message)
    finally:
        if access_token:
            # дальше сайт живёт на собственной сессии, чужой токен хранить незачем
            await discord.revoke(access_token)

    display_name = membership.nick or profile.display_name
    user = await upsert_user(
        db,
        provider="discord",
        external_id=profile.id,
        username=profile.username,
        display_name=display_name,
        avatar_url=profile.avatar_url(),
        is_admin=profile.id in settings.discord_admin_ids,
    )
    request.session[SESSION_KEY] = SessionUser.from_model(user).to_session()
    log.info("Вход: %s (%s)", user.display_name, user.external_id)
    return RedirectResponse(next_url, status_code=303)


@router.post("/guest", summary="Гостевой вход (когда Discord выключен)")
async def guest_login(
    request: Request,
    settings: SettingsDep,
    db: DbDep,
    display_name: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "/",
) -> RedirectResponse:
    if settings.discord_auth_enabled:
        return _fail("Гостевой вход выключен: войдите через Discord.")
    user = await login_guest(db, display_name, settings)
    request.session[SESSION_KEY] = user.to_session()
    return RedirectResponse(_safe_next(next), status_code=303)


@router.post("/logout", summary="Выйти")
async def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@router.get("/me", summary="Текущий пользователь")
async def me(request: Request) -> dict[str, object]:
    user = SessionUser.from_session(request.session.get(SESSION_KEY))
    if user is None:
        return {"authenticated": False}
    return {"authenticated": True, "user": user.public() | {"is_admin": user.is_admin}}


def _fail(message: str) -> RedirectResponse:
    return RedirectResponse(f"/login?error={quote(message)}", status_code=303)


__all__ = ["router"]
