/**
 * F.R.I.D.A.Y. — ui/preload.js
 * Secure contextBridge between Electron main and React renderer.
 */

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("friday", {
  // Window controls
  minimize: () => ipcRenderer.send("minimize-window"),
  maximize: () => ipcRenderer.send("maximize-window"),
  close:    () => ipcRenderer.send("close-window"),
  hideToTray: () => ipcRenderer.send("hide-to-tray"),

  // Opens a folder in the OS's default handler (Obsidian, if installed
  // for .md folders; otherwise the file explorer). Returns null on
  // success, an error string on failure.
  openPath: (targetPath) => ipcRenderer.invoke("open-path", targetPath),

  // Focus card — the always-on-top overlay (ui/card.html). A separate
  // BrowserWindow, toggled from the main window.
  showFocusCard:   () => ipcRenderer.send("show-focus-card"),
  hideFocusCard:   () => ipcRenderer.send("hide-focus-card"),
  toggleFocusCard: () => ipcRenderer.invoke("toggle-focus-card"),

  // Platform info
  platform: process.platform,

  // WebSocket auth — set on this process's environment by start.py when
  // it launches Electron. The renderer can't read process.env directly
  // (nodeIntegration is off), so this is the one bridge point for it.
  wsAuthToken: process.env.WS_AUTH_TOKEN || "",
});
