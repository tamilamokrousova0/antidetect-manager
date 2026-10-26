// Build ui/bundle-<platform>/: a relocatable Python 3.12 (python-build-standalone),
// the project's Python packages (uv, wheels for the TARGET platform) and the adm
// core. electron-builder ships it as <resources>/adm (extraResources), so the
// app runs without the project folder, uv, Homebrew or a venv.
//   node build/make-bundle.js --platform mac|win
// Cross-builds: the Windows bundle is produced on macOS (pure wheels + binary
// wheels resolved by uv for x86_64-pc-windows-msvc; nothing is compiled).
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');

const PBS = { tag: '20261026', version: '3.12.16' }; // astral-sh/python-build-standalone release
const TARGETS = {
  mac: { triple: 'aarch64-apple-darwin', uvPlatform: 'aarch64-apple-darwin', py: 'bin/python3',
    trim: ['lib/python3.12/test', 'lib/python3.12/idlelib', 'lib/python3.12/tkinter', 'lib/python3.12/ensurepip',
      'lib/python3.12/lib2to3', 'lib/python3.12/__phello__', 'lib/tcl9', 'lib/tcl9.0', 'lib/tk9.0', 'lib/itcl4.3.8',
      'lib/thread3.0.6', 'lib/libtcl9.0.dylib', 'lib/libtcl9tk9.0.dylib', 'lib/pkgconfig', 'share', 'include',
      'lib/python3.12/site-packages', 'lib/python3.12/venv'],
    pylibDrop: ['bin'] },
  win: { triple: 'x86_64-pc-windows-msvc', uvPlatform: 'x86_64-pc-windows-msvc', py: 'python.exe',
    trim: ['Lib/test', 'Lib/idlelib', 'Lib/tkinter', 'Lib/ensurepip', 'Lib/lib2to3', 'Lib/__phello__', 'tcl', 'Scripts',
      'include', 'libs', 'DLLs/_tkinter.pyd', 'DLLs/tcl86t.dll', 'DLLs/tk86t.dll', 'DLLs/tcl90.dll', 'DLLs/tk90.dll',
      'DLLs/tcl9tk90.dll', 'Lib/site-packages', 'Lib/venv'],
    pylibDrop: ['Scripts'] },
};

const platform = (process.argv.indexOf('--platform') > -1 ? process.argv[process.argv.indexOf('--platform') + 1] : null)
  || (process.platform === 'win32' ? 'win' : 'mac');
const T = TARGETS[platform];
if (!T) throw new Error(`unknown platform ${platform}`);

const UI = path.resolve(__dirname, '..');
const ROOT = path.resolve(UI, '..');
const OUT = path.join(UI, `bundle-${platform}`);
const CACHE = path.join(UI, '.cache');
const run = (cmd, args, opts = {}) => execFileSync(cmd, args, { stdio: 'inherit', ...opts });
const rmrf = (p) => fs.rmSync(p, { recursive: true, force: true });

fs.mkdirSync(CACHE, { recursive: true });
rmrf(OUT); fs.mkdirSync(OUT, { recursive: true });

console.log(`== python runtime (${T.triple})`);
const asset = `cpython-${PBS.version}+${PBS.tag}-${T.triple}-install_only_stripped.tar.gz`;
const tarball = path.join(CACHE, asset);
if (!fs.existsSync(tarball)) {
  const url = `https://github.com/astral-sh/python-build-standalone/releases/download/${PBS.tag}/${asset}`;
  console.log('   downloading', url);
  run('curl', ['-fL', '-#', '--retry', '3', '-o', tarball + '.part', url]);
  fs.renameSync(tarball + '.part', tarball);
}
run('tar', ['-xzf', tarball, '-C', OUT]); // extracts python/
const PY = path.join(OUT, 'python');
for (const rel of T.trim) rmrf(path.join(PY, rel));
if (!fs.existsSync(path.join(PY, T.py))) throw new Error(`no interpreter at ${path.join(PY, T.py)}`);

console.log('== packages');
run('uv', ['pip', 'install', '--quiet', '--python-platform', T.uvPlatform, '--python-version', '3.12',
  '--only-binary', ':all:', '--target', path.join(OUT, 'pylib'), '-r', path.join(ROOT, 'requirements.txt')], { cwd: ROOT });
for (const rel of T.pylibDrop) rmrf(path.join(OUT, 'pylib', rel));

console.log('== adm core');
fs.cpSync(path.join(ROOT, 'adm'), path.join(OUT, 'adm'), { recursive: true });
fs.copyFileSync(path.join(ROOT, 'requirements.txt'), path.join(OUT, 'requirements.txt'));
// strip caches and install records
const walk = (dir) => {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) { if (e.name === '__pycache__') rmrf(p); else walk(p); }
    else if (e.name === 'RECORD' && dir.endsWith('.dist-info')) fs.unlinkSync(p);
  }
};
walk(OUT);
if (platform === 'mac') {
  // convenience for a terminal: <bundle>/run engine status
  fs.writeFileSync(path.join(OUT, 'run'), '#!/bin/sh\nDIR="$(cd "$(dirname "$0")" && pwd)"\nexport PYTHONPATH="$DIR:$DIR/pylib" PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1\nexec "$DIR/python/bin/python3" -m adm.cli "$@"\n', { mode: 0o755 });
}

console.log('== check');
const expect = platform === 'win'
  ? ['pylib/playwright/driver/node.exe', 'pylib/camoufox/fonts.json', 'python/python312.dll']
  : ['pylib/playwright/driver/node', 'pylib/camoufox/fonts.json', 'python/lib/libpython3.12.dylib'];
for (const rel of expect) if (!fs.existsSync(path.join(OUT, rel))) throw new Error(`missing ${rel}`);
if (platform === (process.platform === 'win32' ? 'win' : 'mac')) {
  run(path.join(PY, T.py), ['-c', 'import camoufox, playwright, numpy; print("imports ok")'],
    { env: { ...process.env, PYTHONPATH: `${OUT}${path.delimiter}${path.join(OUT, 'pylib')}`, PYTHONNOUSERSITE: '1' } });
  run(path.join(PY, T.py), ['-m', 'adm.cli', '--json', 'engine', 'status'],
    { env: { ...process.env, PYTHONPATH: `${OUT}${path.delimiter}${path.join(OUT, 'pylib')}`, PYTHONNOUSERSITE: '1' }, stdio: 'ignore' });
} else {
  console.log('   (cross-build: runtime smoke test skipped)');
}
const du = execFileSync('du', ['-sh', OUT]).toString().trim();
console.log('bundle:', du);
