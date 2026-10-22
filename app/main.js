const { app, BrowserWindow, Tray, Menu, ipcMain, dialog, shell, session } = require('electron');
const { spawn, exec } = require('child_process');
const fs = require('fs');
const path = require('path');
const http = require('http');
const net = require('net');

app.setAppUserModelId('cn.scholarlens.app');

const PORT_RANGE_START = 21800;
const PORT_RANGE_SIZE = 50;
// 后端就绪等待上限
const HEALTH_TIMEOUT_MS = 60000;
const ROOT = app.isPackaged ? process.resourcesPath : path.join(__dirname, '..');
const DATA_ROOT = app.isPackaged
  ? (process.env.PORTABLE_EXECUTABLE_DIR
      ? path.join(process.env.PORTABLE_EXECUTABLE_DIR, 'data')
      : path.join(app.getPath('appData'), 'ScholarLens', 'data'))
  : path.join(ROOT, 'data');
try {
  fs.mkdirSync(DATA_ROOT, { recursive: true });
} catch (e) {
  console.error('[data]', e);
}
if (app.isPackaged) {
  try {
    const profile = path.join(DATA_ROOT, 'electron-profile');
    fs.mkdirSync(profile, { recursive: true });
    app.setPath('userData', profile);
  } catch (e) {
    console.error('[userData]', e);
  }
  app.commandLine.appendSwitch('no-sandbox');
}
const PYTHON = path.join(ROOT, 'python-runtime', 'python.exe');
const ICON_PATH = path.join(ROOT, 'assets', 'icon.ico');
const ICON_PNG = path.join(ROOT, 'assets', 'icon.png');

let mainWindow = null;
let linkWindow = null;
let tray = null;
let backendProc = null;
let backendPort = 0;
let backendRestarts = 0;
let quitting = false;


function findFreePort(startPort) {
  return new Promise((resolve, reject) => {
    const tryPort = (port) => {
      if (port >= startPort + PORT_RANGE_SIZE) {
        reject(new Error('no free port in range'));
        return;
      }
      const srv = net.createServer();
      srv.once('error', () => tryPort(port + 1));
      srv.once('listening', () => srv.close(() => resolve(port)));
      srv.listen(port, '127.0.0.1');
    };
    tryPort(startPort);
  });
}

function healthCheck(port, timeoutMs) {
  return new Promise((resolve) => {
    const started = Date.now();
    const poll = () => {
      const req = http.get({ host: '127.0.0.1', port, path: '/health', timeout: 1000 }, (res) => {
        res.resume();
        if (res.statusCode === 200) return resolve(true);
        retry();
      });
      req.on('error', retry);
      req.on('timeout', () => { req.destroy(); retry(); });
      function retry() {
        if (Date.now() - started > timeoutMs) return resolve(false);
        setTimeout(poll, 250);
      }
    };
    poll();
  });
}

function killBackend() {
  if (!backendProc || backendProc.killed || !backendProc.pid) return Promise.resolve();
  return new Promise((resolve) => {
    const pid = backendProc.pid;
    exec(`taskkill /PID ${pid} /T /F`, () => resolve());
    setTimeout(() => { try { backendProc.kill(); } catch (_) {} resolve(); }, 3000);
  });
}


const BACKEND_LOCK = path.join(DATA_ROOT, 'backend.lock');

function killStaleBackend() {
  try {
    const { pid, port } = JSON.parse(fs.readFileSync(BACKEND_LOCK, 'utf8'));
    const n = Number(pid);
    if (!Number.isInteger(n) || n <= 0) {
      try { fs.unlinkSync(BACKEND_LOCK); } catch (_) {}
      return;
    }
    console.log(`[backend] stale lock found: pid=${n} port=${port}, killing`);
    exec(`taskkill /PID ${n} /T /F`, () => {});
    try { fs.unlinkSync(BACKEND_LOCK); } catch (_) {}
  } catch (_) { /* no lock */ }
}

function writeBackendLock(pid, port) {
  try { fs.writeFileSync(BACKEND_LOCK, JSON.stringify({ pid, port })); } catch (_) {}
}

function clearBackendLock() {
  try { fs.unlinkSync(BACKEND_LOCK); } catch (_) {}
}


async function startBackend() {
  killStaleBackend();
  const port = await findFreePort(PORT_RANGE_START);
  backendPort = port;
  backendProc = spawn(PYTHON, ['-m', 'uvicorn', 'server.api:app', '--host', '127.0.0.1', '--port', String(port)], {
    cwd: ROOT,
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, HF_HOME: path.join(ROOT, 'models', 'hf'),
           HF_HUB_OFFLINE: '1', SCHOLARLENS_DATA_DIR: DATA_ROOT,
           // 不在只读的安装目录写 .pyc
           PYTHONDONTWRITEBYTECODE: '1' },
  });
  if (!backendProc.pid) {
    console.error('[backend] spawn produced no pid — python-runtime/python.exe missing?');
  } else {
    writeBackendLock(backendProc.pid, port);
  }
  backendProc.on('error', (err) => {
    console.error('[backend] spawn error:', err.message);
    try { clearBackendLock(); } catch (_) {}
  });
  backendProc.stdout.on('data', (d) => console.log(`[backend] ${d}`.trimEnd()));
  backendProc.stderr.on('data', (d) => console.log(`[backend:err] ${d}`.trimEnd()));
  backendProc.on('exit', (code) => {
    console.log(`[backend] exited code=${code}`);
    backendProc = null;
    if (!quitting) onBackendCrash(code);
  });
  const ok = await healthCheck(port, HEALTH_TIMEOUT_MS);
  if (!ok) throw new Error(`backend not healthy after ${HEALTH_TIMEOUT_MS}ms`);
  console.log(`[backend] ready on port ${port}`);
  return port;
}

function onBackendCrash(code) {
  if (backendRestarts < 1) {
    backendRestarts += 1;
    console.log('[backend] auto-restarting (1/1)');
    startBackend().then((port) => {
      if (mainWindow) mainWindow.webContents.send('backend-status', `running:${port}`);
    }).catch((err) => {
      console.error('[backend] restart failed:', err.message);
      if (mainWindow) mainWindow.webContents.send('backend-status', 'offline');
    });
  } else if (mainWindow) {
    mainWindow.webContents.send('backend-status', 'offline');
  }
}


function createTrayIcon() {
  const { nativeImage } = require('electron');
  return nativeImage.createFromPath(ICON_PNG);
}

function createTray() {
  try {
    const icon = createTrayIcon();
    if (!icon || icon.isEmpty()) throw new Error('tray icon missing or empty');
    tray = new Tray(icon);
  } catch (e) {
    console.error('[tray] unavailable:', e.message);
    tray = null;
    return;
  }
  tray.setToolTip('ScholarLens');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: '打开 ScholarLens', click: showMainWindow },
    { label: `后端: 运行中 (port ${backendPort})`, enabled: false, id: 'backendStatus' },
    { type: 'separator' },
    { label: '退出', click: quitApp },
  ]));
  tray.on('click', showMainWindow);
}

function openLinkWindow(url) {
  if (linkWindow && !linkWindow.isDestroyed()) {
    linkWindow.loadURL(url);
    linkWindow.show();
    linkWindow.focus();
    return;
  }
  linkWindow = new BrowserWindow({
    width: 1040,
    height: 780,
    minWidth: 720,
    minHeight: 520,
    title: url,
    autoHideMenuBar: true,
    backgroundColor: '#1e1f24',
    webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false },
  });
  linkWindow.on('closed', () => { linkWindow = null; });
  linkWindow.webContents.on('will-navigate', (e, next) => {
    if (!/^https?:\/\//i.test(next)) e.preventDefault();
  });
  linkWindow.webContents.on('did-fail-load', (e, code, desc, failedUrl) => {
    if (code === -3) return; // aborted by a newer navigation
    const html = `<html><meta charset="utf-8"><body style="font-family:Segoe UI,Microsoft YaHei;background:#1e1f24;color:#d6d7db;padding:40px">
      <h2>无法打开链接</h2><p style="color:#8a8b93">${failedUrl}</p><p style="color:#e5534b">${desc} (${code})</p></body></html>`;
    linkWindow.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(html));
  });
  linkWindow.webContents.setWindowOpenHandler(({ url: next }) => {
    if (/^https?:\/\//i.test(next)) openLinkWindow(next);
    return { action: 'deny' };
  });
  linkWindow.loadURL(url);
  linkWindow.show();
}

function showMainWindow() {
  if (!mainWindow) { createWindow(); return; }
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 960,
    minHeight: 600,
    backgroundColor: '#1e1f24',
    autoHideMenuBar: true,
    icon: ICON_PATH,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  mainWindow.loadURL(`http://127.0.0.1:${backendPort}/ui/index.html`);
  mainWindow.once('ready-to-show', () => {
    try { mainWindow.setIcon(ICON_PATH); } catch (_) {}
  });
  mainWindow.webContents.on('did-finish-load', () => {
    mainWindow.webContents.send('backend-status', backendProc ? `running:${backendPort}` : 'offline');
  });
  mainWindow.webContents.on('did-fail-load', (e, code, desc, url) => {
    if (code === -3) return;  // aborted — ignore
    dialog.showErrorBox('界面加载失败', `无法加载 UI (${url}): ${desc}\n请重启应用。`);
  });

  mainWindow.on('close', (e) => {
    if (quitting) return;
    e.preventDefault();
    const prefs = readPrefs();
    if (prefs.onClose === 'exit') {
      quitApp();
      return;
    }
    if (prefs.onClose === 'tray') {
      hideToTray();
      return;
    }
    dialog.showMessageBox(mainWindow, {
      type: 'question',
      title: '关闭 ScholarLens',
      message: '要彻底退出，还是最小化到系统托盘（后台常驻）？',
      detail: '后台常驻时后端继续运行，托盘图标可随时恢复窗口。',
      checkboxLabel: '记住我的选择，不再询问',
      checkboxChecked: false,
      buttons: ['最小化到托盘', '彻底退出'],
      defaultId: 0,
      cancelId: 0,
    }).then(({ response, checkboxChecked }) => {
      if (checkboxChecked) {
        writePrefs({ onClose: response === 1 ? 'exit' : 'tray' });
      }
      if (response === 1) quitApp();
      else hideToTray();
    });
  });
  mainWindow.on('closed', () => { mainWindow = null; });
}

function prefsPath() {
  return path.join(DATA_ROOT, 'app_prefs.json');
}

function readPrefs() {
  try {
    return JSON.parse(fs.readFileSync(prefsPath(), 'utf8'));
  } catch (_) {
    return {};
  }
}

function writePrefs(obj) {
  try {
    fs.mkdirSync(path.dirname(prefsPath()), { recursive: true });
    fs.writeFileSync(prefsPath(), JSON.stringify(obj, null, 1));
  } catch (_) { /* non-fatal */ }
}

function hideToTray() {
  if (!tray) {
    if (mainWindow && mainWindow.isMinimized() === false) mainWindow.minimize();
    return;
  }
  mainWindow.hide();
  if (!createTray.notified) {
    createTray.notified = true;
    tray.displayBalloon({
      iconType: 'info',
      title: 'ScholarLens 仍在运行',
      content: '已最小化到系统托盘，点击托盘图标可恢复窗口。',
    });
  }
}

function setupLinkPolicy() {
  const { shell } = require('electron');
  app.on('web-contents-created', (_, wc) => {
    wc.setWindowOpenHandler(({ url }) => {
      if (/^https?:\/\//i.test(url)) shell.openExternal(url);
      return { action: 'deny' };
    });
    wc.on('will-navigate', (e, url) => {
      if (!url.startsWith('file://') && !url.startsWith(`http://127.0.0.1:`)) e.preventDefault();
    });
  });
}

function quitApp() {
  quitting = true;
  killBackend().then(() => {
    clearBackendLock();
    if (tray) tray.destroy();
    app.quit();
  });
}


const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', showMainWindow);
  app.on('render-process-gone', (e, wc, details) => {
    console.error('[render-gone]', JSON.stringify(details));
  });

  app.whenReady().then(async () => {
    try {
      await session.defaultSession.clearCache();
    } catch (_) { /* cache is optional */ }
    ipcMain.handle('get-backend-info', () => ({ port: backendPort, running: !!backendProc }));
    ipcMain.handle('open-external', (_, url) => {
      if (typeof url !== 'string' || !/^https?:\/\/[^\s"'<>]+$/i.test(url)) return false;
      openLinkWindow(url);
      return true;
    });
    ipcMain.handle('select-pdfs', async () => {
      const { filePaths } = await dialog.showOpenDialog(mainWindow, {
        title: '选择 PDF 论文',
        filters: [{ name: 'PDF 论文', extensions: ['pdf'] }],
        properties: ['openFile', 'multiSelections'],
      });
      return filePaths;
    });
    setupLinkPolicy();
    try {
      await startBackend();
    } catch (err) {
      await killBackend();
      clearBackendLock();
      dialog.showErrorBox('ScholarLens 后端启动失败', String(err.message || err));
      app.quit();
      return;
    }
    createTray();
    createWindow();
  });

  app.on('window-all-closed', () => { /* stay in tray; quit only via tray menu */ });
}
