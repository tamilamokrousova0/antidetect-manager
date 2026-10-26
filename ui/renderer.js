const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

let profiles = [];
let states = {};          // id -> 'starting' | 'running'
let proxyStates = {};     // id -> 'down' | 'up' (from the forwarder, only while running)
const STATUS_LABEL = { warmup: 'Прогрев', active: 'Рабочий', checkpoint: 'Чекпоинт', banned: 'Бан' };
let selected = new Set();
let editing = null;       // profile id in the drawer, or 'new'
let removeProxy = false;

// ipcRenderer wraps errors as "Error invoking remote method 'x': Error: msg"
const errText = (e) => String(e?.message || e).replace(/^Error invoking remote method '[^']+': (Error: )?/, '');

function status(text, bad = false) {
  const el = $('#status');
  el.textContent = text || '';
  el.className = bad ? 'bad' : 'muted';
}

async function busy(label, fn) {
  status(label);
  try {
    const r = await fn();
    status('');
    return r;
  } catch (e) {
    status(errText(e), true);
    throw e;
  }
}

function ago(ts) {
  if (!ts) return '—';
  const s = Date.now() / 1000 - ts;
  if (s < 60) return 'только что';
  if (s < 3600) return `${Math.floor(s / 60)} мин назад`;
  if (s < 86400) return `${Math.floor(s / 3600)} ч назад`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)} дн назад`;
  return new Date(ts * 1000).toLocaleDateString('ru-RU');
}

function proxyCell(p) {
  if (!p.proxy) return '<span class="warn">нет</span>';
  const m = p.proxy.match(/^(\w+):\/\/(?:[^@]*@)?(.+)$/);
  return `<span class="mono">${esc(m ? m[2] : p.proxy)}</span>${m && m[1] !== 'socks5' ? ` <span class="muted">${esc(m[1])}</span>` : ''}`;
}

function exitCell(p) {
  if (!p.proxy) return '<span class="warn">реальный IP этого Mac</span>';
  const c = p.proxy_check;
  if (!c) return '<span class="muted">не проверен</span>';
  if (!c.ok) return `<span class="bad" title="${esc(c.raw_error || c.error)}">${esc(c.error)}</span>`;
  const flag = c.flag_hosting ? ' <span class="warn" title="ip-api помечает IP как датацентр/хостинг">ДЦ</span>' : '';
  const stale = c.stale ? ` <span class="warn" title="Гео-сервисы были недоступны при запуске, использована прошлая проверка: ${esc(c.stale_reason || '')}">кэш</span>` : '';
  return `<span title="${esc(c.city)} · ${esc(c.isp)} · ${esc(c.provider || 'ip-api.com')}">${esc(c.country)}</span> <span class="mono">${esc(c.ip)}</span> ${flag}${stale} <span class="muted">${c.latency_ms} мс · ${esc(c.city || '')}</span>`;
}

function matches(p, q, tag, status) {
  if (tag && !p.tags.includes(tag)) return false;
  if (status === 'none' ? p.status : (status && p.status !== status)) return false;
  if (!q) return true;
  const c = p.proxy_check || {};
  return [p.name, p.notes, p.id, ...p.tags, c.ip, c.country, c.city, p.proxy, STATUS_LABEL[p.status] || ''].join(' ').toLowerCase().includes(q);
}

function selfCheckDot(p) {
  const s = p.self_check;
  if (!s) return '';
  const title = s.level === 'ok'
    ? `Самопроверка после запуска: WebRTC = IP прокси, язык и таймзона совпадают (${ago(s.checked_at)})`
    : `Самопроверка (${ago(s.checked_at)}): ${(s.problems || []).join('; ') || s.error || ''}`;
  return `<span class="sc sc-${esc(s.level)}" title="${esc(title)}"></span>`;
}

function render() {
  const q = $('#search').value.trim().toLowerCase();
  const tag = $('#tagFilter').value;
  const status = $('#statusFilter').value;
  const visible = profiles.filter((p) => matches(p, q, tag, status));

  $('#count').textContent = profiles.length ? `${profiles.length}` : '';
  $('#empty').hidden = profiles.length > 0;
  $('#table').hidden = profiles.length === 0;

  $('#rows').innerHTML = visible.map((p) => {
    const st = states[p.id];
    const act = st === 'running'
      ? (proxyStates[p.id] === 'down'
        ? `<button class="proxy-down" data-stop="${p.id}" title="Прокси перестал отвечать: соединения из браузера не проходят">Прокси ✕ · Закрыть</button>`
        : `<button class="running" data-stop="${p.id}" title="Браузер открыт">Закрыть</button>`)
      : st === 'starting'
        ? `<button disabled>Запуск…</button>`
        : `<button data-launch="${p.id}">Запустить</button>`;
    return `<tr data-id="${p.id}" class="${selected.has(p.id) ? 'selected' : ''} ${editing === p.id ? 'active' : ''}">
      <td class="c-check"><input type="checkbox" data-sel="${p.id}" ${selected.has(p.id) ? 'checked' : ''}></td>
      <td>${p.status ? `<span class="st st-${esc(p.status)}" title="${esc(STATUS_LABEL[p.status] || p.status)}"></span>` : ''}<span class="name">${esc(p.name)}</span>${p.notes ? `<span class="note">${esc(p.notes)}</span>` : ''}</td>
      <td>${p.tags.map((t) => `<span class="tag">${esc(t)}</span>`).join('')}</td>
      <td>${proxyCell(p)}</td>
      <td>${selfCheckDot(p)}${exitCell(p)}</td>
      <td class="muted">${ago(p.last_used_at)}</td>
      <td class="c-act">${act}</td>
    </tr>`;
  }).join('');

  const allTags = [...new Set(profiles.flatMap((p) => p.tags))].sort();
  const tf = $('#tagFilter');
  const cur = tf.value;
  tf.innerHTML = '<option value="">Все теги</option>' + allTags.map((t) => `<option ${t === cur ? 'selected' : ''}>${esc(t)}</option>`).join('');

  $('#selAll').checked = visible.length > 0 && visible.every((p) => selected.has(p.id));
  $('#selection').hidden = selected.size === 0;
  $('#selCount').textContent = `Выбрано: ${selected.size}`;
}

async function refresh() {
  profiles = await window.adm.list();
  const ids = new Set(profiles.map((p) => p.id));
  selected = new Set([...selected].filter((id) => ids.has(id)));
  render();
  if (editing && editing !== 'new') {
    const p = profiles.find((x) => x.id === editing);
    p ? fillInfo(p) : closeDrawer();
  }
}

// ---------- drawer ----------
const form = $('#form');

function setMode(mode) {
  document.querySelectorAll('[data-mode]').forEach((el) => (el.hidden = el.dataset.mode !== mode));
}

function openCreate() {
  editing = 'new';
  removeProxy = false;
  form.reset();
  $('#drawerTitle').textContent = 'Новый профиль';
  $('#submitBtn').textContent = 'Создать';
  form.proxy.placeholder = 'host:port:user:pass или socks5://user:pass@host:port';
  $('#proxyResult').textContent = '';
  setMode('create');
  $('#drawer').hidden = false;
  form.name.focus();
  render();
}

function openEdit(id) {
  const p = profiles.find((x) => x.id === id);
  if (!p) return;
  editing = id;
  removeProxy = false;
  form.reset();
  $('#drawerTitle').textContent = p.name;
  $('#submitBtn').textContent = 'Сохранить';
  form.name.value = p.name;
  form.tags.value = p.tags.join(', ');
  form.status.value = p.status || '';
  form.notes.value = p.notes;
  form.locale.value = p.locale_manual ? p.locale : '';
  form.locale.placeholder = p.locale_manual || !p.locale ? 'авто: основной язык страны прокси' : `авто: ${p.locale}`;
  form.proxy.placeholder = p.proxy ? `${p.proxy} — оставьте пустым, чтобы не менять` : 'без прокси';
  $('#proxyResult').textContent = '';
  setMode('edit');
  $('#ckResult').textContent = '';
  $('#ckReplace').checked = false;
  fillInfo(p);
  $('#drawer').hidden = false;
  render();
}

function fillInfo(p) {
  const c = p.proxy_check;
  const pp = p.self_check?.passport;
  const rows = [['ID', `<span class="mono">${esc(p.id)}</span>`]];
  if (pp) {
    // what the browser actually presented at the last launch
    const s = p.self_check;
    rows.push(
      ['User-Agent', `<span class="mono">${esc(pp.ua)}</span>`],
      ['Платформа', esc(pp.platform)],
      ['Экран', `${esc((pp.screen || []).join('×'))} @${pp.dpr}x · ${pp.cores} ядер`],
      ['GPU', esc(pp.gpu || '—')],
      ['Языки', esc((pp.langs || []).join(', '))],
      ['Таймзона', esc(pp.tz)],
      ['WebRTC', s.webrtc_ips?.length ? esc(s.webrtc_ips.join(', ')) : 'кандидатов нет'],
      ['Самопроверка', s.level === 'ok' ? `<span class="ok">OK</span> · ${ago(s.checked_at)}` : `<span class="${s.level === 'bad' ? 'bad' : 'warn'}">${esc((s.problems || []).join('; ') || s.error || '')}</span> · ${ago(s.checked_at)}`],
    );
  } else {
    rows.push(
      ['Платформа', esc(`${p.summary.platform || p.os}`)],
      ['GPU', esc(p.summary.gpu || '—')],
      ['Экран', `${esc(p.summary.screen || '—')} <span class="muted">(оценка; точный паспорт появится после первого запуска)</span>`],
    );
  }
  rows.push(['Создан', new Date(p.created_at * 1000).toLocaleString('ru-RU')]);
  if (c?.ok) rows.push(['Проверка прокси', `${esc(c.ip)} · ${esc(c.country_name || c.country)} · ${esc(c.timezone)} · ${esc(c.isp)} · ${esc(c.provider || '')} · ${ago(c.checked_at)}${c.stale ? ' · <span class="warn">из кэша</span>' : ''}`]);
  else if (c) rows.push(['Проверка прокси', `<span class="bad">${esc(c.error)}</span>`]);
  $('#info').className = 'passport';
  $('#info').innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('');
}

function closeDrawer() {
  editing = null;
  $('#drawer').hidden = true;
  render();
}

function describeCheck(c) {
  if (!c.ok) return `<span class="bad">${esc(c.error)}</span>`;
  const dc = c.flag_hosting ? ' · <span class="warn">датацентр</span>' : '';
  return `<span class="ok">OK</span> ${esc(c.ip)} · ${esc(c.country)} ${esc(c.city || '')} · ${esc(c.timezone)} · ${c.latency_ms} мс${dc}`;
}

$('#testProxy').onclick = async () => {
  const v = form.proxy.value.trim();
  const out = $('#proxyResult');
  out.textContent = 'проверяю…';
  try {
    let c;
    if (v) c = await window.adm.checkProxy(v);
    else if (editing && editing !== 'new') c = (await window.adm.check(editing))[0];
    if (!c) { out.textContent = 'нечего проверять'; return; }
    out.innerHTML = describeCheck(c);
    if (!v) refresh();
  } catch (e) {
    out.innerHTML = `<span class="bad">${esc(errText(e))}</span>`;
  }
};

$('#removeProxy').onclick = () => {
  removeProxy = true;
  form.proxy.value = '';
  form.proxy.placeholder = 'прокси будет убран после сохранения';
  $('#proxyResult').innerHTML = '<span class="warn">Без прокси профиль откроется с реальным IP этого Mac.</span>';
};

form.onsubmit = async (e) => {
  e.preventDefault();
  const f = Object.fromEntries(new FormData(form));
  const btn = $('#submitBtn');
  btn.disabled = true;
  try {
    if (editing === 'new') {
      const n = Number(f.count) || 1;
      const created = await busy(n > 1 ? `Создаю ${n} профилей…` : 'Создаю профиль…', () =>
        window.adm.create({ name: f.name.trim(), os: f.os, count: n, proxy: f.proxy.trim(), tags: f.tags, notes: f.notes, status: f.status }));
      await refresh();
      status(`Создано: ${created.length}`);
      created.length === 1 ? openEdit(created[0].id) : closeDrawer();
    } else {
      const upd = { name: f.name.trim(), tags: f.tags, notes: f.notes, status: f.status };
      const p = profiles.find((x) => x.id === editing);
      const curLocale = p.locale_manual ? p.locale : '';
      if (f.locale.trim() !== curLocale) upd.locale = f.locale.trim();
      if (f.proxy.trim()) upd.proxy = f.proxy.trim();
      else if (removeProxy) upd.proxy = '';
      await busy('Сохраняю…', () => window.adm.update(editing, upd));
      await refresh();
      openEdit(editing);
      status('Сохранено');
    }
  } catch { /* shown in status */ } finally {
    btn.disabled = false;
  }
};

$('#deleteOne').onclick = async () => {
  if (await window.adm.delete([editing])) {
    closeDrawer();
    refresh();
  }
};
$('#closeDrawer').onclick = closeDrawer;

// ---------- cookies ----------
async function cookieOp(label, fn) {
  const out = $('#ckResult');
  const id = editing;
  if (states[id]) { out.innerHTML = '<span class="warn">Закройте браузер профиля — cookies доступны только у закрытого профиля.</span>'; return; }
  document.querySelectorAll('fieldset.cookies button').forEach((b) => (b.disabled = true));
  out.textContent = label;
  try {
    const r = await fn(id);
    if (r === null) { out.textContent = ''; return; } // dialog cancelled
    out.innerHTML = describeCookies(r);
    status('');
  } catch (e) {
    out.innerHTML = `<span class="bad">${esc(errText(e))}</span>`;
  } finally {
    document.querySelectorAll('fieldset.cookies button').forEach((b) => (b.disabled = false));
  }
}

function describeCookies(r) {
  if ('imported' in r) {
    const notes = [...r.failed, ...r.skipped];
    return `<span class="ok">Импортировано: ${r.imported}</span> · всего в профиле: ${r.total_in_profile}` +
      (notes.length ? `\n<span class="warn">Пропущено ${notes.length}:</span>\n${notes.slice(0, 8).map(esc).join('\n')}${notes.length > 8 ? '\n…' : ''}` : '');
  }
  return `<span class="ok">Экспортировано: ${r.exported}</span>${r.file ? ` → ${esc(r.file)}` : ' — JSON в буфере обмена'}`;
}

const ckWait = 'Открываю профиль в фоне (без сети)…';
$('#ckFile').onclick = () => cookieOp(ckWait, (id) => window.adm.cookiesImport(id, 'file', $('#ckReplace').checked));
$('#ckPaste').onclick = () => cookieOp(ckWait, (id) => window.adm.cookiesImport(id, 'clipboard', $('#ckReplace').checked));
$('#ckExport').onclick = () => cookieOp(ckWait, (id) => window.adm.cookiesExport(id, profiles.find((p) => p.id === id).name));
$('#ckCopy').onclick = () => cookieOp(ckWait, (id) => window.adm.cookiesCopy(id));

// ---------- table & toolbar ----------
$('#rows').addEventListener('click', async (e) => {
  const t = e.target;
  if (t.dataset.launch) {
    status('');
    return window.adm.launch(t.dataset.launch).catch((err) => status(errText(err), true));
  }
  if (t.dataset.stop) return window.adm.stop(t.dataset.stop);
  if (t.dataset.sel) {
    t.checked ? selected.add(t.dataset.sel) : selected.delete(t.dataset.sel);
    return render();
  }
  const tr = t.closest('tr');
  if (tr) editing === tr.dataset.id ? closeDrawer() : openEdit(tr.dataset.id);
});

$('#rows').addEventListener('dblclick', (e) => {
  const tr = e.target.closest('tr');
  if (tr && !e.target.closest('button,input') && !states[tr.dataset.id]) window.adm.launch(tr.dataset.id);
});

$('#selAll').onchange = (e) => {
  const q = $('#search').value.trim().toLowerCase();
  const tag = $('#tagFilter').value;
  for (const p of profiles.filter((x) => matches(x, q, tag))) e.target.checked ? selected.add(p.id) : selected.delete(p.id);
  render();
};

async function checkMany(ids) {
  let done = 0;
  const queue = [...ids];
  const worker = async () => {
    while (queue.length) {
      const id = queue.shift();
      try { await window.adm.check(id); } catch { /* per-profile error is stored in proxy_check */ }
      status(`Проверено ${++done} из ${ids.length}…`);
    }
  };
  await Promise.all(Array.from({ length: Math.min(6, ids.length) }, worker));
  await refresh();
  const bad = profiles.filter((p) => ids.includes(p.id) && p.proxy && !p.proxy_check?.ok).length;
  status(bad ? `Готово, с ошибкой: ${bad}` : 'Все прокси в порядке', bad > 0);
}

$('#checkAll').onclick = () => checkMany(profiles.filter((p) => p.proxy).map((p) => p.id));
$('#selCheck').onclick = () => checkMany([...selected].filter((id) => profiles.find((p) => p.id === id)?.proxy));
$('#selDelete').onclick = async () => {
  if (await window.adm.delete([...selected])) {
    selected.clear();
    if (editing && !profiles.some((p) => p.id === editing)) closeDrawer();
    refresh();
  }
};

$('#newBtn').onclick = openCreate;
$('#emptyNew').onclick = openCreate;
$('#search').oninput = render;
$('#tagFilter').onchange = render;
$('#statusFilter').onchange = render;

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !$('#drawer').hidden) closeDrawer();
  if (e.key === 'Escape' && !$('#settings').hidden) { $('#settings').hidden = true; return; }
  if (e.key === 'n' && e.metaKey) { e.preventDefault(); openCreate(); }
  if (e.key === 'f' && e.metaKey) { e.preventDefault(); $('#search').focus(); }
});

window.adm.onProxy(({ id, state }) => {
  proxyStates[id] = state;
  if (state === 'down') { const p = profiles.find((x) => x.id === id); status(`${p ? p.name : id}: прокси перестал отвечать`, true); }
  render();
});

window.adm.onState(({ id, state, error }) => {
  if (state === 'stopped') { delete states[id]; delete proxyStates[id]; }
  else states[id] = state;
  if (error) {
    const p = profiles.find((x) => x.id === id);
    status(`${p ? p.name : id}: ${error}`, true);
  }
  // launch re-checks the proxy and stamps last_used_at — pull fresh data
  if (state !== 'starting') refresh();
  else render();
});

// ---------- first run: browser engine ----------
async function engineGate() {
  let st;
  try { st = await window.adm.engineStatus(); } catch (e) { status(errText(e), true); return; }
  if (st && st.installed) return;
  $('#engine').hidden = false;
}

$('#engineLater').onclick = () => { $('#engine').hidden = true; };
$('#engineFetch').onclick = async () => {
  const btn = $('#engineFetch');
  btn.disabled = true; $('#engineLater').disabled = true;
  $('#engineError').hidden = true;
  $('#engineProgress').hidden = false;
  $('#engineText').textContent = 'Подготовка…';
  try {
    await window.adm.engineFetch();
    $('#engine').hidden = true;
    status('Движок установлен');
  } catch (e) {
    $('#engineError').textContent = errText(e);
    $('#engineError').hidden = false;
  } finally {
    btn.disabled = false; $('#engineLater').disabled = false;
  }
};
window.adm.onEngineProgress((ev) => {
  if (ev.stage === 'browser' && ev.total) {
    const pct = Math.min(100, Math.round(ev.done / ev.total * 100));
    $('#engineBar').style.width = pct + '%';
    $('#engineText').textContent = `${ev.file}: ${(ev.done / 1048576) | 0} / ${(ev.total / 1048576) | 0} МБ (${pct}%)`;
  } else if (ev.stage === 'browser') {
    $('#engineText').textContent = `Скачиваю ${ev.file}…`;
  } else if (ev.stage === 'other') {
    $('#engineText').textContent = `${ev.file}…`;
  } else if (ev.stage === 'done') {
    $('#engineBar').style.width = '100%';
    $('#engineText').textContent = `Готово: ${ev.browser}`;
  }
});

// ---------- settings ----------
let appUpdateUrl = null, dataDir = null, cacheDir = null;
async function openSettings() {
  $('#settings').hidden = false;
  $('#stAppResult').textContent = ''; $('#stEngineResult').textContent = '';
  $('#stAppOpen').hidden = true; $('#stEngineFetch').hidden = true;
  try {
    const info = await window.adm.appInfo();
    $('#stAppVersion').textContent = info.version + (info.packaged ? '' : ' (dev)');
    const st = await window.adm.engineStatus();
    $('#stPkg').textContent = st.package;
    $('#stBrowser').textContent = st.browser || 'не установлен';
    $('#stData').textContent = st.data_dir; dataDir = st.data_dir; cacheDir = st.cache_dir;
  } catch (e) { status(errText(e), true); }
}
$('#settingsBtn').onclick = openSettings;
$('#settingsClose').onclick = () => { $('#settings').hidden = true; };
$('#stRepo').onclick = async () => window.adm.openExternal((await window.adm.appInfo()).repo);
$('#stDataOpen').onclick = () => dataDir && window.adm.openPath(dataDir);
$('#stCache').onclick = () => cacheDir && window.adm.openPath(cacheDir);
$('#stAppOpen').onclick = () => appUpdateUrl && window.adm.openExternal(appUpdateUrl);
$('#stAppCheck').onclick = async () => {
  const out = $('#stAppResult'); out.textContent = 'Проверяю GitHub…'; $('#stAppOpen').hidden = true;
  try {
    const r = await window.adm.appCheckUpdate();
    if (!r.latest) { out.textContent = 'Релизов на GitHub пока нет'; return; }
    if (r.newer) { out.textContent = `Доступна версия ${r.latest}. Скачайте dmg, перетащите в «Программы» поверх старой, профили сохранятся.`; appUpdateUrl = r.url; $('#stAppOpen').hidden = false; }
    else out.textContent = `Установлена последняя версия (${r.latest} на GitHub)`;
  } catch (e) { out.textContent = 'Не удалось проверить: ' + errText(e); }
};
$('#stEngineCheck').onclick = async () => {
  const out = $('#stEngineResult'); out.textContent = 'Проверяю PyPI и установленную сборку…'; $('#stEngineFetch').hidden = true;
  try {
    const r = await window.adm.engineCheck();
    const parts = [];
    if (r.package_outdated) parts.push(`На PyPI camoufox ${r.pypi_latest}, в приложении ${r.package}: новый движок придёт с обновлением приложения.`);
    else if (r.pypi_latest) parts.push(`Пакет camoufox актуален (${r.package}).`);
    else parts.push(r.error || 'PyPI недоступен.');
    if (!r.browser) { parts.push('Браузер не установлен.'); $('#stEngineFetch').hidden = false; }
    else if (!r.browser_paired) { parts.push(`Установлен браузер ${r.browser}, а этой версии нужен ${r.pin}.`); $('#stEngineFetch').hidden = false; }
    else parts.push(`Браузер ${r.browser} — парная сборка для этого пакета.`);
    out.textContent = parts.join(' ');
  } catch (e) { out.textContent = 'Не удалось проверить: ' + errText(e); }
};
$('#stEngineFetch').onclick = () => { $('#settings').hidden = true; $('#engine').hidden = false; $('#engineFetch').click(); };

(async () => {
  states = await window.adm.running();
  proxyStates = await window.adm.proxyStates();
  await busy('Загрузка…', refresh).catch(() => {});
  await engineGate();
  if (location.hash === '#settings') { await openSettings(); $('#stAppCheck').click(); $('#stEngineCheck').click(); }
  if (location.hash.startsWith('#edit=')) { openEdit(location.hash.slice(6)); setTimeout(() => { $('#drawer').scrollTop = 1e6; }, 500); } // dev aid
})();
