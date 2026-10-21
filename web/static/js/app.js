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
const state = { meta: null, role: null, session: new URLSearchParams(location.search).get('session') || store.get('as_session') || (crypto.randomUUID ? crypto.randomUUID().replace(/-/g, '') : String(Math.random()).slice(2)), messages: [], used: 0, busy: false, alertCount: 0 };
store.setS('as_session', state.session);

async function api(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: { 'Content-Type': 'application/json', 'X-Role': state.role || '', 'X-Session': state.session, ...(opts.headers || {}) } });
  if (res.status === 403) { const j = await res.json().catch(() => ({})); throw Object.assign(new Error(j.message || 'Not permitted'), { forbidden: true }); }
  if (!res.ok) { const j = await res.json().catch(() => ({})); throw Object.assign(new Error(j.message || `Request failed (${res.status})`), { status: res.status }); }
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
    growBars(el, f, layout);
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
  let cls = 'flat'; if (k.delta) { const up = String(k.delta).trim().startsWith('+'); cls = up ? 'up' : 'down'; if (k.good === 'down') cls = up ? 'down' : 'up'; }
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
document.addEventListener('click', e => { const b = e.target.closest('[data-ask]'); if (b) { location.hash = '#/ask'; sendQuestion(b.dataset.ask); } });

// ------------------------------------------------------------------ router
const ROUTES = [
  { id: 'ask', title: 'Ask AskShelf', sub: 'Plain-English questions, answers you can trace.', icon: 'chat', gate: null, render: pageAsk },
  { id: 'dashboard', title: 'Executive dashboard', sub: 'Health, money, sales, operations and early warnings.', icon: 'dashboard', gate: 'business_summary', render: pageDashboard },
  { id: 'alerts', title: 'Alerts centre', sub: 'What needs attention, before you ask.', icon: 'bell', gate: null, render: pageAlerts },
  { id: 'schedule', title: 'Delivery schedule', sub: 'What is due, what is at risk and what it could cost.', icon: 'clock', gate: 'delivery_schedule', render: pageSchedule },
  { id: 'stock', title: 'Stock availability', sub: 'Days of cover and shortfalls against open orders.', icon: 'layers', gate: 'stock_cover', render: pageStock },
  { id: 'penalties', title: 'Penalty control', sub: 'What late deliveries cost, and what expediting would save.', icon: 'flag', gate: 'penalty_control', render: pagePenalties },
  { id: 'forecasts', title: 'Forecasts and risk', sub: 'Predictions with the accuracy shown, not hidden.', icon: 'trend', gate: 'forecast_outlook', render: pageForecasts },
  { id: 'planner', title: 'Scenario planner', sub: 'Move the levers and watch profit and delivery respond.', icon: 'sliders', gate: 'scenario_simulator', render: pagePlanner },
  { id: 'lab', title: 'Data Lab', sub: 'Upload your own files, ask questions, forecast and generate formal reports.', icon: 'upload', gate: null, labGate: true, render: pageLab },
  { id: 'governance', title: 'Governance', sub: 'Access control, audit trail and data privacy.', icon: 'shield', gate: null, render: pageGovernance },
  { id: 'glossary', title: 'Knowledge base', sub: 'What every metric means and which data is used.', icon: 'book', gate: null, render: pageGlossary },
];
let currentRoute = null;
function renderNav() {
  $('#nav').innerHTML = ROUTES.map(r => {
    const locked = (r.gate && !state.meta.roles[state.role].allowed.includes(r.gate)) || (r.labGate && !state.meta.roles[state.role].can_export);
    const badge = r.id === 'alerts' && state.alertCount ? `<span class="badge">${state.alertCount}</span>` : '';
    return `<a class="nav-i ${currentRoute === r.id ? 'on' : ''} ${locked ? 'locked' : ''}" href="#/${r.id}">${icon(r.icon)}<span>${r.title.replace(' AskShelf', '').replace('Executive dashboard', 'Dashboard').replace('Forecasts and risk', 'Forecasts').replace('Delivery schedule', 'Deliveries').replace('Stock availability', 'Stock').replace('Penalty control', 'Penalties')}</span>${badge}${locked ? `<span class="lk">${icon('lock')}</span>` : ''}</a>`;
  }).join('');
}
async function route() {
  const id = (location.hash.replace('#/', '') || 'ask').split('?')[0];
  const r = ROUTES.find(x => x.id === id) || ROUTES[0];
  currentRoute = r.id; renderNav(); $('#side').classList.remove('open');
  $('#ptitle').textContent = r.title; $('#psub').textContent = r.sub; document.title = `${r.title} | AskShelf`;
  const view = $('#view'); view.className = 'view' + (r.id === 'ask' ? ' chatview' : ''); view.scrollTop = 0;
  const token = (route.token = (route.token || 0) + 1);
  try {
    await r.render(view, token);
  } catch (e) {
    if (token !== route.token) return;
    view.innerHTML = e.forbidden ? restricted(e.message) : `<div class="restricted"><div class="big">${icon('warn')}</div><h2>Something went wrong</h2><p>${esc(e.message)}</p><button class="btn btn-primary" onclick="route()">Try again</button></div>`;
  }
  drawCharts(view); wireTabs(view); hydrateIcons(view); animateNumbers(view);
}
const stale = t => t !== route.token;

// ------------------------------------------------------------------ PAGE: ask
const THINK = ['Checking your access and the privacy rules', 'Choosing the right analysis', 'Running the models on your data', 'Writing a clear answer'];
function pageAsk(view) {
  view.innerHTML = `<div class="chat-scroll" id="cs"><div class="chat-col" id="thread"></div></div>
  <div class="composer"><div class="cbox"><textarea id="q" rows="1" maxlength="800" placeholder="Ask about sales, production, profit, people, risk, forecasts or 'what if...'" aria-label="Your question"></textarea>
  <button class="sendbtn" id="send" aria-label="Send">${icon('send')}</button></div>
  <div class="cmeta"><span>AskShelf reports aggregates only. Restricted personal details are never shown. <button class="linkbtn" id="newchat" style="margin-left:.6rem">${icon('refresh')} New chat</button></span><span id="qcount"></span></div></div>`;
  const q = $('#q'), send = $('#send');
  const submit = () => { const v = q.value.trim(); if (v && !state.busy) { q.value = ''; q.style.height = 'auto'; sendQuestion(v); } };
  send.onclick = submit;
  $('#newchat').onclick = () => { if (state.busy) return; state.messages = []; renderThread(); toast('Started a new conversation'); };
  q.onkeydown = e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit(); } };
  q.oninput = () => { q.style.height = 'auto'; q.style.height = Math.min(q.scrollHeight, 140) + 'px'; };
  renderThread(); updateCount(); q.focus();
}
function updateCount() { const c = $('#qcount'); if (c) c.textContent = `${state.used}/${state.meta.max_questions} questions this session`; }
function renderThread() {
  const th = $('#thread'); if (!th) return;
  if (!state.messages.length) {
    const items = Object.entries(state.meta.suggested).flatMap(([cat, qs]) => qs.map(q => [cat, q])).slice(0, 9);
    th.innerHTML = `<div class="welcome"><div class="mark">${LOGO}</div><h2>What would you like to know?</h2>
      <p>Ask in plain English. AskShelf picks the right analysis, runs it on your data and explains what it found.</p></div>
      <div class="sgrid">${items.map(([c, q], i) => `<button class="sug" style="--i:${i}" data-ask="${esc(q)}"><small>${icon('spark')}${esc(c)}</small><span>${esc(q)}</span></button>`).join('')}</div>`;
    return;
  }
  th.innerHTML = state.messages.map((m, i) => msgHTML(m, i)).join('');
  wireMessages(th); drawCharts(th); wireTabs(th); animateNumbers(th); scrollDown();
}
function scrollDown() { const cs = $('#cs'); if (cs) cs.scrollTop = cs.scrollHeight; }
function msgHTML(m, idx) {
  if (m.role === 'user') return `<div class="msg u"><div class="ub">${esc(m.text)}</div></div>`;
  if (m.pending) return `<div class="msg"><div class="av">${LOGO}</div><div class="abody"><div class="abubble"><div class="think"><span class="typing"><i></i><i></i><i></i></span><span id="thinktxt">${THINK[0]}...</span></div></div></div></div>`;
  const r = m.reply, last = idx === state.messages.length - 1;
  if (m.error) return `<div class="msg"><div class="av">${LOGO}</div><div class="abody"><div class="abubble"><div class="blocked">${icon('warn')}<div>${esc(m.error)}</div></div></div></div></div>`;
  const kp = r.results.find(x => x.kpis?.length);
  const charts = r.results.flatMap(x => x.charts);
  const tid = 't' + idx;
  const chartsHTML = !charts.length ? '' : charts.length === 1 ? `<div class="achart">${chartBox(charts[0].fig)}</div>`
    : `${tabsHTML(tid, charts.map(c => c.title))}<div class="achart">${charts.map((c, i) => `<div ${paneAttrs(tid, i)}>${chartBox(c.fig)}</div>`).join('')}</div>`;
  const src = r.results.map(x => `<div class="sect" style="margin-top:.4rem">${esc(x.title)} &middot; ${esc(x.period)}</div>
      <div class="chips" style="margin:.3rem 0">${(x.sources || []).map(s => `<span class="chip">${icon('db')}${esc(s)}</span>`).join('')}</div>
      ${x.confidence ? `<div class="note caution">Method confidence: ${esc(x.confidence)}</div>` : ''}${tableHTML(x.table, x.tool)}
      ${(x.extra_tables || []).map(t => `<div class="sect">${esc(t.caption)}</div>${tableHTML(t.table, x.tool + '_detail')}`).join('')}
      ${(x.notes || []).map(n => `<div class="sub" style="font-size:.8rem;color:var(--muted);margin-top:.3rem">Note: ${esc(n)}</div>`).join('')}`).join('');
  const trace = r.trace.map(s => `<div><b>${esc(s.step)}</b><span>${esc(s.detail)}</span><em>${s.ms} ms</em></div>`).join('');
  const mode = r.mode.startsWith('gemini') ? ['blue', 'Generative AI'] : r.mode === 'offline' ? ['amber', 'Built-in mode'] : ['', r.mode];
  return `<div class="msg"><div class="av">${LOGO}</div><div class="abody"><div class="ahead">AskShelf <span class="chip ${mode[0]}">${esc(mode[1])}</span></div>
  <div class="abubble">${r.notice ? `<div class="note caution">${esc(r.notice)}</div>` : ''}
  ${r.blocked ? `<div class="blocked">${icon('lock')}<div>${esc(r.text)}</div></div>` : `
    ${kp ? `<div class="kstrip">${kp.kpis.map(kpiHTML).join('')}</div>` : ''}<div class="md">${md(r.text)}</div>${chartsHTML}
    ${(r.results.length || r.trace.length) ? `<details class="src"><summary>${icon('file')} Data behind this answer and how it was made</summary><div class="in">${src}
      <div class="sect">Steps</div><div class="trace">${trace}</div>
      ${r.critic ? `<div class="sub" style="font-size:.82rem;color:var(--muted)">Self-review: <b>${esc(r.critic.verdict)}</b>${r.critic.issues?.length ? ' - ' + esc(r.critic.issues.join('; ')) : ''}</div>` : ''}
      <div class="chips" style="margin-top:.6rem"><span class="chip">${icon('users')}${esc(r.role)}</span><span class="chip">${icon('clock')}${r.latency_ms} ms</span><span class="chip">$${r.est_cost_usd.toFixed(4)} est. cost</span></div></div></details>` : ''}`}
  </div>
  ${r.blocked ? '' : `<div class="acts"><button class="act ${m.rating === 'up' ? 'on' : ''}" data-rate="up" data-i="${idx}" aria-label="Helpful">${icon('up')}</button>
    <button class="act ${m.rating === 'down' ? 'on' : ''}" data-rate="down" data-i="${idx}" aria-label="Not helpful">${icon('down')}</button>
    <button class="act" data-copy="${idx}">${icon('copy')} Copy</button><button class="act" data-report="${idx}">${icon('flag')} Report or teach a term</button></div>`}
  ${last && r.follow_ups?.length ? `<div class="fups">${r.follow_ups.map(f => `<button class="fup" data-ask="${esc(f)}">${icon('arrow')}${esc(f)}</button>`).join('')}</div>` : ''}
  </div></div>`;
}
function wireMessages(root) {
  $$('[data-rate]', root).forEach(b => b.onclick = async () => {
    const m = state.messages[+b.dataset.i]; if (m.rating) return; m.rating = b.dataset.rate;
    api('/api/feedback', { method: 'POST', body: JSON.stringify({ kind: 'rating', rating: m.rating, question: m.question, tools: m.reply.results.map(r => r.tool).join(','), mode: m.reply.mode }) }).catch(() => {});
    renderThread(); toast('Thanks for the feedback');
  });
  $$('[data-copy]', root).forEach(b => b.onclick = () => { navigator.clipboard?.writeText(state.messages[+b.dataset.copy].reply.text); toast('Answer copied'); });
  $$('[data-report]', root).forEach(b => b.onclick = () => {
    const m = state.messages[+b.dataset.report];
    modal(`<h3>Report a problem or teach a term</h3><p>Your note goes to the team's review log. Please do not include personal information.</p>
      <div class="field"><label>What was the issue?</label><select class="sel" id="fb-cat"><option>Numbers look wrong</option><option>Answer unclear</option><option>Missing information</option><option>Term or definition misunderstood</option><option>Sensitive data concern</option><option>Other</option></select></div>
      <div class="field"><label>Details</label><textarea class="inp" id="fb-txt" rows="4" maxlength="500" placeholder="e.g. 'lead time should exclude weekends'"></textarea></div>
      <div class="mrow"><button class="btn btn-ghost" id="fb-x">Cancel</button><button class="btn btn-primary" id="fb-ok">Send feedback</button></div>`, () => {
      $('#fb-x').onclick = closeModal;
      $('#fb-ok').onclick = async () => {
        const cat = $('#fb-cat').value;
        await api('/api/feedback', { method: 'POST', body: JSON.stringify({ kind: cat.startsWith('Term') ? 'definition' : 'issue', category: cat, comment: $('#fb-txt').value, question: m.question, tools: m.reply.results.map(r => r.tool).join(','), mode: m.reply.mode }) }).catch(() => {});
        closeModal(); toast('Thanks - logged for the team to review');
      };
    });
  });
}
async function sendQuestion(text) {
  if (state.busy) return;
  if (currentRoute !== 'ask') { location.hash = '#/ask'; await sleep(60); }
  const history = state.messages.filter(m => !m.pending && !m.error).slice(-8).map(m => ({ role: m.role, text: m.role === 'user' ? m.text : m.reply.text }));
  state.messages.push({ role: 'user', text }, { role: 'assistant', pending: true });
  state.busy = true; renderThread(); $('#send') && ($('#send').disabled = true);
  let k = 0; const tick = setInterval(() => { const t = $('#thinktxt'); if (t) t.textContent = THINK[Math.min(++k, THINK.length - 1)] + '...'; }, 1400);
  try {
    const reply = await api('/api/ask', { method: 'POST', body: JSON.stringify({ question: text, history }) });
    state.used = reply.questions_used; state.messages.pop();
    state.messages.push({ role: 'assistant', reply, question: text });
  } catch (e) {
    state.messages.pop(); state.messages.push({ role: 'assistant', error: e.message });
  } finally {
    clearInterval(tick); state.busy = false; renderThread(); updateCount(); const s = $('#send'); if (s) s.disabled = false; refreshAlertCount();
  }
}

// ------------------------------------------------------------------ PAGE: dashboard
const PERIODS = [['last_quarter', 'Last quarter'], ['last_12_months', 'Last 12 months'], ['2026', '2026'], ['2025', '2025'], ['all', 'All time']];
async function pageDashboard(view, token) {
  view.innerHTML = skeleton();
  const period = store.get('as_period', 'last_quarter');
  const d = await api('/api/dashboard?period=' + period); if (stale(token)) return;
  const col = v => v >= 70 ? '#059669' : v >= 50 ? '#d97706' : '#dc2626';
  view.innerHTML = `<div class="page">
  <div class="toolbar"><div class="seg" id="per">${PERIODS.map(([k, l]) => `<button data-k="${k}" class="${k === period ? 'on' : ''}">${l}</button>`).join('')}</div>
    <span class="chip">Showing ${esc(d.period)}${d.compared ? ' vs previous period' : ''}</span>
    <span class="sp"></span><button class="btn btn-ghost btn-sm" id="rep">${icon('download')} Executive report</button></div>
  <div class="grid g23" style="margin-bottom:16px"><div class="card"><div id="gauge" class="chart" style="min-height:230px"></div></div>
    <div class="card"><h3>What is driving the score</h3><div class="sub">Five components, each scaled from poor to good.</div>
      ${Object.entries(d.components).map(([k, v], i) => `<div class="comp-row"><span class="l">${esc(k)}</span><span class="trk"><span class="fill" style="width:${Math.max(v, 3)}%;background:${col(v)};animation-delay:${i * 90}ms;display:block"></span></span><b>${Math.round(v)}</b></div>`).join('')}</div></div>
  <div style="margin-bottom:16px">${kpiRow(d.kpis, 'g6')}</div>
  <div class="seg" id="dt" style="margin-bottom:12px"><button class="on" data-i="0">Money</button><button data-i="1">Sales</button><button data-i="2">Operations</button><button data-i="3">Early warnings</button></div>
  <div data-pane="dt" data-i="0"><div class="grid g2"><div class="card">${chartBox(d.money.bridge.charts[0]?.fig)}</div><div class="card">${chartBox(d.money.trend.charts[0]?.fig)}</div></div>${callouts(d.money.bridge.insights.slice(0, 2))}</div>
  <div data-pane="dt" data-i="1" hidden><div class="grid g2"><div class="card">${chartBox(d.sales.funnel.charts[0]?.fig)}</div><div class="card">${chartBox(d.sales.forecast.charts[0]?.fig)}</div></div>${callouts([...d.sales.funnel.insights.slice(0, 1), ...d.sales.forecast.insights.slice(0, 2)])}</div>
  <div data-pane="dt" data-i="2" hidden><div class="grid g2"><div class="card">${chartBox(d.ops.charts[0]?.fig)}</div><div class="card">${chartBox(d.ops.charts[1]?.fig)}</div></div>${callouts(d.ops.insights.slice(0, 2), d.ops.actions.slice(0, 2))}</div>
  <div data-pane="dt" data-i="3" hidden><div class="card">${chartBox(d.warnings.charts[0]?.fig)}</div>${callouts(d.warnings.insights, d.warnings.actions)}<div class="card" style="margin-top:12px">${tableHTML(d.warnings.table, 'anomalies')}</div></div>
  <div style="margin-top:16px">${askLink(`Give me a full executive briefing for ${PERIODS.find(p => p[0] === period)[1].toLowerCase()}.`, 'Ask AskShelf for a full briefing')}</div></div>`;
  $('#per').onclick = e => { const b = e.target.closest('button'); if (b) { store.setS('as_period', b.dataset.k); route(); } };
  $('#rep').onclick = () => download('/api/report?period=' + period, 'askshelf_executive_briefing.html');
  $('#dt').onclick = e => { const b = e.target.closest('button'); if (!b) return; $$('#dt button').forEach(x => x.classList.toggle('on', x === b)); $$('[data-pane=dt]').forEach(p => p.hidden = p.dataset.i !== b.dataset.i); drawCharts(view); };
  Plotly.newPlot('gauge', [{ type: 'indicator', mode: 'gauge+number', value: d.score, number: { suffix: '/100', font: { size: 44, family: 'Inter', color: '#0f172a' } },
    title: { text: `Business health: <b>${d.label}</b>`, font: { size: 14, family: 'Inter', color: '#64748b' } },
    gauge: { axis: { range: [0, 100], tickcolor: '#94a3b8' }, bar: { color: col(d.score), thickness: .28 }, borderwidth: 0, steps: [{ range: [0, 50], color: '#fdecec' }, { range: [50, 70], color: '#fff6e5' }, { range: [70, 100], color: '#e7f7f0' }] } }],
    { height: 230, margin: { l: 44, r: 44, t: 50, b: 8 }, paper_bgcolor: 'rgba(0,0,0,0)' }, { displayModeBar: false, responsive: true });
}

// ------------------------------------------------------------------ PAGE: alerts
async function pageAlerts(view, token) {
  view.innerHTML = skeleton(2);
  const d = await api('/api/alerts'); if (stale(token)) return;
  state.alertCount = d.alerts.filter(a => a.severity !== 'info').length; renderNav();
  const n = s => d.alerts.filter(a => a.severity === s).length;
  const icn = { critical: 'warn', warning: 'bell', info: 'spark' };
  const filter = view._f || 'all';
  const shown = d.alerts.filter(a => filter === 'all' || a.severity === filter);
  view.innerHTML = `<div class="page"><div class="grid g4" style="margin-bottom:16px">${kpiHTML({ label: 'Critical', value: n('critical') }, 0)}${kpiHTML({ label: 'Warnings', value: n('warning') }, 1)}${kpiHTML({ label: 'For information', value: n('info') }, 2)}${kpiHTML({ label: 'Rules monitored', value: d.rules.length }, 3)}</div>
  <div class="toolbar"><div class="seg" id="af">${['all', 'critical', 'warning', 'info'].map(f => `<button data-f="${f}" class="${f === filter ? 'on' : ''}">${f[0].toUpperCase() + f.slice(1)}</button>`).join('')}</div>
    <span class="sp"></span><button class="btn btn-ghost btn-sm" id="dg">${icon('download')} Download daily digest</button></div>
  ${shown.length ? shown.map((a, i) => `<div class="alert ${a.severity}" style="--i:${i}"><div class="ai">${icon(icn[a.severity])}</div><div style="flex:1;min-width:0">
    <div class="chips"><span class="chip ${a.severity === 'critical' ? 'red' : a.severity === 'warning' ? 'amber' : 'blue'}">${a.severity.toUpperCase()}</span><span class="chip">${esc(a.category)}</span></div>
    <h4>${esc(a.headline)}</h4><div class="ev">${esc(a.evidence)}</div><div class="rec">Recommended: ${esc(a.action)}</div>
    <div style="margin-top:.4rem">${askLink(a.ask, 'Investigate with AskShelf')}</div></div></div>`).join('') : '<div class="empty">Nothing at this level for your role.</div>'}
  <details class="src" style="margin-top:18px"><summary>${icon('file')} How alerts are decided</summary><div class="in"><div class="tablewrap"><table class="t"><thead><tr><th>Rule</th><th>Fires when</th></tr></thead><tbody>${d.rules.map(r => `<tr><td><b>${esc(r.rule)}</b></td><td>${esc(r.fires)}</td></tr>`).join('')}</tbody></table></div></div></details></div>`;
  $('#af').onclick = e => { const b = e.target.closest('button'); if (b) { view._f = b.dataset.f; pageAlerts(view, token); } };
  $('#dg').onclick = () => download('/api/digest', 'askshelf_daily_digest.html');
  hydrateIcons(view);
}

// ------------------------------------------------------------------ PAGE: delivery schedule
async function pageSchedule(view, token) {
  view.innerHTML = skeleton(2);
  const r = await api('/api/schedule'); if (stale(token)) return;
  view.innerHTML = `<div class="page">${kpiRow(r.summary)}
    <div class="card" style="margin-top:16px">${chartBox(r.charts[0]?.fig)}</div>
    <div class="grid g2" style="margin-top:16px"><div class="card">${chartBox(r.charts[1]?.fig)}</div><div>${callouts(r.insights, r.actions)}</div></div>
    <div class="card" style="margin-top:16px"><h3>Open orders by due date</h3><div class="sub">Sorted soonest first. Risk comes from the cross-validated late-delivery model.</div>${tableHTML(r.table, 'delivery_schedule')}</div>
    <div style="margin-top:14px">${askLink('What is due for delivery this week?', 'Ask AskShelf what is due this week')}</div></div>`;
}

// ------------------------------------------------------------------ PAGE: stock availability
async function pageStock(view, token) {
  view.innerHTML = skeleton(2);
  const r = await api('/api/supply'); if (stale(token)) return;
  const t = r.table, ix = n => t.columns.indexOf(n);
  const chip = s => `<span class="chip ${s === 'Healthy' ? 'green' : s === 'Tight' ? 'amber' : 'red'}">${s}</span>`;
  view.innerHTML = `<div class="page"><div class="grid g3">${t.data.map((row, i) => `<div class="kpi" style="--i:${i}"><small>${esc(row[ix('Material')])} ${chip(row[ix('Status')])}</small>
      <b>${row[ix('On hand')]} units</b><div class="d flat">${row[ix('Open-order demand')]} needed by open orders &middot; ${esc(row[ix('Days of cover')])} days of cover</div></div>`).join('')}</div>
    <div class="grid g2" style="margin-top:16px"><div class="card">${chartBox(r.charts[0]?.fig)}</div><div class="card">${chartBox(r.charts[1]?.fig)}</div></div>${callouts(r.insights, r.actions, r.notes)}
    <div class="card" style="margin-top:12px"><h3>Stock position by material</h3>${tableHTML(r.table, 'stock_cover')}</div>
    <div style="margin-top:14px">${askLink('Do we have enough timber for the open orders?', 'Ask AskShelf about stock')}</div></div>`;
}

// ------------------------------------------------------------------ PAGE: penalty control
async function pagePenalties(view, token) {
  const v = view._pc || (view._pc = { expedite: 5, makers: 0 });
  view.innerHTML = `<div class="page"><div class="grid gside"><div class="card" style="align-self:start"><h3>Build a recovery plan</h3><div class="sub">Pick what you can do. The estimate updates as you move the sliders.</div>
    <div class="field"><label>Expedite the top orders by exposure <output id="o-ex">${v.expedite}</output></label><input type="range" id="pc-ex" min="0" max="17" value="${v.expedite}"></div>
    <div class="field"><label>Add maker capacity <output id="o-mk">${v.makers}</output></label><input type="range" id="pc-mk" min="0" max="5" value="${v.makers}"></div>
    <div class="note caution" style="font-size:.8rem">Estimates are model-based: extra makers lower the orders-per-maker load in the late-risk model, and an expedited order is assumed to perform like a typical one.</div>
    <div style="margin-top:10px">${askLink('Which orders should we expedite first to cut penalties?', 'Ask AskShelf which to expedite')}</div></div>
    <div id="pres"><div class="sk" style="height:520px"></div></div></div></div>`;
  let timer = null, seq = 0;
  const run = async () => {
    const my = ++seq;
    try {
      const r = await api('/api/penalties', { method: 'POST', body: JSON.stringify(v) }); if (my !== seq || stale(token)) return;
      const rows = r.table.data.map(row => { const a = r.table.columns.indexOf('Action'); const c = row.slice(); c[a] = row[a] ? '\u2713 Expedite' : ''; return c; });
      $('#pres').innerHTML = `${kpiRow(r.kpis)}<div class="card" style="margin-top:16px">${chartBox(r.charts[0]?.fig)}</div>${callouts(r.insights, r.actions)}
        <div class="grid g2" style="margin-top:12px"><div class="card">${chartBox(r.charts[1]?.fig)}</div><div class="card">${chartBox(r.charts[2]?.fig)}</div></div>
        <div class="card" style="margin-top:16px"><h3>Open orders ranked by penalty exposure</h3><div class="sub">Orders marked Expedite are the ones in your plan.</div>${tableHTML({ columns: r.table.columns, data: rows }, 'penalty_plan')}</div>
        <details class="src"><summary>${icon('file')} How the estimate is made</summary><div class="in">${(r.notes || []).map(n => `<div class="note">${esc(n)}</div>`).join('')}<div class="note caution">${esc(r.confidence || '')}</div></div></details>`;
      drawCharts($('#pres')); animateNumbers($('#pres')); hydrateIcons($('#pres'));
    } catch (e) { if (!stale(token)) $('#pres').innerHTML = e.forbidden ? restricted(e.message) : `<div class="note bad">${esc(e.message)}</div>`; }
  };
  const later = () => { clearTimeout(timer); timer = setTimeout(run, 200); };
  $('#pc-ex').oninput = e => { v.expedite = +e.target.value; $('#o-ex').textContent = v.expedite; later(); };
  $('#pc-mk').oninput = e => { v.makers = +e.target.value; $('#o-mk').textContent = v.makers; later(); };
  hydrateIcons(view); run();
}

// ------------------------------------------------------------------ PAGE: forecasts
async function pageForecasts(view, token) {
  const tab = view._tab || 'risk';
  view.innerHTML = `<div class="page"><div class="seg" id="ft" style="margin-bottom:16px">${[['risk', 'Delivery-risk radar'], ['fc', 'Forecast studio'], ['price', 'Pricing lab'], ['card', 'Model report card']].map(([k, l]) => `<button data-k="${k}" class="${k === tab ? 'on' : ''}">${l}</button>`).join('')}</div><div id="fbody">${skeleton(2)}</div></div>`;
  $('#ft').onclick = e => { const b = e.target.closest('button'); if (b) { view._tab = b.dataset.k; pageForecasts(view, token); } };
  const body = $('#fbody');
  try {
    if (tab === 'risk') {
      const r = await api('/api/risk?n=' + (view._n || 10)); if (stale(token)) return;
      body.innerHTML = `${kpiRow(r.summary)}<div class="field" style="max-width:320px;margin:16px 0 0"><label>Orders to show <output>${view._n || 10}</output></label><input type="range" id="rn" min="5" max="17" value="${view._n || 10}"></div>
        <div class="grid g2" style="margin-top:8px"><div class="card">${chartBox(r.charts[0]?.fig)}</div><div class="card">${chartBox(r.charts[1]?.fig)}</div></div>${callouts(r.insights, r.actions)}
        <details class="src"><summary>${icon('file')} Order-level detail and what drives the risk</summary><div class="in">${tableHTML(r.table, 'late_risk')}${(r.extra_tables || []).map(t => `<div class="sect">${esc(t.caption)}</div>${tableHTML(t.table, 'risk_drivers')}`).join('')}</div></details>`;
      $('#rn').onchange = e => { view._n = +e.target.value; pageForecasts(view, token); };
      $('#rn').oninput = e => e.target.previousElementSibling.value = e.target.value;
    } else if (tab === 'fc') {
      const metric = view._m || 'revenue', mo = view._mo || 3;
      const r = await api(`/api/forecast?metric=${metric}&months=${mo}`); if (stale(token)) return;
      body.innerHTML = `<div class="toolbar"><div class="seg" id="fm">${Object.entries(r.metrics).map(([k, l]) => `<button data-k="${k}" class="${k === metric ? 'on' : ''}">${esc(l)}</button>`).join('')}</div>
        <div class="field" style="margin:0;min-width:200px"><label>Months ahead <output>${mo}</output></label><input type="range" id="fh" min="1" max="6" value="${mo}"></div></div>
        <div class="card">${chartBox(r.charts[0]?.fig)}</div><div class="grid g23" style="margin-top:16px"><div>${callouts(r.insights, r.actions)}</div><div class="card"><h3>Forecast table</h3>${tableHTML(r.table, 'forecast_' + metric)}</div></div>
        <details class="src"><summary>${icon('file')} How accurate has each method been? (rolling back-test)</summary><div class="in">${(r.extra_tables || []).map(t => `<div class="sect">${esc(t.caption)}</div>${tableHTML(t.table, 'backtest')}`).join('')}<div class="note caution">${esc(r.confidence || '')}</div></div></details>`;
      $('#fm').onclick = e => { const b = e.target.closest('button'); if (b) { view._m = b.dataset.k; pageForecasts(view, token); } };
      $('#fh').onchange = e => { view._mo = +e.target.value; pageForecasts(view, token); };
      $('#fh').oninput = e => e.target.previousElementSibling.value = e.target.value;
    } else if (tab === 'price') {
      const r = await api('/api/pricing'); if (stale(token)) return;
      body.innerHTML = `<div class="grid g2"><div class="card">${chartBox(r.charts[0]?.fig)}</div><div class="card">${chartBox(r.charts[1]?.fig)}</div></div>${callouts(r.insights, r.actions)}
        <details class="src"><summary>${icon('file')} Profit at each price change, by customer reaction</summary><div class="in">${tableHTML(r.table, 'price_scenarios')}</div></details>`;
    } else {
      const r = await api('/api/pricing'); if (stale(token)) return;
      body.innerHTML = `<p style="color:var(--muted);margin-top:0">Every model here is small, explainable and trained only on the anonymised data. This is how good each one really is.</p>` +
        r.report_card.map(c => `<div class="card" style="margin-bottom:12px"><div class="verdict"><span class="vi v-${c.verdict.replace(' ', '-')}">${esc(c.verdict)}</span><div><h3 style="margin:0">${esc(c.name)}</h3><div style="color:var(--ink2);font-size:.9rem;margin-top:.2rem">${esc(c.evidence)}</div><div style="color:var(--muted);font-size:.88rem;margin-top:.2rem">${esc(c.meaning)}</div></div></div></div>`).join('');
    }
  } catch (e) { if (stale(token)) return; body.innerHTML = e.forbidden ? restricted(e.message) : `<div class="note bad">${esc(e.message)}</div>`; }
  if (stale(token)) return; drawCharts(view); hydrateIcons(view); animateNumbers(view);
}

// ------------------------------------------------------------------ PAGE: planner
const PRESETS = {
  'Current business': [0, 0, 0, 0, 'data'], 'Premium pricing: +8% prices': [8, 0, 0, 0, 'moderate'], 'Growth push: +30% quotes': [0, 30, 0, 0, 'data'],
  'Growth with 2 extra makers': [0, 30, 2, 0, 'data'], 'Timber shock: +15% timber cost': [0, 0, 0, 15, 'data'], 'Lean year: -20% demand': [0, -20, 0, 0, 'data'],
};
async function pagePlanner(view, token) {
  const tab = view._tab || 'sc';
  view.innerHTML = `<div class="page"><div class="seg" id="pt" style="margin-bottom:16px"><button data-k="sc" class="${tab === 'sc' ? 'on' : ''}">Scenario builder</button><button data-k="cap" class="${tab === 'cap' ? 'on' : ''}">Capacity and hiring planner</button></div><div id="pbody">${skeleton(2)}</div></div>`;
  $('#pt').onclick = e => { const b = e.target.closest('button'); if (b) { view._tab = b.dataset.k; pagePlanner(view, token); } };
  const body = $('#pbody');
  if (tab === 'cap') {
    const r = await api('/api/capacity'); if (stale(token)) return;
    body.innerHTML = `<div class="grid g2"><div class="card">${chartBox(r.charts[0]?.fig)}</div><div class="card">${chartBox(r.charts[1]?.fig)}</div></div>${callouts(r.insights, r.actions, r.notes)}
      <details class="src"><summary>${icon('file')} Grid as a table</summary><div class="in">${tableHTML(r.table, 'capacity_grid')}</div></details>`;
    drawCharts(view); hydrateIcons(view); return;
  }
  const v = view._v || (view._v = { price: 0, volume: 0, makers: 0, timber: 0, elasticity: 'data' });
  body.innerHTML = `<div class="grid gside"><div class="card" style="align-self:start"><div class="field"><label>Start from a preset</label><select class="sel" id="pre">${Object.keys(PRESETS).map(p => `<option>${p}</option>`).join('')}</select></div>
    ${[['price', 'Change prices', -20, 30, '%'], ['volume', 'Change quote volume (demand)', -40, 60, '%'], ['makers', 'Add or remove makers', -2, 5, ''], ['timber', 'Change timber cost', -20, 40, '%']].map(([k, l, a, b, u]) =>
      `<div class="field"><label>${l}<output id="o-${k}">${v[k]}${u}</output></label><input type="range" data-k="${k}" data-u="${u}" min="${a}" max="${b}" value="${v[k]}"></div>`).join('')}
    <div class="field"><label>How price-sensitive are customers?</label><div class="radio">${[['data', 'Data estimate'], ['moderate', 'Moderate (-1.0)'], ['high', 'High (-2.0)']].map(([k, l]) => `<label><input type="radio" name="el" value="${k}" ${v.elasticity === k ? 'checked' : ''}> ${l}</label>`).join('')}</div></div>
    <div class="sub" style="font-size:.8rem;color:var(--muted)">The data shows almost no link between price and win rate, but cannot prove it. Use the stressed options to test the downside.</div></div>
    <div id="res"><div class="sk" style="height:420px"></div></div></div>`;
  hydrateIcons(view);
  let timer = null, seq = 0;
  const run = async () => {
    const my = ++seq;
    try {
      const r = await api('/api/whatif', { method: 'POST', body: JSON.stringify(v) }); if (my !== seq || stale(token)) return;
      $('#res').innerHTML = `${kpiRow(r.scenario_kpis, 'g4')}<div class="card" style="margin-top:16px">${chartBox(r.charts[0]?.fig)}</div>${callouts(r.insights, r.actions, r.confidence ? [r.confidence] : [])}
        <div class="grid g2" style="margin-top:12px"><div class="card"><h3>Current vs scenario (per month)</h3>${tableHTML(r.table, 'scenario')}</div>${(r.extra_tables || []).map(t => `<div class="card"><h3>${esc(t.caption)}</h3>${tableHTML(t.table, 'stress_test')}</div>`).join('')}</div>`;
      drawCharts($('#res')); animateNumbers($('#res'));
    } catch (e) { if (!stale(token)) $('#res').innerHTML = e.forbidden ? restricted(e.message) : `<div class="note bad">${esc(e.message)}</div>`; }
  };
  const later = () => { clearTimeout(timer); timer = setTimeout(run, 220); };
  $$('input[type=range]', body).forEach(r => r.oninput = () => { v[r.dataset.k] = +r.value; $('#o-' + r.dataset.k).textContent = r.value + r.dataset.u; later(); });
  $$('input[name=el]', body).forEach(r => r.onchange = () => { v.elasticity = r.value; later(); });
  $('#pre').onchange = e => {
    const [p, q, m, t, el] = PRESETS[e.target.value]; Object.assign(v, { price: p, volume: q, makers: m, timber: t, elasticity: el });
    $$('input[type=range]', body).forEach(r => { r.value = v[r.dataset.k]; $('#o-' + r.dataset.k).textContent = r.value + r.dataset.u; });
    $$('input[name=el]', body).forEach(r => r.checked = r.value === el); later();
  };
  run();
}

// ------------------------------------------------------------------ PAGE: data lab (bring your own data)
const lab = { profile: null, tab: 'ask', messages: [], busy: false, report: null, pdfUrl: null, table: 0 };
const fmtN = v => (v == null ? '' : typeof v === 'number' ? v.toLocaleString('en-US', { maximumFractionDigits: 2 }) : v);

async function uploadFiles(files, view, token) {
  const list = $('#up-list'); list.innerHTML = '';
  for (const f of files) {
    const row = document.createElement('div'); row.className = 'uprow';
    row.innerHTML = `${icon('file')}<span>${esc(f.name)}</span><em>${(f.size / 1024).toFixed(0)} KB</em><b class="st">Uploading...</b>`; list.append(row);
    try {
      const res = await fetch('/api/lab/upload?name=' + encodeURIComponent(f.name), { method: 'POST', body: f, headers: { 'X-Role': state.role, 'X-Session': state.session, 'Content-Type': 'application/octet-stream' } });
      const j = await res.json();
      if (!res.ok) throw new Error(j.message || 'Upload failed');
      lab.profile = j; $('.st', row).textContent = 'Done'; row.classList.add('ok');
    } catch (e) { $('.st', row).textContent = e.message; row.classList.add('bad'); }
  }
  lab.messages = []; lab.report = null; lab.pdfUrl = null;
  if (lab.profile?.loaded) { hydrateIcons(list); renderLab(view, token); }
}

async function pageLab(view, token) {
  view.innerHTML = skeleton(2);
  const d = await api('/api/lab/dataset'); if (stale(token)) return;
  lab.profile = d;
  renderLab(view, token);
}

function dropzone(compact) {
  return `<div class="dz ${compact ? 'compact' : ''}" id="dz"><div class="dz-ic">${icon('upload')}</div>
    <div><b>${compact ? 'Add more files' : 'Drop Excel or CSV files here'}</b><div class="sub" style="margin:.2rem 0 0">${compact ? 'Files with the same columns are combined automatically.' : 'or click to browse. You can upload several files at once; files with the same columns are combined into one table. Up to 25 MB each.'}</div></div>
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

function renderLab(view, token) {
  const p = lab.profile;
  if (!p?.loaded) {
    view.innerHTML = `<div class="page"><div class="card lab-hero"><div class="lab-art">${icon('db')}</div><h2>Analyse your own data</h2>
      <p>Upload your sales, purchase-order or stock files. AskShelf previews and profiles them, answers your questions with charts, forecasts the future with Prophet, and writes a formal report you can download as PDF, Word and Excel.</p>
      ${dropzone(false)}<div style="margin-top:14px"><button class="btn btn-ghost" id="sample">${icon('spark')} Try the sample wholesale data</button></div>
      <div class="note" style="margin-top:16px;text-align:left"><b>Privacy:</b> columns that look like personal data (emails, phone numbers, names, pay, card numbers) are removed the moment you upload, before any analysis or AI call.</div></div></div>`;
    wireDrop(view, token); hydrateIcons(view);
    $('#sample').onclick = async () => { $('#sample').disabled = true; try { lab.profile = await api('/api/lab/sample', { method: 'POST' }); lab.messages = []; renderLab(view, token); } catch (e) { toast(e.message, true); $('#sample').disabled = false; } };
    return;
  }
  const t0 = p.tables[Math.min(lab.table, p.tables.length - 1)];
  view.innerHTML = `<div class="page">
    <div class="grid g4" style="margin-bottom:14px">${kpiHTML({ label: 'Files', value: p.files.length }, 0)}${kpiHTML({ label: 'Tables', value: p.tables.length }, 1)}${kpiHTML({ label: 'Rows', value: p.rows_total }, 2)}${kpiHTML({ label: 'Columns removed (privacy)', value: p.dropped.length }, 3)}</div>
    ${p.fusion_note ? `<div class="note">${icon('layers')} ${esc(p.fusion_note)}</div>` : ''}
    ${p.dropped.length ? `<div class="note caution">${icon('lock')} Personal data removed on upload: ${p.dropped.map(d => `<span class="chip red">${esc(d.column)}</span>`).join(' ')}</div>` : ''}
    <div class="toolbar"><div class="seg" id="lt">${[['preview', 'Preview'], ['profile', 'Profile'], ['ask', 'Ask'], ['report', 'Report']].map(([k, l]) => `<button data-k="${k}" class="${k === lab.tab ? 'on' : ''}">${l}</button>`).join('')}</div>
      <span class="sp"></span><button class="btn btn-ghost btn-sm" id="l-add">${icon('plus')} Add files</button><button class="btn btn-ghost btn-sm" id="l-xl">${icon('download')} Data sample (Excel)</button>
      <button class="btn btn-ghost btn-sm" id="l-mem">${icon('refresh')} Clear memory</button><button class="btn btn-ghost btn-sm" id="l-reset" style="color:var(--red)">${icon('trash')} Reset data</button></div>
    <div id="l-add-box" hidden>${dropzone(true)}</div><div id="lbody"></div></div>`;
  hydrateIcons(view);
  $('#lt').onclick = e => { const b = e.target.closest('button'); if (b) { lab.tab = b.dataset.k; renderLab(view, token); } };
  $('#l-add').onclick = () => { $('#l-add-box').hidden = !$('#l-add-box').hidden; wireDrop(view, token); };
  $('#l-xl').onclick = () => download('/api/lab/export?kind=sample', 'data_sample.xlsx');
  $('#l-mem').onclick = async () => { await api('/api/lab/clear-memory', { method: 'POST' }); lab.messages = []; renderLab(view, token); toast('Conversation memory cleared'); };
  $('#l-reset').onclick = async () => { if (!confirm('Remove all uploaded data and results?')) return; await api('/api/lab/reset', { method: 'POST' }); lab.profile = null; lab.messages = []; lab.report = null; lab.pdfUrl = null; renderLab(view, token); };
  const body = $('#lbody');
  if (lab.tab === 'preview') {
    body.innerHTML = `${p.tables.length > 1 ? `<div class="field" style="max-width:340px"><label>Table</label><select class="sel" id="tsel">${p.tables.map((t, i) => `<option value="${i}" ${i === lab.table ? 'selected' : ''}>${esc(t.name)} (${t.rows.toLocaleString()} rows)</option>`).join('')}</select></div>` : ''}
      <div class="card"><h3>${esc(t0.name)}</h3><div class="sub">First 8 of ${t0.rows.toLocaleString()} rows. Dates are shown as YYYY-MM-DD.</div>${tableHTML(t0.sample, 'preview_' + t0.name)}</div>`;
    const ts = $('#tsel'); if (ts) ts.onchange = () => { lab.table = +ts.value; renderLab(view, token); };
  } else if (lab.tab === 'profile') {
    body.innerHTML = p.tables.map(t => `<div class="card" style="margin-bottom:14px"><div class="toolbar" style="margin-bottom:8px"><h3 style="margin:0">${esc(t.name)}</h3><span class="chip">${t.rows.toLocaleString()} rows</span><span class="chip ${t.missing_cells_pct > 5 ? 'amber' : 'green'}">${t.missing_cells_pct}% missing</span>
      <span class="chip ${t.duplicates ? 'amber' : 'green'}">${t.duplicates} duplicate rows</span>${t.time_column ? `<span class="chip blue">${icon('clock')} date: ${esc(t.time_column)}</span>` : ''}</div>
      <div class="tablewrap"><table class="t"><thead><tr><th>Column</th><th>Type</th><th>Missing</th><th>Distinct</th><th>Range / top values</th></tr></thead><tbody>${t.columns.map(c => `<tr><td><b>${esc(c.name)}</b></td>
      <td><span class="chip ${c.kind === 'numeric' ? 'blue' : c.kind === 'datetime' ? 'green' : ''}">${c.kind}</span></td><td>${c.missing_pct}%</td><td>${c.unique.toLocaleString()}</td>
      <td>${c.kind === 'numeric' ? `${fmtN(c.min)} to ${fmtN(c.max)} (mean ${fmtN(c.mean)})` : c.kind === 'datetime' ? `${c.min} to ${c.max}` : (c.top || []).map(x => esc(x[0])).slice(0, 4).join(', ')}</td></tr>`).join('')}</tbody></table></div></div>`).join('');
  } else if (lab.tab === 'ask') {
    renderLabAsk(body, view, token);
  } else {
    renderLabReport(body, view, token);
  }
  drawCharts(view); wireTabs(view);
}

function labSuggestions(p) {
  const t = p.tables[0], m = (t.measures.find(c => /(revenue|sales|amount|profit|value)/.test(c)) || t.measures[0]), m2 = t.measures.find(c => c !== m), d = t.dimensions[0], d2 = t.dimensions[1];
  const n = c => c.replace(/_/g, ' '), out = [];
  if (m && d) out.push(`Rank ${n(d)} by ${n(m)} (top 5)`);
  if (m && t.time_column) out.push(`Monthly ${n(m)} trend`, `Forecast ${n(m)} for the next 6 months`);
  if (m2 && d) out.push(`Average ${n(m2)} by ${n(d2 || d)}`);
  if (m && d2) out.push(`Which ${n(d2)} has the lowest ${n(m)}?`);
  return out.slice(0, 5);
}

function renderLabAsk(body, view, token) {
  const sug = labSuggestions(lab.profile);
  body.innerHTML = `<div class="card" style="margin-bottom:14px"><div class="cbox" style="max-width:none;box-shadow:none"><textarea id="lq" rows="1" maxlength="800" placeholder="Ask about your data, e.g. 'Top 5 suppliers by sales' or 'Forecast sales for the next 6 months'"></textarea><button class="sendbtn" id="lsend">${icon('send')}</button></div>
    <div class="chips" style="margin-top:10px">${sug.map(q => `<button class="chip" data-lq="${esc(q)}">${esc(q)}</button>`).join('')}</div></div><div id="lthread"></div>`;
  const q = $('#lq'), go = () => { const v = q.value.trim(); if (v && !lab.busy) { q.value = ''; labAsk(v, view, token); } };
  $('#lsend').onclick = go; q.onkeydown = e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); go(); } };
  $$('[data-lq]', body).forEach(b => b.onclick = () => labAsk(b.dataset.lq, view, token));
  renderLabThread();
}

function renderLabThread() {
  const th = $('#lthread'); if (!th) return;
  th.innerHTML = lab.messages.slice().reverse().map((m, ri) => {
    const i = lab.messages.length - 1 - ri;
    if (m.pending) return `<div class="card lres"><div class="think"><span class="typing"><i></i><i></i><i></i></span><span>Analysing "${esc(m.q)}"...</span></div></div>`;
    const r = m.res;
    if (r.blocked) return `<div class="card lres"><div class="qline">${esc(m.q)}</div><div class="blocked">${icon('lock')}<div>${esc(r.text)}</div></div></div>`;
    if (r.error) return `<div class="card lres"><div class="qline">${esc(m.q)}</div><div class="blocked">${icon('warn')}<div>${esc(r.error)}${r.sql ? `<details class="src"><summary>Query</summary><div class="in"><pre class="sqlbox">${esc(r.sql)}</pre></div></details>` : ''}</div></div></div>`;
    const ex = r.explanation || {}, last = ri === 0;
    return `<div class="card lres"><div class="qline">${esc(m.q)} <span class="chip ${r.mode === 'ai' ? 'blue' : 'amber'}">${r.mode === 'ai' ? 'AI-planned' : 'Built-in rules'}</span></div>
      <div class="lsum">${esc(ex.summary || '')}</div>${(ex.bullets || []).length ? `<ul class="lbul">${ex.bullets.map(b => `<li>${esc(b)}</li>`).join('')}</ul>` : ''}
      ${r.chart ? `<div class="achart">${chartBox(r.chart)}</div>` : ''}
      <details class="src"><summary>${icon('file')} Result table (${(r.table?.total_rows ?? 0).toLocaleString()} rows)${r.kind === 'forecast' ? ' and forecast detail' : ''}</summary><div class="in">${tableHTML(r.table, 'lab_result')}${r.group_table ? `<div class="sect">Forecast by group</div>${tableHTML(r.group_table, 'lab_group_forecast')}` : ''}</div></details>
      ${r.sql ? `<details class="src"><summary>${icon('db')} ${r.mode === 'ai' ? 'AI-generated' : 'Rule-generated'} read-only query</summary><div class="in"><pre class="sqlbox">${esc(r.sql)}</pre><div class="sub" style="font-size:.78rem">Validated as a single SELECT, with a row and time limit, before it ran.</div></div></details>` : ''}
      <div class="acts">${last ? `<button class="act" id="lxl">${icon('download')} Export result (Excel)</button>` : ''}<span style="margin-left:auto;font-size:.76rem;color:var(--muted)">${r.latency_ms} ms</span></div>
      ${last && (r.follow_ups || []).length ? `<div class="fups">${r.follow_ups.map(f => `<button class="fup" data-lq="${esc(f)}">${icon('arrow')}${esc(f)}</button>`).join('')}</div>` : ''}</div>`;
  }).join('');
  const x = $('#lxl'); if (x) x.onclick = () => download('/api/lab/export?kind=result', 'analysis_result.xlsx');
  $$('[data-lq]', th).forEach(b => b.onclick = () => labAsk(b.dataset.lq));
  drawCharts(th); hydrateIcons(th);
}

async function labAsk(q) {
  if (lab.busy) return;
  lab.busy = true; lab.messages.push({ q, pending: true }); renderLabThread();
  const hist = lab.messages.filter(m => !m.pending && m.res && !m.res.error).slice(-5).flatMap(m => [{ role: 'user', text: m.q }, { role: 'assistant', text: m.res.explanation?.summary || '' }]);
  try { const res = await api('/api/lab/ask', { method: 'POST', body: JSON.stringify({ question: q, history: hist }) }); lab.messages.pop(); lab.messages.push({ q, res }); }
  catch (e) { lab.messages.pop(); lab.messages.push({ q, res: { error: e.message } }); }
  lab.busy = false; renderLabThread();
}

const REPORT_STEPS = ['Profile the data', 'Plan the report structure', 'Run the analyses and draft the narrative', 'Forecast the main measure', 'Write the executive summary and recommendations', 'Assemble the report', 'Typeset the PDF report', 'Build the Word document', 'Build the Excel workbook'];

function renderLabReport(body, view, token) {
  const r = lab.report;
  body.innerHTML = `<div class="grid gside"><div class="card" style="align-self:start"><h3>Generate a formal report</h3><div class="sub">A title page, table of contents, lists of figures and tables, numbered sections with captioned charts and tables, recommendations, references and appendices.</div>
    <div class="field"><label>Report title (optional)</label><input class="inp" id="r-title" placeholder="Data Analysis Report: ..." value="${esc(r?.meta?.title || '')}"></div>
    <div class="field"><label>Organisation</label><input class="inp" id="r-org" placeholder="e.g. Modern Furniture Co." value="${esc(r?.meta?.organisation || '')}"></div>
    <div class="field"><label>Prepared for</label><input class="inp" id="r-for" placeholder="e.g. Executive Team" value="${esc(r?.meta?.prepared_for || '')}"></div>
    <div class="field"><label>Prepared by</label><input class="inp" id="r-by" placeholder="AskShelf Data Lab" value="${esc(r?.meta?.prepared_by || '')}"></div>
    <button class="btn btn-primary" id="r-go" style="width:100%;justify-content:center" ${r?.status === 'running' ? 'disabled' : ''}>${icon('file')} ${r?.status === 'running' ? 'Generating...' : 'Generate report'}</button>
    <div class="sub" style="font-size:.78rem;margin-top:10px">Takes 10-60 seconds. AI-written narrative is checked against the data before it is included.</div></div>
    <div id="r-out">${!r ? `<div class="card empty"><div class="lab-art" style="margin:0 auto 10px">${icon('file')}</div><b>No report yet</b><div class="sub">Fill in the details and press Generate. You will get PDF, Word and Excel versions.</div></div>` : ''}</div></div>`;
  hydrateIcons(body);
  $('#r-go').onclick = () => startReport(view, token);
  if (r) paintReport(body);
}

function paintReport(body) {
  const r = lab.report, out = $('#r-out'); if (!out) return;
  if (r.status === 'running' || r.status === 'starting') {
    const done = r.steps.length, total = REPORT_STEPS.length;
    out.innerHTML = `<div class="card"><h3>Building your report</h3><div class="sub">${esc(r.steps[r.steps.length - 1] || 'Starting')}...</div><div class="progress"><i style="width:${Math.min(95, done / total * 100)}%"></i></div>
      <div class="rsteps">${REPORT_STEPS.map((s, i) => `<div class="rs ${i < done ? 'done' : i === done ? 'now' : ''}">${i < done ? icon('check') : '<span class="dot"></span>'}${esc(s)}</div>`).join('')}</div></div>`;
  } else if (r.status === 'error') {
    out.innerHTML = `<div class="note bad">Report failed: ${esc(r.message || 'unknown error')}</div>`;
  } else if (r.status === 'done') {
    const s = r.summary;
    out.innerHTML = `<div class="card" style="margin-bottom:14px"><div class="toolbar" style="margin-bottom:6px"><div><h3 style="margin:0">${esc(s.title)}</h3><div class="sub" style="margin:2px 0 0">${s.sections} sections &middot; ${s.figures} figures &middot; ${s.tables} tables &middot; ${esc(s.model)}</div></div><span class="sp"></span>
      <button class="btn btn-primary btn-sm" data-dl="pdf">${icon('download')} PDF</button><button class="btn btn-ghost btn-sm" data-dl="docx">${icon('download')} Word</button><button class="btn btn-ghost btn-sm" data-dl="xlsx">${icon('download')} Excel</button></div>
      ${(s.notes || []).map(n => `<div class="note caution">${esc(n)}</div>`).join('')}
      <div class="sect">Key findings</div>${s.findings.map((f, i) => `<div class="note"><b>${i + 3}. ${esc(f.title)}.</b> ${esc(f.finding)}</div>`).join('')}</div>
      <div class="card flush"><div class="toolbar" style="padding:12px 16px 0"><h3 style="margin:0">Preview</h3></div>${lab.pdfUrl ? `<iframe class="pdfframe" src="${lab.pdfUrl}#view=FitH" title="Report preview"></iframe>` : `<div class="empty">Loading preview...</div>`}</div>`;
    $$('[data-dl]', out).forEach(b => b.onclick = () => download(`/api/lab/report/download?id=${r.id}&fmt=${b.dataset.dl}`, `askshelf_report.${b.dataset.dl}`));
    hydrateIcons(out);
  }
}

async function startReport(view, token) {
  const meta = { title: $('#r-title').value, organisation: $('#r-org').value, prepared_for: $('#r-for').value, prepared_by: $('#r-by').value };
  lab.report = { status: 'starting', steps: [], meta }; lab.pdfUrl = null; renderLab(view, token);
  try {
    const { id } = await api('/api/lab/report', { method: 'POST', body: JSON.stringify(meta) });
    lab.report.id = id; lab.report.status = 'running';
    while (true) {
      await sleep(900);
      const st = await api('/api/lab/report/status?id=' + id);
      lab.report = { ...lab.report, ...st, id, meta };
      if (lab.tab === 'report') paintReport($('#lbody'));
      if (st.status !== 'running') break;
    }
    if (lab.report.status === 'done') {
      const res = await fetch(`/api/lab/report/download?id=${id}&fmt=pdf`, { headers: { 'X-Role': state.role, 'X-Session': state.session } });
      lab.pdfUrl = URL.createObjectURL(await res.blob());
      toast('Report ready');
    }
    if (lab.tab === 'report') renderLab(view, token);
  } catch (e) { lab.report = { status: 'error', steps: [], message: e.message, meta }; if (lab.tab === 'report') renderLab(view, token); }
}

// ------------------------------------------------------------------ PAGE: governance
async function pageGovernance(view, token) {
  view.innerHTML = skeleton(2);
  const d = await api('/api/governance'); if (stale(token)) return;
  const roles = state.meta.roles; const c = d.chain;
  view.innerHTML = `<div class="page">
  <div class="grid g4" style="margin-bottom:16px">${kpiHTML({ label: 'Your role', value: state.role.split(' / ')[0] }, 0)}${kpiHTML({ label: 'Audit entries', value: c.entries }, 1)}${kpiHTML({ label: 'Answers rated helpful', value: d.feedback.up }, 2)}${kpiHTML({ label: 'Issues reported', value: d.feedback.reports }, 3)}</div>
  <div class="card" style="margin-bottom:16px"><h3>Roles and what they can do</h3><div class="sub">Access is enforced inside the pipeline, before any analysis runs. Switch role (top right) to try it.</div>
    <div class="grid g4">${Object.entries(roles).map(([n, r]) => `<div class="card" style="box-shadow:none;${n === state.role ? 'border-color:var(--primary);background:var(--primary-l)' : ''}"><b>${esc(n)}</b><div class="sub" style="margin:.3rem 0 .5rem;font-size:.82rem">${esc(r.blurb)}</div><span class="chip ${r.can_export ? 'green' : ''}">${r.can_export ? 'Can export' : 'No export'}</span></div>`).join('')}</div></div>
  <div class="card" style="margin-bottom:16px"><h3>Access matrix</h3><div class="sub">Which analyses each role may run.</div>
    <div class="tablewrap"><table class="t"><thead><tr>${d.matrix.columns.map(x => `<th>${esc(x)}</th>`).join('')}</tr></thead><tbody>${d.matrix.data.map(r => `<tr>${r.map((x, i) => `<td class="${x === 'Allowed' ? 'ok' : x === 'Blocked' ? 'no' : ''}">${esc(x)}</td>`).join('')}</tr>`).join('')}</tbody></table></div></div>
  <div class="card" style="margin-bottom:16px"><div class="toolbar" style="margin-bottom:8px"><div><h3>Audit trail</h3><div class="sub" style="margin:0">Every request is recorded with its outcome and cost. Entries are hash-chained, so any edit is detectable.</div></div><span class="sp"></span>
    <span class="chip ${c.intact ? 'green' : 'red'}">${icon(c.intact ? 'check' : 'warn')} Chain ${c.intact ? 'intact' : 'broken at entry ' + c.bad_line} &middot; ${c.entries} entries</span>
    <button class="btn btn-ghost btn-sm" id="ad">${icon('download')} Download</button></div>
    ${d.audit.length ? `<div class="tablewrap"><table class="t"><thead><tr><th>Time</th><th>Role</th><th>Question</th><th>Outcome</th><th>Analyses</th><th>Mode</th><th>ms</th><th>Cost</th><th>Hash</th></tr></thead><tbody>${d.audit.map(r => `<tr><td>${esc(r.time)}</td><td>${esc(r.role)}</td><td>${esc(r.question)}</td><td><span class="chip ${r.outcome === 'answered' ? 'green' : 'amber'}">${esc(r.outcome)}</span></td><td>${esc(r.tools)}</td><td>${esc(r.mode)}</td><td>${r.latency_ms}</td><td>$${(+r.cost).toFixed(4)}</td><td><code>${esc(r.hash)}</code></td></tr>`).join('')}</tbody></table></div>` : '<div class="empty">No entries yet. Ask a question and it appears here.</div>'}</div>
  <div class="card"><h3>Data privacy</h3><div class="sub">Removed when data is loaded. Never shown to the AI, to users or in logs.</div><div class="chips">${d.restricted.map(x => `<span class="chip red">${icon('lock')}${esc(x)}</span>`).join('')}</div>
    <div class="note" style="margin-top:12px">Only aggregates are reported. Output is scanned and redacted for IDs, emails, phone numbers and keys, and questions are masked before they are written to the audit log.</div></div></div>`;
  $('#ad').onclick = () => download('/api/audit/download', 'audit_log.jsonl');
  hydrateIcons(view);
}

// ------------------------------------------------------------------ PAGE: glossary
async function pageGlossary(view, token) {
  view.innerHTML = skeleton(2);
  const d = await api('/api/glossary'); if (stale(token)) return;
  view.innerHTML = `<div class="page"><div class="grid g2"><div class="card"><h3>Definitions</h3><div class="sub">Plain-English meanings for the terms used in answers.</div>
    ${d.definitions.map(x => `<p style="margin:.6rem 0"><b>${esc(x.term)}</b><br><span style="color:var(--ink2);font-size:.9rem">${esc(x.meaning)}</span></p>`).join('')}</div>
    <div class="card"><h3>Data sources</h3><div class="sub">The files behind every analysis.</div>${d.files.map(x => `<p style="margin:.6rem 0"><span class="chip">${icon('db')}${esc(x.file)}</span><br><span style="color:var(--ink2);font-size:.9rem">${esc(x.about)}</span></p>`).join('')}
    <div class="note">Missing a term? Use "Report or teach a term" under any answer and it goes to the review log.</div></div></div></div>`;
  hydrateIcons(view);
}

// ------------------------------------------------------------------ boot
async function refreshAlertCount() {
  try { const d = await api('/api/alerts'); state.alertCount = d.alerts.filter(a => a.severity !== 'info').length; renderNav(); } catch { /* ignore */ }
}
async function boot() {
  $('#lg').innerHTML = LOGO; hydrateIcons();
  try { state.meta = await api('/api/meta'); } catch (e) { $('#view').innerHTML = `<div class="restricted"><h2>Cannot reach the server</h2><p>${esc(e.message)}</p></div>`; return; }
  const saved = store.get('as_role'); state.role = state.meta.roles[saved] ? saved : state.meta.default_role;
  const sel = $('#role'); sel.innerHTML = Object.keys(state.meta.roles).map(r => `<option ${r === state.role ? 'selected' : ''}>${esc(r)}</option>`).join('');
  sel.onchange = () => { state.role = sel.value; store.setL('as_role', state.role); store.setS('as_role', state.role); toast(`Now acting as ${state.role}`); refreshAlertCount(); route(); };
  const gen = state.meta.mode === 'generative';
  $('#modebox').innerHTML = `<b><span class="dot-live" style="${gen ? '' : 'background:#d97706'}"></span>${gen ? 'Generative AI mode' : 'Built-in mode'}</b><span>${gen ? esc(state.meta.model || 'Gemini') + ' with self-review' : 'No AI key set. Keyword routing.'}</span><span>${esc(state.meta.coverage)}</span>`;
  $('#menu').onclick = () => $('#side').classList.toggle('open'); $('#scrim').onclick = () => $('#side').classList.remove('open');
  addEventListener('hashchange', route); addEventListener('resize', () => drawCharts());
  refreshAlertCount(); route();
  const pre = new URLSearchParams(location.search).get('q');
  if (pre) { history.replaceState(null, '', location.pathname + location.hash); sendQuestion(pre); }
}
boot();
