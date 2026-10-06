import { mountOrbit, renderOrbit } from './orbit.js?v=20261005-ui9';
import { prepareImages } from './image-cache.js?v=20261005-ui9';

const mount = document.querySelector('#loginOrbitMount');
if (mount) {
  mount.innerHTML = renderOrbit();
  const stage = mount.querySelector('#orbit-stage');
  const disposeOrbit = mountOrbit(() => {});
  prepareImages(stage).catch(() => {});
  window.addEventListener('pagehide', disposeOrbit, { once: true });
}

const musicButton = document.querySelector('#bgm-control');
const music = document.querySelector('#loginCoverMusic');
if (musicButton && music) {
  music.volume = 0.32;
  musicButton.addEventListener('click', async () => {
    if (music.paused) {
      try {
        await music.play();
        musicButton.classList.add('is-playing');
        musicButton.setAttribute('aria-pressed', 'true');
        musicButton.setAttribute('aria-label', '关闭背景音乐');
        musicButton.title = '关闭背景音乐';
      } catch {
        musicButton.setAttribute('aria-label', '背景音乐暂时无法播放');
        musicButton.title = '背景音乐暂时无法播放';
      }
      return;
    }
    music.pause();
    musicButton.classList.remove('is-playing');
    musicButton.setAttribute('aria-pressed', 'false');
    musicButton.setAttribute('aria-label', '开启背景音乐');
    musicButton.title = '开启背景音乐';
  });
  document.addEventListener('click', event => {
    if (!event.target.closest('[data-open-auth]')) return;
    music.pause();
    musicButton.classList.remove('is-playing');
    musicButton.setAttribute('aria-pressed', 'false');
    musicButton.setAttribute('aria-label', '开启背景音乐');
    musicButton.title = '开启背景音乐';
  });
}
