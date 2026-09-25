// Game: menu, five modes (Checkpoint, Blitz, Circuit, Cops & Robbers, Cruise), cops with heat,
// camera, input and the fixed-step loop. Menu choices live in fused.params so a link reopens them.
import * as THREE from 'three';
import { buildCity, N, BLOCK, HALF, LANE, SIZE, DIRS, inGrid, nearestNode, manhattan, clamp } from './city.js';
import { Car, CARS, SEDAN, COP, TRAFFIC_COLORS, collideCars, carMesh } from './car.js';
import { makeAI, drive, reseed, putOnRoad } from './ai.js';
import { HUD } from './hud.js';
import { audio } from './audio.js';

// ---------- scene ----------
const canvas = document.getElementById('c');
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(64, 1, 0.5, 800);
const hemi = new THREE.HemisphereLight(0xffffff, 0x556644, 1), sun = new THREE.DirectionalLight(0xffffff, 1);
sun.position.set(-0.4, 1, -0.3); scene.add(hemi, sun);
const TIMES = {
  day: { sky: 0xa8d8f0, hemi: [0xffffff, 0x667755, 1.0], sun: [0xffffff, 1.0] },
  dusk: { sky: 0xe9a57c, hemi: [0xffd9b8, 0x4a3b55, 0.8], sun: [0xffa860, 0.9] },
  night: { sky: 0x0b1230, hemi: [0x7080c0, 0x202030, 0.7], sun: [0x8090ff, 0.2] },
};
function applyTime(k) {
  const t = TIMES[k]; scene.background = new THREE.Color(t.sky); scene.fog = new THREE.Fog(t.sky, 140, 460);
  hemi.color.setHex(t.hemi[0]); hemi.groundColor.setHex(t.hemi[1]); hemi.intensity = t.hemi[2]; sun.color.setHex(t.sun[0]); sun.intensity = t.sun[1];
}
function resize() { renderer.setSize(window.innerWidth, window.innerHeight, false); camera.aspect = window.innerWidth / window.innerHeight; camera.updateProjectionMatrix(); }
window.addEventListener('resize', resize); resize();

const city = buildCity(scene, 7); // fixed seed: the same city every time, so it can be learned
const hud = new HUD(city);
const markers = new THREE.Group(), show = new THREE.Group(); scene.add(markers, show); // show: the chosen car, floated in front of the menu camera
const arrow = new THREE.Mesh(new THREE.ConeGeometry(0.7, 2.2, 3).rotateX(Math.PI / 2).rotateZ(Math.PI).scale(1, 0.35, 1), new THREE.MeshBasicMaterial({ color: 0xffd32a }));
scene.add(arrow);

// ---------- config (URL params) ----------
const MODES = {
  checkpoint: ['Checkpoint', 'Race 5 rivals to the finish through live traffic'],
  blitz: ['Blitz', 'Hit every checkpoint in any order before the clock runs out'],
  circuit: ['Circuit', '3 laps round the blocks. No traffic, no cops'],
  cnr: ['Cops & Robbers', 'Grab the gold, bring it home, ram to steal. First to 3'],
  cruise: ['Cruise', 'Free roam. Score jumps, smashes and close calls'],
};
const CHOICES = { mode: Object.keys(MODES), car: CARS.map((c) => c.key), traffic: ['off', 'light', 'heavy'], cops: ['off', 'some', 'lots'], time: ['day', 'dusk', 'night'], skill: ['easy', 'normal', 'hard'] };
const cfg = { mode: 'checkpoint', car: 'cab', traffic: 'light', cops: 'some', time: 'day', skill: 'normal' };
const P = window.fused && window.fused.params;
for (const k in cfg) { const v = P && P.get(k); if (v && CHOICES[k].includes(v)) cfg[k] = v; }
function setCfg(k, v) { cfg[k] = v; try { P && P.set(k, v); } catch (e) { /* outside fused-render */ } if (k === 'time') applyTime(v); renderMenu(); }

// ---------- state ----------
const STEP = 1 / 60, ri = (n) => (Math.random() * n) | 0, pick = (a) => a[ri(a.length)];
const wrap = (a) => Math.atan2(Math.sin(a), Math.cos(a)), dist = (a, b) => Math.hypot(a.x - b.x, a.z - b.z);
const nodePt = ([i, j]) => ({ x: i * BLOCK, z: j * BLOCK });
const fmt = (t) => {
  let m = Math.floor(t / 60), s = (t - m * 60).toFixed(1);
  if (s === '60.0') { m += 1; s = '0.0'; }
  return `${m}:${s.padStart(4, '0')}`;
};
const SKILL = { easy: [0.84, 12, 1.07], normal: [0.93, 15, 1.05], hard: [1.0, 18, 1.02] }; // top-speed share, corner speed, catch-up cap
let state = 'menu', T = 0, countT = 0, lastCount = 0, camMode = 0, camH = 0, resetCool = 0, copCool = 0, bustT = 0, pending = null, pendingT = 0, course = null;
let player = null, cars = [], cops = [], racers = [], robbers = [], race = null, cnr = null, cruise = null;
const camPos = new THREE.Vector3();

function addCar(spec, color, kind) { const c = new Car(scene, spec, color, kind); cars.push(c); return c; }
function clearAll() {
  for (const c of cars) c.remove(scene);
  cars = []; cops = []; racers = []; robbers = []; player = null; race = cnr = cruise = null; pending = null; bustT = 0; copCool = 0;
  markers.clear(); city.resetProps(); audio.stopLoops();
  hud.el.heat.className = 'heat'; arrow.visible = false;
}
function placeRandom(car, avoid, minD) {
  for (let k = 0; k < 50; k++) {
    const i = ri(N + 1), j = ri(N + 1), d = pick(DIRS.filter(([dx, dz]) => inGrid(i + dx, j + dz))), u = 12 + Math.random() * 34;
    const x = i * BLOCK + d[0] * u - d[1] * LANE, z = j * BLOCK + d[1] * u + d[0] * LANE;
    if (avoid.every((a) => Math.hypot(a.x - x, a.z - z) > minD) && cars.every((c) => c === car || Math.hypot(c.x - x, c.z - z) > 8)) { car.place(x, z, Math.atan2(d[0], d[1]), city); break; }
  }
  if (car.ai) reseed(car);
}
function spawnTraffic(n, avoid = []) {
  for (let k = 0; k < n; k++) { const c = addCar(SEDAN, pick(TRAFFIC_COLORS), 'traffic'); makeAI(c, 'traffic', { cruise: 10 + Math.random() * 5, turnSpeed: 7 }); placeRandom(c, avoid, 60); }
}
function spawnCops(n, avoid = []) {
  for (let k = 0; k < n; k++) { const c = addCar(COP, COP.color, 'cop'); makeAI(c, 'cop', { patrol: true, cruise: 13, turnSpeed: 8 }); c.chase = null; cops.push(c); placeRandom(c, avoid, 120); }
}

// ---------- courses ----------
const node = () => [ri(N + 1), ri(N + 1)];
function dirToward(a, b) { const dx = b[0] - a[0], dz = b[1] - a[1]; return Math.abs(dx) >= Math.abs(dz) && dx ? [Math.sign(dx), 0] : [0, Math.sign(dz) || 1]; }
function makeCourse(mode) {
  if (mode === 'circuit') {
    const w = 3 + ri(3), h = 3 + ri(3), i0 = ri(N - w + 1), j0 = ri(N - h + 1);
    return { cps: [[i0 + w, j0], [i0 + w, j0 + h], [i0, j0 + h], [i0, j0]], laps: 3, start: [i0 + 1, j0], d: [1, 0] };
  }
  for (;;) {
    const s = node(), cps = [];
    if (mode === 'blitz') {
      while (cps.length < 7) { const n = node(); if (manhattan(n, s) >= 2 && cps.every((c) => manhattan(c, n) >= 3)) cps.push(n); }
      const d = pick(DIRS.filter(([dx, dz]) => inGrid(s[0] - dx, s[1] - dz)));
      let at = s, left = cps.slice(), tour = 0; // nearest-neighbour tour sets a fair clock
      while (left.length) { left.sort((a, b) => manhattan(at, a) - manhattan(at, b)); tour += manhattan(at, left[0]); at = left.shift(); }
      return { cps, laps: 1, start: s, d, any: true, limit: Math.round(tour * BLOCK / { easy: 13, normal: 15.5, hard: 18 }[cfg.skill] + 10) };
    }
    let prev = s;
    while (cps.length < 6) { const n = node(), m = manhattan(prev, n); if (m >= 4 && m <= 9 && !cps.some((c) => c[0] === n[0] && c[1] === n[1])) { cps.push(n); prev = n; } }
    const d = dirToward(s, cps[0]);
    if (inGrid(s[0] - d[0], s[1] - d[1])) return { cps, laps: 1, start: s, d };
  }
}
function gridSpot(c, k) {
  const [si, sj] = c.start, [dx, dz] = c.d, row = k >> 1, side = k & 1 ? -1 : 1, back = HALF + 6 + row * 9;
  return [si * BLOCK - dx * back - dz * side * LANE * 0.9, sj * BLOCK - dz * back + dx * side * LANE * 0.9, Math.atan2(dx, dz)];
}

// ---------- start / end ----------
const RIVALS = [['Rico', 0xe84393], ['Vera', 0x00b894], ['Dash', 0xfdcb6e], ['Moxie', 0x6c5ce7], ['Tank', 0xe17055]];
function startGame(retry = false) {
  clearAll(); audio.init(); applyTime(cfg.time);
  const mode = cfg.mode, spec = CARS.find((c) => c.key === cfg.car), sk = SKILL[cfg.skill];
  player = addCar(spec, spec.color, 'player');
  if (mode === 'cruise' || mode === 'cnr') {
    if (mode === 'cruise') { placeRandom(player, [], 0); cruise = { score: 0, air: 0, smash: 0, close: 0 }; }
    else setupCnR(sk);
  } else {
    course = retry && course && course.mode === mode ? course : { ...makeCourse(mode), mode };
    const st = new Map(), mk = [];
    race = { ...course, st, order: [], t: 0, pen: 0, mk, key: mode + ':' + course.cps.flat().join('.') };
    const slots = mode === 'blitz' ? [0] : [0, 1, 2, 3, 5];
    if (mode !== 'blitz') slots.forEach((k, n) => {
      const [name, color] = RIVALS[n], s = CARS[ri(4)], r = addCar(s, color, 'racer');
      r.name = name; r.place(...gridSpot(course, k), city);
      makeAI(r, 'racer', { cruise: s.top * sk[0] * (0.97 + Math.random() * 0.06), turnSpeed: sk[1] + Math.random() * 3, lane: (Math.random() * 2 - 1) * 2.4, goal: () => { const q = st.get(r); return !q || q.done ? null : course.cps[q.cp]; },
        goalAfter: () => { const q = st.get(r); if (!q) return null; const n = q.cp + 1; return n < course.cps.length ? course.cps[n] : q.lap < course.laps - 1 ? course.cps[0] : null; } });
      reseed(r); racers.push(r);
    });
    player.place(...gridSpot(course, mode === 'blitz' ? 0 : 4), city);
    for (const c of [player, ...racers]) st.set(c, { cp: 0, lap: 0, done: false, left: new Set(course.cps.map((_, k) => k)) });
    const ring = new THREE.CylinderGeometry(10, 10, 14, 28, 1, true).translate(0, 7, 0);
    for (const [i, j] of course.cps) {
      const m = new THREE.Mesh(ring, new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.15, side: THREE.DoubleSide, depthWrite: false }));
      m.position.set(i * BLOCK, 0, j * BLOCK); markers.add(m); mk.push(m);
    }
  }
  const avoid = [player];
  if (mode !== 'circuit') { spawnTraffic({ off: 0, light: 22, heavy: 45 }[cfg.traffic], avoid); spawnCops(Math.max(mode === 'cnr' ? 2 : 0, { off: 0, some: 3, lots: 7 }[cfg.cops]), avoid); }
  camH = player.h; updateCamera(0, true);
  state = 'count'; countT = 3.2; lastCount = 0; hud.show(true); showScreen(null);
}
function finishWith(result, delay = 1.6) { if (!pending) { pending = result; pendingT = delay; } }
function best(key, value, lowerIsBetter) {
  const k = 'mm_best_' + key, old = Number(localStorage.getItem(k) || (lowerIsBetter ? Infinity : 0));
  const better = lowerIsBetter ? value < old : value > old;
  if (better) localStorage.setItem(k, String(value));
  return { old, better };
}
function bestText(mode, key = mode) {
  const v = localStorage.getItem('mm_best_' + key);
  if (!v) return key === mode && mode !== 'cruise' && mode !== 'cnr' ? 'New random course every start. Retry keeps it' : 'No record yet';
  return mode === 'cruise' ? `Best score ${v}` : mode === 'cnr' ? `Wins ${v}` : `Best ${fmt(Number(v))}`;
}
function showResults(r) {
  state = 'done'; audio.stopLoops();
  const lines = r.lines.map((l) => `<div>${l}</div>`).join('');
  document.getElementById('results-body').innerHTML = `<h2>${r.title}</h2><div class="stats">${lines}</div>`;
  showScreen('results');
}

// ---------- modes ----------
function raceProg(car, q) {
  if (q.done) return 1e6 - race.order.indexOf(car);
  if (race.any) return race.cps.length - q.left.size;
  return (q.lap * race.cps.length + q.cp) - Math.min(600, dist(car, nodePt(race.cps[q.cp]))) / 600;
}
function raceStep(dt) {
  race.t += dt;
  const ptime = () => race.t + race.pen;
  for (const [car, q] of race.st) {
    if (q.done) continue;
    let passed = false;
    if (race.any) { for (const k of q.left) if (dist(car, nodePt(race.cps[k])) < 12) { q.left.delete(k); passed = true; } if (!q.left.size) q.done = true; }
    else if (dist(car, nodePt(race.cps[q.cp])) < 12) {
      passed = true; q.cp++;
      if (q.cp === race.cps.length) { q.cp = 0; q.lap++; if (q.lap === race.laps) q.done = true; else if (car === player) hud.message(q.lap === race.laps - 1 ? 'Final lap!' : `Lap ${q.lap + 1}`); }
    }
    if (q.done) {
      race.order.push(car);
      if (car !== player) Object.assign(car.ai, { role: 'traffic', goal: null, cruise: 11, lane: LANE });
    }
    if (car === player && passed) {
      audio.blip(q.done ? 1.5 : 1);
      if (q.done) {
        const place = race.order.indexOf(player) + 1, t = ptime(), b = best(race.key, Number(t.toFixed(2)), true);
        const title = race.any ? 'Blitz cleared!' : place === 1 ? '1st place!' : `Finished ${['', '1st', '2nd', '3rd', '4th', '5th', '6th'][place]}`;
        hud.message(title, 2, 'big');
        finishWith({ title, lines: [`Time <b>${fmt(t)}</b>${race.pen ? ` (incl. +${race.pen}s busted)` : ''}`, race.any ? `${fmt(Math.max(0, race.limit - t))} to spare` : `Position <b>${place} / ${race.st.size}</b>`, b.better ? 'New record!' : `Record ${fmt(b.old)}`] });
      } else if (!race.any) hud.message(`Checkpoint ${q.cp || race.cps.length} / ${race.cps.length}`, 1.2);
      else hud.message(`${race.cps.length - q.left.size} / ${race.cps.length}`, 1.2);
    }
  }
  if (race.limit && !race.st.get(player).done && ptime() > race.limit) { hud.message('Out of time', 2, 'big'); finishWith({ title: 'Out of time', lines: [`Checkpoints <b>${race.cps.length - race.st.get(player).left.size} / ${race.cps.length}</b>`, 'Plan a shorter route on the minimap'] }); race.limit = 0; }
  // gentle catch-up: rivals ease off when far ahead and push when far behind, capped by difficulty
  const pp = raceProg(player, race.st.get(player)), cap = SKILL[cfg.skill][2];
  for (const r of racers) { const q = race.st.get(r); if (!q.done) r.ai.band = clamp(1 + (pp - raceProg(r, q)) * 0.05, 2 - cap, cap); }
}

function setupCnR(sk) {
  const corners = [[1, 1], [N - 1, N - 1], [1, N - 1], [N - 1, 1]], k = ri(4), home = corners[k], rival = corners[k ^ 1];
  cnr = { home, rival, gold: { x: 0, z: 0 }, carrier: null, immune: 0, you: 0, them: 0, t: 0 };
  const ring = (n, color) => { const m = new THREE.Mesh(new THREE.CylinderGeometry(11, 11, 3, 28, 1, true), new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.5, side: THREE.DoubleSide })); m.position.set(n[0] * BLOCK, 1.5, n[1] * BLOCK); markers.add(m); };
  ring(home, 0x3498db); ring(rival, 0xe74c3c);
  cnr.goldMesh = new THREE.Group();
  cnr.goldMesh.add(new THREE.Mesh(new THREE.BoxGeometry(1.6, 1, 1), new THREE.MeshLambertMaterial({ color: 0xffc312, emissive: 0x664400 })),
    new THREE.Mesh(new THREE.CylinderGeometry(0.4, 0.4, 60, 6).translate(0, 30, 0), new THREE.MeshBasicMaterial({ color: 0xffd32a, transparent: true, opacity: 0.35 })));
  markers.add(cnr.goldMesh);
  const at = (n, k2) => { const d = n[0] < N / 2 ? [1, 0] : [-1, 0]; return [n[0] * BLOCK + d[0] * (14 + k2 * 10), n[1] * BLOCK + (k2 % 2 ? -LANE : LANE), Math.atan2(d[0], 0)]; };
  player.place(...at(home, 0), city);
  for (let n = 0; n < 2; n++) {
    const s = CARS[ri(4)], r = addCar(s, n ? 0xb33939 : 0xff5252, 'robber'); r.place(...at(rival, n), city); robbers.push(r);
    const tgt = () => { const c = cnr.carrier; return c === r ? nodePt(cnr.rival) : !c ? cnr.gold : c === player ? { x: player.x + player.vx * 0.4, z: player.z + player.vz * 0.4 } : player; };
    makeAI(r, 'robber', { cruise: s.top * sk[0], turnSpeed: sk[1], lane: 1, goal: () => nearestNode(tgt().x, tgt().z), direct: tgt });
    reseed(r);
  }
  spawnGold();
}
function spawnGold() {
  for (let k = 0; k < 200; k++) {
    const n = node(), a = manhattan(n, cnr.home), b = manhattan(n, cnr.rival);
    if (a >= 4 && b >= 4 && Math.abs(a - b) <= 1) { Object.assign(cnr.gold, nodePt(n)); break; }
  }
  if (cnr.carrier) cnr.carrier.topMul = 1;
  cnr.carrier = null;
}
function cnrStep(dt) {
  cnr.t += dt; cnr.immune -= dt;
  const c = cnr.carrier;
  if (!c) {
    for (const r of [player, ...robbers]) if (dist(r, cnr.gold) < 7) { grab(r); break; }
  } else {
    cnr.gold.x = c.x; cnr.gold.z = c.z;
    if (dist(c, nodePt(c === player ? cnr.home : cnr.rival)) < 11) {
      c === player ? cnr.you++ : cnr.them++; audio.blip(c === player ? 1.5 : 0.6);
      if (cnr.you === 3 || cnr.them === 3) {
        const win = cnr.you === 3, b = win ? best('cnr', Number(localStorage.getItem('mm_best_cnr') || 0) + 1, false) : null;
        hud.message(win ? 'You win!' : 'Rival gang wins', 2.2, 'big');
        finishWith({ title: win ? 'The gold is yours!' : 'Rival gang wins', lines: [`Score <b>${cnr.you} - ${cnr.them}</b>`, `Time ${fmt(cnr.t)}`, win ? `Total wins ${localStorage.getItem('mm_best_cnr')}` : 'Ram the carrier to steal the gold'] });
        cnr.carrier.topMul = 1; cnr.carrier = null; cnr.gold.x = -999;
      } else { hud.message(c === player ? 'Delivered! +1' : 'Rivals delivered', 1.8); spawnGold(); }
    }
  }
  cnr.goldMesh.position.set(cnr.gold.x, cnr.carrier ? cnr.carrier.y + cnr.carrier.spec.ht + 1.6 : 0.6, cnr.gold.z);
}
function grab(r) {
  if (cnr.carrier) cnr.carrier.topMul = 1;
  cnr.carrier = r; r.topMul = 0.92; cnr.immune = 2; audio.blip(r === player ? 1.2 : 0.7);
  hud.message(r === player ? 'Got the gold! Take it home' : 'Rivals have the gold. Ram them!', 1.8);
}
function cnrHit(a, b, imp) {
  const c = cnr.carrier; if (!c || cnr.immune > 0 || imp < 4 || (a !== c && b !== c)) return;
  const o = a === c ? b : a;
  if (o.kind === 'cop') { hud.message('Cops seized the gold!', 1.8); spawnGold(); }
  else if ((o === player) !== (c === player) && (o === player || o.kind === 'robber')) grab(o);
}

function cruiseAward(pts, text) { cruise.score += pts; if (text) hud.message(`${text} <b>+${pts}</b>`, 1.1, 'small'); }

// ---------- cops ----------
function copTarget() { return cnr ? cnr.carrier : race && race.st.get(player).done ? null : player; }
function startChase(cop, t) {
  if (cop.chase === t) return;
  const first = t === player && !cops.some((c) => c.chase === player);
  cop.chase = t; cop.siren = true; cop.lost = 0;
  Object.assign(cop.ai, { patrol: false, cruise: COP.top * SKILL[cfg.skill][0], turnSpeed: 16, lane: 0, goal: () => nearestNode(t.x, t.z), direct: () => ({ x: t.x + t.vx * 0.4, z: t.z + t.vz * 0.4 }) });
  if (first) hud.message('Cops on your tail!', 1.4, 'warn');
}
function stopChase(cop) { cop.chase = null; cop.siren = false; Object.assign(cop.ai, { patrol: true, cruise: 13, turnSpeed: 8, lane: LANE, goal: null, direct: null }); }
function copsStep(dt) {
  copCool -= dt;
  const t = copTarget();
  for (const cop of cops) {
    if (cop.chase && cop.chase !== t) stopChase(cop);
    if (!t) continue;
    const d = dist(cop, t);
    if (!cop.chase) { if (copCool <= 0 && d < (cnr ? 70 : 45) && (cnr || t.speed > 24) && city.clearLine(cop.x, cop.z, t.x, t.z)) startChase(cop, t); }
    else if ((cop.lost = d > 170 ? cop.lost + dt : 0) > 4) { stopChase(cop); if (t === player && !cops.some((c) => c.chase)) hud.message('Lost the cops', 1.4); }
  }
  const tail = cops.filter((c) => c.chase === player), close = tail.some((c) => dist(c, player) < 10);
  bustT = close && player.speed < 3.5 ? bustT + dt : Math.max(0, bustT - dt * 0.5);
  if (bustT > 2) {
    bustT = 0; copCool = 10; player.frozen = 2; cops.forEach((c) => c.chase && stopChase(c)); audio.crash(4);
    if (race) { race.pen += 10; hud.message('BUSTED! +10s', 2, 'warn'); }
    else if (cruise) { cruise.score = Math.floor(cruise.score * 0.8); hud.message('BUSTED! -20% score', 2, 'warn'); }
    else if (cnr && cnr.carrier === player) { hud.message('BUSTED! Gold confiscated', 2, 'warn'); spawnGold(); }
  }
  const heat = tail.length ? Math.max(...tail.map((c) => 1 - Math.min(1, dist(c, player) / 120))) : 0;
  audio.siren(heat);
  hud.el.heat.className = 'heat' + (tail.length ? ' on' : '');
  if (tail.length) hud.set('heat', `COPS x${tail.length}<div class="bust"><i style="width:${(bustT / 2) * 100}%"></i></div>`);
}

// ---------- input ----------
const keys = {}, padPrev = [];
let steerIn = 0;
window.addEventListener('keydown', (e) => {
  if (['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Space'].includes(e.code)) e.preventDefault();
  audio.init();
  if (e.repeat) return;
  keys[e.code] = true; press(e.code);
});
window.addEventListener('keyup', (e) => { keys[e.code] = false; });
window.addEventListener('blur', () => { for (const k in keys) keys[k] = false; if (state === 'play') togglePause(); });
function press(code) {
  if (code === 'Escape' || code === 'KeyP') { if (state === 'play' || state === 'count' || state === 'pause') togglePause(); }
  else if (code === 'KeyC') camMode = (camMode + 1) % CAMS.length;
  else if (code === 'KeyM') hud.message(audio.toggleMute() ? 'Sound off' : 'Sound on', 0.8, 'small');
  else if (code === 'KeyR' && state === 'play' && resetCool <= 0 && player) { putOnRoad(player, city); resetCool = 2; camH = player.h; updateCamera(0, true); }
  else if (code === 'Enter') { if (state === 'menu') startGame(); else if (state === 'done') startGame(true); else if (state === 'pause') togglePause(); }
}
function readInput(dt) {
  let steerT = (keys.ArrowRight || keys.KeyD ? 1 : 0) - (keys.ArrowLeft || keys.KeyA ? 1 : 0), analog = null;
  let thr = keys.ArrowUp || keys.KeyW ? 1 : 0, brk = keys.ArrowDown || keys.KeyS ? 1 : 0, hb = !!keys.Space, horn = !!keys.KeyH;
  const gp = [...(navigator.getGamepads ? navigator.getGamepads() : [])].find(Boolean);
  if (gp) {
    const ax = gp.axes[0] || 0, btn = (i) => gp.buttons[i] || { value: 0, pressed: false };
    if (Math.abs(ax) > 0.12) analog = Math.sign(ax) * ((Math.abs(ax) - 0.12) / 0.88) ** 1.6;
    thr = Math.max(thr, btn(7).value); brk = Math.max(brk, btn(6).value); hb = hb || btn(0).pressed; horn = horn || btn(1).pressed;
    const edge = { 3: 'KeyR', 9: 'Escape', 5: 'KeyC', 4: 'KeyC' };
    for (const i in edge) { if (btn(i).pressed && !padPrev[i]) press(state === 'menu' || state === 'done' ? 'Enter' : edge[i]); padPrev[i] = btn(i).pressed; }
  }
  // keyboard steering eases in and snaps back, so taps give small corrections instead of full lock
  const rate = steerT === 0 || Math.sign(steerT) !== Math.sign(steerIn) ? 8 : 3.2;
  steerIn += clamp(steerT - steerIn, -rate * dt, rate * dt);
  const lim = 1 - Math.min(0.45, player.speed / 90);
  player.steer = analog !== null ? analog * lim : steerIn * lim;
  player.throttle = thr; player.brake = brk; player.handbrake = hb;
  audio.horn(horn);
  if (horn) for (const c of cars) if (c.kind === 'traffic' && dist(c, player) < 30) c.ai.yieldT = 2;
}

// ---------- step ----------
function onHit(a, b, imp) {
  if (a === player || b === player) {
    const o = a === player ? b : a; o.nmT = 3;
    if (imp > 4) { audio.crash(imp); player.damage += (imp - 3) * 1.1 / Math.sqrt(player.spec.mass); }
    if (o.kind === 'cop' && !o.chase && copCool <= 0 && state === 'play' && !cnr) startChase(o, player);
  }
  if (cnr && state === 'play') cnrHit(a, b, imp);
}
function step(dt) {
  T += dt; resetCool -= dt;
  const live = state === 'play';
  if (state === 'count') {
    countT -= dt; const n = Math.ceil(countT);
    if (n !== lastCount && n <= 3) { lastCount = n; if (n > 0) { hud.message(String(n), 0.9, 'big'); audio.blip(0.8); } }
    if (countT <= 0) { state = 'play'; hud.message('GO!', 0.8, 'big'); audio.blip(1.6); }
  }
  if (player) { readInput(dt); if (state === 'count') { player.throttle = 0; player.brake = 1; } }
  for (const c of cars) {
    if (!c.ai) continue;
    if (live || c.kind === 'traffic' || c.kind === 'cop') drive(c, dt, cars, city); else { c.throttle = 0; c.brake = 1; c.steer = 0; }
    c.nmT = (c.nmT || 0) - dt;
  }
  for (const c of cars) {
    const r = c.step(dt, city);
    if (c !== player) continue;
    if (r.hit > 6) { audio.crash(r.hit); c.damage += (r.hit - 5) * 1.5 / Math.sqrt(c.spec.mass); }
    if (cruise && r.landed > 0.55) cruiseAward(Math.round(r.landed * 150), 'Big air!');
  }
  collideCars(cars, onHit);
  for (const c of cars) { const pts = city.hitProps(c); if (pts && c === player) { audio.crash(2); if (cruise) { cruise.score += pts; cruise.smash++; } } }
  city.updateProps(dt);
  if (!player) return;
  if (player.damage >= 100 && !player.wreck) { player.wreck = 1.6; player.frozen = 1.6; hud.message('WRECKED', 1.6, 'warn'); audio.crash(20); }
  if (player.wreck && (player.wreck -= dt) <= 0) { player.wreck = 0; player.damage = 0; putOnRoad(player, city); player.damage = 0; camH = player.h; updateCamera(0, true); }
  if (live) {
    if (race) raceStep(dt); else if (cnr) cnrStep(dt);
    if (cops.length) copsStep(dt);
    if (cruise && player.speed > 18) for (const o of cars) {
      if (o === player || o.nmT > 0) continue;
      const d = dist(o, player), rr = o.r + player.r;
      if (d > rr + 0.1 && d < rr + 1.4) { o.nmT = 3; cruise.close++; cruiseAward(25, 'Close call!'); }
    }
  }
  if (pending && (pendingT -= dt) <= 0) { const r = pending; pending = null; showResults(r); }
}

// ---------- camera ----------
const CAMS = [{ d: 9.5, h: 4.3, look: 6 }, { d: 17, h: 7.5, look: 6 }, { hood: true, look: 20 }];
const tmpV = new THREE.Vector3();
function updateCamera(dt, snap) {
  const c = CAMS[camMode], p = player, k = p.spec.truck ? 1.4 : 1;
  camH += wrap(p.h - camH) * (snap ? 1 : 1 - Math.exp(-5 * dt)); // the lag lets you see a slide
  const h = c.hood ? p.h : camH, fx = Math.sin(h), fz = Math.cos(h);
  if (c.hood) camera.position.set(p.x + fx * 0.4, p.y + p.spec.ht + 0.35, p.z + fz * 0.4);
  else {
    let d = c.d * k * (1 + Math.min(0.2, p.speed / 200));
    while (d > 3 && city.solidAt(p.x - fx * d, p.z - fz * d)) d -= 1; // don't park the camera in a wall
    tmpV.set(p.x - fx * d, p.y + c.h * k, p.z - fz * d);
    if (snap) camPos.copy(tmpV); else camPos.lerp(tmpV, 1 - Math.exp(-9 * dt));
    camera.position.copy(camPos);
  }
  camera.lookAt(p.x + fx * c.look, p.y + 1.3 * k, p.z + fz * c.look);
  const fov = 62 + Math.min(14, p.speed * 0.3);
  if (Math.abs(camera.fov - fov) > 0.3) { camera.fov = fov; camera.updateProjectionMatrix(); }
}

// ---------- per-frame visuals + HUD ----------
function arrowTarget() {
  if (race) {
    const q = race.st.get(player); if (q.done) return null;
    if (race.any) { let b = null, bd = 1e9; for (const k of q.left) { const d = dist(player, nodePt(race.cps[k])); if (d < bd) { bd = d; b = race.cps[k]; } } return b && nodePt(b); }
    return nodePt(race.cps[q.cp]);
  }
  if (cnr) return cnr.carrier === player ? nodePt(cnr.home) : cnr.carrier || cnr.gold;
  return null;
}
function frameVisuals(dt) {
  for (const c of cars) c.sync(city, T);
  hud.tick(dt);
  if (!player) return;
  updateCamera(dt, false);
  const tg = arrowTarget();
  arrow.visible = !!tg && camMode !== 2;
  if (tg) { arrow.position.set(player.x, player.y + player.spec.ht + 1.5, player.z); arrow.rotation.y = Math.atan2(tg.x - player.x, tg.z - player.z); }
  audio.engine(Math.min(1, Math.abs(player.fwd) / player.spec.top), player.throttle, state === 'play' || state === 'count');
  hud.stats(player.speed, player.damage);
  const marks = [];
  if (race) {
    const q = race.st.get(player), last = !race.any && q.lap === race.laps - 1 && q.cp === race.cps.length - 1;
    race.mk.forEach((m, k) => {
      const next = race.any ? q.left.has(k) : k === q.cp, left = race.any ? q.left.has(k) : race.laps > 1 || k >= q.cp;
      m.visible = !q.done && left;
      m.material.color.setHex(next ? (last ? 0x2ecc71 : 0xffd32a) : 0xffffff); m.material.opacity = next ? 0.4 : 0.12;
      if (m.visible) marks.push({ ...nodePt(race.cps[k]), c: next ? '#ffd32a' : '#ffffffaa', r: next ? 6 : 4, label: race.any ? '' : String(k + 1) });
    });
    const order = [...race.st.entries()].sort((a, b) => raceProg(b[0], b[1]) - raceProg(a[0], a[1])).map((e) => e[0]);
    const t = race.t + race.pen;
    if (race.any) { hud.set('pos', `${race.cps.length - q.left.size} / ${race.cps.length}`); hud.set('sub', 'checkpoints, any order'); hud.set('time', `<span class="${race.limit - t < 10 ? 'red' : ''}">${fmt(Math.max(0, (race.limit || t) - t))}</span>`); }
    else { hud.set('pos', `${order.indexOf(player) + 1}<small>/${order.length}</small>`); hud.set('sub', (race.laps > 1 ? `Lap ${Math.min(q.lap + 1, race.laps)}/${race.laps} · ` : '') + `Checkpoint ${q.done ? race.cps.length : q.cp + 1}/${race.cps.length}`); hud.set('time', fmt(t)); }
    hud.set('best', bestText(race.mode, race.key));
    for (const r of racers) marks.push({ x: r.x, z: r.z, c: '#' + r.mesh.userData.baseColor.getHexString(), r: 3 });
  } else if (cnr) {
    hud.set('pos', `<span class="blue">${cnr.you}</span> - <span class="red">${cnr.them}</span>`);
    hud.set('sub', !cnr.carrier ? 'The gold is loose' : cnr.carrier === player ? 'You have the gold. Get home!' : 'Rivals have the gold. Ram them!');
    hud.set('time', fmt(cnr.t)); hud.set('best', 'First to 3');
    marks.push({ ...nodePt(cnr.home), c: '#3498db', shape: 'ring', r: 6 }, { ...nodePt(cnr.rival), c: '#e74c3c', shape: 'ring', r: 6 }, { ...cnr.gold, c: '#ffc312', shape: 'sq', r: 4 });
    for (const r of robbers) marks.push({ x: r.x, z: r.z, c: '#e74c3c', r: 3 });
  } else if (cruise) {
    hud.set('pos', String(cruise.score)); hud.set('sub', `smashes ${cruise.smash} · close calls ${cruise.close}`); hud.set('time', ''); hud.set('best', bestText('cruise'));
  }
  for (const c of cops) marks.push({ x: c.x, z: c.z, c: c.chase ? (Math.floor(T * 6) % 2 ? '#ff3b3b' : '#3b7bff') : '#1e3a8a', r: 3 });
  marks.push({ x: player.x, z: player.z, h: player.h, c: '#ffffff', shape: 'tri' });
  hud.minimap(marks);
}

// ---------- screens ----------
const optsEl = document.querySelector('#menu details');
function showScreen(id) { for (const s of document.querySelectorAll('.screen')) s.classList.toggle('show', s.id === id); }
function togglePause() {
  if (state === 'pause') { state = paused; showScreen(null); }
  else { paused = state; state = 'pause'; audio.stopLoops(); showScreen('pause'); document.getElementById('end-cruise').style.display = cruise ? '' : 'none'; }
}
let paused = 'play';
function toMenu() {
  if (cruise && cruise.score) best('cruise', cruise.score, false);
  clearAll(); state = 'menu'; hud.show(false); showScreen('menu'); applyTime(cfg.time); spawnTraffic(28); renderMenu();
}
const LABELS = { mode: Object.values(MODES).map((m) => m[0]), car: CARS.map((c) => c.name), traffic: ['Off', 'Light', 'Heavy'], cops: ['Off', 'Some', 'Lots'], time: ['Day', 'Dusk', 'Night'], skill: ['Easy', 'Normal', 'Hard'] };
const TITLES = { mode: 'Mode', car: 'Car', traffic: 'Traffic', cops: 'Cops', time: 'Time', skill: 'Rivals' };
document.getElementById('m-opts').innerHTML = Object.keys(CHOICES).map((k) => `<label class="sel"><span>${TITLES[k]}</span><select data-k="${k}">${CHOICES[k].map((v, n) => `<option value="${v}">${LABELS[k][n]}</option>`).join('')}</select></label>`).join('');
function renderMenu() {
  const spec = CARS.find((c) => c.key === cfg.car), $ = (id) => document.getElementById(id);
  for (const sel of document.querySelectorAll('#m-opts select')) sel.value = cfg[sel.dataset.k];
  $('m-car').textContent = spec.name; $('m-mode').textContent = MODES[cfg.mode][0]; $('m-desc').textContent = MODES[cfg.mode][1];
  $('m-best').innerHTML = `${bestText(cfg.mode)} &middot; <kbd>Enter</kbd> to play`;
  if (show.userData.key !== cfg.car) { show.clear(); show.add(carMesh(spec, spec.color)); show.userData.key = cfg.car; }
}
document.getElementById('menu').addEventListener('change', (e) => { if (e.target.dataset.k) { audio.init(); setCfg(e.target.dataset.k, e.target.value); } });
document.getElementById('menu').addEventListener('click', (e) => { if (e.target.closest('#go')) { audio.init(); startGame(); } });
document.getElementById('pause').addEventListener('click', (e) => {
  const id = e.target.closest('button')?.id;
  if (id === 'resume') togglePause(); else if (id === 'restart') startGame(true); else if (id === 'quit') toMenu();
  else if (id === 'end-cruise') { const b = best('cruise', cruise.score, false); showResults({ title: 'Cruise over', lines: [`Score <b>${cruise.score}</b>`, `Smashes ${cruise.smash} · close calls ${cruise.close}`, b.better ? 'New record!' : `Record ${b.old}`] }); cruise.score = 0; }
});
document.getElementById('results').addEventListener('click', (e) => {
  const id = e.target.closest('button')?.id;
  if (id === 'again') startGame(true); else if (id === 'fresh') startGame(false); else if (id === 'menu-btn') toMenu();
});
document.addEventListener('visibilitychange', () => { if (document.hidden && state === 'play') togglePause(); });

// ---------- loop ----------
let last = performance.now(), acc = 0;
function frame(now) {
  requestAnimationFrame(frame);
  const dt = Math.min(0.1, (now - last) / 1000); last = now;
  if (state !== 'pause' && state !== 'done') { acc += dt; while (acc >= STEP) { step(STEP); acc -= STEP; } }
  show.visible = state === 'menu';
  if (state === 'menu') {
    const a = T * 0.05; camera.position.set(SIZE / 2 + Math.cos(a) * 330, 150, SIZE / 2 + Math.sin(a) * 330); camera.lookAt(SIZE / 2, 0, SIZE / 2);
    const o = optsEl.open, L = Math.max(4.4, CARS.find((c) => c.key === cfg.car).len); // the car floats above the caption, higher when Options is open
    camera.updateMatrixWorld(); show.position.set(0, o ? 0.35 : -0.6, -(o ? 2.3 : 1.9) * L).applyMatrix4(camera.matrixWorld); show.rotation.y = a * 4 + 0.9 + a; show.rotation.x = 0;
  }
  frameVisuals(dt);
  if (window.__mm.cam) window.__mm.cam(camera); // test hook: screenshots from a fixed viewpoint
  renderer.render(scene, camera);
}
window.__mm = { get state() { return state; }, get cars() { return cars; }, get player() { return player; }, get race() { return race; }, get cnr() { return cnr; }, get cruise() { return cruise; }, start: startGame, cfg, cam: null }; // test hook
toMenu();
requestAnimationFrame(frame);
