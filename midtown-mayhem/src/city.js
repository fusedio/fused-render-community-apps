// Procedural grid city: roads on a lattice of intersections, blocks of buildings or parks,
// kicker ramps in some parks, smashable sidewalk props. Collision is plain AABBs and circles.
import * as THREE from 'three';

export const N = 10, BLOCK = 60, ROAD = 14, HALF = ROAD / 2, LANE = 3.5, SIZE = N * BLOCK;
export const DIRS = [[1, 0], [-1, 0], [0, 1], [0, -1]];
export const inGrid = (i, j) => i >= 0 && j >= 0 && i <= N && j <= N;
export const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
export const nearestNode = (x, z) => [clamp(Math.round(x / BLOCK), 0, N), clamp(Math.round(z / BLOCK), 0, N)];
export const manhattan = (a, b) => Math.abs(a[0] - b[0]) + Math.abs(a[1] - b[1]);
export function rng(seed) {
  let a = seed >>> 0;
  return () => { a = (a + 0x6D2B79F5) | 0; let t = Math.imul(a ^ (a >>> 15), 1 | a); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
}

const PALETTE = [0xd9c7a7, 0xc9a27e, 0xa7b8c4, 0xe0d5c1, 0x9aa5b1, 0xc2836b, 0xb8b0a0, 0x8e9aaf, 0xd4b483, 0x7f8c8d, 0xb56a5a];
const PROPS = [ // hydrant, cone, mailbox, trash can
  { geo: new THREE.CylinderGeometry(0.28, 0.32, 0.9, 8).translate(0, 0.45, 0), color: 0xd63031, pts: 10 },
  { geo: new THREE.ConeGeometry(0.35, 0.9, 8).translate(0, 0.45, 0), color: 0xff7f24, pts: 5 },
  { geo: new THREE.BoxGeometry(0.6, 1.2, 0.5).translate(0, 0.6, 0), color: 0x2e5aac, pts: 15 },
  { geo: new THREE.CylinderGeometry(0.4, 0.35, 1, 8).translate(0, 0.5, 0), color: 0x3c6e47, pts: 10 },
];
const unitBox = new THREE.BoxGeometry(1, 1, 1).translate(0, 0.5, 0);
const tmp = new THREE.Object3D(), col = new THREE.Color();

function instanced(geo, items, mat) {
  const m = new THREE.InstancedMesh(geo, mat, Math.max(1, items.length));
  m.count = items.length;
  items.forEach((it, k) => {
    tmp.position.set(it.x, it.y || 0, it.z); tmp.rotation.set(0, it.ry || 0, 0); tmp.scale.set(it.sx ?? 1, it.sy ?? 1, it.sz ?? 1);
    tmp.updateMatrix(); m.setMatrixAt(k, tmp.matrix); m.setColorAt(k, col.setHex(it.c));
  });
  return m;
}

export function buildCity(scene, seed = 7) {
  const r = rng(seed), g = new THREE.Group();
  const city = { group: g, blocks: [], ramps: [], props: [], propMeshes: [] };
  const lam = (c) => new THREE.MeshLambertMaterial({ color: c });
  const plane = (w, h, c, y) => { const m = new THREE.Mesh(new THREE.PlaneGeometry(w, h), lam(c)); m.rotation.x = -Math.PI / 2; m.position.set(SIZE / 2, y, SIZE / 2); g.add(m); };
  plane(5000, 5000, 0x6d8f4e, -0.05);
  plane(SIZE + ROAD + 6, SIZE + ROAD + 6, 0x3b3d42, 0);

  const slabs = [], boxes = [], dashes = [], trunks = [], crowns = [], propItems = PROPS.map(() => []);
  const addProp = (x, z) => { const t = (r() * PROPS.length) | 0; const p = { t, k: propItems[t].length, x, z, x0: x, z0: z, y: 0, vx: 0, vy: 0, vz: 0, ry: 0, spin: 0, hitT: -1 }; propItems[t].push({ x, z, c: 0xffffff }); city.props.push(p); };
  const c0 = SIZE / 2;
  for (let bi = 0; bi < N; bi++) {
    city.blocks[bi] = [];
    for (let bj = 0; bj < N; bj++) {
      const x0 = bi * BLOCK + HALF, x1 = (bi + 1) * BLOCK - HALF, z0 = bj * BLOCK + HALF, z1 = (bj + 1) * BLOCK - HALF;
      const mx = (x0 + x1) / 2, mz = (z0 + z1) / 2, lot = x1 - x0;
      const landmark = bi === 4 && bj === 4;
      const park = !landmark && r() < 0.14;
      const blk = { box: null, trees: [], park };
      city.blocks[bi][bj] = blk;
      slabs.push({ x: mx, z: mz, sx: lot, sy: 0.18, sz: lot, c: park ? 0x78a858 : 0xb5b2aa });
      if (!park) {
        const s = lot - 6, bx = x0 + 3, bz = z0 + 3;
        blk.box = { x0: bx, x1: bx + s, z0: bz, z1: bz + s };
        const tall = Math.max(0, 1 - Math.hypot(mx - c0, mz - c0) / (SIZE * 0.5)) ** 1.5;
        const parts = landmark || r() < 0.3 ? [[mx, mz, s]] : [[0, 0], [1, 0], [0, 1], [1, 1]].map(([a, b]) => [bx + s / 4 + a * s / 2, bz + s / 4 + b * s / 2, s / 2]);
        for (const [px, pz, ps] of parts) {
          const h = landmark ? 140 : 6 + r() * 10 + tall * r() * 85;
          boxes.push({ x: px, z: pz, sx: ps - 0.5, sy: h, sz: ps - 0.5, c: landmark ? 0x2d3436 : PALETTE[(r() * PALETTE.length) | 0] });
          if (h > 30 && r() < 0.5) boxes.push({ x: px, y: h, z: pz, sx: ps * 0.45, sy: 3 + r() * 8, sz: ps * 0.45, c: 0x8a8f96 });
        }
        if (landmark) boxes.push({ x: mx - 6, y: 140, z: mz + 4, sx: 1, sy: 25, sz: 1, c: 0xdddddd }, { x: mx + 6, y: 140, z: mz + 4, sx: 1, sy: 25, sz: 1, c: 0xdddddd });
      } else {
        let ramp = null;
        if (r() < 0.75) {
          ramp = { x: mx, z: mz, axis: r() < 0.5 ? 'x' : 'z', s: r() < 0.5 ? 1 : -1, L: 13, W: 8, H: 3.6 };
          city.ramps.push(ramp);
        }
        for (let k = 0; k < 7; k++) {
          const tx = x0 + 5 + r() * (lot - 10), tz = z0 + 5 + r() * (lot - 10);
          if (ramp && Math.abs(tx - mx) < 13 && Math.abs(tz - mz) < 13) continue;
          blk.trees.push({ x: tx, z: tz, r: 0.9 });
          const hh = 3 + r() * 2;
          trunks.push({ x: tx, z: tz, sx: 0.5, sy: hh, sz: 0.5, c: 0x7a5230 });
          crowns.push({ x: tx, y: hh, z: tz, sx: 1, sy: 1 + r() * 0.5, sz: 1, c: r() < 0.5 ? 0x3f7d3a : 0x4f9444 });
        }
      }
      // sidewalk props along the four curbs
      for (let e = 0; e < 4; e++) for (let k = 0; k < 2; k++) {
        const u = 6 + r() * (lot - 12), inset = 1.3;
        const [px, pz] = [[x0 + u, z0 + inset], [x0 + u, z1 - inset], [x0 + inset, z0 + u], [x1 - inset, z0 + u]][e];
        if (r() < 0.8) addProp(px, pz);
      }
    }
  }
  // dashed centre lines
  for (let a = 0; a <= N; a++) for (let b = 0; b < N; b++) for (let u = b * BLOCK + HALF + 4; u < (b + 1) * BLOCK - HALF - 3; u += 8) {
    dashes.push({ x: a * BLOCK, y: 0.02, z: u + 1.75, sx: 0.25, sy: 0.02, sz: 3.5, c: 0xf2d24b });
    dashes.push({ x: u + 1.75, y: 0.02, z: a * BLOCK, sx: 3.5, sy: 0.02, sz: 0.25, c: 0xf2d24b });
  }
  // low barrier around the city
  const E = SIZE + HALF + 2;
  for (const [x, z, sx, sz] of [[SIZE / 2, -HALF - 2, E + HALF + 3, 1], [SIZE / 2, E, E + HALF + 3, 1], [-HALF - 2, SIZE / 2, 1, E + HALF + 3], [E, SIZE / 2, 1, E + HALF + 3]])
    boxes.push({ x, z, sx, sy: 1.2, sz, c: 0xe8e8e8 });

  const white = lam(0xffffff);
  g.add(instanced(unitBox, slabs, white), instanced(unitBox, boxes, white), instanced(unitBox, dashes, white),
    instanced(unitBox, trunks, white), instanced(new THREE.IcosahedronGeometry(2.2, 0).translate(0, 1.6, 0), crowns, white));
  city.propMeshes = PROPS.map((p, t) => { const m = instanced(p.geo, propItems[t], lam(p.color)); g.add(m); return m; });

  const rampMat = lam(0xf4c430);
  for (const rp of city.ramps) {
    const sh =new THREE.Shape([new THREE.Vector2(-rp.L / 2, 0), new THREE.Vector2(rp.L / 2, 0), new THREE.Vector2(rp.L / 2, rp.H)]);
    const m = new THREE.Mesh(new THREE.ExtrudeGeometry(sh, { depth: rp.W, bevelEnabled: false }).translate(0, 0, -rp.W / 2), rampMat);
    m.position.set(rp.x, 0, rp.z);
    m.rotation.y = rp.axis === 'x' ? (rp.s > 0 ? 0 : Math.PI) : (rp.s > 0 ? -Math.PI / 2 : Math.PI / 2);
    g.add(m);
  }
  scene.add(g);

  city.groundAt = (x, z) => {
    for (const rp of city.ramps) {
      const du = (rp.axis === 'x' ? x - rp.x : z - rp.z) * rp.s, dv = rp.axis === 'x' ? z - rp.z : x - rp.x;
      if (Math.abs(dv) < rp.W / 2 && Math.abs(du) < rp.L / 2) return rp.H * (du + rp.L / 2) / rp.L;
    }
    return 0;
  };
  const near = [];
  city.near = (x, z) => {
    near.length = 0; const bi = Math.floor(x / BLOCK), bj = Math.floor(z / BLOCK);
    for (let a = bi - 1; a <= bi + 1; a++) for (let b = bj - 1; b <= bj + 1; b++) { const blk = city.blocks[a]?.[b]; if (blk) near.push(blk); }
    return near;
  };
  city.solidAt = (x, z) => { const b = city.blocks[Math.floor(x / BLOCK)]?.[Math.floor(z / BLOCK)]?.box; return !!b && x > b.x0 && x < b.x1 && z > b.z0 && z < b.z1; };
  city.clearLine = (ax, az, bx, bz) => { const n = Math.ceil(Math.hypot(bx - ax, bz - az) / 4); for (let k = 1; k < n; k++) if (city.solidAt(ax + (bx - ax) * k / n, az + (bz - az) * k / n)) return false; return true; };

  // props: hit -> fly, tumble, vanish; resetProps() stands them back up
  city.hitProps = (car) => {
    let pts = 0;
    for (const p of city.props) {
      if (p.hitT >= 0 || Math.abs(p.x - car.x) > 3 || Math.abs(p.z - car.z) > 3 || Math.hypot(p.x - car.x, p.z - car.z) > car.r + 0.5) continue;
      p.hitT = 0; p.vx = car.vx * 1.1 + (Math.random() - 0.5) * 4; p.vz = car.vz * 1.1 + (Math.random() - 0.5) * 4; p.vy = 5 + car.speed * 0.2; p.spin = (Math.random() - 0.5) * 12;
      car.vx *= 0.96; car.vz *= 0.96; pts += PROPS[p.t].pts;
    }
    return pts;
  };
  city.updateProps = (dt) => {
    const dirty = new Set();
    for (const p of city.props) {
      if (p.hitT < 0 || p.hitT > 3) continue;
      p.hitT += dt; p.vy -= 25 * dt; p.x += p.vx * dt; p.y += p.vy * dt; p.z += p.vz * dt; p.ry += p.spin * dt;
      if (p.y < 0) { p.y = 0; p.vy *= -0.3; p.vx *= 0.6; p.vz *= 0.6; p.spin *= 0.6; }
      tmp.position.set(p.x, p.y, p.z); tmp.rotation.set(p.ry * 0.7, p.ry, 0); tmp.scale.setScalar(p.hitT > 3 ? 0 : 1);
      tmp.updateMatrix(); city.propMeshes[p.t].setMatrixAt(p.k, tmp.matrix); dirty.add(p.t);
    }
    for (const t of dirty) city.propMeshes[t].instanceMatrix.needsUpdate = true;
  };
  city.resetProps = () => {
    for (const p of city.props) {
      Object.assign(p, { x: p.x0, z: p.z0, y: 0, ry: 0, hitT: -1 });
      tmp.position.set(p.x, 0, p.z); tmp.rotation.set(0, 0, 0); tmp.scale.setScalar(1); tmp.updateMatrix(); city.propMeshes[p.t].setMatrixAt(p.k, tmp.matrix);
    }
    for (const m of city.propMeshes) m.instanceMatrix.needsUpdate = true;
  };
  return city;
}
