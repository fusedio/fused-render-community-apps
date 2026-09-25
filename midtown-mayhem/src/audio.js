// Tiny WebAudio kit: a geared engine drone, siren, horn, crash noise and a blip. No sound files.
let ctx, master, eng, engGain, engLp, siren, sirenGain, horn, hornGain, noise;

export const audio = {
  muted: localStorage.getItem('mm_muted') === '1',
  init() {
    if (ctx) { if (ctx.state === 'suspended') ctx.resume(); return; }
    ctx = new (window.AudioContext || window.webkitAudioContext)();
    master = ctx.createGain(); master.gain.value = this.muted ? 0 : 0.5; master.connect(ctx.destination);
    const tone = (type, f) => { const o = ctx.createOscillator(), a = ctx.createGain(); o.type = type; o.frequency.value = f; a.gain.value = 0; o.connect(a); o.start(); return [o, a]; };
    [eng, engGain] = tone('sawtooth', 40); engLp = ctx.createBiquadFilter(); engLp.type = 'lowpass'; engGain.connect(engLp).connect(master);
    [siren, sirenGain] = tone('square', 600); sirenGain.connect(master);
    [horn, hornGain] = tone('square', 415); hornGain.connect(master);
    const h2 = ctx.createOscillator(); h2.type = 'square'; h2.frequency.value = 330; h2.connect(hornGain); h2.start();
    noise = ctx.createBuffer(1, ctx.sampleRate * 0.4, ctx.sampleRate);
    const d = noise.getChannelData(0); for (let i = 0; i < d.length; i++) d[i] = (Math.random() * 2 - 1) * (1 - i / d.length) ** 2;
  },
  toggleMute() { this.muted = !this.muted; localStorage.setItem('mm_muted', this.muted ? '1' : '0'); if (master) master.gain.value = this.muted ? 0 : 0.5; return this.muted; },
  engine(ratio, load, on) { // ratio 0..1 of top speed; five fake gears make the pitch climb and drop
    if (!ctx) return;
    const t = ctx.currentTime, gear = Math.min(4, Math.floor(ratio * 5)), rpm = 0.3 + (ratio * 5 - gear) * 0.7;
    eng.frequency.setTargetAtTime(32 + rpm * 70 + gear * 6, t, 0.04);
    engLp.frequency.setTargetAtTime(300 + rpm * 700 + load * 500, t, 0.05);
    engGain.gain.setTargetAtTime(on ? 0.05 + load * 0.05 : 0, t, 0.08);
  },
  siren(vol) { if (!ctx) return; const t = ctx.currentTime; siren.frequency.setTargetAtTime(Math.floor(t * 2.2) % 2 ? 760 : 590, t, 0.01); sirenGain.gain.setTargetAtTime(vol * 0.035, t, 0.1); },
  horn(on) { if (ctx) hornGain.gain.setTargetAtTime(on ? 0.05 : 0, ctx.currentTime, 0.02); },
  crash(strength) {
    if (!ctx) return;
    const s = ctx.createBufferSource(), f = ctx.createBiquadFilter(), g = ctx.createGain();
    s.buffer = noise; f.type = 'lowpass'; f.frequency.value = 500 + strength * 60; g.gain.value = Math.min(0.6, 0.08 + strength * 0.025);
    s.connect(f).connect(g).connect(master); s.start();
  },
  blip(hi = 1) {
    if (!ctx) return;
    const o = ctx.createOscillator(), g = ctx.createGain(), t = ctx.currentTime;
    o.type = 'triangle'; o.frequency.setValueAtTime(520 * hi, t); o.frequency.setValueAtTime(780 * hi, t + 0.07);
    g.gain.setValueAtTime(0.15, t); g.gain.exponentialRampToValueAtTime(0.001, t + 0.25);
    o.connect(g).connect(master); o.start(t); o.stop(t + 0.26);
  },
  stopLoops() { if (!ctx) return; const t = ctx.currentTime; for (const g of [engGain, sirenGain, hornGain]) g.gain.setTargetAtTime(0, t, 0.05); },
};
