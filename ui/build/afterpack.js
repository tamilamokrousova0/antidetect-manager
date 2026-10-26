// Ad-hoc sign the packed app (no Developer ID): Apple Silicon refuses to run a
// modified bundle whose seal is broken, and electron-builder skips signing when
// `identity` is null. Runs before the dmg/zip targets pick the app up.
const { execFileSync } = require('child_process');
const path = require('path');

exports.default = async function (context) {
  if (context.electronPlatformName !== 'darwin') return;
  const app = path.join(context.appOutDir, `${context.packager.appInfo.productFilename}.app`);
  execFileSync('codesign', ['--force', '--deep', '--sign', '-', app], { stdio: 'inherit' });
  console.log('  • ad-hoc signed', app);
};
