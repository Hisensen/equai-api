const CAPS = [420, 520, 640, 760, 160];
const PROBE_TIMEOUT = 12000;
const cache = new Map();
const indexOriginals = new Map();
const objectOriginals = new Map();
const objectUrls = new Set();
const jobs = [];
let activeJobs = 0;
let generation = 0;

function pageCap() {
  const width = window.innerWidth || document.documentElement.clientWidth;
  return width <= 380 ? 420 : width <= 640 ? 520 : width <= 900 ? 640 : 760;
}

function shotIndex(value) {
  if (value === null || String(value).trim() === '') return null;
  const index = Number(value);
  return Number.isInteger(index) && index >= 0 ? index : null;
}

function originalFor(image, index) {
  const explicit = image.getAttribute('data-original');
  if (explicit) return explicit;
  const source = image.getAttribute('src') || image.currentSrc || image.src;
  return objectOriginals.get(source)
    || (source && !source.startsWith('blob:') ? source : indexOriginals.get(index))
    || '';
}

function currentImages() {
  return [...document.querySelectorAll('img[data-shot]')];
}

function applyToCurrentImages(entry) {
  if (entry.generation !== generation) return;
  currentImages().forEach(image => {
    const index = shotIndex(image.getAttribute('data-shot'));
    if (index === null || !image.isConnected || originalFor(image, index) !== entry.original) return;
    const cap = image.hasAttribute('data-avatar') ? 160 : pageCap();
    const source = entry.variants.get(cap) || entry.original;
    image.setAttribute('data-original', entry.original);
    if (image.getAttribute('src') !== source) image.src = source;
  });
}

function drainJobs() {
  while (activeJobs < 2 && jobs.length) {
    const job = jobs.shift();
    if (job.generation !== generation) {
      job.resolve();
      continue;
    }
    activeJobs++;
    Promise.resolve().then(job.run).catch(() => {}).finally(() => {
      activeJobs--;
      job.resolve();
      drainJobs();
    });
  }
}

function queueJob(run, epoch) {
  return new Promise(resolve => {
    jobs.push({ run, resolve, generation: epoch });
    drainJobs();
  });
}

function probeImage(entry) {
  return new Promise(resolve => {
    const image = new Image();
    let finished = false;
    let timer;
    function finish(result) {
      if (finished) return;
      finished = true;
      window.clearTimeout(timer);
      image.onload = null;
      image.onerror = null;
      entry.cancelProbe = null;
      if (!result || entry.generation !== generation) {
        image.removeAttribute('src');
        resolve(null);
      } else {
        resolve(result);
      }
    }
    entry.cancelProbe = () => finish(null);
    image.crossOrigin = 'anonymous';
    image.decoding = 'async';
    image.onload = () => {
      const decoded = typeof image.decode === 'function' ? image.decode() : Promise.resolve();
      Promise.resolve(decoded).then(() => finish(image), () => finish(null));
    };
    image.onerror = () => finish(null);
    timer = window.setTimeout(() => finish(null), PROBE_TIMEOUT);
    image.src = entry.original;
  });
}

function scaleImage(entry, image, cap) {
  const longest = Math.max(image.naturalWidth, image.naturalHeight);
  if (!longest || entry.generation !== generation) return Promise.resolve(null);
  if (longest <= cap) return Promise.resolve(entry.original);
  const canvas = document.createElement('canvas');
  const ratio = cap / longest;
  canvas.width = Math.max(1, Math.round(image.naturalWidth * ratio));
  canvas.height = Math.max(1, Math.round(image.naturalHeight * ratio));
  try {
    const context = canvas.getContext('2d');
    if (!context) return Promise.resolve(null);
    context.drawImage(image, 0, 0, canvas.width, canvas.height);
    return new Promise(resolve => {
      try {
        canvas.toBlob(blob => {
          canvas.width = 0;
          canvas.height = 0;
          if (!blob || blob.type !== 'image/webp' || entry.generation !== generation) {
            resolve(null);
            return;
          }
          try {
            const url = URL.createObjectURL(blob);
            objectUrls.add(url);
            objectOriginals.set(url, entry.original);
            resolve(url);
          } catch {
            resolve(null);
          }
        }, 'image/webp', 0.88);
      } catch {
        canvas.width = 0;
        canvas.height = 0;
        resolve(null);
      }
    });
  } catch {
    canvas.width = 0;
    canvas.height = 0;
    return Promise.resolve(null);
  }
}

function getEntry(original, preferredCap) {
  if (cache.has(original)) return cache.get(original);
  const entry = {
    original,
    generation,
    variants: new Map(),
    cancelProbe: null,
    pending: null,
  };
  cache.set(original, entry);
  entry.pending = queueJob(async () => {
    if (entry.generation !== generation) return;
    const image = await probeImage(entry);
    if (!image) return;
    try {
      // All small variants share one original decode; no full-size bitmap stays cached.
      const caps = [...new Set([preferredCap, ...CAPS])];
      for (const cap of caps) {
        if (entry.generation !== generation) return;
        const source = await scaleImage(entry, image, cap);
        if (!source) break;
        entry.variants.set(cap, source);
        // Display the requested size once; other variants do not change this viewport.
        if (cap === preferredCap) applyToCurrentImages(entry);
      }
    } finally {
      image.removeAttribute('src');
    }
  }, generation);
  return entry;
}

export function decodedThumb(index, originalUrl) {
  const original = String(originalUrl || '');
  const validIndex = shotIndex(index);
  if (validIndex !== null && original) indexOriginals.set(validIndex, original);
  return cache.get(original)?.variants.get(pageCap()) || original;
}

export async function prepareImages(root = document) {
  const images = [...root.querySelectorAll('img[data-shot]')];
  if (root.matches?.('img[data-shot]')) images.unshift(root);
  const entries = new Set();
  images.forEach(image => {
    const index = shotIndex(image.getAttribute('data-shot'));
    if (index === null) return;
    const original = originalFor(image, index);
    if (!original) return;
    indexOriginals.set(index, original);
    image.setAttribute('data-original', original);
    const cap = image.hasAttribute('data-avatar') ? 160 : pageCap();
    const entry = getEntry(original, cap);
    entries.add(entry);
    applyToCurrentImages(entry);
  });
  await Promise.all([...entries].map(entry => entry.pending));
  entries.forEach(applyToCurrentImages);
}

export function releaseImageCache() {
  generation++;
  cache.forEach(entry => entry.cancelProbe?.());
  jobs.splice(0).forEach(job => job.resolve());
  currentImages().forEach(image => {
    const source = image.getAttribute('src') || image.currentSrc || image.src;
    const original = objectOriginals.get(source);
    if (original) image.src = original;
  });
  objectUrls.forEach(url => URL.revokeObjectURL(url));
  cache.clear();
  indexOriginals.clear();
  objectOriginals.clear();
  objectUrls.clear();
}
