"""Трекинг: автодобавление в список, отметка серии и «продолжить просмотр»."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.db.models import WatchStatus
from app.services.tracking import AnimeRef, TrackingService

REF = AnimeRef(source="animego", anime_id="7", title="Тестовое аниме", poster="https://p/1.jpg")


@pytest.fixture
def tracking() -> TrackingService:
    return TrackingService(Settings(secret_key="x"))


async def test_progress_creates_the_entry(db, user, tracking: TrackingService) -> None:
    await tracking.record_progress(db, user.id, REF, episode=1, position=60, duration=1440)
    entry = await tracking.get_entry(db, user.id, REF)
    assert entry is not None
    assert entry.title == "Тестовое аниме"
    assert entry.status == WatchStatus.WATCHING
    assert entry.last_episode == 0  # серия ещё не досмотрена


async def test_episode_is_completed_past_the_ratio(db, user, tracking: TrackingService) -> None:
    progress = await tracking.record_progress(
        db, user.id, REF, episode=3, position=1300, duration=1440
    )
    assert progress.completed is True
    entry = await tracking.get_entry(db, user.id, REF)
    assert entry.last_episode == 3


async def test_last_episode_never_goes_backwards(db, user, tracking: TrackingService) -> None:
    await tracking.record_progress(db, user.id, REF, episode=5, position=1400, duration=1440)
    await tracking.record_progress(db, user.id, REF, episode=2, position=1400, duration=1440)
    entry = await tracking.get_entry(db, user.id, REF)
    assert entry.last_episode == 5


async def test_finishing_the_last_episode_completes_the_title(
    db, user, tracking: TrackingService
) -> None:
    await tracking.ensure_entry(db, user.id, REF, episodes_total=2)
    await db.commit()
    await tracking.record_progress(db, user.id, REF, episode=1, position=1400, duration=1440)
    await tracking.record_progress(db, user.id, REF, episode=2, position=1400, duration=1440)
    entry = await tracking.get_entry(db, user.id, REF)
    assert entry.status == WatchStatus.COMPLETED


async def test_watching_resumes_a_planned_title(db, user, tracking: TrackingService) -> None:
    await tracking.set_status(db, user.id, REF, WatchStatus.PLANNED)
    await tracking.record_progress(db, user.id, REF, episode=1, position=100, duration=1440)
    entry = await tracking.get_entry(db, user.id, REF)
    assert entry.status == WatchStatus.WATCHING


async def test_continue_watching_lists_unfinished_episodes(
    db, user, tracking: TrackingService
) -> None:
    await tracking.record_progress(db, user.id, REF, episode=1, position=1400, duration=1440)
    await tracking.record_progress(db, user.id, REF, episode=2, position=300, duration=1440)

    items = await tracking.continue_watching(db, user.id)
    assert len(items) == 1
    assert items[0]["episode"] == 2
    assert items[0]["title"] == "Тестовое аниме"
    assert 15 < items[0]["percent"] < 30


async def test_dropped_titles_are_not_suggested(db, user, tracking: TrackingService) -> None:
    await tracking.record_progress(db, user.id, REF, episode=2, position=300, duration=1440)
    await tracking.set_status(db, user.id, REF, WatchStatus.DROPPED)
    assert await tracking.continue_watching(db, user.id) == []


async def test_barely_started_episodes_are_not_suggested(
    db, user, tracking: TrackingService
) -> None:
    await tracking.record_progress(db, user.id, REF, episode=1, position=5, duration=1440)
    assert await tracking.continue_watching(db, user.id) == []


async def test_rating_is_validated(db, user, tracking: TrackingService) -> None:
    await tracking.set_rating(db, user.id, REF, 9)
    entry = await tracking.get_entry(db, user.id, REF)
    assert entry.rating == 9
    with pytest.raises(ValueError):
        await tracking.set_rating(db, user.id, REF, 42)


async def test_removing_an_entry_clears_progress(db, user, tracking: TrackingService) -> None:
    await tracking.record_progress(db, user.id, REF, episode=1, position=1400, duration=1440)
    await tracking.remove_entry(db, user.id, REF)
    assert await tracking.get_entry(db, user.id, REF) is None
    assert await tracking.episodes_progress(db, user.id, REF) == {}


async def test_status_counts_and_stats(db, user, tracking: TrackingService) -> None:
    await tracking.record_progress(db, user.id, REF, episode=1, position=1400, duration=1440)
    other = AnimeRef(source="animego", anime_id="8", title="Второе")
    await tracking.set_status(db, user.id, other, WatchStatus.PLANNED)

    counts = await tracking.status_counts(db, user.id)
    assert counts["watching"] == 1
    assert counts["planned"] == 1
    assert counts["all"] == 2

    stats = await tracking.stats(db, user.id)
    assert stats["episodes_watched"] == 1
    assert stats["titles"] == 2
    assert stats["hours_watched"] == pytest.approx(0.4, abs=0.05)


async def test_tracking_can_be_disabled(db, user) -> None:
    disabled = TrackingService(Settings(secret_key="x", tracking_enabled=False))
    with pytest.raises(RuntimeError, match="TRACKING_ENABLED"):
        await disabled.record_progress(db, user.id, REF, episode=1, position=10)
