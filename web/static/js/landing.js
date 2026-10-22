document.getElementById('lg').innerHTML = LOGO; document.getElementById('lg2').innerHTML = LOGO;
document.getElementById('yr').textContent = new Date().getFullYear();
hydrateIcons();
const REDUCED = matchMedia('(prefers-reduced-motion: reduce)').matches;
const $ = s => document.querySelector(s), sleep = ms => new Promise(r => setTimeout(r, ms));
const fmt$ = n => '$' + Math.round(n).toLocaleString('en-US');

const nav = $('#nav');
addEventListener('scroll', () => nav.classList.toggle('scrolled', scrollY > 8), { passive: true });

// ---------- count-up + reveal on scroll
function countUp(el) {
  const to = +el.dataset.count, t0 = performance.now(), dur = 1100;
  (function tick(t) { const p = Math.min((t - t0) / dur, 1); el.textContent = Math.round(to * (1 - Math.pow(1 - p, 3))); if (p < 1) requestAnimationFrame(tick); })(t0);
}
const io = new IntersectionObserver(es => es.forEach(e => {
  if (!e.isIntersecting) return;
  e.target.classList.add('in');
  e.target.querySelectorAll?.('[data-count]').forEach(countUp);
  if (e.target.dataset.count) countUp(e.target);
  io.unobserve(e.target);
}), { threshold: .2 });
document.querySelectorAll('.rv, .stat').forEach(el => io.observe(el));

// ---------- hero mock: conversations that type themselves
const SCEN = [
  { q: 'Which suppliers deliver late most often?', a: '<b>One supplier is late on about a third of orders</b>, well above the rest. Review its terms or move volume to a faster supplier.',
    k: [['Late rate', '34%', 'r'], ['Best supplier', '7%'], ['Orders', '1,200']], bars: [34, 21, 15, 11, 9, 7], hot: 2 },
  { q: 'Which products are about to run out of stock?', a: '<b>3 products are out of stock and 8 are below their reorder level.</b> Raise purchase orders now; suggested quantities are on the Stock page.',
    k: [['Out of stock', '3', 'r'], ['Low cover', '8'], ['Weekly sales at risk', '7.7K']], bars: [0, 2, 4, 9, 14, 30, 62], hot: 3 },
  { q: 'Forecast sales for the next 6 months', a: '<b>Sales are expected to dip then recover</b>, with an 80% range and the back-test error shown so you can plan to the range, not a single number.',
    k: [['Next 6 months', '519K'], ['Back-test error', '23%'], ['History', '24 mo']], bars: [40, 55, 70, 58, 45, 52, 66, 80], hot: 0 },
];
const mq = $('#mq'), ma = $('#ma'), mk = $('#mk'), mc = $('#mc');
function setPanel(s, show) {
  mk.innerHTML = s.k.map(([l, v, c]) => `<div class="mini-kpi"><small>${l}</small><b ${c ? 'style="color:var(--red)"' : ''}>${v}</b></div>`).join('');
  mc.innerHTML = s.bars.map((h, i) => `<i class="${i < s.hot ? 'hot' : ''}" style="height:${show ? h : 0}%"></i>`).join('');
  if (show) return;
  requestAnimationFrame(() => requestAnimationFrame(() => mc.querySelectorAll('i').forEach((b, i) => { b.style.transitionDelay = i * 60 + 'ms'; b.style.height = s.bars[i] + '%'; })));
}
async function runHero() {
  if (REDUCED) { mq.textContent = SCEN[0].q; ma.innerHTML = SCEN[0].a; setPanel(SCEN[0], true); return; }
  for (let i = 0; ; i = (i + 1) % SCEN.length) {
    const s = SCEN[i];
    mq.textContent = ''; mq.classList.add('caret'); ma.innerHTML = '<span class="typing"><i></i><i></i><i></i></span>'; ma.style.visibility = 'hidden'; mk.style.opacity = 0; mc.style.opacity = 0;
    for (const ch of s.q) { mq.textContent += ch; await sleep(26); }
    mq.classList.remove('caret'); ma.style.visibility = 'visible'; await sleep(1100);
    ma.innerHTML = s.a; setPanel(s, false); mk.style.transition = mc.style.transition = 'opacity .5s'; mk.style.opacity = 1; mc.style.opacity = 1;
    await sleep(5200);
  }
}
runHero();
// subtle 3D tilt that follows the mouse
const mock = $('#mock'), win = mock.querySelector('.window');
if (!REDUCED && matchMedia('(hover:hover)').matches) {
  mock.addEventListener('mousemove', e => { const r = mock.getBoundingClientRect(), x = (e.clientX - r.left) / r.width - .5, y = (e.clientY - r.top) / r.height - .5; win.style.transform = `rotateY(${x * 10 - 2}deg) rotateX(${3 - y * 8}deg)`; });
  mock.addEventListener('mouseleave', () => { win.style.transform = ''; });
}

// ---------- solutions
const UCS = [
  { tab: 'Purchasing', h: 'Order before the shelf is empty', p: 'See days of cover for every product and a suggested order quantity, so purchase orders go out in time.',
    li: ['Days of cover at the recent sales rate', 'Reorder-level and out-of-stock alerts', 'Suggested order quantities'], q: 'Which products will run out of stock this week?', a: 'Example: 3 products are out of stock and 8 more have under a week of cover. The Stock page lists each one with a suggested order quantity.' },
  { tab: 'Operations', h: 'Catch late deliveries before they are late', p: 'Rank open orders by predicted lateness using a model trained on your own delivery history, and see which suppliers cause the delays.',
    li: ['Late rate by supplier and store', 'Late-risk score on every open order', 'Penalty exposure and an expedite plan'], q: 'Which open orders are most likely to be late?', a: 'Example: the model flags the highest-risk open orders and shows its own accuracy, so you know how far to trust it.' },
  { tab: 'Sales and category', h: 'Know what is selling and what is not', p: 'Track revenue and margin by product, category and store, and spot products that are declining before they become a problem.',
    li: ['Top products and category mix', 'Margin by category', 'Declining-product alerts'], q: 'Why did sales drop in October?', a: 'Example: ask in plain English and get a chart, the key numbers and the read-only query that produced them.' },
  { tab: 'Finance and owners', h: 'Plan with forecasts you can defend', p: 'Forecast revenue, units and orders with a back-tested error, test price and volume changes, and export a formal report.',
    li: ['Prophet forecasts with an 80% range', 'Scenario planner for price, volume and cost', 'PDF, Word and Excel reports'], q: 'Forecast sales for the next 6 months.', a: 'Example: expected sales over the period with a range, plus how large the forecast error was on recent months.' },
];
const tabs = $('#uc-tabs'), card = $('#uc-card');
function showUC(i) {
  const u = UCS[i];
  tabs.innerHTML = UCS.map((x, j) => `<button class="tab ${j === i ? 'on' : ''}" data-i="${j}">${x.tab}</button>`).join('');
  card.innerHTML = `<div class="l"><h3>${u.h}</h3><p>${u.p}</p><ul>${u.li.map(l => `<li>${icon('check')}${l}</li>`).join('')}</ul><div style="margin-top:1.4rem"><a class="btn btn-primary btn-sm" href="/app">Try it live ${icon('arrow')}</a></div></div>
  <div class="r"><div class="bubble u">${u.q}</div><div class="bubble a">${u.a}</div></div>`;
  tabs.querySelectorAll('.tab').forEach(b => b.onclick = () => showUC(+b.dataset.i));
}
showUC(0);

// ---------- pipeline diagram (animated rails + travelling dots)
(function pipeline() {
  const N = [['Your files', 'Excel or CSV'], ['Privacy filter', 'IDs and pay removed'], ['Role check', 'access before any data'], ['AI picks analysis', 'AI plans a read-only query'], ['Models run', 'risk, forecast, what-if'], ['Answer + action', 'chart, numbers, sources']];
  const w = 1000, gap = (w - 140) / (N.length - 1), y = 75, path = `M70 ${y} H${w - 70}`;
  const nodes = N.map(([t, s], i) => { const x = 70 + i * gap; return `<g class="node" style="--i:${i}"><rect class="pn-box" x="${x - 66}" y="${y - 34}" width="132" height="68" rx="14"/><text class="pn-txt" x="${x}" y="${y - 3}">${t}</text><text class="pn-sub" x="${x}" y="${y + 17}">${s}</text></g>`; }).join('');
  const dots = REDUCED ? '' : [0, -1.6, -3.2].map(b => `<circle r="6" fill="#2563eb"><animateMotion dur="5s" begin="${b}s" repeatCount="indefinite" path="${path}"/></circle><circle r="12" fill="#2563eb" opacity=".18"><animateMotion dur="5s" begin="${b}s" repeatCount="indefinite" path="${path}"/></circle>`).join('');
  $('#pipe').innerHTML = `<svg viewBox="0 0 ${w} 150" role="img" aria-label="Question flows through data, privacy filter, role check, AI routing, models and answer"><path class="rail" d="${path}" fill="none"/>${nodes}${dots}</svg>`;
})();

// ---------- pricing
const PLANS = [
  { id: 'starter', name: 'Starter', price: 149, users: 3, src: 1, ans: 1000, tag: 'For a small team getting its first answers.', feats: ['3 people', '1 data source', '1,000 answers a month', 'Chat, dashboard and forecasts', 'Email support'] },
  { id: 'growth', name: 'Growth', price: 499, users: 10, src: 3, ans: 5000, pop: true, tag: 'For purchasing, warehouse and sales working together.', feats: ['10 people', '3 data sources', '5,000 answers a month', 'Delivery schedule and stock availability', 'Alerts and daily digest', 'Role-based access', 'Scenario planner'] },
  { id: 'scale', name: 'Scale', price: 1299, users: 30, src: 10, ans: 20000, tag: 'For multi-site distributors with strict controls.', feats: ['30 people', '10 data sources', '20,000 answers a month', 'Audit-trail export', 'Single sign-on', 'Custom roles', 'Priority support'] },
  { id: 'ent', name: 'Enterprise', price: null, tag: 'For large groups with custom needs.', feats: ['Unlimited people and sources', 'Volume answer pricing', 'Private cloud or on-premises', 'Security review and SLA', 'Dedicated success manager'] },
];
let annual = false;
const disc = () => (annual ? 0.8 : 1);
function renderPlans() {
  $('#plans').innerHTML = PLANS.map((p, i) => `<div class="plan rv in ${p.pop ? 'pop' : ''}" style="--d:${i}">${p.pop ? '<span class="badge">MOST POPULAR</span>' : ''}<h3>${p.name}</h3><div class="tag">${p.tag}</div>
    <div class="price">${p.price ? fmt$(p.price * disc()) + '<small> /month</small>' : 'Custom'}</div><div class="per">${p.price ? (annual ? `Billed ${fmt$(p.price * disc() * 12)} a year` : 'Billed monthly') : 'Talk to us'}</div>
    <ul>${p.feats.map(f => `<li>${icon('check')}${f}</li>`).join('')}</ul><a class="btn ${p.pop ? 'btn-primary' : 'btn-ghost'}" href="/app">${p.price ? 'Start free trial' : 'Contact sales'}</a></div>`).join('');
}
$('#b-t').onclick = () => { annual = !annual; $('#b-t').classList.toggle('y', annual); $('#b-m').classList.toggle('on', !annual); $('#b-y').classList.toggle('on', annual); renderPlans(); calc(); };
renderPlans();

// ---------- calculator with tweened numbers
const shown = {};
function tween(id, to, render) {
  const el = document.getElementById(id); if (!el) return; const from = shown[id] ?? 0; shown[id] = to;
  if (REDUCED) { el.innerHTML = render(to); return; }
  const t0 = performance.now(); (function tk(t) { const p = Math.min((t - t0) / 600, 1), e = 1 - Math.pow(1 - p, 3); el.innerHTML = render(from + (to - from) * e); if (p < 1) requestAnimationFrame(tk); })(t0);
}
function calc() {
  const g = id => +document.getElementById(id).value;
  const u = g('c-u'), s = g('c-s'), a = g('c-a'), h = g('c-h'), r = g('c-r'), share = g('c-p') / 100;
  $('#o-u').textContent = u; $('#o-s').textContent = s; $('#o-a').textContent = a.toLocaleString('en-US'); $('#o-h').textContent = h.toFixed(2).replace(/0$/, ''); $('#o-r').textContent = '$' + r; $('#o-p').textContent = Math.round(share * 100) + '%';
  const eligible = PLANS.filter(p => p.price && u <= p.users && s <= p.src);
  const out = $('#calc-r');
  const manual = a * share * h * r;
  if (!eligible.length) {
    out.innerHTML = `<div class="rec">Recommended plan</div><div class="tot">Enterprise</div><p style="color:#cbd5e1;margin:0">Your team size or number of data sources is above the Scale plan. We will price it to your volume.</p><a class="btn btn-primary" href="/app">Talk to sales ${icon('arrow')}</a>`; return;
  }
  const priced = eligible.map(p => ({ p, base: p.price * disc(), over: Math.max(0, a - p.ans) / 1000 * 20 })).map(x => ({ ...x, total: x.base + x.over })).sort((x, y) => x.total - y.total)[0];
  const saving = Math.max(manual - priced.total, 0), hours = a * share * h, perAns = priced.total / a, max = Math.max(manual, priced.total, 1);
  out.innerHTML = `<div class="rec">Recommended: ${priced.p.name} plan</div><div class="tot"><span id="t-tot"></span><small> /month${annual ? ', billed annually' : ''}</small></div>
    <div class="rows"><div><span>${priced.p.name} plan (${priced.p.ans.toLocaleString('en-US')} answers included)</span><b>${fmt$(priced.base)}</b></div>
      <div><span>Extra answers (${Math.max(0, a - priced.p.ans).toLocaleString('en-US')} at $0.02)</span><b>${fmt$(priced.over)}</b></div>
      <div><span>Effective cost per answer</span><b id="t-per"></b></div></div>
    <div class="cmp"><div class="l"><span>Answering by hand</span><b>${fmt$(manual)} /mo</b></div><div class="bar2" style="width:${manual / max * 100}%;background:#f87171"></div>
      <div class="l"><span>With AskShelf</span><b>${fmt$(priced.total)} /mo</b></div><div class="bar2" style="width:${priced.total / max * 100}%;background:#34d399;margin-bottom:0"></div></div>
    <div class="save"><span id="t-sav"></span> a month back &middot; <span id="t-hrs"></span> analyst hours</div>
    <p style="margin:0;color:#94a3b8;font-size:.78rem">Assumes ${Math.round(share * 100)}% of answers would otherwise need an analyst, at ${h} hour(s) and $${r}/hour. These are your numbers, not ours.</p>
    <a class="btn btn-primary" href="/app">Start free trial ${icon('arrow')}</a>`;
  tween('t-tot', priced.total, v => fmt$(v)); tween('t-per', perAns, v => '$' + v.toFixed(2)); tween('t-sav', saving, v => fmt$(v)); tween('t-hrs', hours, v => Math.round(v).toLocaleString('en-US'));
}
['c-u', 'c-s', 'c-a', 'c-h', 'c-r', 'c-p'].forEach(id => document.getElementById(id).addEventListener('input', calc));
calc();

