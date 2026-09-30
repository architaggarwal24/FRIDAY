/**
 * F.R.I.D.A.Y. — ui/main.js
 * Electron main process.
 * Handles: window creation, system tray, close-to-tray behaviour.
 */

const { app, BrowserWindow, Tray, Menu, ipcMain, nativeImage, dialog, shell } = require("electron");
const path = require("path");

let mainWindow = null;
let tray = null;
let forceQuit = false;

// ── Window ────────────────────────────────────────────────────────────────────

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 1000,
    minHeight: 700,
    backgroundColor: "#0a0a0a",
    titleBarStyle: "hidden",
    titleBarOverlay: {
      color: "#0a0a0a",
      symbolColor: "#f59e0b",
      height: 40,
    },
    frame: true,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  // Load the renderer
  mainWindow.loadFile(path.join(__dirname, "renderer", "dist", "index.html"));

  // Without these, a renderer crash (JS error, failed asset load, GPU
  // process crash) produces zero visible output anywhere — the window
  // just goes solid black with no trace of what happened, since DevTools
  // isn't open by default and nothing else surfaces the error.
  mainWindow.webContents.on("console-message", (_e, level, message, line, sourceId) => {
    if (level >= 2) { // 2 = warning, 3 = error
      console.error(`[Renderer] ${message} (${sourceId}:${line})`);
    }
  });
  mainWindow.webContents.on("did-fail-load", (_e, errorCode, errorDescription, validatedURL) => {
    console.error(`[FRIDAY] Renderer failed to load: ${errorCode} ${errorDescription} (${validatedURL})`);
  });
  mainWindow.webContents.on("render-process-gone", (_e, details) => {
    console.error(`[FRIDAY] Renderer process gone: ${details.reason} (exitCode ${details.exitCode})`);
  });
  mainWindow.webContents.on("unresponsive", () => {
    console.error("[FRIDAY] Renderer became unresponsive.");
  });

  // Show once ready to avoid white flash
  mainWindow.once("ready-to-show", () => {
    mainWindow.show();
  });

  // ── Close → offer tray option ──────────────────────────────────
  mainWindow.on("close", (e) => {
    if (forceQuit) return; // actually closing

    e.preventDefault();

    const choice = dialog.showMessageBoxSync(mainWindow, {
      type: "question",
      buttons: ["Minimize to Tray", "Quit F.R.I.D.A.Y."],
      defaultId: 0,
      cancelId: 0,
      title: "F.R.I.D.A.Y.",
      message: "What would you like to do?",
      detail: "Minimize to tray keeps F.R.I.D.A.Y. running in the background.",
    });

    if (choice === 0) {
      mainWindow.hide();
    } else {
      forceQuit = true;
      app.quit();
    }
  });

  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

// ── Tray ─────────────────────────────────────────────────────────────────────

function createTray() {
  // Create a simple amber circle icon programmatically — no file needed
  const { nativeImage } = require("electron");
  const size = 16;
  const canvas = Buffer.alloc(size * size * 4);
  const cx = size / 2, cy = size / 2, r = size / 2 - 1;
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const i = (y * size + x) * 4;
      const dist = Math.sqrt((x - cx) ** 2 + (y - cy) ** 2);
      if (dist <= r) {
        canvas[i]     = 245; // R (amber)
        canvas[i + 1] = 158; // G
        canvas[i + 2] = 11;  // B
        canvas[i + 3] = 255; // A
      } else {
        canvas[i + 3] = 0;   // transparent outside circle
      }
    }
  }
  const icon = nativeImage.createFromBuffer(canvas, { width: size, height: size });
  tray = new Tray(icon);

  const contextMenu = Menu.buildFromTemplate([
    {
      label: "Open F.R.I.D.A.Y.",
      click: () => {
        if (mainWindow) {
          mainWindow.show();
          mainWindow.focus();
        } else {
          createWindow();
        }
      },
    },
    {
      label: "Toggle Focus Card",
      click: () => {
        if (cardWindow && !cardWindow.isDestroyed()) destroyCardWindow();
        else createCardWindow();
      },
    },
    { type: "separator" },
    {
      label: "Quit",
      click: () => {
        forceQuit = true;
        app.quit();
      },
    },
  ]);

  tray.setToolTip("F.R.I.D.A.Y. — Online");
  tray.setContextMenu(contextMenu);

  tray.on("double-click", () => {
    if (mainWindow) {
      mainWindow.show();
      mainWindow.focus();
    }
  });
}

// ── IPC handlers ─────────────────────────────────────────────────────────────

ipcMain.on("minimize-window", () => mainWindow?.minimize());
ipcMain.on("maximize-window", () => {
  if (mainWindow?.isMaximized()) mainWindow.unmaximize();
  else mainWindow?.maximize();
});
ipcMain.on("close-window", () => mainWindow?.close());
ipcMain.on("hide-to-tray", () => mainWindow?.hide());

// Opens a folder with whatever the OS has registered for it (Obsidian
// itself, if installed; otherwise the file explorer). shell.openPath is
// Electron's sandboxed-safe way to hand a path to the OS — it opens
// with the OS's default handler, the same as double-clicking the
// folder, not arbitrary code execution. The renderer supplies the path
// (sourced from the Python backend's own config, via the memory_data WS
// payload) rather than this process re-deriving it.
ipcMain.handle("open-path", async (event, targetPath) => {
  if (typeof targetPath !== "string" || !targetPath) return "no path given";
  try {
    const result = await shell.openPath(targetPath);
    return result || null; // empty string = success, per Electron's API
  } catch (e) {
    return String(e);
  }
});

// ── Focus card (always-on-top overlay) ───────────────────────────────────────
// A separate, deliberately minimal BrowserWindow — NOT a reuse of the main
// window's chrome. It's frameless, transparent and skips the taskbar so it
// reads as an overlay rather than a second app window, and it stays up when
// the main window is hidden to tray (which is the point of it).
//
// Same webPreferences as the main window: contextIsolation on, nodeIntegration
// off, same preload — so it gets the same WS auth token by the same bridge,
// with no extra privilege.

let cardWindow = null;

function createCardWindow() {
  if (cardWindow && !cardWindow.isDestroyed()) {
    cardWindow.show();
    return cardWindow;
  }
  cardWindow = new BrowserWindow({
    width: 260,
    height: 132,
    frame: false,
    transparent: true,
    resizable: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    fullscreenable: false,
    maximizable: false,
    minimizable: false,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  // "screen-saver" keeps it above fullscreen apps too — a focus card that
  // vanishes the moment you go fullscreen in the thing you're focusing on
  // would be useless exactly when it matters.
  cardWindow.setAlwaysOnTop(true, "screen-saver");
  cardWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });

  cardWindow.loadFile(path.join(__dirname, "card.html"));
  cardWindow.once("ready-to-show", () => cardWindow.show());
  cardWindow.on("closed", () => { cardWindow = null; });
  return cardWindow;
}

function destroyCardWindow() {
  if (cardWindow && !cardWindow.isDestroyed()) cardWindow.close();
  cardWindow = null;
}

ipcMain.on("show-focus-card", () => createCardWindow());
ipcMain.on("hide-focus-card", () => destroyCardWindow());
ipcMain.handle("toggle-focus-card", () => {
  if (cardWindow && !cardWindow.isDestroyed()) { destroyCardWindow(); return false; }
  createCardWindow();
  return true;
});

// ── App lifecycle ─────────────────────────────────────────────────────────────

// ── THE EAR LAW ───────────────────────────────────────────────────────────────
// The posture watch uses the camera. It must never be able to open the
// microphone. Rather than relying on every future renderer change to keep
// passing `audio: false` to getUserMedia, this denies audio capture at the
// Electron permission layer — the renderer CANNOT obtain a mic stream, even
// if someone later writes code that asks for one. Voice input does not go
// through getUserMedia at all: it's captured in Python (voice/vad.py, via
// sounddevice) and gated behind the explicit start_listen WS command, so
// denying it here costs the mic feature nothing.
function installPermissionPolicy(session) {
  session.setPermissionRequestHandler((webContents, permission, callback, details) => {
    if (permission === "media") {
      const types = (details && details.mediaTypes) || [];
      if (types.includes("audio")) {
        console.warn("[FRIDAY] Denied a renderer microphone request (ear law).");
        return callback(false);
      }
      return callback(true); // video-only — the posture watch
    }
    // Nothing else in this app needs a permission prompt.
    return callback(false);
  });

  // Same policy for the synchronous check Chromium makes before some
  // requests — without this, a mic request can be approved without ever
  // reaching the handler above.
  session.setPermissionCheckHandler((webContents, permission, origin, details) => {
    if (permission === "media") {
      const type = details && details.mediaType;
      if (type === "audio") return false;
      return true;
    }
    return false;
  });
}

app.whenReady().then(() => {
  const { session } = require("electron");
  installPermissionPolicy(session.defaultSession);
  createTray();
  createWindow();
});

app.on("window-all-closed", () => {
  // Keep app running in tray on Windows/Linux
  if (process.platform === "darwin") app.quit();
});

app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});

app.on("before-quit", () => {
  forceQuit = true;
  destroyCardWindow();
});