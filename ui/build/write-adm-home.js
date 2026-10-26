// Bakes the project location into the packaged app: the Python core and its
// venv stay in the project folder, the .app only carries the UI.
const fs = require('fs');
const path = require('path');
const home = path.resolve(__dirname, '..', '..');
fs.writeFileSync(path.join(__dirname, '..', 'adm-home.json'), JSON.stringify({ path: home }, null, 2) + '\n');
console.log('adm home:', home);
