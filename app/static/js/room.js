/**
 * Комната совместного просмотра.
 *
 * Синхронизация устроена так: сервер — источник правды. Локальные действия
 * (play / pause / перемотка) уходят на сервер, а всё, что приходит обратно,
 * применяется к <video> с «глушилкой» событий, чтобы применение чужого
 * действия не породило новое сообщение и не закрутило эхо.
 */

import { api, copyText, formatClock, readJsonScript, toast } from './app.js';

const config = readJsonScript('room-config');
if (config) start(config, readJsonScript('room-state', {}));

function start(config, initialState) {
  const video = document.getElementById('player');
  const ui = {
    overlay: document.querySelector('[data-overlay]'),
    overlayText: document.querySelector('[data-overlay-text]'),
    overlaySpinner: document.querySelector('[data-overlay-spinner]'),
    members: document.querySelector('[data-members]'),
    memberCount: document.querySelector('[data-member-count]'),
    chat: document.querySelector('[data-chat]'),
    chatForm: document.querySelector('[data-chat-form]'),
    episodeLabel: document.querySelector('[data-episode-label]'),
    sourceTitle: document.querySelector('[data-source-title]'),
    sourceSub: document.querySelector('[data-source-sub]'),
    roomName: document.querySelector('[data-room-name]'),
    translation: document.querySelector('[data-translation-select]'),
    quality: document.querySelector('[data-quality-select]'),
    skipIntro: document.querySelector('[data-skip-intro]'),
  };

  const state = {
    source: initialState?.source || null,
    playback: initialState?.playback || { playing: false, position: 0 },
    clockOffset: 0,
    suppressUntil: 0,
    socket: null,
    hls: null,
    reconnectDelay: 1000,
    closedByServer: false,
    lastProgressSent: 0,
    overlayKind: null,
    seenChatIds: new Set(),
    loadedKey: null,
    opening: null,
  };

  const tolerance = Number(config.sync_tolerance) || 1.5;
  const progressInterval = (Number(config.progress_interval) || 15) * 1000;

  // ------------------------------------------------------------- утилиты
  const serverNow = () => Date.now() / 1000 + state.clockOffset;
  const suppress = (ms = 600) => {
    state.suppressUntil = performance.now() + ms;
  };
  const suppressed = () => performance.now() < state.suppressUntil;

  function send(message) {
    if (state.socket?.readyState === WebSocket.OPEN) {
      state.socket.send(JSON.stringify(message));
      return true;
    }
    return false;
  }

  function showOverlay(text, { spinner = false, kind = 'info' } = {}) {
    if (!ui.overlay) return;
    state.overlayKind = kind;
    ui.overlay.hidden = false;
    if (ui.overlayText) ui.overlayText.textContent = text;
    if (ui.overlaySpinner) ui.overlaySpinner.hidden = !spinner;
  }

  function hideOverlay() {
    state.overlayKind = null;
    if (ui.overlay) ui.overlay.hidden = true;
  }

  // ------------------------------------------------------------ WebSocket
  function connect() {
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
    const socket = new WebSocket(`${scheme}://${location.host}/ws/room/${config.code}`);
    state.socket = socket;

    socket.addEventListener('open', () => {
      state.reconnectDelay = 1000;
      measureClock();
    });

    socket.addEventListener('message', (event) => {
      let message;
      try {
        message = JSON.parse(event.data);
      } catch {
        return;
      }
      handle(message);
    });

    socket.addEventListener('close', (event) => {
      if (state.closedByServer) return;
      if (event.code === 4401) {
        location.href = `/login?next=${encodeURIComponent(location.pathname)}`;
        return;
      }
      if (event.code === 4404 || event.code === 4409) {
        showOverlay(event.reason || 'Комната недоступна');
        return;
      }
      showOverlay('Соединение потеряно, переподключаемся…', { spinner: true });
      setTimeout(connect, state.reconnectDelay);
      state.reconnectDelay = Math.min(state.reconnectDelay * 1.7, 15000);
    });
  }

  function handle(message) {
    switch (message.type) {
      case 'state':
        applyRoom(message.room);
        break;
      case 'source':
        applySource(message.source);
        applyPlayback(message.playback, { force: true });
        if (message.by) toast(`${message.by} переключил серию`);
        break;
      case 'playback':
        applyPlayback(message.playback, { force: message.action === 'seek' });
        break;
      case 'sync':
        applyPlayback(message.playback);
        break;
      case 'ack':
        state.playback = message.playback;
        break;
      case 'members':
        renderMembers(message.members);
        break;
      case 'chat':
        appendChat(message.message);
        break;
      case 'settings':
        if (message.settings.name && ui.roomName) ui.roomName.textContent = message.settings.name;
        break;
      case 'loading':
        showOverlay(`Загружаем серию ${message.episode}…`, { spinner: true });
        break;
      case 'closed':
        state.closedByServer = true;
        showOverlay(message.reason || 'Комната закрыта');
        toast(message.reason || 'Комната закрыта', 'error');
        setTimeout(() => (location.href = '/'), 2500);
        break;
      case 'pong':
        applyPong(message);
        break;
      case 'error':
        toast(message.message, 'error', 6000);
        if (message.code === 'catalog_error') hideOverlay();
        break;
      default:
        break;
    }
  }

  function applyRoom(room) {
    if (!room) return;
    if (ui.roomName) ui.roomName.textContent = room.name;
    renderMembers(room.members || []);
    (room.chat || []).forEach(appendChat);
    if (room.source) {
      applySource(room.source);
      applyPlayback(room.playback, { force: true });
    } else {
      showOverlay('Комната пуста — выберите аниме на странице поиска');
    }
  }

  // ---------------------------------------------------------- часы сервера
  function measureClock() {
    send({ type: 'ping', client_time: Date.now() / 1000 });
  }

  function applyPong(message) {
    const clientTime = Number(message.client_time);
    if (!clientTime) return;
    const now = Date.now() / 1000;
    const roundTrip = now - clientTime;
    // серверное время соответствует середине round-trip
    state.clockOffset = Number(message.server_time) + roundTrip / 2 - now;
  }

  setInterval(measureClock, 30000);

  // ---------------------------------------------------------------- плеер
  /** Ключ дорожки: сменился — значит плеер надо перезагрузить. */
  function sourceKey(source) {
    return `${source.anime_id}|${source.episode}|${source.player_key}`;
  }

  function applySource(source) {
    if (!source) return;
    // сравниваем с тем, что реально загружено в <video>, а не с тем, что пришло
    // с сервера при рендере страницы — иначе первую серию никто не включит
    const changed = state.loadedKey !== sourceKey(source);
    state.source = source;

    if (ui.sourceTitle) ui.sourceTitle.textContent = source.anime_title;
    if (ui.sourceSub) {
      ui.sourceSub.textContent = `Серия ${source.episode} · ${source.translation} · ${source.player}`;
    }
    if (ui.episodeLabel) ui.episodeLabel.textContent = `${source.episode}`;
    if (source.poster) video.poster = source.poster;
    document.title = `${source.anime_title} — серия ${source.episode}`;

    if (changed) {
      state.loadedKey = sourceKey(source);
      loadStream(source.streams?.[0], 0);
      loadTranslations(source);
    }
    setupSkipIntro(source);
    fillQualities(source);
  }

  /** Подключает дорожку к <video>: через hls.js или нативно. */
  function loadStream(stream, startAt = 0) {
    if (!stream) {
      showOverlay('Не нашлось подходящей дорожки');
      return;
    }
    destroyHls();
    showOverlay('Загружаем видео…', { spinner: true });
    // MediaSource открывается только когда элемент действительно начинает грузиться
    video.preload = 'metadata';

    const resume = () => {
      hideOverlay();
      if (startAt > 0) {
        suppress();
        video.currentTime = startAt;
      }
      applyPlayback(state.playback, { force: true });
    };

    if (stream.kind === 'hls' && window.Hls?.isSupported()) {
      const hls = new window.Hls({
        enableWorker: true,
        lowLatencyMode: false,
        backBufferLength: 60,
      });
      state.hls = hls;
      hls.on(window.Hls.Events.MANIFEST_PARSED, () => {
        fillHlsQualities(hls);
        resume();
      });
      hls.on(window.Hls.Events.ERROR, (_event, data) => {
        if (!data.fatal) return;
        if (data.type === window.Hls.ErrorTypes.NETWORK_ERROR) {
          showOverlay('Видео не отвечает, пробуем ещё раз…', { spinner: true });
          hls.startLoad();
        } else if (data.type === window.Hls.ErrorTypes.MEDIA_ERROR) {
          hls.recoverMediaError();
        } else {
          showOverlay('Не удалось проиграть эту дорожку — выберите другую озвучку');
        }
      });
      // сначала attachMedia, потом loadSource: при обратном порядке Chrome
      // не успевает открыть MediaSource и hls.js молча стоит в IDLE
      hls.attachMedia(video);
      hls.loadSource(stream.url);
      return;
    }

    // Safari и mp4 — нативно
    video.src = stream.url;
    video.addEventListener('loadedmetadata', resume, { once: true });
    video.addEventListener(
      'error',
      () => showOverlay('Не удалось загрузить видео — попробуйте другую озвучку'),
      { once: true },
    );
    video.load();
  }

  function destroyHls() {
    if (state.hls) {
      state.hls.destroy();
      state.hls = null;
    }
    video.removeAttribute('src');
  }

  function applyPlayback(playback, { force = false } = {}) {
    if (!playback) return;
    state.playback = playback;

    const drift = playback.playing ? Math.max(0, serverNow() - playback.server_time) : 0;
    const target = Math.max(0, playback.position + drift);

    if (Number.isFinite(video.duration) && video.duration > 0) {
      if (force || Math.abs(video.currentTime - target) > tolerance) {
        suppress();
        video.currentTime = Math.min(target, video.duration);
      }
    } else if (target > 0) {
      // метаданные ещё не подтянулись — доедем при loadedmetadata
      video.addEventListener(
        'loadedmetadata',
        () => {
          suppress();
          video.currentTime = target;
        },
        { once: true },
      );
    }

    if (playback.playing && video.paused) {
      suppress(1200);
      video.play().catch(() => {
        showOverlay('Нажмите, чтобы включить воспроизведение', { kind: 'gesture' });
        ui.overlay?.addEventListener(
          'click',
          () => {
            hideOverlay();
            video.play().catch(() => {});
          },
          { once: true },
        );
      });
    } else if (!playback.playing) {
      if (state.overlayKind === 'gesture') hideOverlay();
      if (!video.paused) {
        suppress();
        video.pause();
      }
    }
  }

  // --------------------------------------------------- локальные действия
  video.addEventListener('play', () => {
    if (suppressed()) return;
    send({ type: 'play', position: video.currentTime });
  });

  video.addEventListener('pause', () => {
    if (suppressed()) return;
    if (video.ended) return;
    send({ type: 'pause', position: video.currentTime });
  });

  video.addEventListener('seeked', () => {
    if (suppressed()) return;
    send({ type: 'seek', position: video.currentTime });
  });

  video.addEventListener('playing', hideOverlay);

  video.addEventListener('waiting', () => {
    // если буфер опустел — при возвращении подтянемся к общей позиции
    send({ type: 'sync_request' });
  });

  video.addEventListener('ended', () => {
    reportProgress(true);
  });

  video.addEventListener('timeupdate', () => {
    if (!config.tracking_enabled || !state.source) return;
    const now = Date.now();
    if (now - state.lastProgressSent < progressInterval) return;
    state.lastProgressSent = now;
    reportProgress(false);
  });

  function reportProgress(finished) {
    if (!config.tracking_enabled || !state.source) return;
    send({
      type: 'progress',
      position: finished ? video.duration || video.currentTime : video.currentTime,
      duration: Number.isFinite(video.duration) ? video.duration : null,
    });
  }

  // --------------------------------------------------------------- серии
  document.querySelector('[data-episode-prev]')?.addEventListener('click', () => {
    changeEpisode(-1);
  });
  document.querySelector('[data-episode-next]')?.addEventListener('click', () => {
    changeEpisode(1);
  });

  function changeEpisode(delta) {
    if (!state.source) {
      toast('Сначала выберите аниме', 'error');
      return;
    }
    const next = Number(state.source.episode) + delta;
    if (next < 1) return;
    send({ type: 'set_source', anime_id: state.source.anime_id, episode: next });
  }

  document.querySelector('[data-resync]')?.addEventListener('click', () => {
    send({ type: 'sync_request' });
    toast('Подтягиваемся к общей позиции');
  });

  // ------------------------------------------------------------- озвучки
  async function loadTranslations(source) {
    if (!ui.translation) return;
    ui.translation.disabled = true;
    ui.translation.innerHTML = '<option>загрузка…</option>';
    try {
      const data = await api(
        `/api/anime/${encodeURIComponent(source.anime_id)}/players?episode=${source.episode}`,
      );
      ui.translation.innerHTML = data.players
        .map(
          (option) =>
            `<option value="${escapeAttr(option.key)}"${
              option.key === source.player_key ? ' selected' : ''
            }>${escapeHtml(option.label)} · ${escapeHtml(option.player)}</option>`,
        )
        .join('');
      ui.translation.disabled = false;
    } catch {
      ui.translation.innerHTML = `<option>${escapeHtml(source.translation)}</option>`;
    }
  }

  ui.translation?.addEventListener('change', (event) => {
    if (!state.source) return;
    send({
      type: 'set_source',
      anime_id: state.source.anime_id,
      episode: state.source.episode,
      player_key: event.target.value,
    });
  });

  // ------------------------------------------------------------- качество
  function fillQualities(source) {
    if (!ui.quality || state.hls) return;
    const options = (source.streams || []).filter((stream) => stream.quality);
    if (!options.length) {
      ui.quality.innerHTML = '<option>авто</option>';
      ui.quality.disabled = true;
      return;
    }
    ui.quality.innerHTML = options
      .map((stream, index) => `<option value="${index}">${stream.quality}p</option>`)
      .join('');
    ui.quality.disabled = false;
    ui.quality.onchange = () => {
      const stream = options[Number(ui.quality.value)];
      loadStream(stream, video.currentTime);
    };
  }

  function fillHlsQualities(hls) {
    if (!ui.quality) return;
    const levels = hls.levels || [];
    if (levels.length < 2) {
      ui.quality.innerHTML = '<option>авто</option>';
      ui.quality.disabled = true;
      return;
    }
    ui.quality.innerHTML =
      '<option value="-1">авто</option>' +
      levels
        .map((level, index) => `<option value="${index}">${level.height || '?'}p</option>`)
        .join('');
    ui.quality.disabled = false;
    ui.quality.onchange = () => {
      hls.currentLevel = Number(ui.quality.value);
    };
  }

  // ------------------------------------------------------- пропуск заставки
  function setupSkipIntro(source) {
    const opening = (source.skip_segments || []).find((segment) => segment.kind === 'opening');
    state.opening = opening || null;
    updateSkipIntro();
  }

  function updateSkipIntro() {
    if (!ui.skipIntro) return;
    const opening = state.opening;
    if (!opening) {
      ui.skipIntro.hidden = true;
      return;
    }
    const inside = video.currentTime >= opening.start && video.currentTime < opening.end;
    ui.skipIntro.hidden = !inside;
  }

  video.addEventListener('timeupdate', updateSkipIntro);

  ui.skipIntro?.addEventListener('click', () => {
    if (!state.opening) return;
    video.currentTime = state.opening.end;
    send({ type: 'seek', position: state.opening.end });
  });

  // ------------------------------------------------------------- зрители
  function renderMembers(members) {
    if (!ui.members) return;
    ui.memberCount && (ui.memberCount.textContent = String(members.length));
    ui.members.innerHTML = members
      .map(
        (member) => `
        <li>
          ${
            member.avatar
              ? `<img class="avatar" src="${escapeAttr(member.avatar)}" alt="" width="26" height="26">`
              : `<span class="avatar avatar--letter">${escapeHtml((member.name || '?')[0])}</span>`
          }
          <span>${escapeHtml(member.name)}</span>
          ${member.is_host ? '<span class="members__host">хост</span>' : ''}
        </li>`,
      )
      .join('');
  }

  // ----------------------------------------------------------------- чат
  function appendChat(message) {
    if (!ui.chat || !message) return;
    const key = `${message.kind}:${message.id}:${message.created_at}`;
    if (state.seenChatIds.has(key)) return;
    state.seenChatIds.add(key);

    const item = document.createElement('li');
    if (message.kind === 'system') {
      item.className = 'chat__system';
      item.textContent = message.text;
    } else {
      item.innerHTML =
        `<span class="chat__author">${escapeHtml(message.user_name)}</span>` +
        `<span>${escapeHtml(message.text)}</span>` +
        `<span class="chat__time">${formatClock(message.created_at)}</span>`;
    }
    const atBottom = ui.chat.scrollTop + ui.chat.clientHeight >= ui.chat.scrollHeight - 30;
    ui.chat.append(item);
    if (atBottom) ui.chat.scrollTop = ui.chat.scrollHeight;
  }

  ui.chatForm?.addEventListener('submit', (event) => {
    event.preventDefault();
    const input = ui.chatForm.querySelector('input');
    const text = input.value.trim();
    if (!text) return;
    if (send({ type: 'chat', text })) input.value = '';
  });

  // ----------------------------------------------------- настройки комнаты
  document.querySelector('[data-control-mode]')?.addEventListener('change', (event) => {
    send({ type: 'settings', control_mode: event.target.value });
  });

  document.querySelector('[data-public-toggle]')?.addEventListener('change', (event) => {
    send({ type: 'settings', is_public: event.target.checked });
  });

  document.querySelector('[data-close-room]')?.addEventListener('click', async () => {
    if (!confirm('Закрыть комнату для всех зрителей?')) return;
    try {
      await api(`/api/rooms/${config.code}`, { method: 'DELETE' });
      location.href = '/';
    } catch (error) {
      toast(error.message, 'error');
    }
  });

  document.querySelector('[data-copy-link]')?.addEventListener('click', async (event) => {
    const button = event.currentTarget;
    const ok = await copyText(button.dataset.link || location.href);
    const original = button.textContent;
    button.textContent = ok ? 'Скопировано!' : 'Не вышло :(';
    setTimeout(() => (button.textContent = original), 1600);
  });

  // ------------------------------------------------------- горячие клавиши
  document.addEventListener('keydown', (event) => {
    if (event.target.matches('input, textarea, select')) return;
    if (event.code === 'Space') {
      event.preventDefault();
      video.paused ? video.play().catch(() => {}) : video.pause();
    }
    if (event.code === 'ArrowRight' && event.shiftKey) changeEpisode(1);
    if (event.code === 'ArrowLeft' && event.shiftKey) changeEpisode(-1);
  });

  window.addEventListener('beforeunload', () => reportProgress(false));

  connect();
}

function escapeHtml(value) {
  return String(value ?? '').replace(
    /[&<>"']/g,
    (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char],
  );
}

const escapeAttr = escapeHtml;
