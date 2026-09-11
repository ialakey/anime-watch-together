"""REST для уведомлений: подписки на тайтлы, настройки доставки и админская рассылка."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.config import Settings
from app.deps import AdminDep, DbDep, NotificationsDep, SettingsDep, UserDep
from app.services.notifications import subscription_to_dict
from app.services.tracking import AnimeRef

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


class SubscriptionPayload(BaseModel):
    anime_id: str = Field(max_length=128)
    source: str = Field(default="animego", max_length=16)
    title: str = Field(default="", max_length=255)
    poster: str | None = Field(default=None, max_length=512)

    def to_ref(self) -> AnimeRef:
        return AnimeRef(
            source=self.source, anime_id=self.anime_id, title=self.title, poster=self.poster
        )


class SettingsPayload(BaseModel):
    enabled: bool | None = None
    channel_id: str | None = Field(default=None, max_length=32)


class BroadcastPayload(SubscriptionPayload):
    """Админская рассылка: один тайтл — сразу нескольким участникам."""

    user_ids: list[int] = Field(min_length=1, max_length=200)
    subscribe: bool = True


def _ensure_enabled(settings: Settings) -> None:
    if not settings.notifications_enabled:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Уведомления выключены настройкой NOTIFICATIONS_ENABLED.",
        )


@router.get("/subscriptions", summary="Мои отслеживаемые тайтлы")
async def list_subscriptions(
    db: DbDep, notifications: NotificationsDep, user: UserDep, settings: SettingsDep
) -> dict[str, Any]:
    _ensure_enabled(settings)
    items = await notifications.list_subscriptions(db, user.id)
    return {"subscriptions": [subscription_to_dict(item) for item in items]}


@router.post("/subscriptions", summary="Следить за новыми сериями")
async def subscribe(
    payload: SubscriptionPayload,
    db: DbDep,
    notifications: NotificationsDep,
    user: UserDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    _ensure_enabled(settings)
    subscription = await notifications.subscribe(db, user.id, payload.to_ref())
    return subscription_to_dict(subscription)


@router.delete("/subscriptions", summary="Перестать следить")
async def unsubscribe(
    payload: SubscriptionPayload,
    db: DbDep,
    notifications: NotificationsDep,
    user: UserDep,
    settings: SettingsDep,
) -> dict[str, str]:
    _ensure_enabled(settings)
    await notifications.unsubscribe(db, user.id, payload.to_ref())
    return {"status": "removed"}


@router.put("/settings", summary="Настройки доставки уведомлений")
async def update_settings(
    payload: SettingsPayload,
    db: DbDep,
    notifications: NotificationsDep,
    user: UserDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    _ensure_enabled(settings)
    try:
        updated = await notifications.set_user_settings(
            db, user.id, enabled=payload.enabled, channel_id=payload.channel_id
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {
        "enabled": updated.notifications_enabled,
        "channel_id": updated.notify_channel_id,
    }


@router.post("/test", summary="Отправить проверочное сообщение")
async def send_test(
    db: DbDep, notifications: NotificationsDep, user: UserDep, settings: SettingsDep
) -> dict[str, Any]:
    _ensure_enabled(settings)
    try:
        delivered = await notifications.send_test(db, user.id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if not delivered:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Бот не смог доставить сообщение. Проверьте, что он на вашем сервере, "
            "а личные сообщения от участников сервера у вас разрешены.",
        )
    return {"status": "sent"}


# ------------------------------------------------------------------ админское


@router.put("/users/{user_id}", summary="Включить или выключить уведомления участнику")
async def update_user(
    user_id: int,
    payload: SettingsPayload,
    db: DbDep,
    notifications: NotificationsDep,
    admin: AdminDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    _ensure_enabled(settings)
    try:
        updated = await notifications.set_user_settings(
            db, user_id, enabled=payload.enabled, channel_id=payload.channel_id
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {
        "id": updated.id,
        "enabled": updated.notifications_enabled,
        "channel_id": updated.notify_channel_id,
    }


@router.post("/broadcast", summary="Подписать выбранных участников на тайтл")
async def broadcast(
    payload: BroadcastPayload,
    db: DbDep,
    notifications: NotificationsDep,
    admin: AdminDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    _ensure_enabled(settings)
    ref = payload.to_ref()
    for user_id in payload.user_ids:
        if payload.subscribe:
            await notifications.subscribe(db, user_id, ref)
        else:
            await notifications.unsubscribe(db, user_id, ref)
    return {
        "status": "subscribed" if payload.subscribe else "unsubscribed",
        "count": len(payload.user_ids),
    }


@router.post("/check", summary="Проверить новые серии прямо сейчас")
async def check_now(
    db: DbDep, notifications: NotificationsDep, admin: AdminDep, settings: SettingsDep
) -> dict[str, int]:
    _ensure_enabled(settings)
    return {"sent": await notifications.check_once(db)}
