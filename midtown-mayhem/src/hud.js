// DOM HUD plus a north-up minimap: the street grid is painted once, markers every frame.
import { N, BLOCK, HALF, SIZE } from './city.js';

const $ = (id) => document.getElementById(id);
const MAP = 200, PAD = HALF + 2, SCALE = MAP / (SIZE + PAD * 2);
const toMap = (x, z) => [(x + PAD) * SCALE, (z + PAD) * SCALE];

export class HUD {
  constructor(city) {
    this.el = { hud: $('hud'), pos: $('h-pos'), sub: $('h-sub'), time: $('h-time'), best: $('h-best'), speed: $('h-speed'), dmg: $('h-dmg'), heat: $('h-heat'), msg: $('h-msg') };
    this.map = $('minimap').getContext('2d');
    this.base = document.createElement('canvas'); this.base.width = this.base.height = MAP;
    const b = this.base.getContext('2d');
    b.fillStyle = '#3b3d42'; b.fillRect(0, 0, MAP, MAP);
    for (let i = 0; i < N; i++) for (let j = 0; j < N; j++) {
      const blk = city.blocks[i][j], [x, z] = toMap(i * BLOCK + HALF, j * BLOCK + HALF), s = (BLOCK - HALF * 2) * SCALE;
      b.fillStyle = blk.park ? '#6f9f52' : '#a9a6a0'; b.fillRect(x, z, s, s);
    }
    this.msgT = 0;
  }
  show(on) { this.el.hud.classList.toggle('hidden', !on); }
  message(text, secs = 1.6, cls = '') { const m = this.el.msg; m.innerHTML = text; m.className = 'msg show ' + cls; this.msgT = secs; }
  tick(dt) { if (this.msgT > 0 && (this.msgT -= dt) <= 0) this.el.msg.className = 'msg'; }
  set(k, html) { const e = this.el[k]; if (e.__v !== html) { e.innerHTML = html; e.__v = html; } }
  stats(speedMs, damage) {
    this.set('speed', String(Math.round(speedMs * 2.237)));
    this.el.dmg.style.width = Math.max(0, 100 - damage) + '%';
    this.el.dmg.parentElement.classList.toggle('low', damage > 65);
  }
  // marks: [{x, z, c, r, shape: 'dot'|'ring'|'sq'|'tri', h, label}]
  minimap(marks) {
    const m = this.map; m.drawImage(this.base, 0, 0);
    for (const k of marks) {
      const [x, y] = toMap(k.x, k.z), r = k.r || 3;
      m.fillStyle = m.strokeStyle = k.c; m.lineWidth = 2;
      m.beginPath();
      if (k.shape === 'tri') { const s = Math.sin(k.h), c = Math.cos(k.h); m.moveTo(x + s * 7, y + c * 7); m.lineTo(x - c * 4 - s * 4, y + s * 4 - c * 4); m.lineTo(x + c * 4 - s * 4, y - s * 4 - c * 4); m.closePath(); m.fill(); m.strokeStyle = '#000'; m.lineWidth = 1; m.stroke(); }
      else if (k.shape === 'ring') { m.arc(x, y, r, 0, Math.PI * 2); m.stroke(); }
      else if (k.shape === 'sq') m.fillRect(x - r, y - r, r * 2, r * 2);
      else { m.arc(x, y, r, 0, Math.PI * 2); m.fill(); }
      if (k.label) { m.fillStyle = '#111'; m.font = 'bold 9px sans-serif'; m.textAlign = 'center'; m.fillText(k.label, x, y + 3); }
    }
  }
}
