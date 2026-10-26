const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('adm', {
  list: () => ipcRenderer.invoke('list'),
  show: (id) => ipcRenderer.invoke('show', id),
  create: (o) => ipcRenderer.invoke('create', o),
  update: (id, o) => ipcRenderer.invoke('update', id, o),
  check: (id) => ipcRenderer.invoke('check', id),
  checkProxy: (proxy) => ipcRenderer.invoke('check-proxy', proxy),
  delete: (ids) => ipcRenderer.invoke('delete', ids),
  cookiesImport: (id, source, replace) => ipcRenderer.invoke('cookies-import', id, source, replace),
  cookiesExport: (id, name) => ipcRenderer.invoke('cookies-export', id, name),
  cookiesCopy: (id) => ipcRenderer.invoke('cookies-copy', id),
  launch: (id) => ipcRenderer.invoke('launch', id),
  stop: (id) => ipcRenderer.invoke('stop', id),
  running: () => ipcRenderer.invoke('running'),
  onState: (cb) => ipcRenderer.on('profile-state', (_e, s) => cb(s)),
});
