// The race, actually raced.
//
// server.py is copied verbatim into docs/data/ and loaded through pyodide with
// sqlite3, so the booking paths here are the ones the harness drives over HTTP:
// same do_book, same schema, same deliberate absence of a unique index. What
// the page adds is the part a chart cannot show, which is that the outcome is
// not fixed. Each agent reads, waits a real delay, then writes, and the
// interleaving is whatever the event loop happens to produce.
(() => {
  const el = (id) => document.getElementById(id);
  const MODES = [
    ['legacy', 'Legacy'],
    ['recheck', 'Re-read'],
    ['occ', 'Compare-and-set'],
    ['lease', 'Lease'],
  ];
  const st = { mode: 'legacy', n: 4, think: 400, py: null, api: null, running: false };

  function picker(node, items, current, onPick) {
    node.innerHTML = '';
    items.forEach(([key, label]) => {
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

  function drawAgents(rows) {
    el('try-agents').innerHTML = rows
      .map(
        (r, i) =>
          `<div class="agent ${r.cls || ''}" id="try-ag-${i}">` +
          `<span class="who">agent ${i + 1}</span>` +
          `<span class="bar"><i style="right:${100 - (r.pct || 0)}%"></i></span>` +
          `<span class="verdict">${r.text}</span></div>`,
      )
      .join('');
  }

  async function race() {
    if (st.running || !st.api) return;
    st.running = true;
    el('try-go').disabled = true;

    st.api.reset(st.mode);
    el('try-hint').textContent =
      'Reading the screen. Every agent sees the room as free before any of them books.';
    const rows = Array.from({ length: st.n }, () => ({ text: 'reading the screen', pct: 6 }));
    drawAgents(rows);

    // Every agent reads first, which is what makes it a race: they all saw the
    // room free before any of them wrote.
    const seen = rows.map(() => JSON.parse(st.api.read()));

    // Real waits, staggered a little, so the interleaving is genuine rather
    // than a fixed order dressed up as one.
    await Promise.all(
      rows.map(async (row, i) => {
        const jitter = Math.random() * 90;
        const total = st.think + jitter;
        const started = performance.now();
        await new Promise((r) => setTimeout(r, total));
        row.pct = 70;
        row.text = 'booking';
        drawAgents(rows);
        const res = JSON.parse(
          st.api.book(JSON.stringify({
            actor: `agent-${i + 1}`,
            guest: `agent-${i + 1}`,
            room_version: seen[i].version,
            epoch: 0,
          })),
        );
        row.pct = 100;
        row.ms = Math.round(performance.now() - started);
        if (res.code === 200) {
          row.cls = 'booked';
          row.text = `booked · ${row.ms} ms`;
          row.booked = true;
        } else {
          row.cls = 'declined';
          row.text = `${res.body.error || 'declined'} · ${row.ms} ms`;
        }
        drawAgents(rows);
      }),
    );

    el('try-hint').textContent =
      'All ' + st.n + ' read the room as free, then wrote one after another. What they were ' +
      'told is on the right.';
    const booked = rows.filter((r) => r.booked).length;
    const active = st.api.active();
    const dupes = Math.max(active - 1, 0);
    if (dupes > 0) rows.filter((r) => r.booked).forEach((r) => { r.cls = 'dupe'; });
    drawAgents(rows);

    el('try-booked').textContent = booked;
    el('try-declined').textContent = st.n - booked;
    const d = el('try-dupes');
    d.textContent = dupes;
    d.className = dupes ? 'bad' : 'good';

    const b = el('try-banner');
    if (dupes > 0) {
      b.className = 'banner alarm';
      b.textContent =
        `One room, sold ${active} times. ${booked} of ${st.n} agents were told yes, and ` +
        `nothing in the database stopped it, because there is no unique index to stop it with.`;
    } else {
      b.className = 'banner calm';
      b.textContent =
        `One room, sold once. ${st.n - booked} agents were turned away by the check inside ` +
        `the write, which is the whole difference between this path and the legacy one.`;
    }
    st.running = false;
    el('try-go').disabled = false;
  }

  async function boot() {
    try {
      const py = await loadPyodide();
      await py.loadPackage('sqlite3');
      py.FS.writeFile('server.py', await (await fetch('./data/server.py')).text());
      const api = py.runPython(`
import json, server

def _reset(mode):
    server.init_db(mode)

def _read():
    c = server.db()
    r = c.execute("SELECT status, version FROM rooms WHERE room='101'").fetchone()
    c.close()
    return json.dumps({"status": r["status"], "version": r["version"]})

def _book(payload):
    code, body = server.do_book(json.loads(payload))
    return json.dumps({"code": code, "body": body})

def _active():
    c = server.db()
    n = c.execute("SELECT COUNT(*) n FROM reservations WHERE status='active'").fetchone()["n"]
    c.close()
    return n

{"reset": _reset, "read": _read, "book": _book, "active": _active}
`).toJs({ dict_converter: Object.fromEntries });
      st.api = api;
      st.py = py;
      el('try-engine').textContent = 'server.py running in your tab, via pyodide';
      el('try-go').disabled = false;
    } catch (e) {
      el('try-engine').textContent = 'the engine did not start';
      el('try-banner').className = 'banner alarm';
      el('try-banner').textContent = `Could not start the store: ${e}`;
    }
  }

  picker(el('try-mode'), MODES, () => st.mode, (k) => { st.mode = k; });
  el('try-n').addEventListener('input', (e) => {
    st.n = Number(e.target.value); el('try-n-val').textContent = st.n;
  });
  el('try-think').addEventListener('input', (e) => {
    st.think = Number(e.target.value); el('try-think-val').textContent = `${st.think}ms`;
  });
  el('try-go').addEventListener('click', race);
  boot();
})();
