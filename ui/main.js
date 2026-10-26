const { app, BrowserWindow, ipcMain, dialog, clipboard, shell } = require('electron');
const fs = require('fs');
const os = require('os');
const { spawn } = require('child_process');
const path = require('path');

// Packaged app: <resources>/adm/ carries a Python runtime + packages + the adm
// core (ui/build/make-bundle.js). Dev (`electron .`): the project's .venv.
const WIN = process.platform === 'win32';
function pythonCmd() {
  if (app.isPackaged) {
    const root = path.join(process.resourcesPath, 'adm');
    return {
      cmd: WIN ? path.join(root, 'python', 'python.exe') : path.join(root, 'python', 'bin', 'python3'),
      env: { PYTHONPATH: [root, path.join(root, 'pylib')].join(path.delimiter), PYTHONNOUSERSITE: '1', PYTHONDONTWRITEBYTECODE: '1' },
    };
  }
  const proj = path.resolve(__dirname, '..');
  return {
    cmd: WIN ? path.join(proj, '.venv', 'Scripts', 'python.exe') : path.join(proj, '.venv', 'bin', 'python'),
    env: { PYTHONPATH: proj },
  };
}
function spawnAdm(args) {
  const py = pythonCmd();
  return spawn(py.cmd, ['-m', 'adm.cli', '--json', ...args], {
    env: { ...process.env, ...py.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8', ADM_UI: '1' },
    windowsHide: true,
  });
}

let win;
// profileId -> { proc, state: 'starting' | 'running', log: string[] }
const running = new Map();
// profiles whose cookie store is open in a background (headless) session
const cookieBusy = new Set();

async function withCookieLock(id, fn) {
  if (running.has(id)) throw new Error('Профиль открыт — закройте браузер');
  if (cookieBusy.has(id)) throw new Error('С cookies этого профиля уже идёт операция');
  cookieBusy.add(id);
  try { return await fn(); } finally { cookieBusy.delete(id); }
}

function runAdm(args) {
  return new Promise((resolve, reject) => {
    const p = spawnAdm(args);
    let out = '', err = '';
    p.stdout.on('data', (d) => (out += d));
    p.stderr.on('data', (d) => (err += d));
    p.on('error', reject);
    p.on('close', (code) => {
      if (code === 0) {
        try { resolve(out.trim() ? JSON.parse(out) : null); } catch (e) { reject(new Error(`bad JSON from adm: ${out.slice(0, 200)}`)); }
      } else {
        reject(new Error(lastErrorLine(err) || `adm exited with ${code}`));
      }
    });
  });
}

// Python tracebacks end with the actual message; warnings are noise.
function lastErrorLine(text) {
  const lines = text.split('\n').map((l) => l.trim()).filter((l) => l && !/warn/i.test(l) && !/i_know_what_im_doing/.test(l));
  return lines[lines.length - 1] || '';
}

function emitState(id, state, error) {
  if (win && !win.isDestroyed()) win.webContents.send('profile-state', { id, state, error });
}

function launchProfile(id) {
  if (running.has(id)) return;
  if (cookieBusy.has(id)) throw new Error('Идёт импорт/экспорт cookies — запустите после завершения');
  const proc = spawnAdm(['launch', id]);
  const entry = { proc, state: 'starting', log: [] };
  running.set(id, entry);
  emitState(id, 'starting');

  let buf = '';
  proc.stdout.on('data', (d) => {
    buf += d;
    const lines = buf.split('\n');
    buf = lines.pop();
    for (const line of lines) {
      if (!line.startsWith('{')) continue;
      let ev;
      try { ev = JSON.parse(line); } catch { continue; }
      if (ev.event === 'ready') {
        entry.state = 'running';
        emitState(id, 'running');
      } else if (ev.event === 'selfcheck') {
        emitState(id, 'running'); // renderer re-reads the profile (self_check is stored by the launcher)
      } else if (ev.event === 'proxy') {
        entry.proxy = ev.state;
        if (win && !win.isDestroyed()) win.webContents.send('profile-proxy', { id, state: ev.state });
      }
    }
  });
  proc.stderr.on('data', (d) => {
    entry.log.push(String(d));
    if (entry.log.length > 200) entry.log.shift();
  });
  proc.on('close', (code) => {
    running.delete(id);
    const error = code && code !== 0 && !entry.stopping ? lastErrorLine(entry.log.join('')) || `exit ${code}` : null;
    emitState(id, 'stopped', error);
  });
}

function stopProfile(id) {
  const entry = running.get(id);
  if (!entry) return;
  entry.stopping = true;
  // "stop" on stdin works on every platform (the launcher watches it when
  // ADM_UI=1); SIGTERM is the second channel where the loop supports it.
  try { entry.proc.stdin.write('stop\n'); entry.proc.stdin.end(); } catch { /* already gone */ }
  if (!WIN) entry.proc.kill('SIGTERM');
  // Hard kill if the browser refuses to close.
  setTimeout(() => running.get(id) === entry && entry.proc.kill(WIN ? undefined : 'SIGKILL'), 8000);
}

ipcMain.handle('list', () => runAdm(['list']));
ipcMain.handle('show', (_e, id) => runAdm(['show', id]));
ipcMain.handle('create', (_e, o) => {
  const args = ['create', o.name, '--os', o.os || 'macos', '--count', String(o.count || 1)];
  if (o.proxy) args.push('--proxy', o.proxy);
  if (o.tags) args.push('--tags', o.tags);
  if (o.notes) args.push('--notes', o.notes);
  if (o.status) args.push('--status', o.status);
  return runAdm(args);
});
ipcMain.handle('update', (_e, id, o) => {
  const args = ['set', id];
  if (o.name != null) args.push('--name', o.name);
  if (o.proxy != null) args.push('--proxy', o.proxy); // '' removes proxy
  if (o.tags != null) args.push('--tags', o.tags);
  if (o.notes != null) args.push('--notes', o.notes);
  if (o.locale != null) args.push('--locale', o.locale); // '' = auto
  if (o.status != null) args.push('--status', o.status); // '' = none
  return runAdm(args);
});
ipcMain.handle('check', (_e, id) => runAdm(['check', id]));
ipcMain.handle('check-proxy', (_e, proxy) => runAdm(['check-proxy', proxy]));
ipcMain.handle('delete', async (_e, ids) => {
  const { response } = await dialog.showMessageBox(win, {
    type: 'warning',
    buttons: ['Удалить', 'Отмена'],
    defaultId: 1,
    cancelId: 1,
    message: ids.length === 1 ? 'Удалить профиль?' : `Удалить профили (${ids.length})?`,
    detail: 'Куки, история и отпечаток будут удалены без возможности восстановления.',
  });
  if (response !== 0) return false;
  for (const id of ids) {
    if (running.has(id)) stopProfile(id);
    await runAdm(['delete', id]);
  }
  return true;
});
// Cookies: the profile must be closed (Firefox locks its cookie store).
ipcMain.handle('cookies-import', (_e, id, source, replace) => withCookieLock(id, async () => {
  let file;
  if (source === 'clipboard') {
    const text = String((await clipboard.readText()) ?? "");
    if (!text.trim()) throw new Error('Буфер обмена пуст');
    file = path.join(os.tmpdir(), `adm-cookies-${process.pid}-${Date.now()}.txt`);
    fs.writeFileSync(file, text, { mode: 0o600 });
  } else {
    const r = await dialog.showOpenDialog(win, {
      title: 'Импорт cookies',
      filters: [{ name: 'Cookies (JSON, cookies.txt)', extensions: ['json', 'txt'] }, { name: 'Все файлы', extensions: ['*'] }],
      properties: ['openFile'],
    });
    if (r.canceled) return null;
    file = r.filePaths[0];
  }
  try {
    return await runAdm(['cookies-import', id, file, ...(replace ? ['--replace'] : [])]);
  } finally {
    if (source === 'clipboard') fs.rmSync(file, { force: true });
  }
}));
ipcMain.handle('cookies-export', (_e, id, name) => withCookieLock(id, async () => {
  const r = await dialog.showSaveDialog(win, {
    title: 'Экспорт cookies',
    defaultPath: `${name}-cookies.json`,
    filters: [{ name: 'JSON (Cookie-Editor, Octo, Dolphin)', extensions: ['json'] }, { name: 'cookies.txt (Netscape)', extensions: ['txt'] }],
  });
  if (r.canceled) return null;
  const format = r.filePath.endsWith('.txt') ? 'netscape' : 'json';
  return runAdm(['cookies-export', id, '--format', format, '-o', r.filePath]);
}));
ipcMain.handle('cookies-copy', (_e, id) => withCookieLock(id, async () => {
  const r = await runAdm(['cookies-export', id]);
  await clipboard.writeText(r.text);
  return r;
}));
ipcMain.handle('launch', (_e, id) => launchProfile(id));
ipcMain.handle('stop', (_e, id) => stopProfile(id));
ipcMain.handle('running', () => Object.fromEntries([...running].map(([id, e]) => [id, e.state])));
ipcMain.handle('proxy-states', () => Object.fromEntries([...running].filter(([, e]) => e.proxy).map(([id, e]) => [id, e.proxy])));

// ---------- settings: versions and update checks ----------
const REPO = 'tamilamokrousova0/antidetect-manager';
const vtuple = (v) => String(v || '').match(/\d+/g)?.map(Number) || [];
const newer = (a, b) => { // a > b ?
  const x = vtuple(a), y = vtuple(b);
  for (let i = 0; i < Math.max(x.length, y.length); i++) { const d = (x[i] || 0) - (y[i] || 0); if (d) return d > 0; }
  return false;
};
ipcMain.handle('app-info', () => ({ version: app.getVersion(), packaged: app.isPackaged, repo: `https://github.com/${REPO}` }));
ipcMain.handle('app-check-update', async () => {
  const r = await fetch(`https://api.github.com/repos/${REPO}/releases/latest`, { headers: { Accept: 'application/vnd.github+json', 'User-Agent': 'antidetect-manager' } });
  if (r.status === 404) return { latest: null, url: `https://github.com/${REPO}/releases`, newer: false };
  if (!r.ok) throw new Error(`GitHub: HTTP ${r.status}`);
  const rel = await r.json();
  const latest = String(rel.tag_name || '').replace(/^v/, '');
  const dmg = (rel.assets || []).find((a) => /\.dmg$/.test(a.name));
  return { latest, url: dmg ? dmg.browser_download_url : rel.html_url, page: rel.html_url, newer: newer(latest, app.getVersion()), published: rel.published_at };
});
ipcMain.handle('engine-check', () => runAdm(['engine', 'check']));
ipcMain.handle('open-external', (_e, url) => (/^https:\/\//.test(url) ? shell.openExternal(url) : Promise.reject(new Error('bad url'))));
ipcMain.handle('open-path', (_e, p) => shell.openPath(p));

// ---------- browser engine (first run: ~1.3 GB download into the user cache dir) ----------
let engineFetch = null;
ipcMain.handle('engine-status', () => runAdm(['engine', 'status']));
ipcMain.handle('engine-fetch', () => new Promise((resolve, reject) => {
  if (engineFetch) return resolve(false);
  const proc = spawnAdm(['engine', 'fetch']);
  engineFetch = proc;
  let err = '', buf = '';
  proc.stdout.on('data', (d) => {
    buf += d;
    const lines = buf.split('\n');
    buf = lines.pop();
    for (const line of lines) {
      if (!line.trim()) continue;
      let ev;
      try { ev = JSON.parse(line); } catch { continue; }
      if (win && !win.isDestroyed()) win.webContents.send('engine-progress', ev);
    }
  });
  proc.stderr.on('data', (d) => { err += d; if (err.length > 20000) err = err.slice(-20000); });
  proc.on('error', (e) => { engineFetch = null; reject(e); });
  proc.on('close', (code) => {
    engineFetch = null;
    if (code === 0) resolve(true);
    else reject(new Error(lastErrorLine(err) || `engine fetch exited with ${code}`));
  });
}));

function createWindow() {
  win = new BrowserWindow({
    width: 1180,
    height: 720,
    minWidth: 860,
    minHeight: 480,
    titleBarStyle: 'hiddenInset',
    backgroundColor: '#f6f6f6',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  win.loadFile(path.join(__dirname, 'index.html'), { hash: process.env.ADM_UI_HASH || '' }); // dev aid: ADM_UI_HASH=settings

  // Dev aid: ADM_UI_SHOT=/tmp/x.png -> screenshot of the window every 3s
  // (screencapture needs Screen Recording permission; capturePage does not).
  if (process.env.ADM_UI_SHOT) {
    setInterval(async () => {
      if (win.isDestroyed()) return;
      const img = await win.webContents.capturePage();
      require('fs').writeFileSync(process.env.ADM_UI_SHOT, img.toPNG());
    }, 3000);
  }
}

app.whenReady().then(createWindow);
app.on('activate', () => BrowserWindow.getAllWindows().length === 0 && createWindow());

// Close every running profile cleanly before quitting.
let quitting = false;
app.on('before-quit', (e) => {
  if (quitting || running.size === 0) return;
  e.preventDefault();
  quitting = true;
  for (const id of running.keys()) stopProfile(id);
  const t0 = Date.now();
  const wait = setInterval(() => {
    if (running.size === 0 || Date.now() - t0 > 10000) {
      clearInterval(wait);
      app.quit();
    }
  }, 200);
});
app.on('window-all-closed', () => app.quit());
