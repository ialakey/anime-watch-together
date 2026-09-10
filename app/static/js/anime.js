/** Страница тайтла: выбор серии/озвучки, трекинг и создание комнаты. */

import { api, createRoom, toast } from './app.js';

const root = document.querySelector('.anime');
if (root) init(root);

function init(root) {
  const state = {
    animeId: root.dataset.animeId,
    source: root.dataset.animeSource,
    title: root.dataset.animeTitle,
    poster: root.dataset.animePoster || null,
    episode: Number(root.dataset.episode || 1),
  };

  const refPayload = () => ({
    anime_id: state.animeId,
    source: state.source,
    title: state.title,
    poster: state.poster,
  });

  // --- выбор серии -------------------------------------------------------
  const playersBox = root.querySelector('[data-players]');
  const playersEpisodeChip = root.querySelector('[data-players-episode]');

  root.querySelectorAll('[data-episode-btn]').forEach((button) => {
    button.addEventListener('click', () => selectEpisode(Number(button.dataset.episodeBtn)));
  });

  async function selectEpisode(episode) {
    if (episode === state.episode) return;
    state.episode = episode;
    root.querySelectorAll('[data-episode-btn]').forEach((button) => {
      button.classList.toggle('episode--active', Number(button.dataset.episodeBtn) === episode);
    });
    if (playersEpisodeChip) playersEpisodeChip.textContent = `серия ${episode}`;
    // ссылку тоже поправим, чтобы обновление страницы не сбрасывало выбор
    const url = new URL(location.href);
    url.searchParams.set('episode', String(episode));
    history.replaceState(null, '', url);
    await loadPlayers(episode);
  }

  async function loadPlayers(episode) {
    if (!playersBox) return;
    playersBox.setAttribute('aria-busy', 'true');
    playersBox.innerHTML = '<p class="muted">Загружаем озвучки…</p>';
    try {
      const data = await api(
        `/api/anime/${encodeURIComponent(state.animeId)}/players?episode=${episode}`,
      );
      renderPlayers(data.players);
    } catch (error) {
      playersBox.innerHTML = `<p class="muted">${escapeHtml(error.message)}</p>`;
    } finally {
      playersBox.removeAttribute('aria-busy');
    }
  }

  function renderPlayers(players) {
    if (!players?.length) {
      playersBox.innerHTML = '<p class="muted">Для этой серии не нашлось плееров.</p>';
      return;
    }
    playersBox.innerHTML = players
      .map(
        (option, index) => `
          <label class="player-option${index === 0 ? ' player-option--active' : ''}">
            <input type="radio" name="player" value="${escapeHtml(option.key)}" ${index === 0 ? 'checked' : ''}>
            <span class="player-option__label">${escapeHtml(option.label)}</span>
            <span class="player-option__player">${escapeHtml(option.player)}</span>
          </label>`,
      )
      .join('');
  }

  playersBox?.addEventListener('change', () => {
    playersBox.querySelectorAll('.player-option').forEach((label) => {
      label.classList.toggle('player-option--active', label.querySelector('input')?.checked);
    });
  });

  const selectedPlayer = () =>
    playersBox?.querySelector('input[name="player"]:checked')?.value || null;

  // --- создание комнаты --------------------------------------------------
  async function startWatching(isPublic) {
    const buttons = root.querySelectorAll('[data-watch-together], [data-watch-alone]');
    buttons.forEach((button) => (button.disabled = true));
    const hint = root.querySelector('[data-watch-hint]');
    if (hint) hint.textContent = 'Готовим комнату и достаём ссылку на видео…';
    try {
      await createRoom({
        name: `${state.title} — серия ${state.episode}`,
        is_public: isPublic,
        anime_id: state.animeId,
        episode: state.episode,
        player_key: selectedPlayer(),
      });
    } catch (error) {
      toast(error.message, 'error', 7000);
      if (hint) hint.textContent = 'Не получилось — попробуйте другую озвучку.';
      buttons.forEach((button) => (button.disabled = false));
    }
  }

  root.querySelector('[data-watch-together]')?.addEventListener('click', () => startWatching(true));
  root.querySelector('[data-watch-alone]')?.addEventListener('click', () => startWatching(false));

  // --- трекинг -----------------------------------------------------------
  root.querySelector('[data-track-status]')?.addEventListener('change', async (event) => {
    const value = event.target.value;
    try {
      if (!value) {
        await api('/api/tracking/entry', { method: 'DELETE', body: refPayload() });
        toast('Убрали из списка', 'ok');
        return;
      }
      await api('/api/tracking/status', { method: 'PUT', body: { ...refPayload(), status: value } });
      toast('Статус обновлён', 'ok');
    } catch (error) {
      toast(error.message, 'error');
    }
  });

  root.querySelector('[data-track-rating]')?.addEventListener('change', async (event) => {
    const raw = event.target.value;
    try {
      await api('/api/tracking/rating', {
        method: 'PUT',
        body: { ...refPayload(), rating: raw ? Number(raw) : null },
      });
      toast(raw ? `Оценка: ${raw}` : 'Оценка снята', 'ok');
    } catch (error) {
      toast(error.message, 'error');
    }
  });

  root.querySelector('[data-track-remove]')?.addEventListener('click', async (event) => {
    try {
      await api('/api/tracking/entry', { method: 'DELETE', body: refPayload() });
      toast('Убрали из списка', 'ok');
      event.target.remove();
      const statusSelect = root.querySelector('[data-track-status]');
      if (statusSelect) statusSelect.value = '';
    } catch (error) {
      toast(error.message, 'error');
    }
  });
}

function escapeHtml(value) {
  return String(value).replace(
    /[&<>"']/g,
    (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char],
  );
}
