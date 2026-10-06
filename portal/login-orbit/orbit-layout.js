const radians = degrees => degrees * Math.PI / 180;

export function orbitSizing(width, height) {
  const small = width <= 380, phone = width <= 640;
  const scale = Math.max(1, Math.min(width / 1920, height / 1080));
  const cap = Math.min(880, 480 * scale);
  const radius = Math.max(small ? 108 : phone ? 120 : 155,
    Math.min(cap, height * (small ? .38 : phone ? .42 : .46), width * (small ? .48 : phone ? .52 : .58)));
  return {
    radius,
    cardWidth: Math.round(Math.max(72, radius * (small ? .44 : phone ? .46 : .47))),
    perspective: width <= 380 ? 620 : width <= 640 ? 760 : width <= 900 ? 920 : 1150 * scale,
    accessScale: Math.min(880 / 480, scale),
  };
}

function rotatePitch(point, pitch) {
  const sine = Math.sin(radians(pitch)), cosine = Math.cos(radians(pitch));
  return { x: point.x, y: point.y * cosine - point.z * sine, z: point.y * sine + point.z * cosine };
}

// Match the card's CSS tangent plane and the camera perspective, including hover growth.
export function projectedCardBounds(unit, { radius, cardWidth, perspective, pitch = -4, tall = false }) {
  const lon = Math.atan2(unit.x, unit.z), lat = Math.asin(Math.max(-1, Math.min(1, unit.y)));
  const c = Math.cos(lon), s = Math.sin(lon), sl = Math.sin(lat), cl = Math.cos(lat);
  const halfWidth = cardWidth * 1.045 / 2;
  const halfHeight = cardWidth * (tall ? 1.25 : 1 / 1.5) * 1.045 / 2;
  const corners = [-1, 1].flatMap(horizontal => [-1, 1].map(vertical => {
    const u = horizontal * halfWidth, v = vertical * halfHeight;
    const point = rotatePitch({
      x: unit.x * radius + u * c + v * s * sl,
      y: -unit.y * radius + v * cl,
      z: unit.z * radius - u * s + v * c * sl,
    }, pitch);
    const projection = perspective / (perspective - point.z);
    return { x: point.x * projection, y: point.y * projection };
  }));
  return {
    left: Math.min(...corners.map(point => point.x)), right: Math.max(...corners.map(point => point.x)),
    top: Math.min(...corners.map(point => point.y)), bottom: Math.max(...corners.map(point => point.y)),
  };
}

export function intersectsCore(bounds, core) {
  return bounds.left < core.right && bounds.right > core.left && bounds.top < core.bottom && bounds.bottom > core.top;
}

export function clearOrbitCore(unit, sizing, core, { pitch = -4, tall = false } = {}) {
  const options = { ...sizing, pitch, tall };
  if (!intersectsCore(projectedCardBounds(unit, options), core)) return { ...unit };
  const camera = rotatePitch({ x: unit.x, y: -unit.y, z: unit.z }, pitch);
  const radial = Math.hypot(camera.x, camera.y);
  const direction = radial > 1e-9 ? { x: camera.x / radial, y: camera.y / radial } : { x: 1, y: 0 };
  const originalAngle = Math.atan2(radial, camera.z);
  let best = null;
  // Move only an obstructing destination along its own meridian, never the entire sphere.
  for (let step = 1; step < 360; step++) {
    const angle = step * Math.PI / 360;
    const candidateCamera = { x: direction.x * Math.sin(angle), y: direction.y * Math.sin(angle), z: Math.cos(angle) };
    const unpitched = rotatePitch(candidateCamera, -pitch);
    const candidate = { ...unit, x: unpitched.x, y: -unpitched.y, z: unpitched.z };
    if (intersectsCore(projectedCardBounds(candidate, options), core)) continue;
    const distance = Math.abs(angle - originalAngle);
    if (!best || distance < best.distance) best = { candidate, distance };
  }
  if (!best) {
    // Narrow viewports can have no clear point on that meridian; choose the closest
    // clear destination on the same left/right half, retaining every photograph.
    for (let bearing = 0; bearing < 72; bearing++) {
      const azimuth = bearing * Math.PI / 36;
      for (let step = 1; step < 180; step++) {
        const angle = step * Math.PI / 180;
        const candidateCamera = { x: Math.cos(azimuth) * Math.sin(angle), y: Math.sin(azimuth) * Math.sin(angle), z: Math.cos(angle) };
        if (candidateCamera.x * unit.x < 0) continue;
        const unpitched = rotatePitch(candidateCamera, -pitch);
        const candidate = { ...unit, x: unpitched.x, y: -unpitched.y, z: unpitched.z };
        const distance = 1 - (camera.x * candidateCamera.x + camera.y * candidateCamera.y + camera.z * candidateCamera.z);
        if (best && distance >= best.distance) continue;
        if (!intersectsCore(projectedCardBounds(candidate, options), core)) best = { candidate, distance };
      }
    }
  }
  return best ? best.candidate : { ...unit };
}
