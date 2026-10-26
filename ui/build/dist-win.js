// Windows targets from any host. On macOS (Apple Silicon) electron-builder's
// bundled makensis is x86_64-only (spawn error -86 without Rosetta), so a
// legacy-layout NSIS bundle is assembled from Homebrew's native makensis
// (`brew install makensis`) and handed over via ELECTRON_BUILDER_NSIS_DIR.
const { execFileSync, execSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const env = { ...process.env };
if (process.platform === 'darwin') {
  const prefix = execSync('brew --prefix makensis', { encoding: 'utf8' }).trim();
  const share = path.join(prefix, 'share', 'nsis');
  const dir = path.join(__dirname, '..', '.cache', 'nsis-native');
  fs.rmSync(dir, { recursive: true, force: true });
  fs.mkdirSync(path.join(dir, 'mac'), { recursive: true });
  fs.symlinkSync(path.join(prefix, 'bin', 'makensis'), path.join(dir, 'mac', 'makensis'));
  // Stubs/Bin/Plugins must match the makensis build -> Homebrew's. Include and
  // Contrib (language files, UI) are what electron-builder's scripts were
  // written against -> its own 3.0.4.1 bundle from the cache (with -WX a
  // missing LangString in a newer language file is a fatal "warning").
  const cache = path.join(process.env.HOME, 'Library', 'Caches', 'electron-builder');
  let ebBundle = null;
  for (const v of fs.existsSync(cache) ? fs.readdirSync(cache) : []) {
    if (!v.startsWith('nsis-') || v.includes('resources')) continue;
    for (const sub of fs.readdirSync(path.join(cache, v))) {
      if (fs.existsSync(path.join(cache, v, sub, 'elevate.exe'))) ebBundle = path.join(cache, v, sub);
    }
  }
  if (!ebBundle) throw new Error('electron-builder NSIS bundle not in cache yet: run `npx electron-builder --win nsis --x64` once (it fails, but downloads it)');
  for (const name of ['Stubs', 'Bin', 'Plugins']) fs.symlinkSync(path.join(share, name), path.join(dir, name));
  for (const name of ['Include', 'Contrib', 'Menu', 'nsisconf.nsh', 'elevate.exe']) {
    if (fs.existsSync(path.join(ebBundle, name))) fs.symlinkSync(path.join(ebBundle, name), path.join(dir, name));
  }
  env.ELECTRON_BUILDER_NSIS_DIR = dir;
}
execFileSync('npx', ['electron-builder', '--win', 'nsis', 'zip', '--x64'], { stdio: 'inherit', env });
