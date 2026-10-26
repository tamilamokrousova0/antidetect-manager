const { app, BrowserWindow, ipcMain, dialog, clipboard } = require('electron');
const fs = require('fs');
const os = require('os');
const { spawn } = require('child_process');
const path = require('path');

// Dev: ui/ sits inside the project. Packaged .app: path baked in at build time.
function admHome() {
  try { return require('./adm-home.json').path; } catch { return path.resolve(__dirname, '..'); }
}
const ADM = path.join(admHome(), 'adm.sh');

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
    const p = spawn(ADM, ['--json', ...args]);
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
  const proc = spawn(ADM, ['--json', 'launch', id]);
  const entry = { proc, state: 'starting', log: [] };
  running.set(id, entry);
  emitState(id, 'starting');

  proc.stdout.on('data', (d) => {
    for (const line of String(d).split('\n')) {
      if (line.includes('"event": "ready"')) {
        entry.state = 'running';
        emitState(id, 'running');
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
  entry.proc.kill('SIGTERM');
  // Hard kill if the browser refuses to close.
  setTimeout(() => running.get(id) === entry && entry.proc.kill('SIGKILL'), 8000);
}

ipcMain.handle('list', () => runAdm(['list']));
ipcMain.handle('show', (_e, id) => runAdm(['show', id]));
ipcMain.handle('create', (_e, o) => {
  const args = ['create', o.name, '--os', o.os || 'macos', '--count', String(o.count || 1)];
  if (o.proxy) args.push('--proxy', o.proxy);
  if (o.tags) args.push('--tags', o.tags);
  if (o.notes) args.push('--notes', o.notes);
  return runAdm(args);
});
ipcMain.handle('update', (_e, id, o) => {
  const args = ['set', id];
  if (o.name != null) args.push('--name', o.name);
  if (o.proxy != null) args.push('--proxy', o.proxy); // '' removes proxy
  if (o.tags != null) args.push('--tags', o.tags);
  if (o.notes != null) args.push('--notes', o.notes);
  if (o.locale != null) args.push('--locale', o.locale); // '' = auto
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
  win.loadFile(path.join(__dirname, 'index.html'));

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
