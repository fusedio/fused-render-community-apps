// Arcade car: velocity split into forward + lateral slip (grip bleeds the slip, handbrake lets it
// slide), yaw scales with speed, ballistic hops off ramps. Same model drives the player and every AI.
import * as THREE from 'three';
import { SIZE, HALF, clamp } from './city.js';

export const CARS = [
  { key: 'bug', name: 'Bug', blurb: 'Round little 60s people’s car. Nimble and forgiving', color: 0x7ec8e3, accel: 17, top: 31, grip: 8.5, turn: 2.7, mass: 1, len: 3.9, w: 1.8, ht: 1.5 },
  { key: 'cab', name: 'Taxi', blurb: '70s checker cab. The all-rounder', color: 0xf5c518, accel: 17, top: 35, grip: 7.5, turn: 2.4, mass: 1.2, len: 4.6, w: 1.9, ht: 1.5, taxi: true },
  { key: 'muscle', name: 'Muscle', blurb: 'Late-60s fastback. Big power, loose tail', color: 0xd63031, accel: 21, top: 40, grip: 5.5, turn: 2.3, mass: 1.4, len: 4.8, w: 2, ht: 1.35 },
  { key: 'sport', name: 'Sports', blurb: 'Long-hood 60s coupe. Fastest, stays planted', color: 0xf7f7f7, accel: 23, top: 44, grip: 8, turn: 2.4, mass: 1.1, len: 4.4, w: 1.95, ht: 1.25 },
  { key: 'truck', name: 'Van', blurb: '50s step-van. Slow. Shoves everyone', color: 0x2e86de, accel: 13, top: 29, grip: 7, turn: 1.8, mass: 3.5, len: 7, w: 2.5, ht: 3, truck: true },
];
export const SEDAN = { key: 'sedan', accel: 12, top: 26, grip: 7, turn: 2.1, mass: 1.3, len: 4.5, w: 1.9, ht: 1.5 };
export const COP = { key: 'cop', color: 0x15181f, accel: 21, top: 41, grip: 8, turn: 2.5, mass: 1.5, len: 4.7, w: 1.95, ht: 1.5, cop: true };
export const TRAFFIC_COLORS = [0x95a5a6, 0x34495e, 0x8e44ad, 0x27ae60, 0xc0392b, 0xecf0f1, 0x16a085, 0xd35400, 0x7f8c8d, 0x2c3e50, 0xb7c7a0, 0x6e4b3a];

// ---------- procedural classic-car bodies ----------
// A model is a side profile (fx = fraction of length, +front; fy = fraction of height) extruded across the
// width with rounded edges. `body` is the painted lower shell (wheel arches are cut automatically), `cab` is
// the tinted greenhouse, and the part of it above `roof` gets a painted cap. Everything is merged into three
// meshes per car (paint, trim with vertex colours, unlit lamps) and the geometry is cached per model.
const CHROME = 0xd8dde3, GLASS = 0x121c26, RUBBER = 0x161616, LAMP = 0xfff3c4, RED = 0xe0231c, AMBER = 0xffb52e;
const roundLights = (fy, dx, r, z) => ({ fy, dx, r, z }), boxTail = (fy, dx, w, h, o = {}) => ({ fy, dx, w, h, ...o }); // tails: round, or n boxes per side
const MODELS = {
  bug: { base: 0.2, rw: 0.33, wheels: [-0.3, 0.32], bumper: 0.27,
    body: [[0.5, 0.34, 1], [0.47, 0.46, 1], [0.38, 0.52, 1], [0.24, 0.56], [-0.22, 0.56], [-0.36, 0.52, 1], [-0.46, 0.43, 1], [-0.5, 0.33, 1], [-0.5, 0.28]],
    cab: [[0.22, 0.56], [0.17, 0.74, 1], [0.08, 0.91, 1], [-0.05, 0.97, 1], [-0.19, 0.92, 1], [-0.3, 0.78, 1], [-0.36, 0.56]], cabW: 0.9, roof: 0.83, tumble: 0.2,
    lights: roundLights(0.48, 0.34, 0.14, 0.45), tail: boxTail(0.46, 0.3, 0.12, 0, { round: true, z: -0.46 }), grille: { fy: 0.36, h: 0.05 }, lines: [-0.34] },
  cab: { base: 0.2, rw: 0.33, wheels: [-0.31, 0.32], bumper: 0.27,
    body: [[0.5, 0.5], [0.47, 0.54], [0.2, 0.56], [-0.24, 0.56], [-0.44, 0.55, 1], [-0.49, 0.5, 1], [-0.5, 0.28]],
    cab: [[0.2, 0.56], [0.13, 0.95], [-0.22, 0.95, 1], [-0.28, 0.8, 1], [-0.31, 0.56]], cabW: 0.9, roof: 0.87, tumble: 0.12,
    lights: roundLights(0.42, 0.35, 0.15), tail: boxTail(0.45, 0.35, 0.2, 0.16), grille: { fy: 0.42, h: 0.1 }, lines: [0.22, -0.3],
    bands: [{ x: [-0.36, 0.42], y: [0.39, 0.45], c: 0x1a1a1a }], sign: { w: 0.7, h: 0.22, c: LAMP, fx: -0.05 } },
  muscle: { base: 0.2, rw: 0.34, wheels: [-0.32, 0.33], bumper: 0.27,
    body: [[0.5, 0.5], [0.46, 0.55], [0.12, 0.58], [-0.2, 0.58], [-0.44, 0.62, 1], [-0.49, 0.57, 1], [-0.5, 0.28]],
    cab: [[0.11, 0.58], [0.0, 0.94], [-0.14, 0.95, 1], [-0.3, 0.8, 1], [-0.44, 0.6]], cabW: 0.9, roof: 0.86, tumble: 0.16,
    lights: roundLights(0.42, 0.37, 0.14), tail: boxTail(0.47, 0.3, 0.13, 0.11, { n: 3, gap: 0.16 }), grille: { fy: 0.43, h: 0.11 }, lines: [0.14, -0.42],
    scoop: { fx: 0.3, w: 0.28, l: 0.16, fy: 0.58 } },
  sport: { base: 0.19, rw: 0.31, wheels: [-0.32, 0.34], bumper: 0.25,
    body: [[0.5, 0.4, 1], [0.46, 0.48, 1], [0.25, 0.56, 1], [0.06, 0.6], [-0.3, 0.6], [-0.47, 0.57], [-0.5, 0.5], [-0.5, 0.27]],
    cab: [[0.05, 0.6], [-0.07, 0.94, 1], [-0.19, 0.96, 1], [-0.3, 0.86, 1], [-0.43, 0.63]], cabW: 0.88, roof: 0.86, tumble: 0.18,
    lights: roundLights(0.42, 0.37, 0.13, 0.48), tail: boxTail(0.45, 0.32, 0.1, 0, { round: true, n: 2, gap: 0.13, z: -0.49 }), grille: { fy: 0.33, h: 0.07 }, lines: [0.03] },
  truck: { base: 0.1, rw: 0.42, wheels: [-0.3, 0.3], bumper: 0.14,
    body: [[0.5, 0.3], [0.46, 0.36], [0.32, 0.38], [0.3, 0.96], [0.27, 1.0], [-0.48, 1.0], [-0.5, 0.97], [-0.5, 0.14]],
    cab: [[0.313, 0.5], [0.313, 0.82], [0.02, 0.82], [0.02, 0.5]], cabW: 1.02, tumble: 0.05,
    lights: roundLights(0.27, 0.34, 0.16), tail: boxTail(0.3, 0.38, 0.16, 0.3), grille: { fy: 0.25, h: 0.1 },
    bands: [{ x: [-0.48, 0.28], y: [0.6, 0.68], c: 0xf2f2ee }], doors: true },
  cop: { base: 0.2, rw: 0.33, wheels: [-0.32, 0.33], bumper: 0.27,
    body: [[0.5, 0.52], [0.47, 0.56], [0.15, 0.58], [-0.25, 0.58], [-0.46, 0.59], [-0.5, 0.55], [-0.5, 0.28]],
    cab: [[0.15, 0.58], [0.06, 0.94], [-0.2, 0.94, 1], [-0.27, 0.8, 1], [-0.32, 0.58]], cabW: 0.9, roof: 0.86, roofColor: 0xf2f4f7, tumble: 0.14,
    lights: roundLights(0.43, 0.36, 0.14), tail: boxTail(0.48, 0.3, 0.3, 0.1), grille: { fy: 0.43, h: 0.12 }, lines: [0.17, -0.34],
    bands: [{ x: [-0.24, 0.16], y: [0.25, 0.53], c: 0xf2f4f7 }] },
  sedan0: { base: 0.2, rw: 0.33, wheels: [-0.31, 0.32], bumper: 0.27, white: true, // 50s fins
    body: [[0.5, 0.5], [0.46, 0.54], [0.15, 0.57], [-0.2, 0.57], [-0.47, 0.63], [-0.5, 0.58], [-0.5, 0.28]],
    cab: [[0.15, 0.57], [0.06, 0.93], [-0.14, 0.93, 1], [-0.21, 0.78, 1], [-0.26, 0.57]], cabW: 0.9, roof: 0.85, tumble: 0.14,
    lights: roundLights(0.42, 0.35, 0.14), tail: boxTail(0.54, 0.4, 0.1, 0.18), grille: { fy: 0.42, h: 0.1 }, lines: [0.17, -0.26],
    bands: [{ x: [-0.5, 0.5], y: [0.49, 0.515], c: CHROME }] },
  sedan1: { base: 0.2, rw: 0.33, wheels: [-0.31, 0.32], bumper: 0.27, // boxy 80s
    body: [[0.5, 0.5], [0.48, 0.53], [0.15, 0.55], [-0.22, 0.55], [-0.5, 0.57], [-0.5, 0.28]],
    cab: [[0.15, 0.55], [0.05, 0.93], [-0.19, 0.93], [-0.29, 0.55]], cabW: 0.9, roof: 0.86, tumble: 0.12,
    lights: { fy: 0.42, dx: 0.33, r: 0.13, rect: true }, tail: boxTail(0.47, 0.28, 0.5, 0.11), grille: { fy: 0.42, h: 0.1 }, lines: [0.17, -0.3] },
  sedan2: { base: 0.2, rw: 0.33, wheels: [-0.31, 0.32], bumper: 0.27, // wagon
    body: [[0.5, 0.5], [0.48, 0.53], [0.15, 0.55], [-0.22, 0.55], [-0.5, 0.57], [-0.5, 0.28]],
    cab: [[0.15, 0.55], [0.05, 0.93], [-0.44, 0.93], [-0.48, 0.55]], cabW: 0.9, roof: 0.86, rack: true, tumble: 0.12,
    lights: roundLights(0.42, 0.34, 0.14), tail: boxTail(0.42, 0.38, 0.14, 0.26), grille: { fy: 0.42, h: 0.1 }, lines: [0.17] },
};

const box = (w, h, d) => new THREE.BoxGeometry(w, h, d), cyl = (r, h, n = 12) => new THREE.CylinderGeometry(r, r, h, n);
function col(geo, hex) { // bake a flat vertex colour so parts with different colours can share a material
  const g = geo.index ? geo.toNonIndexed() : geo, n = g.getAttribute('position').count, c = new THREE.Color(hex), a = new Float32Array(n * 3);
  for (let i = 0; i < n; i++) a.set([c.r, c.g, c.b], i * 3);
  g.setAttribute('color', new THREE.BufferAttribute(a, 3)); return g;
}
function merge(geos) {
  const out = new THREE.BufferGeometry();
  for (const k of ['position', 'normal', 'color']) {
    const arrs = geos.map((g) => g.getAttribute(k).array), a = new Float32Array(arrs.reduce((s, x) => s + x.length, 0));
    let o = 0; for (const x of arrs) { a.set(x, o); o += x.length; }
    out.setAttribute(k, new THREE.BufferAttribute(a, 3));
  }
  return out;
}
function trace(s, pts, L, H) { // runs of "smooth" points ([fx, fy, 1]) become a spline
  for (let i = 0; i < pts.length;) {
    if (!pts[i][2]) { s.lineTo(pts[i][0] * L, pts[i][1] * H); i++; continue; }
    const run = []; while (i < pts.length && pts[i][2]) run.push(new THREE.Vector2(pts[i][0] * L, pts[i][1] * H)), i++;
    s.splineThru(run);
  }
}
function shape(pts, L, H) { const s = new THREE.Shape(); s.moveTo(pts[0][0] * L, pts[0][1] * H); trace(s, pts.slice(1), L, H); s.closePath(); return s; }
function clipAbove(pts, y) {
  const out = [];
  for (let i = 0; i < pts.length; i++) {
    const a = pts[i], b = pts[(i + 1) % pts.length];
    if (a.y >= y) out.push(a);
    if ((a.y >= y) !== (b.y >= y)) out.push(new THREE.Vector2(a.x + (b.x - a.x) * (y - a.y) / (b.y - a.y), y));
  }
  return out;
}
function taper(geo, y0, y1, k, pow = 1) { // tumblehome: pull the sides in by k at y1, starting from y0
  const p = geo.getAttribute('position');
  for (let i = 0; i < p.count; i++) { const t = clamp((p.getY(i) - y0) / (y1 - y0), 0, 1); p.setX(i, p.getX(i) * (1 - k * t ** pow)); }
  return geo.computeVertexNormals(), geo;
}
const yAt = (pts, fx) => { for (let i = 1; i < pts.length; i++) if (pts[i][0] <= fx) { const [a, b] = [pts[i - 1], pts[i]]; return a[1] + (b[1] - a[1]) * (fx - a[0]) / (b[0] - a[0] || 1); } return pts[pts.length - 1][1]; };
// profile shape (x = length, y = height) -> solid of width w with rounded edges, facing +z
const ex = (sh, w, b) => new THREE.ExtrudeGeometry(sh, { depth: w - 2 * b, bevelEnabled: b > 0, bevelThickness: b, bevelSize: b, bevelSegments: 2, curveSegments: 8 }).translate(0, 0, b - w / 2).rotateY(-Math.PI / 2);

function build(m, s) {
  const L = s.len, W = s.w, H = s.ht, paint = [], trim = [], glow = [], add = (arr, geo, c) => arr.push(col(geo, c));
  const base = m.base * H, ra = m.rw + 0.1, body = new THREE.Shape();
  body.moveTo(-L / 2, base);
  for (const fx of m.wheels) { const cx = fx * L, th = Math.asin((base - m.rw) / ra); body.lineTo(cx - ra * Math.cos(th), base); body.absarc(cx, m.rw, ra, Math.PI - th, th, true); }
  body.lineTo(L / 2, base); trace(body, m.body, L, H); body.closePath();
  const belt = Math.max(...m.body.map((p) => p[1])) * H, tum = m.tumble || 0;
  add(paint, taper(ex(body, W, 0.06), m.rw + 0.1, belt, 0.06, 2), 0xffffff); // sides lean in above the wheels
  let top = H;
  if (m.cab) {
    const cs = shape(m.cab, L, H), cw = W * m.cabW, cabTop = Math.max(...m.cab.map((p) => p[1])) * H; top = cabTop + 0.03;
    add(trim, taper(ex(cs, cw, 0.03), belt, cabTop, tum), GLASS);
    if (m.roof) { const cap = taper(ex(new THREE.Shape(clipAbove(cs.getPoints(8), m.roof * H)), cw + 0.04, 0.05), belt, cabTop, tum); m.roofColor ? add(trim, cap, m.roofColor) : add(paint, cap, 0xffffff); top += 0.02; }
  }
  const zf = L / 2 + 0.07, zr = -zf, lt = m.lights, tl = m.tail;
  for (const z of [zf, zr]) add(trim, cyl(0.075, W * 1.04, 10).rotateZ(Math.PI / 2).translate(0, m.bumper * H, z), CHROME); // round-section bumpers
  add(trim, box(0.34, 0.17, 0.03).translate(0, m.bumper * H + 0.2, zr - 0.01), 0x1a1c20); add(glow, box(0.3, 0.13, 0.03).translate(0, m.bumper * H + 0.2, zr - 0.02), 0xe9e4c8); // licence plate
  for (const fx of m.lines || []) add(trim, box(W * 0.8 * (1 - tum * 0.2), 0.035, 0.035).translate(0, yAt(m.body, fx) * H + 0.06, fx * L), 0x1a1c20); // hood / trunk seams
  if (m.doors) { add(trim, box(0.03, H * 0.55, 0.035).translate(0, H * 0.45, zr - 0.02), 0x1a1c20); for (const sx of [1, -1]) { add(trim, box(0.05, 0.28, 0.035).translate(sx * 0.12, H * 0.42, zr - 0.03), CHROME); add(trim, box(W * 0.3, H * 0.14, 0.03).translate(sx * W * 0.22, H * 0.66, zr - 0.01), GLASS); } } // van rear doors + windows
  add(trim, box(W * 0.46, m.grille.h * H, 0.06).translate(0, m.grille.fy * H, zf), 0x1a1c20);
  add(trim, box(W * 0.74, 0.035, 0.08).translate(0, m.grille.fy * H, zf), CHROME);
  for (const sx of [1, -1]) {
    const x = sx * lt.dx * W, y = lt.fy * H, z = (lt.z ?? 0.5) * L + 0.05;
    if (lt.rect) { add(glow, box(lt.r * 2.6, lt.r * 1.3, 0.06).translate(x, y, z + 0.02), LAMP); add(trim, box(lt.r * 2.6 + 0.06, lt.r * 1.3 + 0.06, 0.05).translate(x, y, z), CHROME); }
    else { add(glow, cyl(lt.r, 0.06).rotateX(Math.PI / 2).translate(x, y, z + 0.02), LAMP); add(trim, cyl(lt.r + 0.035, 0.05).rotateX(Math.PI / 2).translate(x, y, z), CHROME); }
    for (let k = 0; k < (tl.n || 1); k++) {
      const tx = sx * (tl.dx * W - k * (tl.gap || 0)), ty = tl.fy * H, tz = tl.z ? tl.z * L - 0.06 : zr;
      if (tl.round) { add(glow, cyl(tl.w, 0.06).rotateX(Math.PI / 2).translate(tx, ty, tz - 0.02), RED); add(trim, cyl(tl.w + 0.03, 0.05).rotateX(Math.PI / 2).translate(tx, ty, tz), CHROME); }
      else { add(glow, box(tl.w, tl.h, 0.06).translate(tx, ty, tz - 0.02), RED); add(trim, box(tl.w + 0.04, tl.h + 0.04, 0.05).translate(tx, ty, tz), CHROME); }
    }
    add(glow, box(0.1, 0.07, 0.05).translate(sx * (lt.dx * W + lt.r + 0.12), m.bumper * H + 0.13, zf), AMBER);
  }
  for (const fx of m.wheels) for (const sx of [1, -1]) {
    const z = fx * L, xo = sx * (W / 2 - 0.015);
    add(trim, cyl(m.rw, 0.28, 16).rotateZ(Math.PI / 2).translate(sx * (W / 2 - 0.16), m.rw, z), RUBBER);
    if (m.white) add(trim, cyl(m.rw * 0.78, 0.012, 16).rotateZ(Math.PI / 2).translate(xo, m.rw, z), 0xe8e6dc);
    add(trim, cyl(m.rw * 0.5, 0.03).rotateZ(Math.PI / 2).translate(xo, m.rw, z), CHROME);
  }
  for (const b of m.bands || []) add(trim, box(W + 0.03, (b.y[1] - b.y[0]) * H, (b.x[1] - b.x[0]) * L).translate(0, (b.y[0] + b.y[1]) / 2 * H, (b.x[0] + b.x[1]) / 2 * L), b.c);
  if (m.sign) { add(glow, box(m.sign.w, m.sign.h, 0.28).translate(0, top + 0.05 + m.sign.h / 2, m.sign.fx * L), m.sign.c); add(trim, box(0.12, 0.06, 0.12).translate(0, top + 0.02, m.sign.fx * L), 0x222222); }
  if (m.scoop) add(paint, box(W * m.scoop.w, 0.09, L * m.scoop.l).translate(0, m.scoop.fy * H + 0.03, m.scoop.fx * L), 0xffffff);
  if (m.rack) for (const sx of [1, -1]) add(trim, box(0.05, 0.07, L * 0.42).translate(sx * W * 0.3, top + 0.06, -L * 0.2), CHROME);
  if (s.cop) add(trim, box(W * 0.6, 0.06, 0.3).translate(0, top + 0.03, -L * 0.06), CHROME);
  return { paint: merge(paint), trim: merge(trim), glow: merge(glow), top };
}

const CACHE = new Map();
const trimMat = new THREE.MeshPhongMaterial({ vertexColors: true, shininess: 90, specular: 0x777777 });
const glowMat = new THREE.MeshBasicMaterial({ vertexColors: true });
const barGeo = new THREE.BoxGeometry(0.44, 0.2, 0.3);
const shadowGeo = new THREE.PlaneGeometry(1, 1).rotateX(-Math.PI / 2);
const shadowMat = new THREE.MeshBasicMaterial({ color: 0, transparent: true, opacity: 0.3, depthWrite: false });

// Group facing +z with its base at y = 0. userData.body is the paint (darkened by damage); userData.bar the cop lights.
export function carMesh(spec, color) {
  const key = spec.key === 'sedan' ? 'sedan' + ((Math.random() * 3) | 0) : spec.key;
  const b = CACHE.get(key) || CACHE.set(key, build(MODELS[key], spec)).get(key);
  const g = new THREE.Group(), body = new THREE.MeshPhongMaterial({ color, shininess: 50, specular: 0x555555 });
  g.add(new THREE.Mesh(b.paint, body), new THREE.Mesh(b.trim, trimMat), new THREE.Mesh(b.glow, glowMat));
  if (spec.cop) {
    g.userData.bar = [0xff2a2a, 0x2a6bff].map((c, k) => { const m = new THREE.Mesh(barGeo, new THREE.MeshBasicMaterial({ color: c })); m.position.set(k ? 0.3 : -0.3, b.top + 0.16, -spec.len * 0.06); g.add(m); return m; });
  }
  g.rotation.order = 'YXZ';
  g.userData.body = body; g.userData.baseColor = new THREE.Color(color);
  return g;
}

export class Car {
  constructor(scene, spec, color, kind) {
    Object.assign(this, { spec, kind, x: 0, z: 0, y: 0, h: 0, vx: 0, vz: 0, vy: 0, steer: 0, throttle: 0, brake: 0, handbrake: false,
      grounded: true, air: 0, damage: 0, frozen: 0, topMul: 1, r: (spec.len + spec.w) / 4 + 0.15 });
    this.mesh = carMesh(spec, color ?? spec.color);
    this.shadow = new THREE.Mesh(shadowGeo, shadowMat); this.shadow.scale.set(spec.w * 1.3, 1, spec.len * 1.1);
    scene.add(this.mesh, this.shadow);
  }
  get speed() { return Math.hypot(this.vx, this.vz); }
  get fwd() { return this.vx * Math.sin(this.h) + this.vz * Math.cos(this.h); }
  place(x, z, h, city) { Object.assign(this, { x, z, h, vx: 0, vz: 0, vy: 0, y: city.groundAt(x, z), grounded: true, air: 0 }); }
  remove(scene) { scene.remove(this.mesh, this.shadow); }

  // returns { hit: wall impact speed, landed: airtime of a landing this step }
  step(dt, city) {
    const s = this.spec, px = this.x, pz = this.z, fx = Math.sin(this.h), fz = Math.cos(this.h);
    let vf = this.vx * fx + this.vz * fz, vl = -this.vx * fz + this.vz * fx, hit = 0, landed = 0;
    if (this.frozen > 0) { this.frozen -= dt; vf *= Math.exp(-5 * dt); vl *= Math.exp(-5 * dt); }
    else if (this.grounded) {
      const top = s.top * this.topMul;
      if (this.throttle > 0) vf += this.throttle * s.accel * Math.max(0, 1 - vf / top) * dt;
      if (this.brake > 0) vf -= (vf > 0.5 ? this.brake * 30 : this.brake * s.accel * 0.6) * dt;
      vf = Math.max(vf, -top * 0.3);
      const roll = (this.throttle || this.brake ? 0.4 : 3) + (this.handbrake ? 9 : 0);
      vf -= Math.sign(vf) * Math.min(Math.abs(vf), roll * dt);
      vl *= Math.exp(-(this.handbrake ? 1.3 : s.grip) * dt);
      const grip = Math.min(1, Math.abs(vf) / 5) * Math.sign(vf);
      this.h -= this.steer * s.turn * grip * (this.handbrake ? 1.4 : 1) / (1 + Math.abs(vf) / 45) * dt;
    }
    this.vx = vf * fx - vl * fz; this.vz = vf * fz + vl * fx;
    this.x += this.vx * dt; this.z += this.vz * dt;

    let g = city.groundAt(this.x, this.z);
    if (this.grounded && g - this.y > 0.8) { // the tall face of a ramp is a wall
      hit = this.speed; this.x = px; this.z = pz; this.vx *= -0.3; this.vz *= -0.3; g = city.groundAt(px, pz);
    }
    if (this.grounded) { if (g < this.y - 0.2) this.grounded = false; else { this.vy = (g - this.y) / dt; this.y = g; } }
    if (!this.grounded) {
      this.air += dt; this.vy -= 26 * dt; this.y += this.vy * dt;
      if (this.y <= g) { landed = this.air; Object.assign(this, { y: g, vy: 0, grounded: true, air: 0 }); }
    }
    for (const b of city.near(this.x, this.z)) {
      if (b.box) hit = Math.max(hit, this.pushBox(b.box));
      for (const t of b.trees) hit = Math.max(hit, this.pushCircle(t.x, t.z, t.r));
    }
    const lo = -HALF - 1 + this.r, hi = SIZE + HALF + 1 - this.r;
    if (this.x < lo) { this.x = lo; hit = Math.max(hit, this.bounce(1, 0)); } else if (this.x > hi) { this.x = hi; hit = Math.max(hit, this.bounce(-1, 0)); }
    if (this.z < lo) { this.z = lo; hit = Math.max(hit, this.bounce(0, 1)); } else if (this.z > hi) { this.z = hi; hit = Math.max(hit, this.bounce(0, -1)); }
    return { hit, landed };
  }
  pushBox(b) {
    let dx = this.x - clamp(this.x, b.x0, b.x1), dz = this.z - clamp(this.z, b.z0, b.z1), d = Math.hypot(dx, dz);
    if (d >= this.r) return 0;
    if (d < 1e-4) { // centre ended up inside: leave by the nearest face
      const ex = [this.x - b.x0, b.x1 - this.x, this.z - b.z0, b.z1 - this.z], k = ex.indexOf(Math.min(...ex));
      dx = [-1, 1, 0, 0][k]; dz = [0, 0, -1, 1][k]; d = -ex[k];
    } else { dx /= d; dz /= d; }
    this.x += dx * (this.r - d); this.z += dz * (this.r - d);
    return this.bounce(dx, dz);
  }
  pushCircle(cx, cz, cr) {
    const dx = this.x - cx, dz = this.z - cz, d = Math.hypot(dx, dz), rr = this.r + cr;
    if (d >= rr || d < 1e-4) return 0;
    this.x = cx + dx / d * rr; this.z = cz + dz / d * rr;
    return this.bounce(dx / d, dz / d);
  }
  bounce(nx, nz) {
    const vn = this.vx * nx + this.vz * nz;
    if (vn >= 0) return 0;
    this.vx = (this.vx - 1.35 * vn * nx) * 0.97; this.vz = (this.vz - 1.35 * vn * nz) * 0.97;
    return -vn;
  }
  sync(city, t) {
    const m = this.mesh;
    m.position.set(this.x, this.y, this.z); m.rotation.y = this.h;
    m.rotation.x = this.grounded ? -Math.atan(this.vy / Math.max(4, Math.abs(this.fwd))) * Math.sign(this.fwd || 1) : -clamp(this.vy * 0.03, -0.35, 0.35);
    m.userData.body.color.copy(m.userData.baseColor).multiplyScalar(1 - Math.min(this.damage, 100) / 170);
    this.shadow.position.set(this.x, city.groundAt(this.x, this.z) + 0.04, this.z); this.shadow.rotation.y = this.h;
    const bar = m.userData.bar;
    if (bar) { const on = this.siren && Math.floor(t * 6) % 2; bar[0].visible = !this.siren || !!on; bar[1].visible = !this.siren || !on; }
  }
}

// Circle-vs-circle with momentum exchange; onHit(a, b, closingSpeed) for crash sounds, damage, steals.
export function collideCars(cars, onHit) {
  for (let i = 0; i < cars.length; i++) for (let j = i + 1; j < cars.length; j++) {
    const a = cars[i], b = cars[j], dx = b.x - a.x, dz = b.z - a.z, rr = a.r + b.r;
    if (Math.abs(dx) > rr || Math.abs(dz) > rr || Math.abs(a.y - b.y) > 2) continue;
    const d = Math.hypot(dx, dz); if (d >= rr || d < 1e-4) continue;
    const nx = dx / d, nz = dz / d, ma = a.spec.mass, mb = b.spec.mass, push = rr - d;
    a.x -= nx * push * mb / (ma + mb); a.z -= nz * push * mb / (ma + mb); b.x += nx * push * ma / (ma + mb); b.z += nz * push * ma / (ma + mb);
    const vrel = (b.vx - a.vx) * nx + (b.vz - a.vz) * nz;
    if (vrel >= 0) continue;
    const jn = -1.3 * vrel / (1 / ma + 1 / mb);
    a.vx -= jn / ma * nx; a.vz -= jn / ma * nz; b.vx += jn / mb * nx; b.vz += jn / mb * nz;
    onHit(a, b, -vrel);
  }
}
