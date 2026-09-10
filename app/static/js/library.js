/** Мой список: смена статуса, оценки и удаление прямо в таблице. */

import { api, toast } from './app.js';

document.querySelectorAll('[data-entry]').forEach((row) => {
  const ref = () => ({
    anime_id: row.dataset.animeId,
    source: row.dataset.animeSource,
    title: row.querySelector('.table__title span:last-child')?.textContent?.trim() || '',
  });

  row.querySelector('[data-track-status]')?.addEventListener('change', async (event) => {
    try {
      await api('/api/tracking/status', {
        method: 'PUT',
        body: { ...ref(), status: event.target.value },
      });
      toast('Статус обновлён', 'ok');
    } catch (error) {
      toast(error.message, 'error');
    }
  });

  row.querySelector('[data-track-rating]')?.addEventListener('change', async (event) => {
    const raw = event.target.value;
    try {
      await api('/api/tracking/rating', {
        method: 'PUT',
        body: { ...ref(), rating: raw ? Number(raw) : null },
      });
      toast('Оценка сохранена', 'ok');
    } catch (error) {
      toast(error.message, 'error');
    }
  });

  row.querySelector('[data-track-remove]')?.addEventListener('click', async () => {
    try {
      await api('/api/tracking/entry', { method: 'DELETE', body: ref() });
      row.remove();
      toast('Убрали из списка', 'ok');
    } catch (error) {
      toast(error.message, 'error');
    }
  });
});
