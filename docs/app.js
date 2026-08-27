// Draws docs/data/runs.json, which scripts/make_page_data.py copies out of the
// harness output. Every number on this page came from a run in the repository;
// nothing is modelled or interpolated between the cells that were measured.

const el = (id) => document.getElementById(id);
const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

const MODES = [
  { key: 'legacy', label: 'Legacy', blurb: 'read, wait, write' },
  { key: 'recheck', label: 'Re-read', blurb: 'check again inside the write' },
  { key: 'occ', label: 'Compare-and-set', blurb: 'write only if the version still matches' },
  { key: 'lease', label: 'Lease', blurb: 'hold the room while deciding' },
];
const SCENARIOS = ['modify_vs_modify', 'cancel_vs_modify', 'stale_read_overwrite'];

const state = { data: null, mode: 'legacy', thinkIdx: 2, thinks: [], think2: null, think2s: [] };

// Text drawn over a dashed line shows the dashes between the glyph strokes, so
// a number that lands on one reads as struck through.
function labelOnPaper(ctx, text, x, y, align = 'center') {
  const w = ctx.measureText(text).width;
  const left = align === 'center' ? x - w / 2 : align === 'right' ? x - w : x;
  const prev = ctx.fillStyle;
  ctx.fillStyle = css('--paper');
  ctx.fillRect(left - 3, y - 11, w + 6, 14);
  ctx.fillStyle = prev;
  ctx.textAlign = align;
  ctx.fillText(text, x, y);
}

// Fixed pixel height: neither chart has anything that grows with the viewport.
function fitCanvas(canvas, h0) {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const w0 = canvas.clientWidth || 1200;
  canvas.width = Math.round(w0 * dpr);
  canvas.height = Math.round(h0 * dpr);
  canvas.style.height = h0 + 'px';
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w0, h0);
  return { ctx, w: w0, h: h0 };
}

const outcomeText = (o) =>
  Object.entries(o)
    .map(([k, v]) => `${v} ${k.replace(/_/g, ' ')}`)
    .join(', ');

// ------------------------------------------------------- figure 1: the race

function raceRows() {
  const think = state.thinks[state.thinkIdx];
  return state.data.sweep
    .filter((r) => r.mode === state.mode && r.think_ms === think)
    .sort((a, b) => a.concurrency - b.concurrency);
}

function drawRace() {
  const rows = raceRows();
  if (!rows.length) return;
  const { ctx, w, h } = fitCanvas(el('plot-race'), 250);
  const pad = { l: 62, r: 24, t: 22, b: 48 };
  const iw = w - pad.l - pad.r;
  const ih = h - pad.t - pad.b;

  // Held fixed across every mode and pause, so switching between them moves the
  // bars instead of rescaling the axis under them.
  const top = 100;
  const Y = (v) => pad.t + ih - (v / top) * ih;

  ctx.strokeStyle = css('--hair');
  ctx.beginPath();
  ctx.moveTo(pad.l, pad.t); ctx.lineTo(pad.l, pad.t + ih); ctx.lineTo(pad.l + iw, pad.t + ih);
  ctx.stroke();
  ctx.font = "11px 'Courier New', monospace";
  ctx.textAlign = 'right';
  for (let v = 0; v <= top; v += 25) {
    ctx.fillStyle = css('--faint');
    ctx.fillText(String(v), pad.l - 8, Y(v) + 3);
    if (v > 0) {
      ctx.strokeStyle = css('--grid');
      ctx.beginPath(); ctx.moveTo(pad.l, Y(v)); ctx.lineTo(pad.l + iw, Y(v)); ctx.stroke();
    }
  }

  const slot = iw / rows.length;
  const bw = slot * 0.5;
  rows.forEach((r, i) => {
    const x = pad.l + slot * i + (slot - bw) / 2;
    const v = r.double_per_100_agents;
    const y = Y(v);
    ctx.fillStyle = css('--ox');
    ctx.fillRect(x, y, bw, pad.t + ih - y);
    ctx.font = "12px 'Times New Roman', serif";
    ctx.fillStyle = css('--sub');
    labelOnPaper(ctx, v.toFixed(1), x + bw / 2, y - 7);
    ctx.fillStyle = css('--faint');
    ctx.font = "11px 'Courier New', monospace";
    ctx.textAlign = 'center';
    ctx.fillText(`${r.concurrency} agents`, x + bw / 2, pad.t + ih + 16);
  });

  ctx.textAlign = 'left';
  ctx.fillStyle = css('--faint');
  ctx.font = "11px 'Courier New', monospace";
  ctx.fillText('double bookings per 100 agents', pad.l, h - 8);
  if (rows.every((r) => r.double_per_100_agents === 0)) {
    ctx.textAlign = 'center';
    ctx.fillStyle = css('--ok');
    ctx.font = "14px 'Times New Roman', serif";
    ctx.fillText('none, at any number of agents', pad.l + iw / 2, pad.t + ih / 2);
  }
}

function renderRace() {
  const rows = raceRows();
  if (!rows.length) return;
  // Always the most contended cell, never the worst-looking one. Picking by
  // defect count would have shown the lease path its quietest row, because it
  // has no defects anywhere, and hidden the four seconds it costs to get there.
  const worst = rows.reduce((a, b) => (b.concurrency > a.concurrency ? b : a));
  const think = state.thinks[state.thinkIdx];
  el('r-conc').textContent = String(worst.concurrency);
  el('r-double').textContent = worst.double_per_100_agents.toFixed(1);
  el('r-outcomes').textContent = outcomeText(worst.outcomes);
  el('r-p95').textContent = `${worst.overhead_p95_ms.toFixed(0)} ms`;
  const mode = MODES.find((m) => m.key === state.mode);
  el('cap-what').textContent = `${mode.label}: ${mode.blurb}, ${think}ms of thinking`;
  drawRace();

  const b = el('race-banner');
  if (worst.double_per_100_agents === 0) {
    b.className = 'banner calm';
    const denied = worst.outcomes.lease_denied || 0;
    const total = Object.values(worst.outcomes).reduce((x, y) => x + y, 0);
    b.className = worst.overhead_p95_ms > 100 ? 'banner' : 'banner calm';
    b.textContent =
      `No room is taken twice at any concurrency. With ${worst.concurrency} agents it adds ` +
      `${worst.overhead_p95_ms.toFixed(0)}ms at p95` +
      (denied ? `, and turns ${Math.round((denied / total) * 100)}% of them away without a booking.` : '.');
  } else {
    b.className = 'banner alarm';
    b.textContent =
      `With ${worst.concurrency} agents thinking for ${think}ms, ${worst.outcomes.booked} bookings ` +
      `were taken where the rooms allowed far fewer: ${worst.double_per_100_agents.toFixed(1)} ` +
      `double bookings per 100 agents.`;
  }
}

// ------------------------------------------------ figure 2: the lost updates

function drawLost() {
  const think = state.think2;
  const { ctx, w, h } = fitCanvas(el('plot-lost'), 240);
  const pad = { l: 62, r: 24, t: 22, b: 66 };
  const iw = w - pad.l - pad.r;
  const ih = h - pad.t - pad.b;
  const top = 100;
  const Y = (v) => pad.t + ih - (v / top) * ih;

  ctx.strokeStyle = css('--hair');
  ctx.beginPath();
  ctx.moveTo(pad.l, pad.t); ctx.lineTo(pad.l, pad.t + ih); ctx.lineTo(pad.l + iw, pad.t + ih);
  ctx.stroke();
  ctx.textAlign = 'right';
  ctx.font = "11px 'Courier New', monospace";
  for (let v = 0; v <= top; v += 25) {
    ctx.fillStyle = css('--faint');
    ctx.fillText(String(v), pad.l - 8, Y(v) + 3);
    if (v > 0) {
      ctx.strokeStyle = css('--grid');
      ctx.beginPath(); ctx.moveTo(pad.l, Y(v)); ctx.lineTo(pad.l + iw, Y(v)); ctx.stroke();
    }
  }

  const slot = iw / MODES.length;
  const gw = slot * 0.74;
  const bw = gw / SCENARIOS.length;
  MODES.forEach((m, i) => {
    const x0 = pad.l + slot * i + (slot - gw) / 2;
    SCENARIOS.forEach((sc, j) => {
      const row = state.data.anomalies.find(
        (r) => r.mode === m.key && r.scenario === sc && r.think_ms === think,
      );
      if (!row) return;
      const x = x0 + bw * j;
      const y = Y(row.lost_per_100_trials);
      ctx.fillStyle = css('--ox');
      ctx.fillRect(x, y, bw - 3, pad.t + ih - y);
      // Each scenario gets its own hatch density, so the three are separable
      // without depending on colour.
      if (j > 0) {
        ctx.save();
        ctx.beginPath(); ctx.rect(x, y, bw - 3, pad.t + ih - y); ctx.clip();
        ctx.strokeStyle = css('--paper');
        ctx.lineWidth = 1.4;
        const step = j === 1 ? 6 : 3;
        for (let k = -ih; k < bw + ih; k += step) {
          ctx.beginPath();
          ctx.moveTo(x + k, pad.t + ih); ctx.lineTo(x + k + ih, pad.t);
          ctx.stroke();
        }
        ctx.restore();
      }
      ctx.font = "11px 'Times New Roman', serif";
      ctx.fillStyle = css('--sub');
      labelOnPaper(ctx, row.lost_per_100_trials.toFixed(0), x + (bw - 3) / 2, y - 6);
    });
    ctx.textAlign = 'center';
    ctx.fillStyle = css('--sub');
    ctx.font = "13px 'Times New Roman', serif";
    ctx.fillText(m.label, pad.l + slot * i + slot / 2, pad.t + ih + 18);
  });

  let lx = pad.l;
  ctx.textAlign = 'left';
  ctx.font = "11px 'Courier New', monospace";
  SCENARIOS.forEach((sc, j) => {
    ctx.fillStyle = css('--ox');
    ctx.fillRect(lx, h - 26, 13, 11);
    if (j > 0) {
      ctx.strokeStyle = css('--paper');
      ctx.lineWidth = 1.3;
      const step = j === 1 ? 6 : 3;
      for (let k = -11; k < 24; k += step) {
        ctx.beginPath(); ctx.moveTo(lx + k, h - 15); ctx.lineTo(lx + k + 11, h - 26); ctx.stroke();
      }
    }
    ctx.fillStyle = css('--sub');
    const t = sc.replace(/_/g, ' ');
    ctx.fillText(t, lx + 19, h - 17);
    lx += 19 + ctx.measureText(t).width + 26;
  });
}

function renderLost() {
  el('cap-think2').textContent = `${state.think2}ms of thinking`;
  drawLost();
  const at = (mode) =>
    SCENARIOS.map(
      (sc) =>
        state.data.anomalies.find(
          (r) => r.mode === mode && r.scenario === sc && r.think_ms === state.think2,
        )?.lost_per_100_trials ?? 0,
    );
  const legacy = at('legacy');
  const recheck = at('recheck');
  const b = el('lost-banner');
  const worseOrSame = recheck.filter((v, i) => v >= legacy[i]).length;
  b.className = worseOrSame >= 2 ? 'banner alarm' : 'banner';
  b.textContent =
    `Re-reading leaves ${recheck.map((v) => v.toFixed(0)).join(', ')} edits lost per 100 trials ` +
    `against ${legacy.map((v) => v.toFixed(0)).join(', ')} for doing nothing. ` +
    `Compare-and-set and leases both reach zero.`;
}

// ------------------------------------------------------ figure 3: scoreboard

function board() {
  const rows = MODES.map((m) => ({
    m,
    race: state.data.sweep.find(
      (r) => r.mode === m.key && r.concurrency === 16 && r.think_ms === 800,
    ),
    lost: state.data.anomalies.find(
      (r) => r.mode === m.key && r.scenario === 'modify_vs_modify' && r.think_ms === 400,
    ),
  })).filter((r) => r.race && r.lost);

  const head =
    '<tr><th>booking path</th><th>rooms booked</th><th>double bookings</th>' +
    '<th>edits lost</th><th>added p95</th><th>turned away</th></tr>';
  const body = rows
    .map(({ m, race, lost }) => {
      const denied = race.outcomes.lease_denied || 0;
      const total = Object.values(race.outcomes).reduce((a, b) => a + b, 0);
      return (
        `<tr><td class="mode">${m.label}</td>` +
        `<td class="num">${race.outcomes.booked ?? 0}</td>` +
        `<td class="num ${race.double_per_100_agents > 0 ? 'bad' : 'good'}">` +
        `${race.double_per_100_agents.toFixed(1)}</td>` +
        `<td class="num ${lost.lost_per_100_trials > 0 ? 'bad' : 'good'}">` +
        `${lost.lost_per_100_trials.toFixed(0)}</td>` +
        `<td class="num ${race.overhead_p95_ms > 100 ? 'bad' : ''}">` +
        `${race.overhead_p95_ms.toFixed(0)} ms</td>` +
        `<td class="num ${denied ? 'bad' : ''}">${denied ? `${Math.round((denied / total) * 100)}%` : 'none'}</td></tr>`
      );
    })
    .join('');
  el('board').innerHTML = `<thead>${head}</thead><tbody>${body}</tbody>`;
  el('board-banner').textContent =
    'Double bookings are per 100 agents at 16 concurrent and 800ms of thinking. Edits lost are per ' +
    '100 trials in modify against modify at 400ms. Only one column separates the last three rows.';
}

// ---------------------------------------------------------------- wiring

function picker(node, items, current, onPick) {
  node.innerHTML = '';
  items.forEach(({ key, label }) => {
    const b = document.createElement('button');
    b.textContent = label;
    b.setAttribute('aria-pressed', String(key === current()));
    b.addEventListener('click', () => {
      onPick(key);
      [...node.children].forEach((c) => c.setAttribute('aria-pressed', String(c === b)));
    });
    node.appendChild(b);
  });
}

async function main() {
  const res = await fetch('./data/runs.json');
  if (!res.ok) {
    el('race-banner').textContent = `Could not load the runs (HTTP ${res.status}).`;
    return;
  }
  state.data = await res.json();
  state.thinks = [...new Set(state.data.sweep.map((r) => r.think_ms))].sort((a, b) => a - b);
  state.think2s = [...new Set(state.data.anomalies.map((r) => r.think_ms))].sort((a, b) => a - b);
  state.thinkIdx = state.thinks.length - 1;
  state.think2 = state.think2s[state.think2s.length - 1];

  picker(el('modes'), MODES.map((m) => ({ key: m.key, label: m.label })), () => state.mode, (k) => {
    state.mode = k;
    renderRace();
  });
  picker(
    el('think2'),
    state.think2s.map((t) => ({ key: t, label: `${t}ms` })),
    () => state.think2,
    (k) => { state.think2 = k; renderLost(); },
  );

  const think = el('think');
  think.max = String(state.thinks.length - 1);
  think.value = String(state.thinkIdx);
  think.addEventListener('input', (e) => { state.thinkIdx = Number(e.target.value); renderRace(); });
  window.addEventListener('resize', () => { renderRace(); renderLost(); });

  renderRace();
  renderLost();
  board();
}

main();
