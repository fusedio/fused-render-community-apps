// Drivers for every non-player car. All of them follow lanes between intersections; a goal() turns
// the random wander into a greedy route, and direct() lets cops/robbers leave the lane to ram a
// target in sight. Each driver watches the road ahead (including the player), brakes or overtakes,
// and backs out or respawns when it gets stuck.
import { BLOCK, HALF, DIRS, inGrid, nearestNode, clamp } from './city.js';

const wrap = (a) => Math.atan2(Math.sin(a), Math.cos(a));

export function makeAI(car, role, o = {}) {
  car.ai = { role, lane: 3.5, cruise: 14, turnSpeed: 9, goal: null, direct: null, prev: [0, 0], node: [0, 0], next: [0, 0], phase: 0,
    dodge: 0, dodgeT: 0, stuckT: 0, stuckN: 0, reverseT: 0, waitT: 0, ghostT: 0, yieldT: 0, band: 1, losT: 0, los: false, wasDirect: false, ...o };
  return car.ai;
}

function choose(ai, at, from) {
  const [i, j] = at, opts = DIRS.map(([dx, dz]) => [i + dx, j + dz]).filter(([a, b]) => inGrid(a, b) && !(a === from[0] && b === from[1]));
  const straight = opts.find(([a, b]) => a - i === i - from[0] && b - j === j - from[1]);
  let goal = ai.goal && ai.goal();
  if (goal && goal[0] === i && goal[1] === j && ai.goalAfter) goal = ai.goalAfter(); // arriving at a checkpoint: head for the one after
  let pool = opts;
  if (goal) {
    const dist = ([a, b]) => Math.abs(a - goal[0]) + Math.abs(b - goal[1]), best = Math.min(...opts.map(dist));
    pool = opts.filter((p) => dist(p) === best);
  }
  if (straight && pool.includes(straight) && Math.random() < 0.6) return straight;
  return pool[(Math.random() * pool.length) | 0] || opts[0];
}

// put the route back on the lattice from wherever the car is now (spawn, respawn, after a chase)
export function reseed(car) {
  const ai = car.ai, n = nearestNode(car.x, car.z), fx = Math.sin(car.h), fz = Math.cos(car.h);
  let d = Math.abs(fx) > Math.abs(fz) ? [Math.sign(fx), 0] : [0, Math.sign(fz)];
  const past = (car.x - n[0] * BLOCK) * d[0] + (car.z - n[1] * BLOCK) * d[1] > 0;
  let node = past ? [n[0] + d[0], n[1] + d[1]] : n;
  if (!inGrid(...node)) { d = [-d[0], -d[1]]; node = [n[0] + d[0], n[1] + d[1]]; }
  ai.prev = [node[0] - d[0], node[1] - d[1]]; ai.node = node; ai.next = choose(ai, node, ai.prev); ai.phase = 0;
}

// respawn on the nearest road, facing along it
export function putOnRoad(car, city) {
  const [i, j] = nearestNode(car.x, car.z);
  const d = DIRS.find(([dx, dz]) => inGrid(i + dx, j + dz) && (car.x - i * BLOCK) * dx + (car.z - j * BLOCK) * dz >= -1) || DIRS.find(([dx, dz]) => inGrid(i + dx, j + dz));
  car.place(i * BLOCK + d[0] * 12 - d[1] * 3.5, j * BLOCK + d[1] * 12 + d[0] * 3.5, Math.atan2(d[0], d[1]), city);
  car.damage = Math.min(car.damage, 40);
  if (car.ai) reseed(car);
}

function lanePoint(from, to, lane, exit) {
  const dx = Math.sign(to[0] - from[0]), dz = Math.sign(to[1] - from[1]), a = exit ? from : to, s = exit ? HALF : -(HALF + 2);
  return [a[0] * BLOCK + dx * s - dz * lane, a[1] * BLOCK + dz * s + dx * lane, dx, dz];
}

export function drive(car, dt, cars, city) {
  const ai = car.ai;
  ai.dodgeT -= dt; ai.ghostT -= dt; ai.yieldT -= dt; ai.losT -= dt;
  let tx, tz, direct = false;
  const dp = ai.direct && ai.direct();
  if (dp && Math.abs(dp.x - car.x) + Math.abs(dp.z - car.z) < 90) {
    if (ai.losT <= 0) { ai.los = city.clearLine(car.x, car.z, dp.x, dp.z); ai.losT = 0.25; }
    direct = ai.los;
  }
  const lane = ai.lane + (ai.dodgeT > 0 ? ai.dodge : 0) + (ai.yieldT > 0 ? 2.2 : 0);
  let turning = false;
  if (direct) { tx = dp.x; tz = dp.z; ai.wasDirect = true; }
  else {
    if (ai.wasDirect) { reseed(car); ai.wasDirect = false; }
    for (let k = 0; k < 2; k++) { // at most one waypoint switch per step
      const p = ai.phase === 0 ? lanePoint(ai.prev, ai.node, lane, false) : lanePoint(ai.node, ai.next, lane, true);
      [tx, tz] = p;
      if ((car.x - tx) * p[2] + (car.z - tz) * p[3] > -2.5 || Math.hypot(car.x - tx, car.z - tz) < 3) {
        if (ai.phase === 0) ai.phase = 1;
        else { ai.prev = ai.node; ai.node = ai.next; ai.next = choose(ai, ai.node, ai.prev); ai.phase = 0; }
      } else break;
    }
    if (ai.phase === 0) {
      const d1 = [ai.node[0] - ai.prev[0], ai.node[1] - ai.prev[1]], d2 = [ai.next[0] - ai.node[0], ai.next[1] - ai.node[1]];
      turning = (d1[0] !== d2[0] || d1[1] !== d2[1]) && Math.hypot(car.x - tx, car.z - tz) < 26;
    } else turning = true;
  }
  const err = wrap(Math.atan2(tx - car.x, tz - car.z) - car.h);
  let steer = clamp(-err * 2.4, -1, 1);
  let target = ai.cruise * ai.band * (1 - Math.min(0.6, Math.abs(err) * 0.7));
  if (turning && !direct) target = Math.min(target, ai.turnSpeed);
  if (ai.yieldT > 0) target *= 0.4;

  // look down the road: brake behind traffic, or swing out and pass
  const fx = Math.sin(car.h), fz = Math.cos(car.h);
  let block = 99, blockV = 0, blockLat = 0;
  if (ai.ghostT <= 0) for (const o of cars) {
    if (o === car) continue;
    const rx = o.x - car.x, rz = o.z - car.z, ahead = rx * fx + rz * fz;
    if (ahead < 0 || ahead > 20 || ahead > block) continue;
    const lat = -rx * fz + rz * fx;
    if (Math.abs(lat) < car.r + o.r + 0.6) { block = ahead; blockV = o.vx * fx + o.vz * fz; blockLat = lat; }
  }
  const v = car.fwd, passer = ai.role !== 'traffic' && !ai.patrol;
  if (block < 20) {
    if (passer) {
      if (blockV < v - 1 && ai.dodgeT <= 0) { ai.dodge = blockLat > 0 ? -3.2 : 3.2; ai.dodgeT = 1.3; }
      if (direct && block < 10) steer = clamp(steer + (blockLat > 0 ? 0.5 : -0.5), -1, 1);
    } else {
      target = Math.min(target, block < 7 ? 0 : Math.max(0, blockV) * (block < 12 ? 0.6 : 1));
      ai.waitT += dt;
      if (ai.waitT > 3.5) { ai.ghostT = 1.5; ai.waitT = 0; } // nudge through a stand-off
    }
  } else ai.waitT = 0;

  if (v < target - 1) { car.throttle = 1; car.brake = 0; }
  else if (v > target + 2) { car.throttle = 0; car.brake = clamp((v - target) / 8, 0.2, 1); }
  else { car.throttle = 0.35; car.brake = 0; }
  car.steer = steer; car.handbrake = false;

  // unstick: back up, then give up and respawn
  if (car.throttle > 0 && Math.abs(v) < 1.5 && car.grounded && car.frozen <= 0) ai.stuckT += dt; else ai.stuckT = Math.max(0, ai.stuckT - dt);
  if (v > 8) ai.stuckN = 0;
  if (ai.reverseT > 0) { ai.reverseT -= dt; car.throttle = 0; car.brake = 1; car.steer = -steer; }
  else if (ai.stuckT > 1.2) { ai.reverseT = 1.1; ai.stuckT = 0; if (++ai.stuckN > 3) { putOnRoad(car, city); ai.stuckN = 0; } }
}
