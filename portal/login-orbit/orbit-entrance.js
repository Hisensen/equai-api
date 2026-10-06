const PHOTO_START = 120;
const PHOTO_DURATION = 4500;
const clamp = value => Math.max(0, Math.min(1, value));
const smooth = value => { const t = clamp(value); return t * t * (3 - 2 * t); };
const flightEase = value => {
  const t = clamp(value);
  return 1 - (1 - t) ** 3;
};

function tangentFrame(unit) {
  const length = Math.hypot(unit.x, unit.y, unit.z);
  const x = unit.x / length, y = -unit.y / length, z = unit.z / length;
  const horizontal = Math.hypot(x, z);
  // The pole basis matches the final rotateY(180deg) rotateX(latitude) pose.
  const right = horizontal > 0 ? [z / horizontal, 0, -x / horizontal] : [-1, 0, 0];
  const up = [y * right[2], z * right[0] - x * right[2], -y * right[0]];
  return [...right, ...up, x, y, z];
}

function transportFrame(frame, axisX, axisY, angle) {
  const cosine = Math.cos(angle), sine = Math.sin(angle), remainder = 1 - cosine;
  const transported = [];
  for (let column = 0; column < 9; column += 3) {
    const x = frame[column], y = frame[column + 1], z = frame[column + 2];
    const projection = axisX * x + axisY * y;
    transported.push(
      x * cosine + axisY * z * sine + axisX * projection * remainder,
      y * cosine - axisX * z * sine + axisY * projection * remainder,
      z * cosine + (axisX * y - axisY * x) * sine,
    );
  }
  return transported;
}

export function flightPose(unit, radius, progress) {
  const p = flightEase(progress);
  const tangentLength = Math.hypot(unit.x, unit.y);
  const angle = Math.atan2(tangentLength, -unit.z);
  const arc = angle * p;
  // This orthonormal form of SLERP avoids dividing by sin(angle) near either pole.
  const tangentX = tangentLength > 0 ? unit.x / tangentLength : 1;
  const tangentY = tangentLength > 0 ? unit.y / tangentLength : 0;
  const x = p === 0 ? 0 : p === 1 ? unit.x * radius : radius * Math.sin(arc) * tangentX;
  const y = p === 0 ? 0 : p === 1 ? -unit.y * radius : -radius * Math.sin(arc) * tangentY;
  const z = p === 0 ? -radius : p === 1 ? unit.z * radius : -radius * Math.cos(arc);
  const horizontal = Math.hypot(unit.x, unit.z);
  const lon = horizontal === 0 ? Math.PI : Math.atan2(unit.x === 0 ? 0 : unit.x, unit.z);
  const lat = Math.atan2(unit.y, horizontal);
  const finalFrame = tangentFrame(unit);
  // Transport the final tangent plane around this card's own fixed great-circle
  // axis. Its outward normal follows the radius, so no edge can enter the sphere.
  // One continuous matrix also avoids Euler branch changes or a final pole flip.
  const rotation = p === 1 ? finalFrame : transportFrame(finalFrame, -tangentY, -tangentX, arc - angle);
  return {
    x,
    y,
    z,
    lon: lon * 180 / Math.PI,
    lat: lat * 180 / Math.PI,
    roll: 0,
    rotation,
    scale: 1,
    opacity: 1,
    reveal: p,
  };
}

export function createOrbitEntrance(stage, units, { immediate = false } = {}) {
  const access = document.querySelector('#scene-access');
  if (access) access.hidden = immediate;
  const start = performance.now();
  const end = PHOTO_DURATION;
  const listeners = [];
  let skipped = immediate;
  let active = !skipped;
  let disposed = false;
  let finished = false;

  function listen(node, type, callback) {
    if (!node) return;
    node.addEventListener(type, callback);
    listeners.push(() => node.removeEventListener(type, callback));
  }
  function finish() {
    if (finished || disposed) return;
    finished = true;
    active = false;
    stage.inert = immediate;
    units.forEach(({ card }) => { card.inert = false; });
    stage.setAttribute('aria-busy', 'false');
    document.body.classList.remove('orbit-entering');
    document.body.style.removeProperty('--entry-chrome');
  }
  function skip() {
    if (finished || disposed) return;
    skipped = true;
    finish();
  }
  listen(document, 'keydown', event => {
    if (event.key === 'Escape' && active) { event.preventDefault(); skip(); }
  });
  document.body.classList.toggle('orbit-entering', active);
  stage.inert = immediate;
  units.forEach(({ card }) => { card.inert = active; });
  stage.setAttribute('aria-busy', String(active));

  function sample(now) {
    if (disposed) return units.map(() => 1);
    const elapsed = Math.max(0, now - start);
    if (active && elapsed >= end) finish();
    const phase = skipped || elapsed >= end ? 'ready' : elapsed < 3100 ? 'flight' : 'settling';
    if (stage.dataset.entrance !== phase) stage.dataset.entrance = phase;
    if (active) document.body.style.setProperty('--entry-chrome', String(smooth((elapsed - 3000) / 1300)));
    const progress = skipped ? 1 : clamp((elapsed - PHOTO_START) / (PHOTO_DURATION - PHOTO_START));
    return units.map(() => progress);
  }
  return {
    sample,
    get active() { return active; },
    dispose() {
      if (disposed) return;
      disposed = true;
      active = false;
      stage.inert = immediate;
      units.forEach(({ card }) => { card.inert = false; });
      stage.setAttribute('aria-busy', 'false');
      listeners.splice(0).forEach(remove => remove());
      document.body.classList.remove('orbit-entering');
      document.body.style.removeProperty('--entry-chrome');
    },
  };
}
