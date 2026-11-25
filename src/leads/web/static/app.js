'use strict';
const $ = (id) => document.getElementById(id);
const main = $('main'), panel = $('panel'), scrim = $('scrim'), tip = $('tip');
const nf = new Intl.NumberFormat('en');
const num = (n) => nf.format(n || 0);
const pct = (a, b) => (b ? Math.round((a / b) * 100) + '%' : '0%');
const regions = new Intl.DisplayNames(['en'], { type: 'region' });
const country = (c) => { try { return c ? regions.of(c) : 'Unknown'; } catch { return c; } };
const day = (s) => (s ? new Date(s).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' }) : '');
const when = (s) => (s ? new Date(s).toLocaleString('en-GB', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : '');
const SOURCE = { overture: 'Overture Maps', foursquare: 'Foursquare', companies_house: 'Companies House (UK)', osm: 'OpenStreetMap', sirene: 'SIRENE (France)', brreg: 'Brønnøysund (Norway)', abn: 'ABN (Australia)', canada: 'Corporations Canada', directories: 'Directories', registry: 'Company registry', wayback: 'Web archive' };
const WEB = { working: 'Working', broken: 'Broken', parked: 'Parked', redirect_social: 'Social page only', none: 'No website found', unsure: 'Could not tell' };
const webState = (r) => (r.website_status ? WEB[r.website_status] : r.website_checked_at ? WEB.unsure : 'Not checked');
const WEB_TONE = { working: 'green', broken: 'red', parked: 'orange', redirect_social: 'violet', none: 'blue', unsure: 'gray' };
const GROUP_TONE = { food_drink: 'orange', beauty_wellness: 'violet', health_medical: 'teal', retail: 'blue', automotive: 'gray', fitness_sports: 'green', hospitality: 'teal', pets: 'orange', education: 'blue', real_estate: 'green', professional_services: 'violet', events_entertainment: 'red', trades_construction: 'orange', home_services: 'teal' };
const ICON = {
  all: 'M4 21V8l8-5 8 5v13M9 21v-6h6v6', date: 'M5 5h14v15H5zM5 10h14M9 3v4M15 3v4', mail: 'M3 6h18v12H3zM3 7l9 6 9-6',
  phone: 'M6 3h4l2 5-2.500 2a12 12 0 005 5l2-2.500 5 2v4c0 1-1 2-2 2A17 17 0 014 5c0-1 1-2 2-2z', star: 'M12 3l2.800 5.700 6.200.900-4.500 4.400 1 6.200L12 17.300 6.500 20.200l1-6.200L3 9.600l6.200-.900z',
};
function icon(d) {
  const ns = 'http://www.w3.org/2000/svg', svg = document.createElementNS(ns, 'svg'), path = document.createElementNS(ns, 'path');
  for (const [k, v] of Object.entries({ viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.8', 'stroke-linejoin': 'round', 'stroke-linecap': 'round', 'aria-hidden': 'true' })) svg.setAttribute(k, v);
  path.setAttribute('d', d); svg.append(path); return svg;
}
const chip = (text, tone) => h('span', { class: 'chip t-' + (tone || 'gray') }, text);
const webChip = (r) => (r.website_status || r.website_checked_at ? chip(webState(r), WEB_TONE[r.website_status || 'unsure']) : h('small', {}, 'Not checked'));
const src = (s) => SOURCE[s] || s;
let opts = null;

// All page text goes in as text nodes: lead data comes from outside sources.
function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'class') el.className = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat(Infinity)) if (kid !== null && kid !== undefined && kid !== false) el.append(kid);
  return el;
}
async function api(path, init) {
  const r = await fetch('/api/' + path, init);
  if (!r.ok) {
    let msg = 'The dashboard could not load this. Check that the database is running.';
    try { const d = (await r.json()).detail; if (typeof d === 'string') msg = d; } catch { /* not JSON */ }
    throw new Error(msg);
  }
  return r.json();
}
function safeLink(url, label) {
  let u;
  try { u = new URL(/^https?:/i.test(url) ? url : 'https://' + url); } catch { return label || url; }
  if (!/^https?:$/.test(u.protocol)) return label || url;
  return h('a', { class: 'ext', href: u.href, target: '_blank', rel: 'noopener noreferrer' }, label || url);
}
function hover(el, text) {
  el.addEventListener('mousemove', (e) => {
    tip.textContent = text; tip.hidden = false;
    tip.style.left = Math.min(e.clientX + 12, innerWidth - tip.offsetWidth - 8) + 'px';
    tip.style.top = e.clientY - 32 + 'px';
  });
  el.addEventListener('mouseleave', () => { tip.hidden = true; });
  return el;
}
const head = (title, text) => [h('h1', {}, title), h('p', { class: 'lede' }, text)];
const empty = (text) => h('div', { class: 'card empty' }, text);
function table(cols, rows) {
  return h('div', { class: 'tablewrap' }, h('table', {},
    h('thead', {}, h('tr', {}, cols.map((c) => h('th', { class: c.num && 'num', scope: 'col' }, c.label)))),
    h('tbody', {}, rows.map((r) => h('tr', {}, cols.map((c) => h('td', { class: c.num && 'num' }, c.get(r))))))));
}

// ---- overview
function hbars(rows, label, go, tone) {
  const max = Math.max(1, ...rows.map((r) => r.total));
  return h('div', { class: 'hbars' }, rows.map((r) => {
    const bar = h('i'); bar.style.width = (r.total / max) * 75 + '%';
    return hover(h('div', { class: 'hrow' + (tone ? ' t-' + tone(r) : ''), onclick: () => go(r) },
      h('button', { class: 'lab' }, label(r)), h('div', { class: 'track' }, bar, h('span', {}, num(r.total)))),
      label(r) + ': ' + num(r.total) + ' leads');
  }));
}
function columns(rows, go) {
  const max = Math.max(1, ...rows.map((r) => r.total));
  return [h('div', { class: 'cols' }, rows.map((r) => {
    const bar = h('i'); bar.style.height = (r.total / max) * 85 + '%';
    return hover(h('button', { class: 'col', 'aria-label': r.key + ': ' + num(r.total) + ' leads', onclick: () => go(r) },
      h('span', {}, num(r.total)), bar), r.key + ': ' + num(r.total) + ' leads');
  })), h('div', { class: 'colx', 'aria-hidden': 'true' }, rows.map((r) => h('span', {}, r.key)))];
}
async function overviewPage() {
  const d = await api('overview'), t = d.totals;
  const go = (f) => { location.hash = 'leads?' + new URLSearchParams(f); };
  const stat = (value, label, note, f, tone, ico) => h('button', { class: 'stat t-' + tone, onclick: () => go(f) }, h('span', { class: 'ico' }, icon(ICON[ico])), h('b', {}, num(value)), h('span', {}, label), h('small', {}, note));
  return [
    ...head('Overview', 'Every business collected so far. Click a number or a bar to see those leads.'),
    t.checked < t.total && h('div', { class: 'notice' }, num(t.checked) + ' of ' + num(t.total) + ' leads have had their website checked. Until then, “no website listed” means no source gave one, not that the business has none.'),
    h('div', { class: 'stats' },
      stat(t.total, 'All leads', d.countries.length + ' countries', {}, 'blue', 'all'),
      stat(t.dated, 'Opening date known', pct(t.dated, t.total) + ' of all', { age: 'known' }, 'violet', 'date'),
      stat(t.email, 'Have an email', pct(t.email, t.total) + ' of all', { email: 'yes' }, 'teal', 'mail'),
      stat(t.phone, 'Have a phone', pct(t.phone, t.total) + ' of all', { phone: 'yes' }, 'orange', 'phone'),
      stat(t.best, 'Best leads', 'Date, email, no website', { age: 'known', email: 'yes', website: 'not_listed' }, 'green', 'star')),
    h('div', { class: 'grid2' },
      h('div', { class: 'card' }, h('h2', {}, 'Leads by opening year (date known)'), columns(d.years, (r) => go({ year: r.key }))),
      h('div', { class: 'card' }, h('h2', {}, 'Leads by country'), table([
        { label: 'Country', get: (r) => h('button', { class: 'linkbtn', onclick: () => go({ country: r.key }) }, country(r.key)) },
        { label: 'Leads', num: true, get: (r) => num(r.total) },
        { label: 'Dated', num: true, get: (r) => num(r.dated) },
        { label: 'Email', num: true, get: (r) => num(r.email) },
        { label: 'Best', num: true, get: (r) => num(r.best) }], d.countries),
        h('h2', { class: 'bar' }, 'Outreach'), h('div', { class: 'outs' },
          [['Drafted', d.outreach.drafted, 'blue'], ['Sent', d.outreach.sent, 'violet'], ['Replied', d.outreach.replied, 'green'], ['Bounced', d.outreach.bounced, 'orange'], ['Opted out', d.opted_out, 'gray']]
            .map(([label, n, tone]) => h('div', { class: 't-' + tone }, h('b', {}, num(n)), h('span', {}, label))))),
      h('div', { class: 'card t-teal' }, h('h2', {}, 'Leads by type of business'), hbars(d.categories, (r) => opts.categories[r.key] || r.key || 'Unknown', (r) => go({ category: r.key }))),
      d.websites.length > 0 && h('div', { class: 'card' }, h('h2', {}, 'Website check results'), hbars(d.websites, (r) => WEB[r.key] || r.key, (r) => go({ website: r.key }), (r) => WEB_TONE[r.key])),
      h('div', { class: 'card t-violet' }, h('h2', {}, 'Top cities'), hbars(d.cities, (r) => r.key, (r) => go({ city: r.key })))),
  ];
}

// ---- leads
const PRESETS = [
  ['All leads', {}],
  ['Best leads', { age: 'known', email: 'yes', website: 'not_listed' }],
  ['Opening date known', { age: 'known' }],
  ['Has email', { email: 'yes' }],
  ['Email and phone', { email: 'yes', phone: 'yes' }],
];
function leadsPage(params) {
  const f = Object.fromEntries(params);
  const set = (change) => {
    const next = { ...f, ...change };
    if (!('page' in change)) delete next.page;
    for (const k of Object.keys(next)) if (next[k] === '') delete next[k];
    location.hash = 'leads?' + new URLSearchParams(next);
  };
  const pick = (key, label, choices) => h('label', { class: 'f' }, label, h('select', { onchange: (e) => set({ [key]: e.target.value }) },
    h('option', { value: '' }, 'Any'), choices.map(([v, t]) => h('option', { value: v, selected: f[key] === String(v) }, t))));
  const field = (key, label, type) => h('label', { class: 'f' }, label, h('input', { type, value: f[key] || '', onchange: (e) => set({ [key]: e.target.value }), onkeydown: (e) => { if (e.key === 'Enter') e.target.blur(); } }));
  const yes = [['yes', 'Yes'], ['no', 'No']];
  const { page, sort, ...only } = f;
  const same = (a) => JSON.stringify(Object.entries(a).sort()) === JSON.stringify(Object.entries(only).sort());
  const out = h('div', { 'aria-live': 'polite' }, h('p', { class: 'muted' }, 'Loading leads…'));
  api('leads?' + new URLSearchParams(f)).then((d) => out.replaceChildren(...leadTable(d, f, set)), (e) => out.replaceChildren(h('p', { class: 'err' }, e.message)));
  return [
    ...head('Leads', 'Click a lead for its details, or its email to prepare a message.'),
    h('div', { class: 'presets' }, PRESETS.map(([label, p]) => h('button', { class: 'btn', 'aria-pressed': String(same(p)), onclick: () => { location.hash = 'leads?' + new URLSearchParams(p); } }, label))),
    h('div', { class: 'filters' },
      field('q', 'Search name, email, phone or postcode', 'search'),
      pick('country', 'Country', opts.countries.map((c) => [c, country(c)])),
      pick('category', 'Type of business', Object.entries(opts.categories)),
      pick('website', 'Website', [['not_listed', 'None listed'], ['listed', 'Listed'], ['unchecked', 'Not checked yet'], ...Object.entries(WEB)]),
      h('label', { class: 'f' }, 'Sort by', h('select', { onchange: (e) => set({ sort: e.target.value }) },
        [['newest', 'Newest first'], ['oldest', 'Oldest first'], ['name', 'Name'], ['city', 'City']].map(([v, t]) => h('option', { value: v, selected: (sort || 'newest') === v }, t)))),
      h('details', { class: 'more', open: ['city', 'year', 'age', 'email', 'phone', 'source'].some((k) => f[k]) && !PRESETS.some(([, p]) => same(p)) },
        h('summary', {}, 'More filters'), h('div', { class: 'filters' },
          field('city', 'City', 'text'),
          pick('year', 'Opened in', opts.years.map((y) => [y, y])),
          pick('age', 'Opening date', [['known', 'Known'], ['unknown', 'Unknown']]),
          pick('email', 'Email', yes), pick('phone', 'Phone', yes),
          pick('source', 'Source', opts.sources.map((s) => [s, src(s)])),
          h('button', { class: 'btn', onclick: () => { location.hash = 'leads'; } }, 'Clear all')))),
    out,
  ];
}
function siteCell(r) {
  if (!r.website) return h('span', { class: 'muted' }, 'None listed');
  return h('span', { class: 'clip' }, safeLink(r.website, r.domain || r.website));
}
function leadTable(d, f, set) {
  if (!d.total) return [empty('No leads match these filters. Remove one to see more.')];
  const pages = Math.ceil(d.total / d.page_size);
  const { page, ...rest } = f;
  const pager = () => h('div', { class: 'bar' },
    h('span', {}, num((d.page - 1) * d.page_size + 1) + '–' + num(Math.min(d.page * d.page_size, d.total)) + ' of ' + num(d.total) + ' leads'),
    h('span', {}, h('button', { class: 'btn', disabled: d.page <= 1, onclick: () => set({ page: d.page - 1 }) }, 'Previous'), ' ',
      h('button', { class: 'btn', disabled: d.page >= pages, onclick: () => set({ page: d.page + 1 }) }, 'Next'), ' ',
      h('a', { class: 'btn', href: '/api/leads.csv?' + new URLSearchParams(rest), download: 'leads.csv' }, 'Download CSV')));
  return [pager(), h('div', { class: 'tablewrap' }, h('table', {},
    h('thead', {}, h('tr', {}, ['Business', 'Location', 'Opened', 'Email', 'Phone', 'Website'].map((t) => h('th', { scope: 'col' }, t)))),
    h('tbody', {}, d.leads.map((r) => h('tr', { class: 'click', onclick: (e) => { if (!e.target.closest('a,button')) openLead(r.lead_id); } },
      h('td', { class: 'name' }, h('button', { class: 'linkbtn strong', onclick: () => openLead(r.lead_id) }, r.name), h('div', {}, chip(opts.categories[r.category_group] || 'Other', GROUP_TONE[r.category_group]))),
      h('td', {}, r.city || h('span', { class: 'muted' }, '—'), h('small', {}, country(r.country_iso2))),
      h('td', {}, r.created_date ? [day(r.created_date), h('small', {}, src(r.age_source))] : h('span', { class: 'muted' }, 'Unknown')),
      h('td', {}, r.email ? h('button', { class: 'linkbtn clip', title: 'Email ' + r.email, onclick: () => openEmail(r.lead_id, r.name) }, r.email) : h('span', { class: 'muted' }, '—')),
      h('td', {}, r.phone_e164 || h('span', { class: 'muted' }, '—')),
      h('td', {}, siteCell(r), h('div', {}, webChip(r)))))))), pager()];
}

// ---- side panel
let returnTo = null;
function showPanel(...kids) {
  if (panel.hidden) returnTo = document.activeElement;
  panel.replaceChildren(...kids.flat());
  panel.hidden = scrim.hidden = false;
  panel.querySelector('button')?.focus();
}
function closePanel() {
  if (panel.hidden) return;
  panel.hidden = scrim.hidden = true;
  if (returnTo?.isConnected) returnTo.focus();
}
scrim.addEventListener('click', closePanel);
document.addEventListener('keydown', (e) => {
  if (panel.hidden) return;
  if (e.key === 'Escape') closePanel();
  if (e.key === 'Tab') {
    const list = [...panel.querySelectorAll('button:not(:disabled),a[href],input')];
    const edge = e.shiftKey ? list[0] : list.at(-1);
    if (document.activeElement === edge) { e.preventDefault(); (e.shiftKey ? list.at(-1) : list[0]).focus(); }
  }
});
const panelHead = (title, sub) => h('header', {}, h('div', {}, h('h1', { id: 'panel-title' }, title), sub && h('span', { class: 'muted' }, sub)), h('button', { class: 'btn', onclick: closePanel }, 'Close'));
let panelRequest = 0;
async function loadPanel(title, path, build) {
  const mine = ++panelRequest;
  showPanel(panelHead(title), h('p', { class: 'muted' }, 'Loading…'));
  try { const d = await api(path); if (mine === panelRequest && !panel.hidden) showPanel(build(d)); }
  catch (e) { if (mine === panelRequest) showPanel(panelHead(title), h('p', { class: 'err' }, e.message)); }
}
function openLead(id) {
  return loadPanel('Lead', 'leads/' + encodeURIComponent(id), (d) => {
    const row = (label, value) => value && [h('dt', {}, label), h('dd', {}, value)];
    const socials = Object.entries(d.socials || {}).filter(([, v]) => typeof v === 'string');
    return [
      panelHead(d.name, [opts.categories[d.category_group], d.city, country(d.country_iso2)].filter(Boolean).join(', ')),
      d.suppressed && h('div', { class: 'notice' }, 'This contact has opted out. Do not contact them.'),
      d.email && h('button', { class: 'btn primary', onclick: () => openEmail(d.lead_id, d.name) }, 'Send email'),
      h('h2', {}, 'Contact'), h('dl', {},
        row('Email', d.email || 'None found'), row('Phone', d.phone_e164 || 'None found'),
        row('WhatsApp', d.whatsapp_number), row('Address', [d.address, d.city, d.region, d.postcode].filter(Boolean).join(', ')),
        row('Social pages', socials.length && socials.map(([k, v], i) => [i ? ', ' : '', safeLink(v, k)]))),
      h('h2', {}, 'Website'), h('dl', {}, row('Address', d.website ? safeLink(d.website) : 'None listed'), row('Status', webChip(d)), row('Reason', d.website_reason), row('Checked', when(d.website_checked_at))),
      h('h2', {}, 'Business'), h('dl', {}, row('Category', d.category),
        row('Opened', d.created_date ? day(d.created_date) + ' (from ' + src(d.age_source) + ')' : 'Unknown')),
      h('h2', {}, 'Where this came from'), h('dl', {}, d.source_records.map((s) => row(src(s.source), 'Record ' + s.source_record_id + ', collected ' + day(s.fetched_at)))),
      d.outreach.length > 0 && [h('h2', { class: 'bar' }, 'Outreach'), h('dl', {}, d.outreach.map((o) => row(o.stage, 'Drafted ' + when(o.drafted_at) + (o.sent_at ? ', sent ' + when(o.sent_at) : ''))))],
    ].flat(2).filter(Boolean);
  });
}
const post = (path, body) => api(path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'leads' }, body: body && JSON.stringify(body) });
const GMAIL_DRAFTS = 'https://mail.google.com/mail/#drafts';
function openEmail(id, name) {
  const path = 'leads/' + encodeURIComponent(id);
  return loadPanel('Email ' + name, path + '/email', (d) => {
    const view = h('button', { class: 'btn', onclick: () => openLead(id) }, 'View lead');
    const top = [panelHead('Email ' + name, d.to || 'No email address')];
    if (d.drafted) return [...top, h('p', {}, 'A draft has been saved for this lead. Review and send it in Gmail.'), h('a', { class: 'btn primary', href: GMAIL_DRAFTS, target: '_blank', rel: 'noopener noreferrer' }, 'Open Gmail drafts'), ' ', view];
    if (d.blockers.length) return [...top, h('div', { class: 'notice' }, 'This lead cannot be emailed:', h('ul', {}, d.blockers.map((b) => h('li', {}, b)))), view];
    const msg = h('p', { role: 'status' });
    const subject = h('input', { type: 'text', id: 'mail-subject' });
    const body = h('textarea', { id: 'mail-body', rows: 18 });
    const say = (text, bad) => { msg.textContent = text; msg.className = bad ? 'err' : 'muted'; };
    const fill = (e) => { subject.value = e.subject; body.value = e.body; form.hidden = false; writeBtn.textContent = 'Write again'; writeBtn.className = 'btn'; };
    const busy = (on) => { writeBtn.disabled = on || !d.ai; saveBtn.disabled = on || !d.gmail; };
    const writeBtn = h('button', { class: 'btn primary', disabled: !d.ai, onclick: async () => {
      const again = !form.hidden;
      busy(true); say('Writing the email. This takes a few seconds.');
      try { fill(await post(path + '/email' + (again ? '?again=true' : ''))); say('Check the text, and change anything you like.'); } catch (e) { say(e.message, true); }
      busy(false);
    } }, 'Write email with AI');
    const saveBtn = h('button', { class: 'btn primary', disabled: !d.gmail, onclick: async () => {
      busy(true); say('Saving to Gmail…');
      try {
        await post(path + '/draft', { subject: subject.value, body: body.value });
        form.replaceChildren(h('p', {}, 'Saved. The email is now in your Gmail drafts. Nothing has been sent.'), h('a', { class: 'btn primary', href: GMAIL_DRAFTS, target: '_blank', rel: 'noopener noreferrer' }, 'Open Gmail drafts'));
        writeBtn.hidden = true; say('');
      } catch (e) { say(e.message, true); busy(false); }
    } }, 'Save to Gmail drafts');
    const form = h('div', { class: 'compose', hidden: true }, h('label', { class: 'f' }, 'Subject', subject), h('label', { class: 'f' }, 'Email', body), saveBtn);
    if (d.email) fill(d.email);
    return [...top,
      d.warnings.length > 0 && h('div', { class: 'notice' }, 'Before sending:', h('ul', {}, d.warnings.map((w) => h('li', {}, w)))),
      !d.ai && h('div', { class: 'notice' }, 'AI writing is not connected: add GROQ_API_KEY to .env and restart the dashboard.'),
      !d.gmail && h('div', { class: 'notice' }, 'Gmail is not connected yet, so drafts cannot be saved. See Setup.'),
      msg, form, h('p', {}, writeBtn, ' ', view)].filter(Boolean);
  });
}

// ---- other pages
async function draftsPage() {
  const rows = await api('outreach');
  return [...head('Email drafts', 'Drafts waiting in Gmail, and what happened after you sent them.'),
    rows.length ? table([
      { label: 'Business', get: (r) => h('button', { class: 'linkbtn strong', onclick: () => openLead(r.lead_id) }, r.name) },
      { label: 'To', get: (r) => r.recipient || r.email || '—' }, { label: 'Stage', get: (r) => chip({ first: 'First email', followup: 'Follow-up', replied: 'Replied', opted_out: 'Opted out' }[r.stage] || r.stage, { first: 'blue', followup: 'violet', replied: 'green' }[r.stage]) },
      { label: 'Drafted', get: (r) => when(r.drafted_at) || '—' }, { label: 'Sent', get: (r) => when(r.sent_at) || '—' },
      { label: 'Replied', get: (r) => when(r.replied_at) || '—' }, { label: 'Follow-up due', get: (r) => day(r.followup_due_at) || '—' },
    ], rows) : empty('No drafts yet. They appear here once Gmail is connected and the first emails are prepared.')];
}
async function optoutsPage() {
  const rows = await api('suppression');
  const input = h('input', { type: 'text', name: 'contact', required: true, placeholder: 'name@example.com or +44…' });
  const msg = h('p', { role: 'status', class: 'err' });
  const form = h('form', { class: 'filters', onsubmit: async (e) => {
    e.preventDefault();
    try {
      await api('suppression', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'leads' }, body: JSON.stringify({ contact: input.value }) });
      show();
    } catch (err) { msg.textContent = err.message; }
  } }, h('label', { class: 'f' }, 'Email or phone', input), h('button', { class: 'btn primary' }, 'Add opt-out'));
  return [...head('Opt-outs', 'People here are never contacted again. Adding one is permanent.'), form, msg,
    rows.length ? table([{ label: 'Contact', get: (r) => r.email_or_phone }, { label: 'Reason', get: (r) => ({ manual: 'Added by hand', opt_out: 'Asked to stop', hard_bounce: 'Address does not exist' })[r.reason] || r.reason }, { label: 'Added', get: (r) => when(r.added_at) }], rows) : empty('Nobody has opted out.')];
}
async function sourcesPage() {
  const [d, s] = await Promise.all([api('overview'), api('setup')]);
  const have = Object.fromEntries(d.sources.map((r) => [r.key, r]));
  const names = [...new Set([...s.sources.map((x) => x.name), ...Object.keys(have)])];
  names.sort((a, b) => (have[b]?.records || 0) - (have[a]?.records || 0));
  return [...head('Sources', 'Where the leads come from. One lead can come from several sources.'),
    table([{ label: 'Source', get: (n) => src(n) }, { label: 'Records in leads', num: true, get: (n) => num(have[n]?.records) },
      { label: 'Last collected', get: (n) => when(have[n]?.last_fetched) || '—' },
      { label: 'Status', get: (n) => (have[n] ? h('span', { class: 'tag ok' }, 'Loaded') : h('span', { class: 'tag' }, 'No leads yet')) }], names),
    h('h2', { class: 'bar' }, 'Latest runs'),
    table([{ label: 'Run', get: (r) => JOBS[r.flow]?.[0] || 'Data load' }, { label: 'Status', get: (r) => h('span', { class: r.status === 'success' ? 'tag ok' : 'tag bad' }, r.status === 'success' ? 'OK' : r.status) }, { label: 'Started', get: (r) => when(r.started_at) },
      { label: 'New', num: true, get: (r) => num((r.counts.process || r.counts).new) }, { label: 'Updated', num: true, get: (r) => num((r.counts.process || r.counts).updated) },
      { label: 'Too old', num: true, get: (r) => num((r.counts.process || r.counts).too_old) }, { label: 'Not a business', num: true, get: (r) => num((r.counts.process || r.counts).not_business) }], d.runs)];
}
const JOBS = {
  'gmail-sync': ['Gmail sync', 'Finds sent, replied, bounced and opted-out emails; prepares follow-up drafts'],
  'daily-drafts': ['Daily check', 'Checks websites and opening dates'],
  'monthly-collect': ['Monthly collect', 'Collects new businesses and closes missing ones'],
  'backup-check': ['Backup check', 'Makes sure last night’s backup exists'],
};
function cronText(cron) {
  const [m, hr, dom] = cron.split(' ');
  if (m.startsWith('*/')) return 'Every ' + m.slice(2) + ' minutes';
  const at = hr.padStart(2, '0') + ':' + m.padStart(2, '0');
  return dom === '*' ? 'Every day at ' + at : 'Monthly, day ' + dom + ' at ' + at;
}
async function setupPage() {
  const d = await api('setup');
  const item = (name, ok, text) => ({ name, ok, text });
  return [...head('Setup', 'What must be connected before emails can be drafted.'),
    table([{ label: 'Item', get: (r) => r.name }, { label: 'Status', get: (r) => h('span', { class: r.ok ? 'tag ok' : 'tag warn' }, r.ok ? 'Ready' : 'Not ready') }, { label: 'Details', get: (r) => r.text }], [
      item('Website checks', d.checked >= d.with_website && d.checked > 0, num(d.checked) + ' leads checked, ' + num(d.with_website) + ' list a website'),
      item('Gmail', d.state.gmail, d.state.gmail ? 'Connected' : 'Sign in with the Google account that should hold the drafts'),
      item('AI writing', d.state.ai, d.state.ai ? 'Connected. Writes a different email for each business' : 'Add GROQ_API_KEY to .env'),
      item('Sender details', d.state.sender, d.warnings.length ? d.warnings.join(' ') : 'Complete'),
    ]),
    h('h2', { class: 'bar' }, 'Automatic schedule'),
    table([{ label: 'Task', get: (r) => JOBS[r.name][0] }, { label: 'What it does', get: (r) => JOBS[r.name][1] }, { label: 'When (' + d.timezone + ')', get: (r) => cronText(r.cron) },
      { label: 'Last run', get: (r) => (r.last ? when(r.last.started_at) : 'Not yet') },
      { label: 'Result', get: (r) => (r.last ? h('span', { class: { success: 'tag ok', failed: 'tag bad' }[r.last.status] || 'tag warn' }, { success: 'OK', failed: 'Failed', running: 'Running' }[r.last.status] || r.last.status) : '—') }], d.schedule),
    h('h2', { class: 'bar' }, 'Current rules'), h('div', { class: 'card' }, h('dl', {},
      h('dt', {}, 'Opened since'), h('dd', {}, day(d.since)), h('dt', {}, 'Unknown dates'), h('dd', {}, d.include_unknown_age ? 'Included' : 'Excluded'),
      h('dt', {}, 'Monthly countries'), h('dd', {}, d.countries.length ? d.countries.map(country).join(', ') : 'All'), h('dt', {}, 'Automatic drafts'), h('dd', {}, d.auto_drafts ? 'On' : 'Off, made one by one here'), h('dt', {}, 'Drafts per day'), h('dd', {}, num(d.drafts_per_day)), h('dt', {}, 'Follow-up after'), h('dd', {}, d.followup_after_days + ' days')))];
}

const PAGES = { overview: overviewPage, leads: leadsPage, drafts: draftsPage, optouts: optoutsPage, sources: sourcesPage, setup: setupPage };
let request = 0;
let current = null;
async function show() {
  const mine = ++request;
  const [name, query] = location.hash.slice(1).split('?');
  const key = PAGES[name] ? name : 'overview';
  closePanel(); tip.hidden = true;
  for (const a of document.querySelectorAll('#nav a')) a.hash === '#' + key ? a.setAttribute('aria-current', 'page') : a.removeAttribute('aria-current');
  if (key !== current) main.replaceChildren(h('p', { class: 'muted' }, 'Loading…'));
  try {
    opts ??= await api('options');
    const kids = await PAGES[key](new URLSearchParams(query || ''));
    if (mine !== request) return;
    main.replaceChildren(...kids.flat(Infinity).filter(Boolean));
    if (key !== current) scrollTo(0, 0);
    current = key;
  } catch (e) { if (mine === request) main.replaceChildren(h('p', { class: 'err' }, e.message), h('button', { class: 'btn', onclick: show }, 'Try again')); }
}
addEventListener('hashchange', show);
show();
