/**
 * Общие мелочи: запросы к API, тосты, форматирование времени.
 * Модуль подключается на каждой странице.
 */

const TOASTS = document.getElementById('toasts');

/** Показывает всплывающее уведомление. kind: 'info' | 'ok' | 'error'. */
export function toast(message, kind = 'info', timeout = 4000) {
  if (!TOASTS) return;
  const node = document.createElement('div');
  node.className = `toast toast--${kind}`;
  node.textContent = message;
  TOASTS.append(node);
  setTimeout(() => {
    node.style.opacity = '0';
    setTimeout(() => node.remove(), 200);
  }, timeout);
}

/** fetch с JSON, понятными ошибками и редиректом на вход при 401. */
export async function api(url, { method = 'GET', body, ...rest } = {}) {
  const response = await fetch(url, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
    credentials: 'same-origin',
    ...rest,
  });

  if (response.status === 401) {
    window.location.href = `/login?next=${encodeURIComponent(location.pathname + location.search)}`;
    throw new Error('Нужно войти');
  }

  const text = await response.text();
  const data = text ? safeJson(text) : null;
  if (!response.ok) {
    throw new Error(data?.detail || `Ошибка ${response.status}`);
  }
  return data;
}

function safeJson(text) {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

/** 3725 -> "1:02:05" */
export function formatTime(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return '0:00';
  const total = Math.floor(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const mm = h ? String(m).padStart(2, '0') : String(m);
  return `${h ? `${h}:` : ''}${mm}:${String(s).padStart(2, '0')}`;
}

/** "14:35" из отметки времени в секундах. */
export function formatClock(epochSeconds) {
  return new Date(epochSeconds * 1000).toLocaleTimeString('ru-RU', {
    hour: '2-digit',
    minute: '2-digit',
  });
}

/** Копирует текст в буфер обмена, с запасным вариантом для http-контекста. */
export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const helper = document.createElement('textarea');
    helper.value = text;
    helper.setAttribute('readonly', '');
    helper.style.position = 'fixed';
    helper.style.opacity = '0';
    document.body.append(helper);
    helper.select();
    const ok = document.execCommand?.('copy') ?? false;
    helper.remove();
    return ok;
  }
}

/** Читает JSON из <script type="application/json"> по id. */
export function readJsonScript(id, fallback = null) {
  const node = document.getElementById(id);
  if (!node) return fallback;
  try {
    return JSON.parse(node.textContent);
  } catch {
    return fallback;
  }
}

/** Создаёт комнату и уводит в неё. */
export async function createRoom(payload = {}) {
  const room = await api('/api/rooms', { method: 'POST', body: payload });
  window.location.href = `/room/${room.code}`;
  return room;
}

// Закрываем выпадающее меню пользователя по клику мимо него.
document.addEventListener('click', (event) => {
  document.querySelectorAll('details.usermenu[open]').forEach((menu) => {
    if (!menu.contains(event.target)) menu.open = false;
  });
});
