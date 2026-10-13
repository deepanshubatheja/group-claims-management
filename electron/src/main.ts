import { app, BrowserWindow, ipcMain, session } from "electron";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { GET_APP_INFO_CHANNEL, type AppInfo } from "./ipc-contract";

const developmentServerUrl = process.env.ELECTRON_RENDERER_URL;
const rendererHtmlPath = path.join(__dirname, "../../frontend/dist/index.html");
const rendererHtmlUrl = pathToFileURL(rendererHtmlPath).toString();

function isAllowedRendererUrl(url: string): boolean {
  if (developmentServerUrl !== undefined) {
    return new URL(url).origin === new URL(developmentServerUrl).origin;
  }

  return url === rendererHtmlUrl;
}

function createAppInfo(): AppInfo {
  const appInfo: AppInfo = {
    name: app.getName(),
    version: app.getVersion(),
    platform: process.platform,
  };

  if (
    appInfo.name.length === 0 ||
    appInfo.version.length === 0 ||
    !["darwin", "win32", "linux"].includes(appInfo.platform)
  ) {
    throw new Error("Unable to provide validated application information.");
  }

  return Object.freeze(appInfo);
}

function createContentSecurityPolicy(): string {
  // Vite's React Fast Refresh preamble is an inline module in development.
  // Production bundles do not need this exception.
  const scriptSource =
    developmentServerUrl !== undefined
      ? "script-src 'self' 'unsafe-inline'"
      : "script-src 'self'";
  const styleSource =
    developmentServerUrl !== undefined
      ? "style-src 'self' 'unsafe-inline'"
      : "style-src 'self'";
  const directives = [
    "default-src 'self'",
    scriptSource,
    styleSource,
    "img-src 'self' data:",
    "font-src 'self'",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-src 'none'",
  ];

  if (developmentServerUrl !== undefined) {
    const server = new URL(developmentServerUrl);
    const websocketProtocol = server.protocol === "https:" ? "wss:" : "ws:";
    const websocketUrl = `${websocketProtocol}//${server.host}`;
    directives[5] = `connect-src 'self' ${server.origin} ${websocketUrl}`;
  }

  return directives.join("; ");
}

function configureSessionSecurity(): void {
  session.defaultSession.setPermissionRequestHandler((_webContents, _permission, callback) => {
    callback(false);
  });

  session.defaultSession.setPermissionCheckHandler(() => false);

  session.defaultSession.webRequest.onHeadersReceived((details, callback) => {
    if (details.resourceType !== "mainFrame") {
      callback({ cancel: false });
      return;
    }

    callback({
      cancel: false,
      responseHeaders: {
        ...details.responseHeaders,
        "Content-Security-Policy": [createContentSecurityPolicy()],
      },
    });
  });
}

function createMainWindow(): BrowserWindow {
  const window = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 900,
    minHeight: 640,
    show: false,
    backgroundColor: "#f6f8fb",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
      webviewTag: false,
    },
  });

  window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  window.webContents.on("will-navigate", (event, url) => {
    if (!isAllowedRendererUrl(url)) {
      event.preventDefault();
    }
  });

  window.once("ready-to-show", () => window.show());

  if (developmentServerUrl !== undefined) {
    void window.loadURL(developmentServerUrl);
  } else {
    void window.loadFile(rendererHtmlPath);
  }

  return window;
}

app.setName("Group Claims Management");

app.whenReady().then(() => {
  configureSessionSecurity();

  ipcMain.handle(GET_APP_INFO_CHANNEL, (event, ...arguments_: unknown[]) => {
    const senderFrame = event.senderFrame;

    if (
      arguments_.length !== 0 ||
      senderFrame === null ||
      !isAllowedRendererUrl(senderFrame.url)
    ) {
      throw new Error("Invalid application information request.");
    }

    return createAppInfo();
  });

  createMainWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createMainWindow();
    }
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});
