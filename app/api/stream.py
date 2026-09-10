"""Отдача видео через сервер: плейлисты HLS и сегменты/файлы."""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import Response, StreamingResponse

from app.deps import StreamDep, UserDep
from app.services.streaming import StreamProxy, StreamTarget, StreamTokenError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stream", tags=["stream"])


@router.get("/playlist", summary="HLS-плейлист с переписанными ссылками")
async def playlist(t: str, streams: StreamDep, user: UserDep) -> Response:
    target = _verify(streams, t)
    try:
        content = await streams.fetch_playlist(target)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Источник видео ответил {exc.response.status_code}. Ссылка могла устареть — "
            "обновите страницу или выберите другую озвучку.",
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "Источник видео не отвечает.") from exc
    return Response(
        content=content,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/segment", summary="Сегмент видео или файл целиком")
async def segment(t: str, request: Request, streams: StreamDep, user: UserDep) -> StreamingResponse:
    target = _verify(streams, t)
    request_headers = {name.lower(): value for name, value in request.headers.items()}

    context = streams.stream_request(target, request_headers)
    upstream = await context.__aenter__()
    if upstream.status_code >= 400:
        await context.__aexit__(None, None, None)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Источник видео ответил {upstream.status_code}. Обновите страницу.",
        )

    async def body_iterator():
        try:
            async for chunk in upstream.aiter_bytes(streams.settings.stream_chunk_size):
                yield chunk
        except httpx.HTTPError:  # зритель перемотал или закрыл вкладку
            log.debug("Поток прерван", exc_info=True)
        finally:
            await context.__aexit__(None, None, None)

    return StreamingResponse(
        body_iterator(),
        status_code=upstream.status_code,
        headers=streams.response_headers(upstream),
        media_type=upstream.headers.get("content-type", "application/octet-stream"),
    )


def _verify(streams: StreamProxy, token: str) -> StreamTarget:
    try:
        return streams.verify(token)
    except StreamTokenError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
