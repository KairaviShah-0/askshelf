/* AskShelf product app (vanilla JS, hash router). */
'use strict';

// ------------------------------------------------------------------ helpers
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const sleep = ms => new Promise(r => setTimeout(r, ms));
const store = {
  get(k, d) { try { return sessionStorage.getItem(k) ?? localStorage.getItem(k) ?? d; } catch { return d; } },
  setL(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
  setS(k, v) { try { sessionStorage.setItem(k, v); } catch { /* private mode */ } },
};
const state = { meta: null, role: null, session: new URLSearchParams(location.search).get('session') || store.get('as_session') || (crypto.randomUUID ? crypto.randomUUID().replace(/-/g, '') : String(Math.random()).slice(2)), ds: null, used: 0, alertCount: 0 };
store.setS('as_session', state.session);

async function api(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: { 'Content-Type': 'application/json', 'X-Role': state.role || '', 'X-Session': state.session, ...(opts.headers || {}) } });
  if (res.status === 403) { const j = await res.json().catch(() => ({})); throw Object.assign(new Error(j.message || 'Not permitted'), { forbidden: true }); }
  if (!res.ok) { const j = await res.json().catch(() => ({})); throw Object.assign(new Error(j.message || `Request failed (${res.status})`), { status: res.status, code: j.error }); }
  return res.json();
}
async function download(path, filename) {
  try {
    const res = await fetch(path, { headers: { 'X-Role': state.role, 'X-Session': state.session } });
    if (!res.ok) { const j = await res.json().catch(() => ({})); throw new Error(j.message || 'Download failed'); }
    const url = URL.createObjectURL(await res.blob());
    const a = Object.assign(document.createElement('a'), { href: url, download: filename }); document.body.append(a); a.click(); a.remove(); URL.revokeObjectURL(url);
    toast('Downloaded ' + filename);
  } catch (e) { toast(e.message, true); }
}
function toast(msg, err = false) {
  const t = document.createElement('div'); t.className = 'toast' + (err ? ' err' : ''); t.innerHTML = (err ? icon('warn') : icon('check')) + `<span>${esc(msg)}</span>`;
  $('#toasts').append(t); setTimeout(() => t.remove(), 3800);
}
function modal(html, onMount) {
  const m = $('#modal'); m.innerHTML = `<div class="mbox">${html}</div>`; m.hidden = false;
  m.onclick = e => { if (e.target === m) closeModal(); }; onMount?.(m);
}
function closeModal() { $('#modal').hidden = true; $('#modal').innerHTML = ''; }

// minimal, safe markdown: headings, bold, italics, code, bullet and numbered lists
function md(src) {
  const inline = s => esc(s).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/(^|[\s(])\*(?!\s)(.+?)\*(?=[\s).,;:!?]|$)/g, '$1<em>$2</em>').replace(/`(.+?)`/g, '<code>$1</code>');
  const out = []; let list = null, para = [];
  const flushP = () => { if (para.length) { out.push(`<p>${inline(para.join(' '))}</p>`); para = []; } };
  const flushL = () => { if (list) { out.push(`<${list.t}>${list.items.map(i => `<li>${inline(i)}</li>`).join('')}</${list.t}>`); list = null; } };
  for (const raw of String(src).split('\n')) {
    const line = raw.trimEnd(); let m;
    if (!line.trim()) { flushP(); flushL(); continue; }
    if ((m = line.match(/^#{1,4}\s+(.*)/))) { flushP(); flushL(); out.push(`<h4>${inline(m[1])}</h4>`); continue; }
    if ((m = line.match(/^\s*[-*•]\s+(.*)/))) { flushP(); if (!list || list.t !== 'ul') { flushL(); list = { t: 'ul', items: [] }; } list.items.push(m[1]); continue; }
    if ((m = line.match(/^\s*\d+[.)]\s+(.*)/))) { flushP(); if (!list || list.t !== 'ol') { flushL(); list = { t: 'ol', items: [] }; } list.items.push(m[1]); continue; }
    flushL(); para.push(line.trim());
  }
  flushP(); flushL();
  return out.map((h, i) => h.replace(/^<(\w+)/, `<$1 style="--i:${i}"`)).join('');
}


// ------------------------------------------------------------------ motion
const REDUCED = matchMedia('(prefers-reduced-motion: reduce)').matches;
function animateNumbers(root = document) {
  if (REDUCED) return;
  $$('.kpi b', root).forEach(el => {
    if (el.dataset.anim) return; el.dataset.anim = 1;
    const m = el.textContent.match(/^([^\d-]*)(-?\d[\d,]*(?:\.\d+)?)(.*)$/); if (!m) return;
    const dec = (m[2].split('.')[1] || '').length, to = parseFloat(m[2].replace(/,/g, '')), t0 = performance.now(), dur = 900;
    const fmt = v => v.toLocaleString('en-US', { minimumFractionDigits: dec, maximumFractionDigits: dec });
    (function tick(t) { const p = Math.min((t - t0) / dur, 1), e = 1 - Math.pow(1 - p, 3); el.textContent = m[1] + fmt(to * e) + m[3]; if (p < 1) requestAnimationFrame(tick); })(t0);
  });
}
function growBars(el, f, layout) {
  // simple bars (plain arrays, no base) grow from zero; everything else just fades in via CSS
  if (REDUCED || !f.data.every(d => d.type === 'bar' && !d.base && Array.isArray(d.orientation === 'h' ? d.x : d.y))) return Plotly.newPlot(el, f.data, layout, { displayModeBar: false, responsive: true });
  const key = d => (d.orientation === 'h' ? 'x' : 'y');
  const start = f.data.map(d => ({ ...d, [key(d)]: d[key(d)].map(() => 0) }));
  Plotly.newPlot(el, start, layout, { displayModeBar: false, responsive: true }).then(() =>
    Plotly.animate(el, { data: f.data.map(d => ({ [key(d)]: d[key(d)] })), traces: f.data.map((_, i) => i) }, { transition: { duration: 800, easing: 'cubic-out' }, frame: { duration: 800 } }));
}

// ------------------------------------------------------------------ charts (Plotly, lazily drawn so hidden tabs size correctly)
let chartSeq = 0; const figs = new Map();
function chartTitle(f) { const t = f.layout?.title; return typeof t === 'string' ? t : t?.text || ''; }
function chartBox(fig, h) {
  if (!fig) return '';
  const id = 'ch' + (++chartSeq); figs.set(id, fig);
  const t = chartTitle(fig);
  return `${t ? `<div class="ctitle">${t.replace(/<(?!\/?b>|br>)[^>]*>/g, '')}</div>` : ''}<div class="chart" id="${id}" style="${h ? `min-height:${h}px` : ''}"></div>`;
}
function drawCharts(root = document) {
  $$('.chart', root).forEach(el => {
    if (el.dataset.done || !figs.has(el.id) || el.offsetParent === null) return;
    const f = figs.get(el.id); el.dataset.done = 1;
    const layout = { ...f.layout, width: undefined, autosize: true, paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)', font: { ...(f.layout?.font || {}), family: 'Inter, system-ui, sans-serif' } };
    delete layout.template;
    const hadTitle = !!chartTitle(f); delete layout.title;
    const m = { ...(layout.margin || {}) }; m.l = Math.max(m.l ?? 0, 8); m.r = Math.max(m.r ?? 0, 16); m.b = Math.max(m.b ?? 0, 8);
    m.t = hadTitle ? Math.max((m.t ?? 56) - 44, 16) : (m.t ?? 16); layout.margin = m;
    if (layout.height) layout.height = Math.max(layout.height - (hadTitle ? 40 : 0), 200);
    Object.keys(layout).filter(k => /^(x|y)axis\d*$/.test(k)).forEach(k => { layout[k] = { ...layout[k], automargin: true }; });
    ['xaxis', 'yaxis'].forEach(k => { layout[k] = { automargin: true, ...(layout[k] || {}) }; });
    Plotly.newPlot(el, f.data, layout, { displayModeBar: false, responsive: true });
  });
  $$('.chart[data-done]', root).forEach(el => { try { Plotly.Plots.resize(el); } catch { /* not drawn yet */ } });
}
function tabsHTML(id, names) { return `<div class="mini-tabs" data-tabs="${id}">${names.map((n, i) => `<button class="${i ? '' : 'on'}" data-i="${i}">${esc(n)}</button>`).join('')}</div>`; }
function wireTabs(root) {
  $$('[data-tabs]', root).forEach(bar => {
    const id = bar.dataset.tabs;
    bar.onclick = e => {
      const b = e.target.closest('button'); if (!b) return;
      $$('button', bar).forEach(x => x.classList.toggle('on', x === b));
      $$(`[data-pane="${id}"]`, root).forEach(p => { p.hidden = p.dataset.i !== b.dataset.i; });
      drawCharts(root);
    };
  });
}
function paneAttrs(id, i) { return `data-pane="${id}" data-i="${i}" ${i ? 'hidden' : ''}`; }

// ------------------------------------------------------------------ small UI builders
const kpiHTML = (k, i = 0) => {
  let cls = 'flat'; if (k.delta && !/^[+-]?0(\.0+)?\s*(%|pp)?$/.test(String(k.delta).trim().replace(/^\+/, ''))) { const up = String(k.delta).trim().startsWith('+'); cls = up ? 'up' : 'down'; if (k.good === 'down') cls = up ? 'down' : 'up'; }
  return `<div class="kpi" style="--i:${i}"><small>${esc(k.label)}</small><b>${esc(k.value)}</b><div class="d ${cls}">${esc(k.delta || ' ')}</div></div>`;
};
const kpiRow = (ks, cls = '') => `<div class="grid ${cls || 'g' + Math.min(ks.length, 6)}">${ks.map(kpiHTML).join('')}</div>`;
function callouts(ins = [], act = [], cau = []) {
  let h = '';
  if (ins.length) h += `<div class="sect">What stands out</div>` + ins.map(i => `<div class="note">${esc(i)}</div>`).join('');
  if (act.length) h += `<div class="sect">What I would do</div>` + act.map(i => `<div class="note action">${esc(i)}</div>`).join('');
  return h + cau.filter(Boolean).map(i => `<div class="note caution">${esc(i)}</div>`).join('');
}
function tableHTML(t, name = 'table') {
  if (!t || !t.data?.length) return '';
  const id = 'tb' + (++chartSeq); figs.set(id, t);
  const body = t.data.map(r => `<tr>${r.map(c => `<td>${esc(c ?? '')}</td>`).join('')}</tr>`).join('');
  return `<div class="tablewrap"><table class="t"><thead><tr>${t.columns.map(c => `<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>${body}</tbody></table></div>
  <button class="linkbtn" data-csv="${id}" data-name="${esc(name)}">${icon('download')} Download CSV</button>`;
}
document.addEventListener('click', e => {
  const b = e.target.closest('[data-csv]'); if (!b) return;
  const t = figs.get(b.dataset.csv); const q = v => `"${String(v ?? '').replace(/"/g, '""')}"`;
  const csv = [t.columns.map(q).join(','), ...t.data.map(r => r.map(q).join(','))].join('\n');
  const a = Object.assign(document.createElement('a'), { href: URL.createObjectURL(new Blob([csv], { type: 'text/csv' })), download: b.dataset.name + '.csv' }); a.click();
});
const skeleton = (n = 3) => `<div class="page"><div class="grid g4" style="margin-bottom:16px">${'<div class="sk" style="height:84px"></div>'.repeat(4)}</div>${'<div class="sk" style="height:300px;margin-bottom:16px"></div>'.repeat(n - 1)}</div>`;
const restricted = msg => `<div class="restricted"><div class="big">${icon('lock')}</div><h2>This area is restricted for your role</h2><p>${esc(msg)}</p>
  <p>Switch role in the top-right corner to explore what each role can access.</p></div>`;
const askLink = (q, label = 'Ask AskShelf about this') => `<button class="linkbtn" data-ask="${esc(q)}">${icon('chat')} ${esc(label)}</button>`;
document.addEventListener('click', e => { const b = e.target.closest('[data-ask]'); if (b) { location.hash = '#/ask'; setTimeout(() => labAsk(b.dataset.ask), 60); } });

// ------------------------------------------------------------------ router
const NAV = [
  ['Workspace', [['data', 'Data', 'upload'], ['ask', 'Ask', 'chat'], ['reports', 'Reports', 'file']]],
  ['Insights', [['dashboard', 'Dashboard', 'dashboard'], ['alerts', 'Alerts', 'bell'], ['deliveries', 'Deliveries', 'clock'], ['penalties', 'Penalties', 'flag'], ['stock', 'Stock', 'layers'], ['forecasts', 'Forecasts', 'trend'], ['planner', 'Scenario planner', 'sliders']]],
  ['Admin', [['governance', 'Governance', 'shield'], ['knowledge', 'Knowledge base', 'book']]],
];
const TITLES = {
  data: ['Data', 'Upload files, confirm what each column means, and check data quality.'], ask: ['Ask', 'Plain-English questions about your data, with the query shown.'], reports: ['Reports', 'Formal reports as PDF, Word and Excel.'],
  dashboard: ['Dashboard', 'Sales, orders, margin and delivery performance at a glance.'], alerts: ['Alerts', 'What needs attention, before you ask.'], deliveries: ['Deliveries', 'On-time performance, supplier lateness and late-risk on open orders.'],
  penalties: ['Penalties', 'What late deliveries cost, and what expediting would save.'], stock: ['Stock', 'Days of cover, reorder needs and suggested orders.'], forecasts: ['Forecasts', 'Prophet forecasts with the back-test error shown.'],
  planner: ['Scenario planner', 'Move price, volume and cost and see revenue and profit respond.'], governance: ['Governance', 'Access control, audit trail and data privacy.'], knowledge: ['Knowledge base', 'What every metric means and which data is needed.'],
};
const CAP = { dashboard: 'dashboard', deliveries: 'deliveries', penalties: 'penalties', stock: 'stock', forecasts: 'forecasts', planner: 'planner' };
const PAGES = { data: pageData, ask: pageAsk, reports: pageReports, dashboard: pageDashboard, alerts: pageAlerts, deliveries: pageDeliveries, penalties: pagePenalties, stock: pageStock, forecasts: pageForecasts, planner: pagePlanner, governance: pageGovernance, knowledge: pageKnowledge };
let currentRoute = null;
const roleCan = p => state.meta.roles[state.role].pages.includes(p);
const loaded = () => !!state.ds?.loaded;

function renderNav() {
  $('#nav').innerHTML = NAV.map(([group, items]) => `<div class="nav-g">${group}</div>` + items.map(([id, label, ic]) => {
    const locked = !roleCan(id), nodata = !locked && CAP[id] && loaded() && state.ds.caps && !state.ds.caps[CAP[id]];
    const badge = id === 'alerts' && state.alertCount ? `<span class="badge">${state.alertCount}</span>` : '';
    return `<a class="nav-i ${currentRoute === id ? 'on' : ''} ${locked ? 'locked' : ''}" href="#/${id}">${icon(ic)}<span>${label}</span>${badge}${locked ? `<span class="lk">${icon('lock')}</span>` : nodata ? `<span class="lk" title="Needs more columns">${icon('warn')}</span>` : ''}</a>`;
  }).join('')).join('');
  const d = state.ds;
  $('#modebox').innerHTML = loaded()
    ? `<b>${icon('db')} ${d.files.length} file${d.files.length > 1 ? 's' : ''} loaded</b><span>${d.rows_total.toLocaleString()} rows${d.range ? ' &middot; ' + esc(d.range) : ''}</span>`
    : `<b>${icon('upload')} No data loaded</b><span>Upload files on the Data page to begin.</span>`;
  const gen = state.meta.mode === 'generative';
  $('#modebox').innerHTML += `<span style="margin-top:6px"><span class="dot-live" style="${gen ? '' : 'background:#d97706'}"></span>${gen ? 'AI mode on' : 'Built-in mode'}</span>`;
}
async function route() {
  let id = (location.hash.replace('#/', '') || '').split('?')[0];
  if (!PAGES[id]) id = loaded() ? 'dashboard' : 'data';
  currentRoute = id; renderNav(); $('#side').classList.remove('open');
  const [title, sub] = TITLES[id];
  $('#ptitle').textContent = title; $('#psub').textContent = sub; document.title = `${title} | AskShelf`;
  const view = $('#view'); view.className = 'view'; view.scrollTop = 0;
  const token = (route.token = (route.token || 0) + 1);
  if (!roleCan(id)) { view.innerHTML = restricted(`Your role (${state.role}) cannot open ${title}.`); hydrateIcons(view); return; }
  try { await PAGES[id](view, token); }
  catch (e) {
    if (token !== route.token) return;
    view.innerHTML = e.forbidden ? restricted(e.message) : e.code === 'no_data' ? emptyState() : e.code === 'missing_columns' ? missingState(e.message) :
      `<div class="restricted"><div class="big">${icon('warn')}</div><h2>${e.code === 'cannot_analyse' ? 'This data cannot be analysed this way' : 'Something went wrong'}</h2><p>${esc(e.message)}</p><button class="btn btn-primary" onclick="route()">Try again</button></div>`;
  }
  if (token !== route.token) return;
  drawCharts(view); wireTabs(view); hydrateIcons(view); animateNumbers(view);
}
const stale = t => t !== route.token;
const emptyState = () => `<div class="restricted"><div class="big" style="background:var(--primary-l);color:var(--primary)">${icon('upload')}</div><h2>Upload your data to begin</h2><p>Every page is built from your own files. Add an order or sales export (Excel or CSV) and AskShelf does the rest.</p><a class="btn btn-primary" href="#/data">Go to Data ${icon('arrow')}</a></div>`;
const missingState = msg => `<div class="restricted"><div class="big" style="background:var(--amber-l);color:var(--amber)">${icon('warn')}</div><h2>Your data is missing a column this page needs</h2><p>${esc(msg)}</p><a class="btn btn-primary" href="#/data">Check column mapping ${icon('arrow')}</a></div>`;

async function refreshDataset() {
  try { state.ds = await api('/api/data'); } catch { state.ds = state.ds || { loaded: false }; }
  renderNav(); refreshAlertCount();
}
async function refreshAlertCount() {
  if (!loaded() || !roleCan('alerts')) { state.alertCount = 0; renderNav(); return; }
  try { const d = await api('/api/alerts'); state.alertCount = d.alerts.filter(a => a.severity !== 'info').length; } catch { state.alertCount = 0; }
  renderNav();
}

// ------------------------------------------------------------------ small shared pieces
const grid2 = (...cs) => `<div class="grid g2">${cs.map(c => `<div class="card">${c}</div>`).join('')}</div>`;
const chartsGrid = charts => {
  if (!charts?.length) return '';
  const [first, ...rest] = charts;
  return `<div class="card" style="margin-bottom:16px">${chartBox(first.fig)}</div>` + (rest.length ? `<div class="grid ${rest.length === 1 ? '' : 'g2'}" style="margin-bottom:16px">${rest.map(c => `<div class="card">${chartBox(c.fig)}</div>`).join('')}</div>` : '');
};
const notesHTML = n => (n || []).map(x => `<div class="note caution">${esc(x)}</div>`).join('');
function slider(id, label, min, max, val, suffix = '') { return `<div class="field"><label>${label}<output id="o-${id}">${val}${suffix}</output></label><input type="range" id="${id}" min="${min}" max="${max}" value="${val}" data-suffix="${suffix}"></div>`; }
function bindSliders(root, onchange, ms = 220) {
  let t = null;
  $$('input[type=range]', root).forEach(r => r.oninput = () => { $('#o-' + r.id).textContent = r.value + (r.dataset.suffix || ''); clearTimeout(t); t = setTimeout(onchange, ms); });
}
const fmtN = v => (v == null ? '' : typeof v === 'number' ? v.toLocaleString('en-US', { maximumFractionDigits: 2 }) : v);

// ------------------------------------------------------------------ PAGE: data (upload, mapping, preview, profile)
const L = { tab: 'map', table: 0, messages: [], busy: false, report: null, pdfUrl: null };

async function uploadFiles(files, view, token) {
  const list = $('#up-list'); list.innerHTML = '';
  for (const f of files) {
    const row = document.createElement('div'); row.className = 'uprow';
    row.innerHTML = `${icon('file')}<span>${esc(f.name)}</span><em>${(f.size / 1024).toFixed(0)} KB</em><b class="st">Uploading...</b>`; list.append(row);
    try {
      const res = await fetch('/api/data/upload?name=' + encodeURIComponent(f.name), { method: 'POST', body: f, headers: { 'X-Role': state.role, 'X-Session': state.session, 'Content-Type': 'application/octet-stream' } });
      const j = await res.json();
      if (!res.ok) throw new Error(j.message || 'Upload failed');
      state.ds = j; $('.st', row).textContent = 'Done'; row.classList.add('ok');
    } catch (e) { $('.st', row).textContent = e.message; row.classList.add('bad'); }
  }
  L.messages = []; L.report = null; L.pdfUrl = null; L.tab = 'map';
  if (loaded()) { renderNav(); refreshAlertCount(); renderData(view, token); }
}
function dropzone(compact) {
  return `<div class="dz ${compact ? 'compact' : ''}" id="dz"><div class="dz-ic">${icon('upload')}</div>
    <div><b>${compact ? 'Add more files' : 'Drop Excel or CSV files here'}</b><div class="sub" style="margin:.2rem 0 0">${compact ? 'Files with the same columns are combined automatically.' : 'or click to browse. Upload several files at once: files with the same columns are combined into one table. Up to 25 MB each.'}</div></div>
    <input type="file" id="dz-in" multiple accept=".xlsx,.xls,.csv,.tsv" hidden></div><div id="up-list" class="uplist"></div>`;
}
function wireDrop(view, token) {
  const dz = $('#dz'), inp = $('#dz-in'); if (!dz) return;
  dz.onclick = () => inp.click();
  inp.onchange = () => inp.files.length && uploadFiles([...inp.files], view, token);
  ['dragenter', 'dragover'].forEach(ev => dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.add('over'); }));
  ['dragleave', 'drop'].forEach(ev => dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.remove('over'); }));
  dz.addEventListener('drop', e => e.dataTransfer.files.length && uploadFiles([...e.dataTransfer.files], view, token));
}
async function pageData(view, token) {
  view.innerHTML = skeleton(2);
  state.ds = await api('/api/data'); if (stale(token)) return;
  renderNav(); renderData(view, token);
}
const CAPLABEL = { dashboard: ['Dashboard', 'Sales amount + Order date'], deliveries: ['Deliveries', 'Due date (+ Delivered date)'], penalties: ['Penalties', 'Due + Delivered date + Sales amount'], stock: ['Stock', 'Product + Stock on hand'], forecasts: ['Forecasts', 'Order date + Sales amount or Quantity'], planner: ['Scenario planner', 'Sales amount (+ cost for profit)'] };

function renderData(view, token) {
  const d = state.ds, canUp = state.meta.roles[state.role].can_upload;
  if (!d?.loaded) {
    view.innerHTML = `<div class="page"><div class="card lab-hero"><div class="lab-art">${icon('db')}</div><h2>Bring your own data</h2>
      <p>Upload an order or sales export from your store, warehouse or ERP. AskShelf maps your columns, then builds your dashboard, alerts, deliveries, stock, forecasts and reports from it.</p>
      ${canUp ? dropzone(false) : `<div class="note caution">Your role (${esc(state.role)}) cannot load data. Ask the Owner, Finance Lead or Operations Manager.</div>`}
      ${canUp ? `<div style="margin-top:14px"><button class="btn btn-ghost" id="sample">${icon('spark')} Try the sample retail data</button></div>` : ''}
      <div class="note" style="margin-top:16px;text-align:left"><b>What to upload:</b> one row per order line with a date and an amount at minimum. Add due and delivered dates, cost, stock on hand and a penalty column to unlock deliveries, margins, stock and penalties.<br><b>Privacy:</b> columns that look like personal data (emails, phone numbers, names, pay, card numbers) are removed on upload, before any analysis or AI call.</div></div></div>`;
    wireDrop(view, token); hydrateIcons(view);
    const sb = $('#sample'); if (sb) sb.onclick = async () => { sb.disabled = true; try { state.ds = await api('/api/data/sample', { method: 'POST' }); L.tab = 'map'; renderNav(); refreshAlertCount(); renderData(view, token); } catch (e) { toast(e.message, true); sb.disabled = false; } };
    return;
  }
  const t0 = d.tables[Math.min(L.table, d.tables.length - 1)];
  view.innerHTML = `<div class="page">
    <div class="grid g5" style="margin-bottom:14px">${kpiHTML({ label: 'Files', value: d.files.length }, 0)}${kpiHTML({ label: 'Tables', value: d.tables.length }, 1)}${kpiHTML({ label: 'Rows', value: d.rows_total }, 2)}${kpiHTML({ label: 'Usable rows (with a date)', value: d.usable_rows ?? 0 }, 3)}${kpiHTML({ label: 'Columns removed', value: d.dropped.length }, 4)}</div>
    ${d.range ? `<div class="note">${icon('clock')} Data covers ${esc(d.range)}.</div>` : ''}${d.fusion_note ? `<div class="note">${icon('layers')} ${esc(d.fusion_note)}</div>` : ''}
    ${d.dropped.length ? `<div class="note caution">${icon('lock')} Personal data removed on upload: ${d.dropped.map(x => `<span class="chip red">${esc(x.column)}</span>`).join(' ')}</div>` : ''}
    <div class="toolbar"><div class="seg" id="lt">${[['map', 'Column mapping'], ['preview', 'Preview'], ['profile', 'Profile']].map(([k, l]) => `<button data-k="${k}" class="${k === L.tab ? 'on' : ''}">${l}</button>`).join('')}</div>
      <span class="sp"></span>${canUp ? `<button class="btn btn-ghost btn-sm" id="l-add">${icon('plus')} Add files</button>` : ''}<button class="btn btn-ghost btn-sm" id="l-xl">${icon('download')} Sample (Excel)</button>
      <button class="btn btn-ghost btn-sm" id="l-mem">${icon('refresh')} Clear Ask history</button>${canUp ? `<button class="btn btn-ghost btn-sm" id="l-reset" style="color:var(--red)">${icon('trash')} Reset data</button>` : ''}</div>
    <div id="l-add-box" hidden>${dropzone(true)}</div><div id="lbody"></div></div>`;
  hydrateIcons(view);
  $('#lt').onclick = e => { const b = e.target.closest('button'); if (b) { L.tab = b.dataset.k; renderData(view, token); } };
  const add = $('#l-add'); if (add) add.onclick = () => { $('#l-add-box').hidden = !$('#l-add-box').hidden; wireDrop(view, token); };
  $('#l-xl').onclick = () => download('/api/data/export?kind=sample', 'data_sample.xlsx');
  $('#l-mem').onclick = async () => { await api('/api/data/clear-memory', { method: 'POST' }); L.messages = []; toast('Ask history cleared'); };
  const rs = $('#l-reset'); if (rs) rs.onclick = async () => { if (!confirm('Remove all uploaded data and results?')) return; await api('/api/data/reset', { method: 'POST' }); state.ds = { loaded: false }; L.messages = []; L.report = null; L.pdfUrl = null; renderNav(); renderData(view, token); };
  const body = $('#lbody');
  if (L.tab === 'map') renderMapping(body, view, token, canUp);
  else if (L.tab === 'preview') {
    body.innerHTML = `${d.tables.length > 1 ? `<div class="field" style="max-width:340px"><label>Table</label><select class="sel" id="tsel">${d.tables.map((t, i) => `<option value="${i}" ${i === L.table ? 'selected' : ''}>${esc(t.name)} (${t.rows.toLocaleString()} rows)</option>`).join('')}</select></div>` : ''}
      <div class="card"><h3>${esc(t0.name)}</h3><div class="sub">First 8 of ${t0.rows.toLocaleString()} rows.</div>${tableHTML(t0.sample, 'preview_' + t0.name)}</div>`;
    const ts = $('#tsel'); if (ts) ts.onchange = () => { L.table = +ts.value; renderData(view, token); };
  } else {
    body.innerHTML = d.tables.map(t => `<div class="card" style="margin-bottom:14px"><div class="toolbar" style="margin-bottom:8px"><h3 style="margin:0">${esc(t.name)}</h3><span class="chip">${t.rows.toLocaleString()} rows</span><span class="chip ${t.missing_cells_pct > 5 ? 'amber' : 'green'}">${t.missing_cells_pct}% missing</span>
      <span class="chip ${t.duplicates ? 'amber' : 'green'}">${t.duplicates} duplicate rows</span></div>
      <div class="tablewrap"><table class="t"><thead><tr><th>Column</th><th>Type</th><th>Missing</th><th>Distinct</th><th>Range / top values</th></tr></thead><tbody>${t.columns.map(c => `<tr><td><b>${esc(c.name)}</b></td>
      <td><span class="chip ${c.kind === 'numeric' ? 'blue' : c.kind === 'datetime' ? 'green' : ''}">${c.kind}</span></td><td>${c.missing_pct}%</td><td>${c.unique.toLocaleString()}</td>
      <td>${c.kind === 'numeric' ? `${fmtN(c.min)} to ${fmtN(c.max)} (mean ${fmtN(c.mean)})` : c.kind === 'datetime' ? `${c.min} to ${c.max}` : (c.top || []).map(x => esc(x[0])).slice(0, 4).join(', ')}</td></tr>`).join('')}</tbody></table></div></div>`).join('');
  }
  drawCharts(view); wireTabs(view);
}

function renderMapping(body, view, token, canUp) {
  const d = state.ds, m = d.mapping, cols = d.columns[m.table] || [];
  const fieldRow = f => {
    const cur = m.map[f.key] || '', ok = cols.filter(c => f.kind === 'date' ? c.kind === 'datetime' : f.kind === 'number' ? c.kind === 'numeric' : true);
    return `<div class="maprow"><div><b>${esc(f.label)}</b>${cur ? '<span class="chip green" style="margin-left:.5rem">mapped</span>' : ''}<div class="sub" style="margin:.1rem 0 0;font-size:.78rem">${esc(f.help)} <i>Used for: ${esc(f.needed_for)}</i></div></div>
      <select class="sel" data-f="${f.key}" ${canUp ? '' : 'disabled'}><option value="">- not in my data -</option>${ok.map(c => `<option value="${esc(c.name)}" ${c.name === cur ? 'selected' : ''}>${esc(c.name)} (${c.kind})</option>`).join('')}</select></div>`;
  };
  body.innerHTML = `<div class="grid gside"><div class="card" style="align-self:start"><h3>What each page needs</h3><div class="sub">Pages unlock as the right columns are mapped.</div>
      ${Object.entries(CAPLABEL).map(([k, [l, need]]) => `<div class="capline ${d.caps[k] ? 'ok' : 'no'}">${icon(d.caps[k] ? 'check' : 'warn')}<div><b>${l}</b><div class="sub" style="margin:0;font-size:.76rem">${d.caps[k] ? 'Ready' : esc(need)}</div></div></div>`).join('')}
      <div class="note" style="margin-top:12px;font-size:.82rem">${m.auto ? 'Columns were detected automatically from their names and values. Check them, change any that are wrong, then press Save.' : 'You confirmed these columns.'}</div></div>
    <div class="card"><div class="toolbar" style="margin-bottom:8px"><div><h3 style="margin:0">Column mapping</h3><div class="sub" style="margin:2px 0 0">Tell AskShelf which column plays which role.</div></div><span class="sp"></span>
      ${d.tables.length > 1 ? `<select class="sel" id="maptable" style="width:auto">${d.tables.map(t => `<option ${t.name === m.table ? 'selected' : ''}>${esc(t.name)}</option>`).join('')}</select>` : ''}</div>
      ${state.meta ? '' : ''}${d.fields.map(fieldRow).join('')}
      ${canUp ? `<div style="margin-top:14px;display:flex;gap:.6rem"><button class="btn btn-primary" id="savemap">${icon('check')} Save mapping</button></div>` : ''}</div></div>`;
  const mt = $('#maptable'); if (mt) mt.onchange = async () => { const mp = { ...m.map }; Object.keys(mp).forEach(k => mp[k] = null); try { state.ds = await api('/api/data/mapping', { method: 'POST', body: JSON.stringify({ table: mt.value, map: {} }) }); } catch (e) { toast(e.message, true); } renderData(view, token); };
  const sv = $('#savemap'); if (sv) sv.onclick = async () => {
    const map = {}; $$('select[data-f]', body).forEach(s => map[s.dataset.f] = s.value || null);
    try { state.ds = await api('/api/data/mapping', { method: 'POST', body: JSON.stringify({ table: m.table, map }) }); toast('Mapping saved'); renderNav(); refreshAlertCount(); renderData(view, token); } catch (e) { toast(e.message, true); }
  };
}

// ------------------------------------------------------------------ PAGE: ask
function suggestions() {
  const d = state.ds, m = d?.mapping?.map || {}; if (!m.amount && !m.qty) return [];
  const meas = m.amount || m.qty, n = c => c.replace(/_/g, ' '), pl = c => { const w = n(c); return /s$/.test(w) ? w : /[^aeiou]y$/.test(w) ? w.slice(0, -1) + 'ies' : w + 's'; }, dim = m.supplier || m.category || m.product || m.region, out = [];
  if (dim) out.push(`Top 5 ${pl(dim)} by ${n(meas)}`);
  if (m.date) out.push(`Monthly ${n(meas)} trend`, `Forecast ${n(meas)} for the next 6 months`);
  if (m.region && dim !== m.region) out.push(`Compare ${n(meas)} by ${n(m.region)}`);
  if (m.due && m.delivered && m.supplier) out.push(`Which ${pl(m.supplier)} deliver late most often?`);
  if (m.stock && m.product) out.push(`Which ${pl(m.product)} have the lowest ${n(m.stock)}?`);
  return out.slice(0, 6);
}
async function pageAsk(view, token) {
  if (!loaded()) { view.innerHTML = emptyState(); return; }
  const sug = suggestions();
  view.innerHTML = `<div class="page"><div class="card" style="margin-bottom:14px"><div class="cbox" style="max-width:none;box-shadow:none"><textarea id="lq" rows="1" maxlength="800" placeholder="Ask about your data, e.g. 'Top 5 products by sales' or 'Forecast sales for the next 6 months'"></textarea><button class="sendbtn" id="lsend" aria-label="Send">${icon('send')}</button></div>
    <div class="chips" style="margin-top:10px">${sug.map(q => `<button class="chip" data-lq="${esc(q)}">${esc(q)}</button>`).join('')}</div>
    <div class="cmeta" style="max-width:none"><span>Answers come from read-only queries on your uploaded data. Personal data is never shown. <button class="linkbtn" id="newchat">${icon('refresh')} New chat</button></span><span id="qcount"></span></div></div><div id="lthread"></div></div>`;
  const q = $('#lq'), go = () => { const v = q.value.trim(); if (v && !L.busy) { q.value = ''; labAsk(v); } };
  $('#lsend').onclick = go; q.onkeydown = e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); go(); } };
  $$('[data-lq]', view).forEach(b => b.onclick = () => labAsk(b.dataset.lq));
  $('#newchat').onclick = () => { if (!L.busy) { L.messages = []; renderLabThread(); toast('Started a new conversation'); } };
  updateCount(); renderLabThread(); hydrateIcons(view);
}
function updateCount() { const c = $('#qcount'); if (c) c.textContent = `${state.used}/${state.meta.max_questions} questions this session`; }
function renderLabThread() {
  const th = $('#lthread'); if (!th) return;
  th.innerHTML = L.messages.slice().reverse().map((m, ri) => {
    if (m.pending) return `<div class="card lres"><div class="think"><span class="typing"><i></i><i></i><i></i></span><span>Analysing "${esc(m.q)}"...</span></div></div>`;
    const r = m.res;
    if (r.blocked) return `<div class="card lres"><div class="qline">${esc(m.q)}</div><div class="blocked">${icon('lock')}<div>${esc(r.text)}</div></div></div>`;
    if (r.error) return `<div class="card lres"><div class="qline">${esc(m.q)}</div><div class="blocked">${icon('warn')}<div>${esc(r.error)}${r.sql ? `<details class="src"><summary>Query</summary><div class="in"><pre class="sqlbox">${esc(r.sql)}</pre></div></details>` : ''}</div></div></div>`;
    const ex = r.explanation || {}, last = ri === 0;
    return `<div class="card lres"><div class="qline">${esc(m.q)} <span class="chip ${r.mode === 'ai' ? 'blue' : 'amber'}">${r.mode === 'ai' ? 'AI-planned' : 'Built-in rules'}</span></div>
      <div class="lsum">${esc(ex.summary || '')}</div>${(ex.bullets || []).length ? `<ul class="lbul">${ex.bullets.map(b => `<li>${esc(b)}</li>`).join('')}</ul>` : ''}
      ${r.chart ? `<div class="achart">${chartBox(r.chart)}</div>` : ''}
      <details class="src"><summary>${icon('file')} Result table (${(r.table?.total_rows ?? 0).toLocaleString()} rows)${r.kind === 'forecast' ? ' and forecast detail' : ''}</summary><div class="in">${tableHTML(r.table, 'ask_result')}${r.group_table ? `<div class="sect">Forecast by group</div>${tableHTML(r.group_table, 'ask_group_forecast')}` : ''}</div></details>
      ${r.sql ? `<details class="src"><summary>${icon('db')} ${r.mode === 'ai' ? 'AI-generated' : 'Rule-generated'} read-only query</summary><div class="in"><pre class="sqlbox">${esc(r.sql)}</pre><div class="sub" style="font-size:.78rem">Validated as a single SELECT, with a row and time limit, before it ran.</div></div></details>` : ''}
      <div class="acts">${last && state.meta.roles[state.role].can_export ? `<button class="act" id="lxl">${icon('download')} Export result (Excel)</button>` : ''}<span style="margin-left:auto;font-size:.76rem;color:var(--muted)">${r.latency_ms} ms</span></div>
      ${last && (r.follow_ups || []).length ? `<div class="fups">${r.follow_ups.map(f => `<button class="fup" data-lq="${esc(f)}">${icon('arrow')}${esc(f)}</button>`).join('')}</div>` : ''}</div>`;
  }).join('');
  const x = $('#lxl'); if (x) x.onclick = () => download('/api/data/export?kind=result', 'analysis_result.xlsx');
  $$('[data-lq]', th).forEach(b => b.onclick = () => labAsk(b.dataset.lq));
  drawCharts(th); hydrateIcons(th);
}
async function labAsk(q) {
  if (L.busy) return;
  L.busy = true; L.messages.push({ q, pending: true }); renderLabThread();
  const hist = L.messages.filter(m => !m.pending && m.res && !m.res.error).slice(-5).flatMap(m => [{ role: 'user', text: m.q }, { role: 'assistant', text: m.res.explanation?.summary || '' }]);
  try { const res = await api('/api/ask', { method: 'POST', body: JSON.stringify({ question: q, history: hist }) }); state.used = res.questions_used ?? state.used; L.messages.pop(); L.messages.push({ q, res }); }
  catch (e) { L.messages.pop(); L.messages.push({ q, res: { error: e.message } }); }
  L.busy = false; updateCount(); renderLabThread();
}

// ------------------------------------------------------------------ PAGE: dashboard
const PERIODS = [['30d', 'Last 30 days'], ['90d', 'Last 90 days'], ['12m', 'Last 12 months'], ['all', 'All time']];
async function pageDashboard(view, token) {
  view.innerHTML = skeleton();
  const period = store.get('as_period', '90d');
  const d = await api('/api/dashboard?period=' + period); if (stale(token)) return;
  view.innerHTML = `<div class="page"><div class="toolbar"><div class="seg" id="per">${PERIODS.map(([k, l]) => `<button data-k="${k}" class="${k === period ? 'on' : ''}">${l}</button>`).join('')}</div>
    <span class="chip">${esc(d.label)}${d.compared ? ' vs previous period' : ''}</span><span class="chip">Data to ${esc(d.as_of)}</span></div>
    ${kpiRow(d.kpis, 'g' + Math.min(d.kpis.length, 4))}<div style="height:16px"></div>${chartsGrid(d.charts)}${callouts(d.insights, d.actions)}
    <div style="margin-top:14px">${askLink('Give me an overview of performance and what changed recently.', 'Ask AskShelf what changed')}</div></div>`;
  $('#per').onclick = e => { const b = e.target.closest('button'); if (b) { store.setS('as_period', b.dataset.k); route(); } };
}

// ------------------------------------------------------------------ PAGE: alerts
async function pageAlerts(view, token) {
  view.innerHTML = skeleton(2);
  const d = await api('/api/alerts'); if (stale(token)) return;
  state.alertCount = d.alerts.filter(a => a.severity !== 'info').length; renderNav();
  const n = s => d.alerts.filter(a => a.severity === s).length, icn = { critical: 'warn', warning: 'bell', info: 'spark' }, filter = view._f || 'all';
  const shown = d.alerts.filter(a => filter === 'all' || a.severity === filter), canExp = state.meta.roles[state.role].can_export;
  view.innerHTML = `<div class="page"><div class="grid g4" style="margin-bottom:16px">${kpiHTML({ label: 'Critical', value: n('critical') }, 0)}${kpiHTML({ label: 'Warnings', value: n('warning') }, 1)}${kpiHTML({ label: 'For information', value: n('info') }, 2)}${kpiHTML({ label: 'Rules active', value: d.rules.filter(r => r.active).length + ' of ' + d.rules.length }, 3)}</div>
  <div class="toolbar"><div class="seg" id="af">${['all', 'critical', 'warning', 'info'].map(f => `<button data-f="${f}" class="${f === filter ? 'on' : ''}">${f[0].toUpperCase() + f.slice(1)}</button>`).join('')}</div>
    <span class="sp"></span>${canExp ? `<button class="btn btn-ghost btn-sm" id="dg">${icon('download')} Download daily digest</button>` : ''}</div>
  ${shown.length ? shown.map((a, i) => `<div class="alert ${a.severity}" style="--i:${i}"><div class="ai">${icon(icn[a.severity])}</div><div style="flex:1;min-width:0">
    <div class="chips"><span class="chip ${a.severity === 'critical' ? 'red' : a.severity === 'warning' ? 'amber' : 'blue'}">${a.severity.toUpperCase()}</span><span class="chip">${esc(a.category)}</span></div>
    <h4>${esc(a.headline)}</h4><div class="ev">${esc(a.evidence)}</div><div class="rec">Recommended: ${esc(a.action)}</div>
    ${roleCan('ask') ? `<div style="margin-top:.4rem">${askLink(a.ask, 'Investigate with Ask')}</div>` : ''}</div></div>`).join('') : '<div class="empty">Nothing at this level for your role.</div>'}
  <details class="src" style="margin-top:18px"><summary>${icon('file')} How alerts are decided</summary><div class="in"><div class="tablewrap"><table class="t"><thead><tr><th>Rule</th><th>Fires when</th><th>Status</th></tr></thead><tbody>${d.rules.map(r => `<tr><td><b>${esc(r.rule)}</b></td><td>${esc(r.fires)}</td><td><span class="chip ${r.active ? 'green' : ''}">${r.active ? 'Active' : 'Needs more columns'}</span></td></tr>`).join('')}</tbody></table></div></div></details></div>`;
  $('#af').onclick = e => { const b = e.target.closest('button'); if (b) { view._f = b.dataset.f; pageAlerts(view, token); } };
  const dg = $('#dg'); if (dg) dg.onclick = () => download('/api/alerts/digest', 'askshelf_daily_digest.html');
  hydrateIcons(view);
}

// ------------------------------------------------------------------ PAGE: deliveries
async function pageDeliveries(view, token) {
  view.innerHTML = skeleton();
  const r = await api('/api/deliveries'); if (stale(token)) return;
  const m = r.model;
  view.innerHTML = `<div class="page">${kpiRow(r.kpis, 'g' + Math.min(r.kpis.length, 5))}<div style="height:16px"></div>
    ${m ? `<div class="card" style="margin-bottom:16px"><div class="verdict"><span class="vi v-${m.verdict === 'Strong' ? 'Strong' : 'Uncertain'}">${m.verdict} model</span><div><h3 style="margin:0">Late-delivery risk model</h3>
      <div style="color:var(--ink2);font-size:.9rem;margin-top:.2rem">Trained on <b>${m.n.toLocaleString()}</b> of your delivered orders (${m.base}% were late). Cross-validated accuracy: AUC <b>${m.auc}</b> (0.50 = coin flip, 1.00 = perfect).</div>
      <div class="chips" style="margin-top:.5rem">${m.drivers.slice(0, 5).map(x => `<span class="chip ${x.effect > 0 ? 'red' : 'green'}">${esc(x.feature)} ${x.effect > 0 ? 'raises' : 'lowers'} risk</span>`).join('')}</div></div></div></div>` : ''}
    ${chartsGrid(r.charts)}${callouts(r.insights, r.actions)}${notesHTML(r.notes)}
    ${r.open_table ? `<div class="card" style="margin-top:16px"><h3>Open orders</h3><div class="sub">Orders with a due date and no delivered date${m ? ', ranked by predicted late risk' : ''}.</div>${tableHTML(r.open_table, 'open_orders')}</div>` : ''}
    <div style="margin-top:14px">${roleCan('penalties') ? `<a class="linkbtn" href="#/penalties">${icon('flag')} Open the penalty recovery plan</a>` : ''}</div></div>`;
}

// ------------------------------------------------------------------ PAGE: penalties
async function pagePenalties(view, token) {
  const v = view._pc || (view._pc = { expedite: 5, rate: 3 });
  const first = await api('/api/penalties', { method: 'POST', body: JSON.stringify({ expedite: v.expedite, rate: v.rate }) }); if (stale(token)) return;
  view.innerHTML = `<div class="page"><div class="grid gside"><div class="card" style="align-self:start"><h3>Build a recovery plan</h3><div class="sub">Choose what you can do. The estimate updates as you move the sliders.</div>
    ${first.plan_ok ? slider('pc-ex', 'Expedite the top orders by exposure', 0, Math.max(5, Math.min(first.n_open, 40)), Math.min(v.expedite, Math.max(5, Math.min(first.n_open, 40)))) : '<div class="note caution" style="font-size:.82rem">The recovery plan needs a trained late-risk model.</div>'}
    ${first.mapped ? `<div class="note" style="font-size:.82rem">Penalty rate comes from your mapped penalty column.</div>` : slider('pc-rt', 'Assumed penalty (% of order value)', 0, 20, v.rate, '%') + '<div class="sub" style="font-size:.78rem">No penalty column is mapped, so cost is estimated at this rate for each late order.</div>'}
    <div style="margin-top:10px">${roleCan('ask') ? askLink('Which suppliers cause the most late deliveries?', 'Ask which suppliers cause delays') : ''}</div></div><div id="pres"></div></div></div>`;
  const paint = r => {
    $('#pres').innerHTML = `${kpiRow(r.kpis, 'g' + Math.min(r.kpis.length, 4))}<div style="height:16px"></div>${chartsGrid(r.charts)}${callouts(r.insights, r.actions)}${notesHTML(r.notes)}
      ${r.table ? `<div class="card" style="margin-top:16px"><h3>Open orders ranked by penalty exposure</h3><div class="sub">Orders marked Expedite are the ones in your plan.</div>${tableHTML(r.table, 'penalty_plan')}</div>` : ''}`;
    drawCharts($('#pres')); animateNumbers($('#pres')); hydrateIcons($('#pres'));
  };
  paint(first);
  let seq = 0;
  const run = async () => { const my = ++seq; try { const r = await api('/api/penalties', { method: 'POST', body: JSON.stringify(v) }); if (my === seq && !stale(token)) paint(r); } catch (e) { if (!stale(token)) $('#pres').innerHTML = `<div class="note bad">${esc(e.message)}</div>`; } };
  const ex = $('#pc-ex'), rt = $('#pc-rt');
  bindSliders(view, () => { if (ex) v.expedite = +ex.value; if (rt) v.rate = +rt.value; run(); });
}

// ------------------------------------------------------------------ PAGE: stock
async function pageStock(view, token) {
  const target = view._t || 30;
  view.innerHTML = skeleton();
  const r = await api('/api/stock?target=' + target); if (stale(token)) return;
  view.innerHTML = `<div class="page">${kpiRow(r.kpis, 'g' + Math.min(r.kpis.length, 5))}<div style="height:16px"></div>
    <div class="card" style="margin-bottom:16px;max-width:420px">${slider('stk', 'Target days of cover', 7, 90, target)}</div>${chartsGrid(r.charts)}${callouts(r.insights, r.actions)}${notesHTML(r.notes)}
    <div class="card" style="margin-top:16px"><h3>Stock position by product</h3><div class="sub">Worst first. Suggested order = sales rate x target days - stock on hand.</div>${tableHTML(r.table, 'stock_position')}</div>
    <div style="margin-top:14px">${roleCan('ask') ? askLink('Which products will run out of stock this week?', 'Ask which products run out this week') : ''}</div></div>`;
  bindSliders(view, () => { view._t = +$('#stk').value; pageStock(view, token); }, 350);
}

// ------------------------------------------------------------------ PAGE: forecasts
async function pageForecasts(view, token) {
  const q = view._q || (view._q = { metric: 'revenue', group: '', periods: 6, unit: '' });
  view.innerHTML = skeleton(2);
  const r = await api(`/api/forecast?metric=${q.metric}&group=${q.group}&periods=${q.periods}&unit=${q.unit}`); if (stale(token)) return;
  q.metric = r.metric;
  view.innerHTML = `<div class="page"><div class="toolbar"><div class="seg" id="fm">${Object.entries(r.metrics).map(([k, l]) => `<button data-k="${k}" class="${k === r.metric ? 'on' : ''}">${esc(l)}</button>`).join('')}</div>
    <select class="sel" id="fg" style="width:auto"><option value="">All products combined</option>${r.dims.map(d => `<option value="${d}" ${d === q.group ? 'selected' : ''}>Split by ${d}</option>`).join('')}</select>
    <select class="sel" id="fu" style="width:auto">${[['', 'Auto period'], ['day', 'Daily'], ['week', 'Weekly'], ['month', 'Monthly'], ['quarter', 'Quarterly']].map(([k, l]) => `<option value="${k}" ${k === q.unit ? 'selected' : ''}>${l}</option>`).join('')}</select>
    <div class="field" style="margin:0;min-width:200px">${slider('fh', 'Periods ahead', 1, 24, q.periods)}</div></div>
    ${kpiRow(r.kpis, 'g3')}<div style="height:16px"></div><div class="card">${chartBox(r.chart)}</div>
    ${r.group_chart ? `<div class="card" style="margin-top:16px">${chartBox(r.group_chart)}</div>` : ''}${callouts(r.insights)}${notesHTML(r.notes)}
    <div class="grid g2" style="margin-top:16px"><div class="card"><h3>Forecast table</h3>${tableHTML(r.table, 'forecast')}</div>${r.group_table ? `<div class="card"><h3>Forecast by ${esc(r.group)}</h3>${tableHTML(r.group_table, 'forecast_group')}</div>` : ''}</div></div>`;
  $('#fm').onclick = e => { const b = e.target.closest('button'); if (b) { q.metric = b.dataset.k; pageForecasts(view, token); } };
  $('#fg').onchange = e => { q.group = e.target.value; pageForecasts(view, token); };
  $('#fu').onchange = e => { q.unit = e.target.value; pageForecasts(view, token); };
  bindSliders(view, () => { q.periods = +$('#fh').value; pageForecasts(view, token); }, 400);
}

// ------------------------------------------------------------------ PAGE: scenario planner
async function pagePlanner(view, token) {
  const v = view._v || (view._v = { price: 0, volume: 0, cost: 0, elasticity: 'none' });
  view.innerHTML = `<div class="page"><div class="grid gside"><div class="card" style="align-self:start"><h3>Scenario</h3><div class="sub">Change the levers and see the monthly effect.</div>
    ${slider('pl-p', 'Change prices', -30, 30, v.price, '%')}${slider('pl-v', 'Change volume (demand)', -50, 60, v.volume, '%')}${slider('pl-c', 'Change unit cost', -20, 40, v.cost, '%')}
    <div class="field"><label>How price-sensitive are customers?</label><div class="radio">${[['none', 'No reaction (volume unchanged)'], ['moderate', 'Moderate (elasticity -1)'], ['high', 'High (elasticity -2)']].map(([k, l]) => `<label><input type="radio" name="el" value="${k}" ${v.elasticity === k ? 'checked' : ''}> ${l}</label>`).join('')}</div></div>
    <button class="btn btn-ghost btn-sm" id="pl-reset">${icon('refresh')} Reset</button></div><div id="res"><div class="sk" style="height:420px"></div></div></div></div>`;
  hydrateIcons(view);
  let seq = 0;
  const run = async () => {
    const my = ++seq;
    try {
      const r = await api('/api/planner', { method: 'POST', body: JSON.stringify(v) }); if (my !== seq || stale(token)) return;
      $('#res').innerHTML = `${kpiRow(r.kpis, 'g' + Math.min(r.kpis.length, 5))}<div class="grid g2" style="margin-top:16px"><div class="card">${chartBox(r.chart)}</div><div class="card">${chartBox(r.heatmap)}</div></div>${callouts(r.insights)}${notesHTML(r.notes)}`;
      drawCharts($('#res')); animateNumbers($('#res'));
    } catch (e) { if (!stale(token)) $('#res').innerHTML = `<div class="note bad">${esc(e.message)}</div>`; }
  };
  bindSliders(view, () => { v.price = +$('#pl-p').value; v.volume = +$('#pl-v').value; v.cost = +$('#pl-c').value; run(); }, 200);
  $$('input[name=el]', view).forEach(r => r.onchange = () => { v.elasticity = r.value; run(); });
  $('#pl-reset').onclick = () => { view._v = null; pagePlanner(view, token); };
  run();
}

// ------------------------------------------------------------------ PAGE: reports
const REPORT_STEPS = ['Profile the data', 'Plan the report structure', 'Run the analyses and draft the narrative', 'Forecast the main measure', 'Write the executive summary and recommendations', 'Assemble the report', 'Typeset the PDF report', 'Build the Word document', 'Build the Excel workbook'];
async function pageReports(view, token) {
  if (!loaded()) { view.innerHTML = emptyState(); return; }
  const r = L.report;
  view.innerHTML = `<div class="page"><div class="grid gside"><div class="card" style="align-self:start"><h3>Generate a formal report</h3><div class="sub">A title page, table of contents, lists of figures and tables, numbered sections with captioned charts and tables, recommendations, references and appendices.</div>
    <div class="field"><label>Report title (optional)</label><input class="inp" id="r-title" placeholder="Data Analysis Report: ..." value="${esc(r?.meta?.title || '')}"></div>
    <div class="field"><label>Organisation</label><input class="inp" id="r-org" placeholder="Your company" value="${esc(r?.meta?.organisation || '')}"></div>
    <div class="field"><label>Prepared for</label><input class="inp" id="r-for" placeholder="e.g. Executive Team" value="${esc(r?.meta?.prepared_for || '')}"></div>
    <div class="field"><label>Prepared by</label><input class="inp" id="r-by" placeholder="AskShelf Data Lab" value="${esc(r?.meta?.prepared_by || '')}"></div>
    <button class="btn btn-primary" id="r-go" style="width:100%;justify-content:center" ${r?.status === 'running' ? 'disabled' : ''}>${icon('file')} ${r?.status === 'running' ? 'Generating...' : 'Generate report'}</button>
    <div class="sub" style="font-size:.78rem;margin-top:10px">Takes 10-60 seconds. AI-written narrative is checked against the data before it is included.</div></div>
    <div id="r-out">${!r ? `<div class="card empty"><div class="lab-art" style="margin:0 auto 10px">${icon('file')}</div><b>No report yet</b><div class="sub">Fill in the details and press Generate. You get PDF, Word and Excel versions.</div></div>` : ''}</div></div></div>`;
  hydrateIcons(view);
  $('#r-go').onclick = () => startReport(view, token);
  if (r) paintReport();
}
function paintReport() {
  const r = L.report, out = $('#r-out'); if (!out) return;
  if (r.status === 'running' || r.status === 'starting') {
    const done = r.steps.length, total = REPORT_STEPS.length;
    out.innerHTML = `<div class="card"><h3>Building your report</h3><div class="sub">${esc(r.steps[r.steps.length - 1] || 'Starting')}...</div><div class="progress"><i style="width:${Math.min(95, done / total * 100)}%"></i></div>
      <div class="rsteps">${REPORT_STEPS.map((s, i) => `<div class="rs ${i < done ? 'done' : i === done ? 'now' : ''}">${i < done ? icon('check') : '<span class="dot"></span>'}${esc(s)}</div>`).join('')}</div></div>`;
  } else if (r.status === 'error') out.innerHTML = `<div class="note bad">Report failed: ${esc(r.message || 'unknown error')}</div>`;
  else if (r.status === 'done') {
    const s = r.summary, canExp = state.meta.roles[state.role].can_export;
    out.innerHTML = `<div class="card" style="margin-bottom:14px"><div class="toolbar" style="margin-bottom:6px"><div><h3 style="margin:0">${esc(s.title)}</h3><div class="sub" style="margin:2px 0 0">${s.sections} sections &middot; ${s.figures} figures &middot; ${s.tables} tables &middot; ${esc(s.model)}</div></div><span class="sp"></span>
      ${canExp ? `<button class="btn btn-primary btn-sm" data-dl="pdf">${icon('download')} PDF</button><button class="btn btn-ghost btn-sm" data-dl="docx">${icon('download')} Word</button><button class="btn btn-ghost btn-sm" data-dl="xlsx">${icon('download')} Excel</button>` : '<span class="chip amber">Downloads need the Owner or Finance Lead role</span>'}</div>
      ${(s.notes || []).map(n => `<div class="note caution">${esc(n)}</div>`).join('')}<div class="sect">Key findings</div>${s.findings.map((f, i) => `<div class="note"><b>${i + 3}. ${esc(f.title)}.</b> ${esc(f.finding)}</div>`).join('')}</div>
      <div class="card flush"><div class="toolbar" style="padding:12px 16px 0"><h3 style="margin:0">Preview</h3></div>${L.pdfUrl ? `<iframe class="pdfframe" src="${L.pdfUrl}#view=FitH" title="Report preview"></iframe>` : `<div class="empty">${canExp ? 'Loading preview...' : 'Preview needs the Owner or Finance Lead role.'}</div>`}</div>`;
    $$('[data-dl]', out).forEach(b => b.onclick = () => download(`/api/report/download?id=${r.id}&fmt=${b.dataset.dl}`, `askshelf_report.${b.dataset.dl}`));
    hydrateIcons(out);
  }
}
async function startReport(view, token) {
  const meta = { title: $('#r-title').value, organisation: $('#r-org').value, prepared_for: $('#r-for').value, prepared_by: $('#r-by').value };
  L.report = { status: 'starting', steps: [], meta }; L.pdfUrl = null; pageReports(view, token);
  try {
    const { id } = await api('/api/report', { method: 'POST', body: JSON.stringify(meta) });
    L.report.id = id; L.report.status = 'running';
    while (true) {
      await sleep(900);
      const st = await api('/api/report/status?id=' + id);
      L.report = { ...L.report, ...st, id, meta };
      if (currentRoute === 'reports') paintReport();
      if (st.status !== 'running') break;
    }
    if (L.report.status === 'done' && state.meta.roles[state.role].can_export) {
      const res = await fetch(`/api/report/download?id=${id}&fmt=pdf`, { headers: { 'X-Role': state.role, 'X-Session': state.session } });
      L.pdfUrl = URL.createObjectURL(await res.blob()); toast('Report ready');
    }
    if (currentRoute === 'reports') pageReports($('#view'), route.token);
  } catch (e) { L.report = { status: 'error', steps: [], message: e.message, meta }; if (currentRoute === 'reports') pageReports($('#view'), route.token); }
}

// ------------------------------------------------------------------ PAGE: governance
async function pageGovernance(view, token) {
  view.innerHTML = skeleton(2);
  const d = await api('/api/governance'); if (stale(token)) return;
  const roles = state.meta.roles, c = d.chain, canExp = state.meta.roles[state.role].can_export;
  view.innerHTML = `<div class="page">
  <div class="grid g4" style="margin-bottom:16px">${kpiHTML({ label: 'Your role', value: state.role.split(' / ')[0] }, 0)}${kpiHTML({ label: 'Audit entries', value: c.entries }, 1)}${kpiHTML({ label: 'Answers rated helpful', value: d.feedback.up }, 2)}${kpiHTML({ label: 'Issues reported', value: d.feedback.reports }, 3)}</div>
  <div class="card" style="margin-bottom:16px"><h3>Roles</h3><div class="sub">Access is enforced on every request, not just in the menu. Switch role (top right) to try it.</div>
    <div class="grid g4">${Object.entries(roles).map(([n, r]) => `<div class="card" style="box-shadow:none;${n === state.role ? 'border-color:var(--primary);background:var(--primary-l)' : ''}"><b>${esc(n)}</b><div class="sub" style="margin:.3rem 0 .5rem;font-size:.82rem">${esc(r.blurb)}</div><span class="chip ${r.can_upload ? 'green' : ''}">${r.can_upload ? 'Can load data' : 'No data loading'}</span> <span class="chip ${r.can_export ? 'green' : ''}">${r.can_export ? 'Can export' : 'No export'}</span></div>`).join('')}</div></div>
  <div class="card" style="margin-bottom:16px"><h3>Access matrix</h3><div class="sub">Which roles can open which pages.</div>
    <div class="tablewrap"><table class="t"><thead><tr>${d.matrix.columns.map(x => `<th>${esc(x)}</th>`).join('')}</tr></thead><tbody>${d.matrix.data.map(r => `<tr>${r.map(x => `<td class="${x === 'Allowed' ? 'ok' : x === 'Blocked' ? 'no' : ''}">${esc(x)}</td>`).join('')}</tr>`).join('')}</tbody></table></div></div>
  <div class="card" style="margin-bottom:16px"><div class="toolbar" style="margin-bottom:8px"><div><h3>Audit trail</h3><div class="sub" style="margin:0">Every request is recorded with its outcome. Entries are hash-chained, so any edit is detectable.</div></div><span class="sp"></span>
    <span class="chip ${c.intact ? 'green' : 'red'}">${icon(c.intact ? 'check' : 'warn')} Chain ${c.intact ? 'intact' : 'broken at entry ' + c.bad_line} &middot; ${c.entries} entries</span>${canExp ? `<button class="btn btn-ghost btn-sm" id="ad">${icon('download')} Download</button>` : ''}</div>
    ${d.audit.length ? `<div class="tablewrap"><table class="t"><thead><tr><th>Time</th><th>Role</th><th>Request</th><th>Outcome</th><th>Mode</th><th>ms</th><th>Hash</th></tr></thead><tbody>${d.audit.map(r => `<tr><td>${esc(r.time)}</td><td>${esc(r.role)}</td><td>${esc(r.question)}</td><td><span class="chip ${r.outcome === 'answered' ? 'green' : 'amber'}">${esc(r.outcome)}</span></td><td>${esc(r.mode)}</td><td>${r.latency_ms}</td><td><code>${esc(r.hash)}</code></td></tr>`).join('')}</tbody></table></div>` : '<div class="empty">No entries yet.</div>'}</div>
  <div class="card"><h3>Data privacy</h3><div class="sub">Removed when files are uploaded. Never analysed, shown, logged or sent to an AI service.</div><div class="chips">${d.restricted.map(x => `<span class="chip red">${icon('lock')}${esc(x)}</span>`).join('')}</div>
    <div class="note" style="margin-top:12px">Only aggregates are reported. Output is scanned and redacted for IDs, emails, phone numbers and keys, and questions are masked before they are written to the audit log. Uploaded data lives in server memory for your session only and is not written to disk.</div></div></div>`;
  const ad = $('#ad'); if (ad) ad.onclick = () => download('/api/audit/download', 'audit_log.jsonl');
  hydrateIcons(view);
}

// ------------------------------------------------------------------ PAGE: knowledge
async function pageKnowledge(view, token) {
  view.innerHTML = skeleton(2);
  const d = await api('/api/glossary'); if (stale(token)) return;
  view.innerHTML = `<div class="page"><div class="grid g2"><div class="card"><h3>Definitions</h3><div class="sub">Plain-English meanings for the terms used across the app.</div>
    ${d.definitions.map(x => `<p style="margin:.6rem 0"><b>${esc(x.term)}</b><br><span style="color:var(--ink2);font-size:.9rem">${esc(x.meaning)}</span></p>`).join('')}</div>
    <div class="card"><h3>What data to upload</h3><div class="sub">The more columns you map, the more pages unlock.</div>${d.files.map(x => `<p style="margin:.6rem 0"><span class="chip">${icon('db')}${esc(x.file)}</span><br><span style="color:var(--ink2);font-size:.9rem">${esc(x.about)}</span></p>`).join('')}</div></div></div>`;
  hydrateIcons(view);
}

// ------------------------------------------------------------------ boot
async function boot() {
  $('#lg').innerHTML = LOGO; hydrateIcons();
  try { state.meta = await api('/api/meta'); } catch (e) { $('#view').innerHTML = `<div class="restricted"><h2>Cannot reach the server</h2><p>${esc(e.message)}</p></div>`; return; }
  const saved = store.get('as_role'); state.role = state.meta.roles[saved] ? saved : state.meta.default_role;
  const sel = $('#role'); sel.innerHTML = Object.keys(state.meta.roles).map(r => `<option ${r === state.role ? 'selected' : ''}>${esc(r)}</option>`).join('');
  sel.onchange = () => { state.role = sel.value; store.setL('as_role', state.role); store.setS('as_role', state.role); toast(`Now acting as ${state.role}`); refreshAlertCount(); route(); };
  $('#menu').onclick = () => $('#side').classList.toggle('open'); $('#scrim').onclick = () => $('#side').classList.remove('open');
  addEventListener('hashchange', route); addEventListener('resize', () => drawCharts());
  await refreshDataset(); route();
}
boot();
