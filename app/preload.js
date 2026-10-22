const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('scholarlens', {
  getBackendInfo: () => ipcRenderer.invoke('get-backend-info'),
  selectPdfs: () => ipcRenderer.invoke('select-pdfs'),
  openExternal: (url) => ipcRenderer.invoke('open-external', url),
  onBackendStatus: (cb) => ipcRenderer.on('backend-status', (_, status) => cb(status)),
});
