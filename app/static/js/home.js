import { createRoom, toast } from './app.js';

const createButton = document.querySelector('[data-create-room]');

createButton?.addEventListener('click', async () => {
  createButton.disabled = true;
  try {
    await createRoom({ is_public: true });
  } catch (error) {
    toast(error.message, 'error');
    createButton.disabled = false;
  }
});
