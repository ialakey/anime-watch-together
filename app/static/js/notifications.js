/** Страница уведомлений: настройки доставки, подписки и админская рассылка. */

import { api, toast } from './app.js';

const API = '/api/notifications';

// --- куда писать ------------------------------------------------------------
const enabledBox = document.querySelector('[data-notify-enabled]');
const channelBox = document.querySelector('[data-notify-channel]');

async function saveSettings(body, okMessage) {
  try {
    await api(`${API}/settings`, { method: 'PUT', body });
    toast(okMessage, 'ok');
    return true;
  } catch (error) {
    toast(error.message, 'error');
    return false;
  }
}

enabledBox?.addEventListener('change', async (event) => {
  const enabled = event.target.checked;
  const saved = await saveSettings(
    { enabled },
    enabled ? 'Уведомления включены' : 'Уведомления выключены',
  );
  if (!saved) event.target.checked = !enabled;
});

channelBox?.addEventListener('change', async (event) => {
  await saveSettings({ channel_id: event.target.value.trim() }, 'Адрес доставки сохранён');
});

document.querySelector('[data-notify-test]')?.addEventListener('click', async (event) => {
  event.target.disabled = true;
  try {
    await api(`${API}/test`, { method: 'POST' });
    toast('Проверочное сообщение отправлено — загляните в Discord', 'ok');
  } catch (error) {
    toast(error.message, 'error', 8000);
  } finally {
    event.target.disabled = false;
  }
});

document.querySelector('[data-notify-check]')?.addEventListener('click', async (event) => {
  event.target.disabled = true;
  try {
    const result = await api(`${API}/check`, { method: 'POST' });
    toast(
      result.sent ? `Отправлено уведомлений: ${result.sent}` : 'Новых серий пока нет',
      'ok',
    );
  } catch (error) {
    toast(error.message, 'error', 8000);
  } finally {
    event.target.disabled = false;
  }
});

// --- мои подписки -----------------------------------------------------------
/** Данные тайтла из <option> селекта. */
function refFromOption(option) {
  if (!option) return null;
  return {
    anime_id: option.value,
    source: option.dataset.source,
    title: option.dataset.title || '',
    poster: option.dataset.poster || null,
  };
}

document.querySelector('[data-subscribe-form]')?.addEventListener('submit', async (event) => {
  event.preventDefault();
  const select = event.target.querySelector('[data-subscribe-pick]');
  const ref = refFromOption(select?.selectedOptions?.[0]);
  if (!ref) return;
  try {
    await api(`${API}/subscriptions`, { method: 'POST', body: ref });
    toast(`Следим за «${ref.title}»`, 'ok');
    location.reload();
  } catch (error) {
    toast(error.message, 'error');
  }
});

document.querySelectorAll('[data-subscription]').forEach((row) => {
  row.querySelector('[data-unsubscribe]')?.addEventListener('click', async () => {
    try {
      await api(`${API}/subscriptions`, {
        method: 'DELETE',
        body: { anime_id: row.dataset.animeId, source: row.dataset.animeSource },
      });
      row.remove();
      toast('Больше не следим', 'ok');
    } catch (error) {
      toast(error.message, 'error');
    }
  });
});

// --- админская рассылка -----------------------------------------------------
const broadcast = document.querySelector('[data-broadcast]');
if (broadcast) initBroadcast(broadcast);

function initBroadcast(root) {
  const picks = () => [...root.querySelectorAll('[data-broadcast-pick]')];
  const selectedIds = () =>
    picks()
      .filter((box) => box.checked)
      .map((box) => Number(box.closest('[data-person]').dataset.userId));

  root.querySelector('[data-broadcast-all]')?.addEventListener('change', (event) => {
    picks().forEach((box) => (box.checked = event.target.checked));
  });

  async function send(subscribe) {
    const select = root.querySelector('[data-broadcast-title]');
    const ref = refFromOption(select?.selectedOptions?.[0]);
    const userIds = selectedIds();
    if (!ref) return;
    if (!userIds.length) {
      toast('Сначала отметьте участников', 'error');
      return;
    }
    try {
      const result = await api(`${API}/broadcast`, {
        method: 'POST',
        body: { ...ref, user_ids: userIds, subscribe },
      });
      toast(
        subscribe
          ? `Подписали участников: ${result.count}`
          : `Отписали участников: ${result.count}`,
        'ok',
      );
      location.reload();
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  root.querySelector('[data-broadcast-form]')?.addEventListener('submit', (event) => {
    event.preventDefault();
    send(true);
  });
  root.querySelector('[data-broadcast-unsubscribe]')?.addEventListener('click', () => send(false));

  root.querySelectorAll('[data-person]').forEach((row) => {
    row.querySelector('[data-person-enabled]')?.addEventListener('change', async (event) => {
      const enabled = event.target.checked;
      try {
        await api(`${API}/users/${row.dataset.userId}`, { method: 'PUT', body: { enabled } });
        toast(enabled ? 'Уведомления включены' : 'Уведомления выключены', 'ok');
      } catch (error) {
        toast(error.message, 'error');
        event.target.checked = !enabled;
      }
    });
  });
}
